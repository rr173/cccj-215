"""Independent brute-force inclusion checker used by the test-suite.

It reads the manifest and the raw JSONL files directly (never the prepared
store), enumerates every legal column mapping, and evaluates each mapping
row by row in the most naive way possible.  Discovery results are compared
against this implementation item by item.
"""

import itertools
import json
import os


def load_tables(manifest_path):
    with open(manifest_path, "r", encoding="utf-8") as fh:
        man = json.load(fh)
    base = os.path.dirname(os.path.abspath(manifest_path))
    tables = []
    for tdoc in man["tables"]:
        rows = []
        with open(os.path.join(base, tdoc["path"]), "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    rows.append(json.loads(line))
        tables.append({
            "name": tdoc["name"],
            "columns": [(c["name"], c["type"]) for c in tdoc["columns"]],
            "rows": rows,
        })
    return tables


def rowid_key(rid):
    if isinstance(rid, int):
        return (0, rid, "")
    return (1, 0, rid)


def enumerate_mappings(tables, k):
    """Every legal (source set, target tuple) pair, both target orders."""
    for arity in range(1, k + 1):
        for s in tables:
            s_sorted = sorted(s["columns"])  # canonical source order by name
            for scombo in itertools.combinations(s_sorted, arity):
                for t in tables:
                    if t["name"] == s["name"]:
                        continue
                    eligible = [[c for c in t["columns"] if c[1] == sc[1]]
                                for sc in scombo]
                    for tcombo in itertools.product(*eligible):
                        names = [c[0] for c in tcombo]
                        if len(set(names)) != arity:
                            continue
                        yield (s["name"], tuple(c[0] for c in scombo),
                               t["name"], tuple(names))


def evaluate(tables_by_name, s_name, s_cols, t_name, t_cols, nulls):
    s = tables_by_name[s_name]
    t = tables_by_name[t_name]
    complete = nulls == "complete"
    tset = set()
    for row in t["rows"]:
        vals = tuple(row[c] for c in t_cols)
        if complete and any(v is None for v in vals):
            continue
        tset.add(vals)
    participating = excluded = matched = unmatched = 0
    unmatched_rows = []
    for row in s["rows"]:
        vals = tuple(row[c] for c in s_cols)
        if complete and any(v is None for v in vals):
            excluded += 1
            continue
        participating += 1
        if vals in tset:
            matched += 1
        else:
            unmatched += 1
            unmatched_rows.append((row["row_id"], vals))
    unmatched_rows.sort(key=lambda e: rowid_key(e[0]))
    return {
        "participating": participating,
        "excluded": excluded,
        "matched": matched,
        "unmatched": unmatched,
        "unmatched_row_ids": [rid for rid, _ in unmatched_rows],
        "unmatched_tuples": [list(v) for _, v in unmatched_rows],
    }


def brute_discover(manifest_path, k, nulls, min_rows, max_bad):
    tables = load_tables(manifest_path)
    by_name = {t["name"]: t for t in tables}
    accepted = []
    for s_name, s_cols, t_name, t_cols in enumerate_mappings(tables, k):
        r = evaluate(by_name, s_name, s_cols, t_name, t_cols, nulls)
        if r["participating"] >= min_rows and r["unmatched"] <= max_bad:
            accepted.append({
                "source": {"table": s_name, "columns": list(s_cols)},
                "target": {"table": t_name, "columns": list(t_cols)},
                "participating": r["participating"],
                "excluded": r["excluded"],
                "matched": r["matched"],
                "unmatched": r["unmatched"],
                "unmatched_row_ids": r["unmatched_row_ids"],
                "unmatched_tuples": r["unmatched_tuples"],
            })
    accepted.sort(key=lambda r: (
        r["source"]["table"], r["source"]["columns"],
        r["target"]["table"], r["target"]["columns"]))
    return accepted
