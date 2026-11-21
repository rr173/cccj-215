"""prepare: validate the manifest + JSONL data once and build the typed store.

Validation rules (all violations are *data errors*, never inclusion failures):
  * every JSONL line must be a JSON object with exactly the keys
    {"row_id"} + declared columns (unknown column / missing column -> error);
  * duplicate keys inside one JSON object -> error;
  * row_id must be a string or an int64 (not null, not bool), unique per table;
  * text columns accept only JSON strings or null;
  * int64 columns accept only JSON integers in int64 range or null;
    a JSON bool is NOT an integer;
  * corrupt JSON lines -> error.
"""

import json
import os
import time
from array import array

from .errors import DataError, UsageError
from .manifest import ROW_ID_FIELD, load_manifest
from .store import CODE_ARRAY_TYPE, STORE_FORMAT, Store

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1
NULL_PLACEHOLDER = -1  # written as null_code(type) once the domain is final


def _object_no_dup_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise DataError("duplicate key %r inside one JSON object" % (key,))
        obj[key] = value
    return obj


def _validate_row_id(tname, path, lineno, rid, seen):
    if rid is None or isinstance(rid, bool) or not isinstance(rid, (str, int)):
        raise DataError(
            "%s:%d (table %r): row_id must be a string or an integer, got %r"
            % (path, lineno, tname, rid))
    if rid in seen:
        raise DataError(
            "%s:%d (table %r): duplicate row_id %r" % (path, lineno, tname, rid))
    seen.add(rid)
    return rid


def _encode_value(tname, path, lineno, cname, ctype, value, text_codes, int_codes):
    if value is None:
        return NULL_PLACEHOLDER
    if ctype == "text":
        if not isinstance(value, str):
            raise DataError(
                "%s:%d (table %r, column %r): expected text or null, got %r"
                % (path, lineno, tname, cname, value))
        domain = text_codes
    else:  # int64
        if isinstance(value, bool) or not isinstance(value, int):
            raise DataError(
                "%s:%d (table %r, column %r): expected int64 or null, got %r "
                "(note: a JSON bool is not an integer)"
                % (path, lineno, tname, cname, value))
        if value < INT64_MIN or value > INT64_MAX:
            raise DataError(
                "%s:%d (table %r, column %r): integer %r out of int64 range"
                % (path, lineno, tname, cname, value))
        domain = int_codes
    code = domain.get(value)
    if code is None:
        code = len(domain)
        domain[value] = code
    return code


def run_prepare(manifest_path, workdir):
    started = time.monotonic()
    man = load_manifest(manifest_path)
    if os.path.exists(workdir):
        if not os.path.isdir(workdir):
            raise UsageError("workdir %r exists and is not a directory" % workdir)
        if os.listdir(workdir):
            raise UsageError(
                "workdir %r is not empty; prepare starts from an empty directory"
                % workdir)
    else:
        os.makedirs(workdir)

    store = Store(workdir)
    text_codes = {}
    int_codes = {}
    tables_meta = []
    pending_columns = []  # (tidx, cidx, ctype, list-of-codes)
    total_rows = 0

    for tidx, table in enumerate(man.tables):
        colnames = [c.name for c in table.columns]
        coltypes = [c.type for c in table.columns]
        colset = set(colnames)
        colcodes = [[] for _ in colnames]
        rowids = []
        seen_ids = set()
        nrows = 0
        try:
            fh = open(table.path, "r", encoding="utf-8")
        except OSError as exc:
            raise DataError("cannot read data file for table %r: %s"
                            % (table.name, exc))
        with fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line, object_pairs_hook=_object_no_dup_keys)
                except json.JSONDecodeError as exc:
                    raise DataError("%s:%d (table %r): invalid JSON: %s"
                                    % (table.path, lineno, table.name, exc))
                if not isinstance(obj, dict):
                    raise DataError("%s:%d (table %r): line is not a JSON object"
                                    % (table.path, lineno, table.name))
                for key in obj:
                    if key != ROW_ID_FIELD and key not in colset:
                        raise DataError(
                            "%s:%d (table %r): unknown column %r not in manifest"
                            % (table.path, lineno, table.name, key))
                if ROW_ID_FIELD not in obj:
                    raise DataError("%s:%d (table %r): missing row_id"
                                    % (table.path, lineno, table.name))
                for cname in colnames:
                    if cname not in obj:
                        raise DataError(
                            "%s:%d (table %r): missing column %r"
                            % (table.path, lineno, table.name, cname))
                rowids.append(_validate_row_id(
                    table.name, table.path, lineno, obj[ROW_ID_FIELD], seen_ids))
                for ci, cname in enumerate(colnames):
                    colcodes[ci].append(_encode_value(
                        table.name, table.path, lineno, cname, coltypes[ci],
                        obj[cname], text_codes, int_codes))
                nrows += 1
        total_rows += nrows
        store.save_rowids(tidx, rowids)
        for ci in range(len(colnames)):
            pending_columns.append((tidx, ci, coltypes[ci], colcodes[ci]))
        tables_meta.append({
            "name": table.name,
            "row_count": nrows,
            "columns": [{"name": c.name, "type": c.type} for c in table.columns],
        })

    # Domains are final now: materialise columns with the null sentinel.
    null_code = {"text": len(text_codes), "int64": len(int_codes)}
    for tidx, cidx, ctype, codes in pending_columns:
        sentinel = null_code[ctype]
        store.save_column(tidx, cidx, array(
            CODE_ARRAY_TYPE, (sentinel if c < 0 else c for c in codes)))

    store.save_dict("text", _dict_values(text_codes))
    store.save_dict("int64", _dict_values(int_codes))
    store.write_meta({
        "format": STORE_FORMAT,
        "domain_sizes": {"text": len(text_codes), "int64": len(int_codes)},
        "tables": tables_meta,
    })
    return {
        "tables": len(tables_meta),
        "rows": total_rows,
        "distinct_text_values": len(text_codes),
        "distinct_int64_values": len(int_codes),
        "seconds": round(time.monotonic() - started, 3),
    }


def _dict_values(domain):
    values = [None] * len(domain)
    for value, code in domain.items():
        values[code] = value
    return values
