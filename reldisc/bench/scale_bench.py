#!/usr/bin/env python3
"""Scale benchmark: 5 tables x 6 columns x 50 000 rows, k=3.

Generated at runtime under bench_work/ (never committed).  The data mixes
text and int64 columns and contains many distractors whose single columns
look related while the composite relationships fail, plus a few planted
real inclusions:

  * T1 is mostly sampled from T0 (whole rows)  -> T1.* -> T0.* holds for
    every column subset of the copied rows (unmatched=0).
  * T2.(a,u) is sampled from T1.(a,u) with exactly 2 doctored rows whose
    pair exists nowhere in T0/T1 although both single values do
    -> accepted only when max_bad >= 2.
  * T3 columns are domain-subsets of T0 columns pairwise (a/u, b/v, c/w)
    but the pairs are independent -> single-column candidates accepted,
    composites rejected ("edge values complete, joint reference absent").
  * T4 is random filler in overlapping domains.

Both prepare and discover run in subprocesses capped at 256 MiB address
space; the benchmark reports candidate pruning, exact verification counts,
wall times and peak RSS, and asserts the planted expectations.
"""

import json
import os
import random
import resource
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORK = os.path.join(HERE, "bench_work")
N_ROWS = 50000
MEMORY_LIMIT_BYTES = 256 * 2 ** 20  # 256 MiB

COLS = [("a", "text"), ("b", "text"), ("c", "text"),
        ("u", "int64"), ("v", "int64"), ("w", "int64")]


def limit_memory():
    resource.setrlimit(resource.RLIMIT_AS,
                       (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))


def cli(*argv):
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "reldisc", *argv],
        cwd=WORK, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, preexec_fn=limit_memory)
    wall = time.monotonic() - t0
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024
    if proc.returncode != 0:
        raise SystemExit("command failed: %s\nstderr:\n%s" % (argv, proc.stderr))
    return json.loads(proc.stdout), wall, peak


def gen_rows(rng, n, null_rate=0.0):
    rows = []
    for i in range(n):
        row = {"row_id": "r%07d" % i}
        for name, typ in COLS:
            if null_rate and rng.random() < null_rate:
                row[name] = None
            elif typ == "text":
                row[name] = "s%s%05d" % (name, rng.randrange(45000))
            else:
                row[name] = rng.randrange(45000)
        rows.append(row)
    return rows


def generate():
    rng = random.Random(20261121)
    tables = {}

    t0 = gen_rows(rng, N_ROWS)
    tables["T0"] = t0

    # T1: 50k whole rows sampled from T0 with replacement, so every column
    # subset of T1 is exactly covered by the corresponding T0 columns.
    t1 = [dict(rng.choice(t0), row_id="t1_%07d" % i) for i in range(N_ROWS)]
    tables["T1"] = t1

    # T2: (a,u) pairs sampled from T1's non-null pairs, plus exactly 2
    # doctored rows whose pair exists nowhere in T0/T1 while both single
    # values do.
    t1_pairs = list({(r["a"], r["u"]) for r in t1
                     if r["a"] is not None and r["u"] is not None})
    t0_pairs = {(r["a"], r["u"]) for r in t0}
    t0_a = [r["a"] for r in t0 if r["a"] is not None]
    t0_u = [r["u"] for r in t0 if r["u"] is not None]
    doctored = []
    while len(doctored) < 2:
        pair = (rng.choice(t0_a), rng.choice(t0_u))
        if pair not in t0_pairs:
            doctored.append(pair)
    t2 = []
    for i in range(N_ROWS - 2):
        a, u = t1_pairs[rng.randrange(len(t1_pairs))]
        row = {"row_id": "t2_%07d" % i, "a": a, "u": u}
        for name, typ in COLS:
            if name in ("a", "u"):
                continue
            if typ == "text":
                row[name] = "s%s%05d" % (name, rng.randrange(45000))
            else:
                row[name] = rng.randrange(45000)
        t2.append(row)
    for i, (a, u) in enumerate(doctored):
        row = {"row_id": "t2_bad%d" % i, "a": a, "u": u,
               "b": "sb00000", "c": "sc00000", "v": 0, "w": 0}
        t2.append(row)
    tables["T2"] = t2

    # T3: per-column domain subsets of T0, independent pairings, 1% nulls.
    dom = {name: [r[name] for r in t0 if r[name] is not None] for name, _ in COLS}
    t3 = []
    for i in range(N_ROWS):
        row = {"row_id": "t3_%07d" % i}
        for name, _ in COLS:
            if rng.random() < 0.01:
                row[name] = None
            else:
                row[name] = dom[name][rng.randrange(len(dom[name]))]
        t3.append(row)
    tables["T3"] = t3

    # T4: random filler in overlapping domains.
    tables["T4"] = gen_rows(rng, N_ROWS, null_rate=0.01)

    data = os.path.join(WORK, "data")
    os.makedirs(data, exist_ok=True)
    entries = []
    for tname, rows in tables.items():
        path = os.path.join(data, tname + ".jsonl")
        with open(path, "w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        entries.append({
            "name": tname, "path": path,
            "columns": [{"name": c, "type": t} for c, t in COLS]})
    manifest = os.path.join(WORK, "manifest.json")
    with open(manifest, "w") as fh:
        json.dump({"tables": entries}, fh)
    return manifest


def find(result, s_table, s_cols, t_table, t_cols):
    for c in result["candidates"]:
        if (c["source"]["table"] == s_table and c["source"]["columns"] == s_cols
                and c["target"]["table"] == t_table
                and c["target"]["columns"] == t_cols):
            return c
    return None


def main():
    run_equal = "--equal" in sys.argv
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)

    print("== generating 5 tables x 6 columns x %d rows ==" % N_ROWS)
    t0 = time.monotonic()
    manifest = generate()
    gen_sec = time.monotonic() - t0
    print("generated in %.1fs" % gen_sec)

    print("== prepare (256 MiB address-space cap) ==")
    prep, prep_wall, prep_peak = cli("prepare", "--manifest", manifest,
                                     "--workdir", os.path.join(WORK, "store"))
    print(json.dumps(prep["stats"], indent=1))
    print("prepare wall %.1fs, child peak RSS so far %.1f MiB"
          % (prep_wall, prep_peak / 2 ** 20))

    expectations = []
    summary = {"generate_seconds": round(gen_sec, 1),
               "prepare": {"stats": prep["stats"],
                           "wall_seconds": round(prep_wall, 1)},
               "memory_limit_bytes": MEMORY_LIMIT_BYTES,
               "child_peak_rss_bytes": prep_peak,
               "runs": {}}

    def check(ok, text):
        expectations.append(ok)
        print("  [%s] %s" % ("OK" if ok else "FAIL", text))

    for nulls in (["complete", "equal"] if run_equal else ["complete"]):
        print("== discover k=3 nulls=%s min_rows=100 max_bad=2 "
              "(256 MiB cap) ==" % nulls)
        out_path = os.path.join(WORK, "discovery_%s.json" % nulls)
        res, wall, peak = cli(
            "discover", "--workdir", os.path.join(WORK, "store"),
            "--k", "3", "--nulls", nulls, "--min-rows", "100",
            "--max-bad", "2", "--out", out_path)
        st = res["stats"]
        summary["runs"][nulls] = {
            "status": res["status"],
            "wall_seconds": round(wall, 1),
            "stats": st,
            "accepted": len(res["candidates"]),
        }
        summary["child_peak_rss_bytes"] = max(
            summary["child_peak_rss_bytes"], peak)
        print("  status=%s wall=%.1fs" % (res["status"], wall))
        print("  candidates evaluated: %d (accepted %d, rejected %d)"
              % (st["candidates_evaluated"], st["candidates_accepted"],
                 st["candidates_rejected"]))
        print("  support-pruned source sets: %s"
              % sum(a["source_sets_insufficient"]
                    for a in st["per_arity"].values()))
        print("  projections built: %d source / %d target (shared)"
              % (st["source_projections_built"],
                 st["target_projections_built"]))
        print("  exact re-verifications of accepted candidates: %d"
              % st["exact_reverifications"])
        print("  per-arity seconds: %s"
              % {a: s["seconds"] for a, s in st["per_arity"].items()})
        print("  peak RSS (self-report): %.1f MiB; child peak RSS: %.1f MiB"
              % (st["peak_rss_bytes"] / 2 ** 20, peak / 2 ** 20))

        if nulls == "complete":
            c = find(res, "T1", ["a", "u"], "T0", ["a", "u"])
            check(c is not None and c["unmatched"] == 0,
                  "planted: T1(a,u)->T0(a,u) exact inclusion")
            c = find(res, "T1", ["a", "c", "u"], "T0", ["a", "c", "u"])
            check(c is not None and c["unmatched"] == 0,
                  "planted: T1(a,c,u)->T0(a,c,u) exact ternary inclusion")
            c = find(res, "T2", ["a", "u"], "T1", ["a", "u"])
            check(c is not None and c["unmatched"] == 2,
                  "planted: T2(a,u)->T1(a,u) with exactly 2 bad rows")
            check(find(res, "T3", ["a", "u"], "T0", ["a", "u"]) is None,
                  "distractor: T3(a,u)->T0(a,u) rejected although both "
                  "single columns look related")
            check(find(res, "T4", ["a", "u"], "T0", ["a", "u"]) is None,
                  "distractor: T4(a,u)->T0(a,u) rejected")
            singles = [c for c in res["candidates"]
                       if c["source"]["table"] == "T3"
                       and c["source"]["columns"] == ["a"]
                       and c["target"]["table"] == "T0"]
            check(bool(singles),
                  "distractor: T3.a->T0.a single-column candidate accepted")

    summary["expectations_passed"] = all(expectations)
    with open(os.path.join(WORK, "bench_result.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print()
    print("summary written to %s" % os.path.join(WORK, "bench_result.json"))
    if not expectations or not all(expectations):
        print("BENCHMARK EXPECTATIONS FAILED")
        return 1
    print("ALL %d BENCHMARK EXPECTATIONS PASSED" % len(expectations))
    return 0


if __name__ == "__main__":
    sys.exit(main())
