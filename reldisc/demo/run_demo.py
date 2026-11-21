#!/usr/bin/env python3
"""Main exercise from the specification, end to end.

Source S(x, y):  (a,1) (a,2) (b,1) (null,2)
Target T(x, y):  (a,1) (b,2) (a,1)

Expected rows and counts (printed and asserted below):

  complete nulls, candidate S(x,y) -> T(x,y):
    participating rows: r1=(a,1) r2=(a,2) r3=(b,1)   -> 3
    excluded (null in a source column): r4=(null,2)  -> 1
    matched:   r1=(a,1)                              -> 1
    unmatched: r2=(a,2) r3=(b,1)                     -> 2
    max_bad=2 -> listed;  max_bad=1 -> not listed
    single columns S.x->T.x and S.y->T.y both cover exactly (unmatched=0),
    yet the composite does not -- single-column coverage never implies
    joint coverage.
  equal nulls: the null row r4=(null,2) participates and does not match
    -> participating 4, excluded 0, matched 1, unmatched 3.
  Duplicating source row (a,2) under a new row_id r5: unmatched 2 -> 3.
  Duplicating target row (a,1) again:            unmatched stays 2.
"""

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORK = os.path.join(HERE, "demo_work")

S_ROWS = [
    {"row_id": "r1", "x": "a", "y": 1},
    {"row_id": "r2", "x": "a", "y": 2},
    {"row_id": "r3", "x": "b", "y": 1},
    {"row_id": "r4", "x": None, "y": 2},
]
T_ROWS = [
    {"row_id": "t1", "x": "a", "y": 1},
    {"row_id": "t2", "x": "b", "y": 2},
    {"row_id": "t3", "x": "a", "y": 1},
]
COLS = [{"name": "x", "type": "text"}, {"name": "y", "type": "int64"}]

CHECKS = []


def report(ok, text):
    CHECKS.append(ok)
    print("  [%s] %s" % ("OK" if ok else "FAIL", text))


def cli(*argv):
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    proc = subprocess.run(
        [sys.executable, "-m", "reldisc", *argv],
        cwd=WORK, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise SystemExit("command failed: %s\n%s" % (argv, proc.stderr))
    return json.loads(proc.stdout)


def build_dataset(name, s_rows, t_rows):
    data = os.path.join(WORK, name)
    os.makedirs(data, exist_ok=True)
    for tname, rows in (("S", s_rows), ("T", t_rows)):
        with open(os.path.join(data, tname + ".jsonl"), "w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
    manifest = os.path.join(data, "manifest.json")
    with open(manifest, "w") as fh:
        json.dump({"tables": [
            {"name": "S", "path": os.path.join(data, "S.jsonl"), "columns": COLS},
            {"name": "T", "path": os.path.join(data, "T.jsonl"), "columns": COLS},
        ]}, fh)
    workdir = os.path.join(WORK, "store-" + name)
    cli("prepare", "--manifest", manifest, "--workdir", workdir)
    return workdir


def composite(result):
    for c in result["candidates"]:
        if (c["source"]["table"] == "S" and c["source"]["columns"] == ["x", "y"]
                and c["target"]["table"] == "T"):
            return c
    return None


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    workdir = build_dataset("main", S_ROWS, T_ROWS)

    print("== complete nulls, k=2, min_rows=1, max_bad=2 ==")
    res = cli("discover", "--workdir", workdir, "--k", "2",
              "--nulls", "complete", "--min-rows", "1", "--max-bad", "2")
    c = composite(res)
    report(c is not None, "composite candidate S(x,y)->T(x,y) is listed")
    if c:
        report((c["participating"], c["excluded"], c["matched"], c["unmatched"])
               == (3, 1, 1, 2),
               "participating=3 excluded=1 matched=1 unmatched=2 (got %s)"
               % ((c["participating"], c["excluded"], c["matched"],
                   c["unmatched"]),))
        report([e["row_id"] for e in c["evidence"]] == ["r2", "r3"],
               "evidence rows are r2=(a,2) and r3=(b,1), sorted by row_id")
        report(all(e["all_single_values_present"] for e in c["evidence"]),
               "every single column value exists in T, yet the joint "
               "tuples do not")

    print("== single columns cover exactly (max_bad=0) but composite does not ==")
    res = cli("discover", "--workdir", workdir, "--k", "2",
              "--nulls", "complete", "--min-rows", "1", "--max-bad", "0")
    sx = any(c["source"]["columns"] == ["x"] and c["source"]["table"] == "S"
             for c in res["candidates"])
    sy = any(c["source"]["columns"] == ["y"] and c["source"]["table"] == "S"
             for c in res["candidates"])
    report(sx and sy, "S.x->T.x and S.y->T.y both listed with unmatched=0")
    report(composite(res) is None,
           "composite S(x,y)->T(x,y) NOT listed with max_bad=0")

    print("== max_bad threshold ==")
    res = cli("discover", "--workdir", workdir, "--k", "2",
              "--nulls", "complete", "--min-rows", "1", "--max-bad", "1")
    report(composite(res) is None,
           "max_bad=1: composite not listed (2 bad rows)")
    res = cli("discover", "--workdir", workdir, "--k", "2",
              "--nulls", "complete", "--min-rows", "1", "--max-bad", "2")
    report(composite(res) is not None, "max_bad=2: composite listed")

    print("== equal nulls: the null row participates and mismatches ==")
    res = cli("discover", "--workdir", workdir, "--k", "2",
              "--nulls", "equal", "--min-rows", "1", "--max-bad", "3")
    c = composite(res)
    report(c is not None
           and (c["participating"], c["excluded"], c["unmatched"]) == (4, 0, 3),
           "participating=4 excluded=0 unmatched=3 (r4=(null,2) added)")
    if c:
        report([e["row_id"] for e in c["evidence"]] == ["r2", "r3", "r4"],
               "evidence rows r2, r3, r4")

    print("== duplicate source row (a,2) with a new row_id: bad 2 -> 3 ==")
    wd = build_dataset("dup-source",
                       S_ROWS + [{"row_id": "r5", "x": "a", "y": 2}], T_ROWS)
    res = cli("discover", "--workdir", wd, "--k", "2",
              "--nulls", "complete", "--min-rows", "1", "--max-bad", "3")
    c = composite(res)
    report(c is not None and c["unmatched"] == 3
           and [e["row_id"] for e in c["evidence"]] == ["r2", "r3", "r5"],
           "unmatched=3 with evidence r2, r3, r5 (same value counted twice)")

    print("== duplicate target row (a,1): nothing changes ==")
    wd = build_dataset("dup-target", S_ROWS,
                       T_ROWS + [{"row_id": "t4", "x": "a", "y": 1}])
    res = cli("discover", "--workdir", wd, "--k", "2",
              "--nulls", "complete", "--min-rows", "1", "--max-bad", "2")
    c = composite(res)
    report(c is not None and (c["matched"], c["unmatched"]) == (1, 2),
           "matched=1 unmatched=2 (duplicate targets do not amplify coverage)")

    print()
    if all(CHECKS) and CHECKS:
        print("ALL %d DEMO CHECKS PASSED" % len(CHECKS))
        return 0
    print("DEMO FAILED (%d/%d checks passed)"
          % (sum(CHECKS), len(CHECKS)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
