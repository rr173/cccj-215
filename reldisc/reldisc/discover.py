"""discover: exhaustive exact search for inclusion candidates.

Search space: for every arity 1..k, every source column set of every table
(canonicalised by sorting column names) is crossed with every ordered,
repetition-free, type-matching target column tuple of every *other* table.
(A,B)->(B,A) and (A,B)->(A,B) are therefore enumerated as different
candidates.

How it stays exact and fast
---------------------------
* ``prepare`` mapped every typed value to an integer code in a global
  per-type dictionary.  A tuple of codes is packed into one exact integer
  key by mixed-radix composition (Python ints never overflow), so key
  equality *is* typed tuple equality -- no hashing, no collisions.
* Source projections (distinct keys + row counts) are computed once per
  source column set and shared by all its target tuples; target projections
  (a set of distinct keys) are computed once per target column tuple and
  shared by all source sets of the same type pattern.  Only one target
  projection lives in memory at a time; source projections of one type
  pattern group are compact arrays.  The JSONL is never re-read.
* Every enumerated candidate is evaluated exactly (distinct absent keys
  weighted by their row counts, with early exit once bad > max_bad).
  Every accepted candidate is additionally re-verified through the
  independent row-oriented evaluator in ``evalmap`` before being listed.

Completeness / pruning argument
-------------------------------
* No candidate inside the requested range (arities 1..k, all tables, all
  type-compatible mappings) is skipped except through *support* pruning:
  a source column set whose participating row count is below min_rows can
  never become acceptable by adding more columns, because under ``complete``
  semantics adding a column can only shrink the participating set (and under
  ``equal`` it stays the whole table).  Such insufficiency is therefore
  propagated to supersets of the *same* source column set of the *same*
  table -- and never across unrelated column sets.
* Crucially, a *bad-row* result at low arity does NOT prune higher arities:
  under ``complete`` a mismatching row may leave the participating set when
  a newly added column is null, so a low-arity non-inclusion can coexist
  with a high-arity inclusion.  Both are reported independently.
* If a time budget or candidate limit stops the run, the output is marked
  "incomplete" and lists the not-yet-covered ranges; it never claims that
  discovery finished.
"""

import itertools
import json
import resource
import time
from array import array

from . import evalmap
from .errors import InternalError, UsageError

EVIDENCE_LIMIT = 10


def _encode_projection(col_arrays, radixes, nulls):
    """Exact mixed-radix tuple keys for the participating rows.

    complete: rows with a null code (radix-1) in any position are dropped.
    equal:    null is just another code, every row participates.
    """
    arity = len(col_arrays)
    if arity == 1:
        (c0,) = col_arrays
        if nulls == "complete":
            s0 = radixes[0] - 1
            return [a for a in c0 if a != s0]
        return list(c0)
    if arity == 2:
        c0, c1 = col_arrays
        r1 = radixes[1]
        if nulls == "complete":
            s0 = radixes[0] - 1
            s1 = r1 - 1
            return [a * r1 + b for a, b in zip(c0, c1) if a != s0 and b != s1]
        return [a * r1 + b for a, b in zip(c0, c1)]
    if arity == 3:
        c0, c1, c2 = col_arrays
        r1 = radixes[1]
        r2 = radixes[2]
        r12 = r1 * r2
        if nulls == "complete":
            s0 = radixes[0] - 1
            s1 = r1 - 1
            s2 = r2 - 1
            return [a * r12 + b * r2 + c
                    for a, b, c in zip(c0, c1, c2)
                    if a != s0 and b != s1 and c != s2]
        return [a * r12 + b * r2 + c for a, b, c in zip(c0, c1, c2)]
    raise InternalError("unsupported arity %d" % arity)


def _group_keys(keys):
    """Distinct keys with per-key row counts (compact parallel arrays)."""
    counts = {}
    for key in keys:
        counts[key] = counts.get(key, 0) + 1
    return array("q", counts.keys()), array("q", counts.values())


def _bad_rows(ukeys, ucounts, tset, max_bad):
    """Exact number of source rows whose tuple is absent from the target.

    Stops early once the count provably exceeds max_bad (the exact count is
    only reported for accepted candidates anyway).
    """
    bad = 0
    for key, cnt in zip(ukeys, ucounts):
        if key not in tset:
            bad += cnt
            if bad > max_bad:
                break
    return bad


def run_discover(store, k, nulls, min_rows, max_bad,
                 time_budget_sec=None, candidate_limit=None, log=None):
    if k not in (1, 2, 3):
        raise UsageError("k must be 1, 2 or 3")
    if not isinstance(min_rows, int) or min_rows < 1:
        raise UsageError("min_rows must be a positive integer")
    if not isinstance(max_bad, int) or max_bad < 0:
        raise UsageError("max_bad must be a non-negative integer")
    if nulls not in evalmap.NULLS_MODES:
        raise UsageError("nulls must be one of %s"
                         % (", ".join(evalmap.NULLS_MODES),))

    def emit(msg):
        if log:
            log(msg)

    meta = store.meta()
    tables = meta["tables"]
    names = [t["name"] for t in tables]
    radix = {tp: meta["domain_sizes"][tp] + 1 for tp in ("text", "int64")}
    # Columns of each table in canonical (name-sorted) order.
    sorted_cols = []
    for tmeta in tables:
        ordered = sorted(enumerate(tmeta["columns"]),
                         key=lambda p: p[1]["name"])
        sorted_cols.append([(cidx, c["name"], c["type"]) for cidx, c in ordered])
    codes = {}
    for tidx, tmeta in enumerate(tables):
        for cidx in range(len(tmeta["columns"])):
            codes[(tidx, cidx)] = store.load_column(tidx, cidx)

    # Full search plan up front so an interrupted run can report exactly
    # which ranges were not covered.
    plan = {}  # arity -> {pattern: [(tidx, cols_tuple)]}
    for arity in range(1, k + 1):
        groups = {}
        for tidx in range(len(tables)):
            for combo in itertools.combinations(sorted_cols[tidx], arity):
                cols = tuple(cidx for cidx, _, _ in combo)
                pattern = tuple(tp for _, _, tp in combo)
                groups.setdefault(pattern, []).append((tidx, cols))
        plan[arity] = groups

    def target_tuples(pattern):
        """All ordered, repetition-free, type-matching target column tuples."""
        out = []
        for ttidx in range(len(tables)):
            eligible = [[cidx for cidx, _, tp in sorted_cols[ttidx] if tp == p]
                        for p in pattern]
            for combo in itertools.product(*eligible):
                if len(set(combo)) == len(combo):
                    out.append((ttidx, combo))
        return out

    def col_names(tidx, cols):
        return [tables[tidx]["columns"][c]["name"] for c in cols]

    started = time.monotonic()
    deadline = ((started + time_budget_sec)
                if time_budget_sec is not None else None)
    results = []
    insufficient = []
    insuff_index = set()  # (tidx, frozenset(cidx)) with support < min_rows
    evaluated = accepted = rejected = 0
    src_projections = tgt_projections = 0
    per_arity = {}
    incomplete = False
    uncovered = []

    for arity in range(1, k + 1):
        if incomplete:
            for pattern, src_sets in sorted(plan[arity].items()):
                uncovered.append({
                    "arity": arity, "pattern": list(pattern),
                    "source_sets": len(src_sets),
                    "state": "not_started"})
            continue
        a_started = time.monotonic()
        a_stats = {"source_sets": 0, "source_sets_insufficient": 0,
                   "candidates_evaluated": 0, "accepted": 0, "rejected": 0}
        patterns = sorted(plan[arity].items())
        for pidx, (pattern, src_sets) in enumerate(patterns):
            if incomplete:
                uncovered.append({
                    "arity": arity, "pattern": list(pattern),
                    "source_sets": len(src_sets), "state": "not_started"})
                continue
            a_stats["source_sets"] += len(src_sets)
            tgt_list = target_tuples(pattern)
            rads = [radix[tp] for tp in pattern]
            # Build (once) and share the projection of every source set of
            # this type pattern; support-insufficient sets are recorded and
            # propagated to their supersets.
            projections = {}
            for stidx, scols in src_sets:
                if arity > 1 and any(
                        (stidx, frozenset(sub)) in insuff_index
                        for sub in itertools.combinations(scols, arity - 1)):
                    insufficient.append({
                        "table": names[stidx],
                        "columns": col_names(stidx, scols),
                        "participating": None,
                        "reason": "propagated_from_insufficient_subset"})
                    a_stats["source_sets_insufficient"] += 1
                    continue
                keys = _encode_projection(
                    [codes[(stidx, c)] for c in scols], rads, nulls)
                if len(keys) < min_rows:
                    insufficient.append({
                        "table": names[stidx],
                        "columns": col_names(stidx, scols),
                        "participating": len(keys),
                        "reason": "support_below_min_rows"})
                    insuff_index.add((stidx, frozenset(scols)))
                    a_stats["source_sets_insufficient"] += 1
                    continue
                ukeys, ucounts = _group_keys(keys)
                projections[(stidx, scols)] = (ukeys, ucounts, len(keys))
                src_projections += 1
            emit("arity %d pattern %s: %d source projections, %d target tuples"
                 % (arity, list(pattern), len(projections), len(tgt_list)))
            stop = False
            for tpos, (ttidx, tcols) in enumerate(tgt_list):
                tset = set(_encode_projection(
                    [codes[(ttidx, c)] for c in tcols], rads, nulls))
                tgt_projections += 1
                for stidx, scols in src_sets:
                    if stidx == ttidx:
                        continue
                    proj = projections.get((stidx, scols))
                    if proj is None:
                        continue
                    if ((deadline is not None and time.monotonic() > deadline)
                            or (candidate_limit is not None
                                and evaluated >= candidate_limit)):
                        incomplete = True
                        uncovered.append({
                            "arity": arity, "pattern": list(pattern),
                            "state": "partially_covered",
                            "remaining_target_tuples": len(tgt_list) - tpos,
                            "remaining_source_sets": len(projections),
                            "reason": ("time_budget_exceeded"
                                       if deadline is not None
                                       and time.monotonic() > deadline
                                       else "candidate_limit_reached")})
                        stop = True
                        break
                    ukeys, ucounts, n_part = proj
                    bad = _bad_rows(ukeys, ucounts, tset, max_bad)
                    evaluated += 1
                    a_stats["candidates_evaluated"] += 1
                    if bad <= max_bad:
                        mapping = {
                            "source_table": names[stidx],
                            "source_columns": col_names(stidx, scols),
                            "target_table": names[ttidx],
                            "target_columns": col_names(ttidx, tcols),
                            "nulls": nulls,
                        }
                        # Independent exact re-verification before listing.
                        ev = evalmap.evaluate_mapping(
                            store, mapping, want_witnesses=True)
                        if (ev["participating"] != n_part
                                or ev["unmatched"] != bad):
                            raise InternalError(
                                "fast path disagrees with exact evaluation "
                                "for %r: (%d,%d) vs (%d,%d)"
                                % (mapping, n_part, bad,
                                   ev["participating"], ev["unmatched"]))
                        accepted += 1
                        a_stats["accepted"] += 1
                        results.append({
                            "source": {"table": names[stidx],
                                       "columns": col_names(stidx, scols)},
                            "target": {"table": names[ttidx],
                                       "columns": col_names(ttidx, tcols)},
                            "nulls": nulls,
                            "participating": ev["participating"],
                            "excluded": ev["excluded"],
                            "matched": ev["matched"],
                            "unmatched": ev["unmatched"],
                            "evidence": ev["unmatched_rows"][:EVIDENCE_LIMIT],
                        })
                    else:
                        rejected += 1
                        a_stats["rejected"] += 1
                if stop:
                    break
        a_stats["seconds"] = round(time.monotonic() - a_started, 3)
        per_arity[str(arity)] = a_stats
        emit("arity %d done: %s" % (arity, a_stats))

    results.sort(key=lambda r: (
        r["source"]["table"], r["source"]["columns"],
        r["target"]["table"], r["target"]["columns"]))
    return {
        "tool": "reldisc",
        "format": 1,
        "status": "incomplete" if incomplete else "complete",
        "uncovered": uncovered,
        "params": {"k": k, "nulls": nulls, "min_rows": min_rows,
                   "max_bad": max_bad},
        "stats": {
            "candidates_evaluated": evaluated,
            "candidates_accepted": accepted,
            "candidates_rejected": rejected,
            "exact_reverifications": accepted,
            "source_projections_built": src_projections,
            "target_projections_built": tgt_projections,
            "per_arity": per_arity,
            "seconds_total": round(time.monotonic() - started, 3),
            "peak_rss_bytes": resource.getrusage(
                resource.RUSAGE_SELF).ru_maxrss * 1024,
        },
        "insufficient": insufficient,
        "candidates": results,
        "note": ("listed mappings are cross-table reference candidates for "
                 "warehouse validation, not declared permanent foreign keys; "
                 "target columns are not required to be unique"),
    }


def discover_to_json(**kwargs):
    return json.dumps(run_discover(**kwargs), ensure_ascii=False, indent=1)
