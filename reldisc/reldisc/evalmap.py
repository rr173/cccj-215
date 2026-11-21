"""Exact evaluation of one column mapping against the prepared store.

This module is deliberately simple and row-oriented: it builds the target
tuple set and then checks every participating source row one by one.  It is
used by the ``check`` and ``witnesses`` commands and as the independent
re-verification path for every candidate accepted by the fast discovery path.

Semantics
---------
A mapping pairs a source column set (canonicalised by sorting column names,
with the target columns permuted to preserve the correspondence) with an
ordered target column tuple.  Both sides must have the same arity, no repeated
columns within a side, equal types per position, and different tables.

Null handling (``nulls``):
  * ``complete``: source rows with a null in any mapped source column are
    *excluded* (they do not participate); target tuples containing a null do
    not participate in matching either.
  * ``equal``: null is a typed common value; every source row participates
    and a null matches a null at the same (type-matched) position.

Counts: ``participating`` source rows split into ``matched`` / ``unmatched``;
``excluded`` = total rows - participating.  Duplicate source rows are counted
per row_id; duplicate target tuples never amplify coverage (set semantics).
"""

from .errors import DataError, UsageError

NULLS_MODES = ("complete", "equal")


def rowid_sort_key(rid):
    """Deterministic total order over row_ids (ints first, then strings)."""
    if isinstance(rid, int):
        return (0, rid, "")
    return (1, 0, rid)


def _table(meta, name):
    for idx, tmeta in enumerate(meta["tables"]):
        if tmeta["name"] == name:
            return idx, tmeta
    raise DataError("unknown table %r" % (name,))


def _column(tmeta, tname, cname):
    for idx, cmeta in enumerate(tmeta["columns"]):
        if cmeta["name"] == cname:
            return idx, cmeta
    raise DataError("unknown column %r in table %r" % (cname, tname))


def normalize_mapping(meta, mapping):
    """Validate a mapping and canonicalise the source column order.

    Returns a dict with table indexes, per-position (name, cidx) pairs and
    types.  Unknown tables/columns are data errors; structural problems are
    usage errors.
    """
    if not isinstance(mapping, dict):
        raise UsageError("mapping must be a JSON object")
    for key in ("source_table", "source_columns", "target_table",
                "target_columns", "nulls"):
        if key not in mapping:
            raise UsageError("mapping is missing key %r" % key)
    nulls = mapping["nulls"]
    if nulls not in NULLS_MODES:
        raise UsageError("nulls must be one of %s" % (", ".join(NULLS_MODES),))
    s_cols = mapping["source_columns"]
    t_cols = mapping["target_columns"]
    if (not isinstance(s_cols, list) or not isinstance(t_cols, list)
            or not s_cols or not all(isinstance(c, str) for c in s_cols)
            or not all(isinstance(c, str) for c in t_cols)):
        raise UsageError("source_columns and target_columns must be "
                         "non-empty arrays of column names")
    if len(s_cols) != len(t_cols):
        raise UsageError("source and target column counts differ")
    if len(set(s_cols)) != len(s_cols):
        raise UsageError("duplicate source column in mapping")
    if len(set(t_cols)) != len(t_cols):
        raise UsageError("duplicate target column in mapping")
    s_name = mapping["source_table"]
    t_name = mapping["target_table"]
    if s_name == t_name:
        raise UsageError("source and target tables must be different")
    s_tidx, s_tmeta = _table(meta, s_name)
    t_tidx, t_tmeta = _table(meta, t_name)
    pairs = []
    for sc, tc in zip(s_cols, t_cols):
        s_cidx, s_cmeta = _column(s_tmeta, s_name, sc)
        t_cidx, t_cmeta = _column(t_tmeta, t_name, tc)
        if s_cmeta["type"] != t_cmeta["type"]:
            raise UsageError(
                "type mismatch: source column %r is %s but target column %r "
                "is %s" % (sc, s_cmeta["type"], tc, t_cmeta["type"]))
        pairs.append((sc, s_cidx, tc, t_cidx, s_cmeta["type"]))
    pairs.sort(key=lambda p: p[0])  # canonical source order: by column name
    return {
        "nulls": nulls,
        "s_tidx": s_tidx,
        "t_tidx": t_tidx,
        "s_cols": [(p[0], p[1]) for p in pairs],
        "t_cols": [(p[2], p[3]) for p in pairs],
        "types": [p[4] for p in pairs],
    }


def canonical_mapping_json(meta, mapping):
    """The mapping as a JSON-able dict in canonical (source-name-sorted) form."""
    nm = normalize_mapping(meta, mapping)
    return {
        "source_table": meta["tables"][nm["s_tidx"]]["name"],
        "source_columns": [name for name, _ in nm["s_cols"]],
        "target_table": meta["tables"][nm["t_tidx"]]["name"],
        "target_columns": [name for name, _ in nm["t_cols"]],
        "nulls": nm["nulls"],
    }


def evaluate_mapping(store, mapping, want_witnesses=False):
    """Exact counts for one mapping.

    Returns {"participating", "excluded", "matched", "unmatched"} and, when
    want_witnesses is true, "unmatched_rows": a list sorted by row_id of
    {"row_id", "tuple", "per_column_value_present"} entries (one entry per
    unmatched row -- duplicated values with different row_ids appear once
    per row).
    """
    meta = store.meta()
    nm = normalize_mapping(meta, mapping)
    nulls = nm["nulls"]
    complete = nulls == "complete"
    arity = len(nm["types"])
    s_arrays = [store.load_column(nm["s_tidx"], cidx) for _, cidx in nm["s_cols"]]
    t_arrays = [store.load_column(nm["t_tidx"], cidx) for _, cidx in nm["t_cols"]]
    sentinels = [store.null_code(tp) for tp in nm["types"]]

    # Target tuple set and per-position value sets (for per-column evidence).
    tset = set()
    vsets = [set() for _ in range(arity)]
    for row in zip(*t_arrays):
        if complete and _has_null(row, sentinels):
            continue
        tset.add(row)
        for pos, code in enumerate(row):
            vsets[pos].add(code)

    participating = excluded = matched = unmatched = 0
    unmatched_rows = []
    for i, row in enumerate(zip(*s_arrays)):
        if complete and _has_null(row, sentinels):
            excluded += 1
            continue
        participating += 1
        if row in tset:
            matched += 1
        else:
            unmatched += 1
            if want_witnesses:
                unmatched_rows.append((i, row))

    out = {
        "participating": participating,
        "excluded": excluded,
        "matched": matched,
        "unmatched": unmatched,
    }
    if want_witnesses:
        rowids = store.load_rowids(nm["s_tidx"])
        entries = [(rowids[i], row) for i, row in unmatched_rows]
        entries.sort(key=lambda e: rowid_sort_key(e[0]))
        out["unmatched_rows"] = [
            _witness_entry(store, nm, rid, row, vsets) for rid, row in entries
        ]
    return out


def _has_null(row, sentinels):
    for pos, code in enumerate(row):
        if code == sentinels[pos]:
            return True
    return False


def _witness_entry(store, nm, rid, row, vsets):
    values = [store.decode_value(tp, code)
              for tp, code in zip(nm["types"], row)]
    flags = [code in vsets[pos] for pos, code in enumerate(row)]
    return {
        "row_id": rid,
        "tuple": values,
        # True per position when the single value exists somewhere in the
        # corresponding target column -- evidence for "every edge value
        # exists, yet the joint tuple does not".
        "per_column_value_present": flags,
        "all_single_values_present": all(flags),
    }
