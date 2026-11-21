"""End-to-end test-suite for reldisc (stdlib unittest).

Covers: the main exercise from the specification, column reorderings,
same-value-different-type, empty tables, low support, all-null columns,
joint breakage only at arity 3, low-arity bad rows rescued by nulls at
higher arity, per-row_id bad-row counting, input permutation invariance,
data-error handling, witnesses pagination, restart continuity, the
incomplete-run contract, and a randomized item-by-item cross-check of
every discovery result against an independent brute-force enumerator.
"""

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reldisc import discover as discover_mod  # noqa: E402
from reldisc import evalmap, prepare  # noqa: E402
from reldisc.errors import DataError, UsageError  # noqa: E402
from reldisc.store import Store  # noqa: E402

from tests import brute  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_manifest(dirpath, tables):
    """tables: list of (name, [(col, type)], rows)."""
    entries = []
    for name, columns, rows in tables:
        rel = name + ".jsonl"
        write_jsonl(os.path.join(dirpath, rel), rows)
        entries.append({
            "name": name,
            "path": rel,
            "columns": [{"name": c, "type": t} for c, t in columns],
        })
    path = os.path.join(dirpath, "manifest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"tables": entries}, fh)
    return path


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="reldisc-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def make_store(self, tables, dirname="data"):
        data_dir = os.path.join(self.dir, dirname)
        os.makedirs(data_dir, exist_ok=True)
        manifest = write_manifest(data_dir, tables)
        workdir = os.path.join(self.dir, "w-" + dirname)
        prepare.run_prepare(manifest, workdir)
        return manifest, Store(workdir), workdir

    def discover(self, store, **kw):
        kw.setdefault("k", 3)
        kw.setdefault("nulls", "complete")
        kw.setdefault("min_rows", 1)
        kw.setdefault("max_bad", 0)
        return discover_mod.run_discover(store, **kw)

    @staticmethod
    def cand(result, s_table, s_cols, t_table, t_cols):
        for c in result["candidates"]:
            if (c["source"]["table"] == s_table
                    and c["source"]["columns"] == s_cols
                    and c["target"]["table"] == t_table
                    and c["target"]["columns"] == t_cols):
                return c
        return None


# --------------------------------------------------------------------------
# Main exercise from the specification
# --------------------------------------------------------------------------

MAIN_COLS = [("x", "text"), ("y", "int64")]
MAIN_S = [
    {"row_id": "r1", "x": "a", "y": 1},
    {"row_id": "r2", "x": "a", "y": 2},
    {"row_id": "r3", "x": "b", "y": 1},
    {"row_id": "r4", "x": None, "y": 2},
]
MAIN_T = [
    {"row_id": "t1", "x": "a", "y": 1},
    {"row_id": "t2", "x": "b", "y": 2},
    {"row_id": "t3", "x": "a", "y": 1},
]


class TestMainExercise(Case):
    def make_main(self, s_rows=MAIN_S, t_rows=MAIN_T):
        return self.make_store([
            ("S", MAIN_COLS, s_rows),
            ("T", MAIN_COLS, t_rows),
        ])

    def test_complete_counts(self):
        _, store, _ = self.make_main()
        res = self.discover(store, k=2, nulls="complete", max_bad=2)
        c = self.cand(res, "S", ["x", "y"], "T", ["x", "y"])
        self.assertIsNotNone(c)
        self.assertEqual(
            (c["participating"], c["excluded"], c["matched"], c["unmatched"]),
            (3, 1, 1, 2))
        self.assertEqual([e["row_id"] for e in c["evidence"]], ["r2", "r3"])
        self.assertEqual([e["tuple"] for e in c["evidence"]], [["a", 2], ["b", 1]])
        # Every single value exists in the target column, yet the joint
        # tuple does not.
        for e in c["evidence"]:
            self.assertTrue(e["all_single_values_present"])

    def test_single_columns_hold_but_composite_fails(self):
        _, store, _ = self.make_main()
        res = self.discover(store, k=2, nulls="complete", max_bad=0)
        self.assertIsNotNone(self.cand(res, "S", ["x"], "T", ["x"]))
        self.assertIsNotNone(self.cand(res, "S", ["y"], "T", ["y"]))
        self.assertIsNone(self.cand(res, "S", ["x", "y"], "T", ["x", "y"]))

    def test_max_bad_threshold(self):
        _, store, _ = self.make_main()
        res2 = self.discover(store, k=2, nulls="complete", max_bad=2)
        res1 = self.discover(store, k=2, nulls="complete", max_bad=1)
        self.assertIsNotNone(self.cand(res2, "S", ["x", "y"], "T", ["x", "y"]))
        self.assertIsNone(self.cand(res1, "S", ["x", "y"], "T", ["x", "y"]))

    def test_equal_mode_counts_null_row(self):
        _, store, _ = self.make_main()
        res = self.discover(store, k=2, nulls="equal", max_bad=3)
        c = self.cand(res, "S", ["x", "y"], "T", ["x", "y"])
        self.assertEqual(
            (c["participating"], c["excluded"], c["matched"], c["unmatched"]),
            (4, 0, 1, 3))
        self.assertEqual([e["row_id"] for e in c["evidence"]],
                         ["r2", "r3", "r4"])
        self.assertEqual(c["evidence"][2]["tuple"], [None, 2])

    def test_duplicate_source_row_increases_bad(self):
        s_rows = MAIN_S + [{"row_id": "r5", "x": "a", "y": 2}]
        _, store, _ = self.make_main(s_rows=s_rows)
        res = self.discover(store, k=2, nulls="complete", max_bad=5)
        c = self.cand(res, "S", ["x", "y"], "T", ["x", "y"])
        self.assertEqual((c["participating"], c["unmatched"]), (4, 3))
        # Same unmatched value in two rows: both row_ids are reported.
        ev_ids = [e["row_id"] for e in c["evidence"]]
        self.assertEqual(ev_ids, ["r2", "r3", "r5"])
        self.assertEqual([e["tuple"] for e in c["evidence"] if e["row_id"] != "r3"],
                         [["a", 2], ["a", 2]])

    def test_duplicate_target_row_changes_nothing(self):
        t_rows = MAIN_T + [{"row_id": "t4", "x": "a", "y": 1}]
        _, store, _ = self.make_main(t_rows=t_rows)
        res = self.discover(store, k=2, nulls="complete", max_bad=2)
        c = self.cand(res, "S", ["x", "y"], "T", ["x", "y"])
        self.assertEqual(
            (c["participating"], c["matched"], c["unmatched"]), (3, 1, 2))


# --------------------------------------------------------------------------
# Focused semantics
# --------------------------------------------------------------------------

class TestColumnOrderMappings(Case):
    def test_both_target_orders_are_distinct_candidates(self):
        # A(p,q) values match B(x,y) only in the (p->x, q->y) order.
        _, store, _ = self.make_store([
            ("A", [("p", "text"), ("q", "int64")],
             [{"row_id": i, "p": p, "q": q}
              for i, (p, q) in enumerate([("a", 1), ("b", 2), ("c", 3)])]),
            ("B", [("x", "text"), ("y", "int64")],
             [{"row_id": i, "x": p, "y": q}
              for i, (p, q) in enumerate([("a", 1), ("b", 2), ("c", 3), ("d", 4)])]),
        ])
        res = self.discover(store, k=2, nulls="complete")
        self.assertIsNotNone(self.cand(res, "A", ["p", "q"], "B", ["x", "y"]))
        self.assertIsNone(self.cand(res, "A", ["p", "q"], "B", ["y", "x"]))

    def test_both_orders_listed_when_both_hold(self):
        _, store, _ = self.make_store([
            ("A", [("p", "int64"), ("q", "int64")],
             [{"row_id": 1, "p": 1, "q": 2}]),
            ("B", [("x", "int64"), ("y", "int64")],
             [{"row_id": 1, "x": 1, "y": 2}, {"row_id": 2, "x": 2, "y": 1}]),
        ])
        res = self.discover(store, k=2, nulls="complete")
        self.assertIsNotNone(self.cand(res, "A", ["p", "q"], "B", ["x", "y"]))
        self.assertIsNotNone(self.cand(res, "A", ["p", "q"], "B", ["y", "x"]))


class TestSameValueDifferentTypes(Case):
    def test_text_1_never_matches_int_1(self):
        _, store, _ = self.make_store([
            ("A", [("v", "text")], [{"row_id": 1, "v": "1"}]),
            ("B", [("v", "int64")], [{"row_id": 1, "v": 1}]),
        ])
        res = self.discover(store, k=1, nulls="complete")
        self.assertEqual(res["candidates"], [])
        self.assertEqual(res["stats"]["candidates_evaluated"], 0)

    def test_check_rejects_type_mismatched_mapping(self):
        _, store, _ = self.make_store([
            ("A", [("v", "text")], [{"row_id": 1, "v": "1"}]),
            ("B", [("v", "int64")], [{"row_id": 1, "v": 1}]),
        ])
        with self.assertRaises(UsageError):
            evalmap.evaluate_mapping(store, {
                "source_table": "A", "source_columns": ["v"],
                "target_table": "B", "target_columns": ["v"],
                "nulls": "complete"})


class TestEmptyAndNullTables(Case):
    def test_empty_source_is_insufficient_not_rejected(self):
        _, store, _ = self.make_store([
            ("A", [("v", "text")], []),
            ("B", [("v", "text")], [{"row_id": 1, "v": "a"}]),
        ])
        res = self.discover(store, k=1, nulls="complete")
        self.assertEqual(res["candidates"], [])
        self.assertEqual(len(res["insufficient"]), 1)
        self.assertEqual(res["insufficient"][0]["participating"], 0)

    def test_empty_target_makes_every_row_bad(self):
        _, store, _ = self.make_store([
            ("A", [("v", "text")], [{"row_id": 1, "v": "a"},
                                    {"row_id": 2, "v": "b"}]),
            ("B", [("v", "text")], []),
        ])
        res = self.discover(store, k=1, nulls="complete", max_bad=2)
        c = self.cand(res, "A", ["v"], "B", ["v"])
        self.assertEqual((c["participating"], c["unmatched"]), (2, 2))

    def test_low_support_is_insufficient_evidence(self):
        _, store, _ = self.make_store([
            ("A", [("v", "text")], [{"row_id": i, "v": "a"} for i in range(4)]),
            ("B", [("v", "text")], [{"row_id": 1, "v": "a"}]),
        ])
        res = self.discover(store, k=1, nulls="complete", min_rows=5)
        self.assertEqual(res["candidates"], [])
        self.assertEqual(res["insufficient"][0]["participating"], 4)
        res = self.discover(store, k=1, nulls="complete", min_rows=4)
        self.assertEqual(len(res["candidates"]), 1)

    def test_all_null_column(self):
        _, store, _ = self.make_store([
            ("A", [("v", "text")],
             [{"row_id": 1, "v": None}, {"row_id": 2, "v": None}]),
            ("B", [("v", "text")],
             [{"row_id": 1, "v": None}, {"row_id": 2, "v": "x"}]),
        ])
        # complete: nobody participates -> insufficient evidence.
        res = self.discover(store, k=1, nulls="complete")
        self.assertEqual(res["candidates"], [])
        # equal: null is a common value and matches.
        res = self.discover(store, k=1, nulls="equal")
        c = self.cand(res, "A", ["v"], "B", ["v"])
        self.assertEqual((c["participating"], c["unmatched"]), (2, 0))
        # ...but not against a target without nulls.
        _, store2, _ = self.make_store([
            ("A", [("v", "text")], [{"row_id": 1, "v": None}]),
            ("C", [("v", "text")], [{"row_id": 1, "v": "x"}]),
        ], dirname="data2")
        res = self.discover(store2, k=1, nulls="equal", max_bad=1)
        c = self.cand(res, "A", ["v"], "C", ["v"])
        self.assertEqual((c["participating"], c["unmatched"]), (1, 1))
        self.assertEqual(c["evidence"][0]["tuple"], [None])


class TestHigherArityRescue(Case):
    def test_bad_row_exits_via_null_and_arity2_holds(self):
        # Row a2 has an x value absent from B.x, but its y is null, so under
        # complete semantics it leaves the participating set at arity 2.
        _, store, _ = self.make_store([
            ("A", [("x", "text"), ("y", "int64")],
             [{"row_id": "a1", "x": "p", "y": 1},
              {"row_id": "a2", "x": "zzz", "y": None}]),
            ("B", [("x", "text"), ("y", "int64")],
             [{"row_id": "b1", "x": "p", "y": 1}]),
        ])
        res1 = self.discover(store, k=1, nulls="complete", max_bad=0)
        self.assertIsNone(self.cand(res1, "A", ["x"], "B", ["x"]))
        res2 = self.discover(store, k=2, nulls="complete", max_bad=0)
        c = self.cand(res2, "A", ["x", "y"], "B", ["x", "y"])
        self.assertIsNotNone(c)
        self.assertEqual((c["participating"], c["excluded"], c["unmatched"]),
                         (1, 1, 0))

    def test_joint_breakage_only_at_arity3(self):
        # Every pair of (1,1,1) exists in B, but the triple does not.
        _, store, _ = self.make_store([
            ("A", [("u", "int64"), ("v", "int64"), ("w", "int64")],
             [{"row_id": "a1", "u": 1, "v": 1, "w": 1}]),
            ("B", [("u", "int64"), ("v", "int64"), ("w", "int64")],
             [{"row_id": "b1", "u": 1, "v": 1, "w": 2},
              {"row_id": "b2", "u": 1, "v": 2, "w": 1},
              {"row_id": "b3", "u": 2, "v": 1, "w": 1}]),
        ])
        res = self.discover(store, k=3, nulls="complete", max_bad=0)
        for cols, tcols in ((["u", "v"], ["u", "v"]), (["u", "w"], ["u", "w"]),
                            (["v", "w"], ["v", "w"])):
            self.assertIsNotNone(self.cand(res, "A", cols, "B", tcols), cols)
        self.assertIsNone(
            self.cand(res, "A", ["u", "v", "w"], "B", ["u", "v", "w"]))
        res = self.discover(store, k=3, nulls="complete", max_bad=1)
        c = self.cand(res, "A", ["u", "v", "w"], "B", ["u", "v", "w"])
        self.assertEqual(c["unmatched"], 1)


class TestInputPermutation(Case):
    def test_permuted_input_gives_identical_results(self):
        rng = random.Random(7)
        rows_a = [{"row_id": "a%d" % i,
                   "x": rng.choice(["p", "q", None]),
                   "y": rng.choice([1, 2, 3, None])} for i in range(40)]
        rows_b = [{"row_id": "b%d" % i,
                   "x": rng.choice(["p", "q", "r"]),
                   "y": rng.choice([1, 2, 3])} for i in range(40)]
        cols = [("x", "text"), ("y", "int64")]
        _, store1, _ = self.make_store(
            [("A", cols, rows_a), ("B", cols, rows_b)], dirname="d1")
        rows_a2 = rows_a[:]
        rows_b2 = rows_b[:]
        random.Random(8).shuffle(rows_a2)
        random.Random(9).shuffle(rows_b2)
        _, store2, _ = self.make_store(
            [("A", cols, rows_a2), ("B", cols, rows_b2)], dirname="d2")
        for nulls in ("complete", "equal"):
            r1 = self.discover(store1, k=2, nulls=nulls, max_bad=3)
            r2 = self.discover(store2, k=2, nulls=nulls, max_bad=3)
            self.assertEqual(r1["candidates"], r2["candidates"])
            self.assertEqual(r1["insufficient"], r2["insufficient"])


class TestDataErrors(Case):
    def prepare_expecting_error(self, tables, manifest_extra=None):
        data_dir = os.path.join(self.dir, "bad")
        os.makedirs(data_dir, exist_ok=True)
        entries = []
        for name, columns, rows in tables:
            rel = name + ".jsonl"
            write_jsonl(os.path.join(data_dir, rel), rows)
            entries.append({"name": name, "path": rel, "columns": columns})
        manifest = os.path.join(data_dir, "manifest.json")
        with open(manifest, "w") as fh:
            json.dump({"tables": entries}, fh)
        with self.assertRaises(DataError):
            prepare.run_prepare(manifest, os.path.join(self.dir, "wbad"))

    def test_duplicate_row_id(self):
        self.prepare_expecting_error([
            ("A", [{"name": "v", "type": "text"}],
             [{"row_id": 1, "v": "a"}, {"row_id": 1, "v": "b"}]),
            ("B", [{"name": "v", "type": "text"}], [{"row_id": 1, "v": "a"}]),
        ])

    def test_unknown_column_in_data(self):
        self.prepare_expecting_error([
            ("A", [{"name": "v", "type": "text"}],
             [{"row_id": 1, "v": "a", "zzz": 1}]),
            ("B", [{"name": "v", "type": "text"}], [{"row_id": 1, "v": "a"}]),
        ])

    def test_missing_column_in_data(self):
        self.prepare_expecting_error([
            ("A", [{"name": "v", "type": "text"}, {"name": "w", "type": "text"}],
             [{"row_id": 1, "v": "a"}]),
            ("B", [{"name": "v", "type": "text"}], [{"row_id": 1, "v": "a"}]),
        ])

    def test_bool_is_not_int64(self):
        self.prepare_expecting_error([
            ("A", [{"name": "v", "type": "int64"}], [{"row_id": 1, "v": True}]),
            ("B", [{"name": "v", "type": "int64"}], [{"row_id": 1, "v": 1}]),
        ])

    def test_float_is_not_int64(self):
        self.prepare_expecting_error([
            ("A", [{"name": "v", "type": "int64"}], [{"row_id": 1, "v": 1.5}]),
            ("B", [{"name": "v", "type": "int64"}], [{"row_id": 1, "v": 1}]),
        ])

    def test_int_is_not_text(self):
        self.prepare_expecting_error([
            ("A", [{"name": "v", "type": "text"}], [{"row_id": 1, "v": 1}]),
            ("B", [{"name": "v", "type": "text"}], [{"row_id": 1, "v": "a"}]),
        ])

    def test_int64_range(self):
        self.prepare_expecting_error([
            ("A", [{"name": "v", "type": "int64"}], [{"row_id": 1, "v": 2 ** 63}]),
            ("B", [{"name": "v", "type": "int64"}], [{"row_id": 1, "v": 1}]),
        ])

    def test_corrupt_json_line(self):
        data_dir = os.path.join(self.dir, "badjson")
        os.makedirs(data_dir)
        with open(os.path.join(data_dir, "A.jsonl"), "w") as fh:
            fh.write('{"row_id": 1, "v": "a"}\n{"row_id": 2, "v": \n')
        with open(os.path.join(data_dir, "B.jsonl"), "w") as fh:
            fh.write('{"row_id": 1, "v": "a"}\n')
        manifest = os.path.join(data_dir, "manifest.json")
        with open(manifest, "w") as fh:
            json.dump({"tables": [
                {"name": "A", "path": "A.jsonl",
                 "columns": [{"name": "v", "type": "text"}]},
                {"name": "B", "path": "B.jsonl",
                 "columns": [{"name": "v", "type": "text"}]}]}, fh)
        with self.assertRaises(DataError):
            prepare.run_prepare(manifest, os.path.join(self.dir, "wbadjson"))

    def test_duplicate_key_in_json_object(self):
        data_dir = os.path.join(self.dir, "dupkey")
        os.makedirs(data_dir)
        with open(os.path.join(data_dir, "A.jsonl"), "w") as fh:
            fh.write('{"row_id": 1, "v": "a", "v": "b"}\n')
        with open(os.path.join(data_dir, "B.jsonl"), "w") as fh:
            fh.write('{"row_id": 1, "v": "a"}\n')
        manifest = os.path.join(data_dir, "manifest.json")
        with open(manifest, "w") as fh:
            json.dump({"tables": [
                {"name": "A", "path": "A.jsonl",
                 "columns": [{"name": "v", "type": "text"}]},
                {"name": "B", "path": "B.jsonl",
                 "columns": [{"name": "v", "type": "text"}]}]}, fh)
        with self.assertRaises(DataError):
            prepare.run_prepare(manifest, os.path.join(self.dir, "wdup"))

    def test_unknown_column_in_check_mapping(self):
        _, store, _ = self.make_store([
            ("A", [("v", "text")], [{"row_id": 1, "v": "a"}]),
            ("B", [("v", "text")], [{"row_id": 1, "v": "a"}]),
        ])
        with self.assertRaises(DataError):
            evalmap.evaluate_mapping(store, {
                "source_table": "A", "source_columns": ["nope"],
                "target_table": "B", "target_columns": ["v"],
                "nulls": "complete"})
        with self.assertRaises(DataError):
            evalmap.evaluate_mapping(store, {
                "source_table": "A", "source_columns": ["v"],
                "target_table": "NOPE", "target_columns": ["v"],
                "nulls": "complete"})

    def test_bad_manifest(self):
        # A manifest that cannot even be read is a usage error...
        with self.assertRaises(UsageError):
            prepare.run_prepare(os.path.join(self.dir, "missing.json"),
                                os.path.join(self.dir, "w1"))
        # ...while a readable but invalid manifest is a data error.
        path = os.path.join(self.dir, "m.json")
        with open(path, "w") as fh:
            json.dump({"tables": [
                {"name": "A", "path": "a.jsonl",
                 "columns": [{"name": "v", "type": "text"}]},
                {"name": "A", "path": "b.jsonl",
                 "columns": [{"name": "v", "type": "text"}]}]}, fh)
        with self.assertRaises(DataError):  # duplicate table name
            prepare.run_prepare(path, os.path.join(self.dir, "w2"))

    def test_illegal_mappings(self):
        _, store, _ = self.make_store([
            ("A", [("v", "text"), ("w", "text")],
             [{"row_id": 1, "v": "a", "w": "b"}]),
            ("B", [("v", "text"), ("w", "text")],
             [{"row_id": 1, "v": "a", "w": "b"}]),
        ])
        base = {"source_table": "A", "target_table": "B", "nulls": "complete"}
        for bad in (
                dict(base, source_columns=["v", "v"], target_columns=["v", "w"]),
                dict(base, source_columns=["v"], target_columns=["v", "w"]),
                dict(base, source_columns=["v"], target_columns=["v", "v"]),
                dict(base, source_columns=["v"], target_columns=["v"],
                     source_table="A", target_table="A"),
                dict(base, source_columns=["v"], target_columns=["v"],
                     nulls="sometimes")):
            with self.assertRaises(UsageError, msg=repr(bad)):
                evalmap.evaluate_mapping(store, bad)

    def test_parameter_validation(self):
        _, store, _ = self.make_store([
            ("A", [("v", "text")], [{"row_id": 1, "v": "a"}]),
            ("B", [("v", "text")], [{"row_id": 1, "v": "a"}]),
        ])
        with self.assertRaises(UsageError):
            self.discover(store, k=4)
        with self.assertRaises(UsageError):
            self.discover(store, k=0)
        with self.assertRaises(UsageError):
            self.discover(store, min_rows=0)
        with self.assertRaises(UsageError):
            self.discover(store, max_bad=-1)


class TestWitnessesPagination(Case):
    def test_pagination_and_flags(self):
        s_rows = [{"row_id": "r%02d" % i, "x": "v%d" % (i % 5), "y": i}
                  for i in range(20)]
        # Target contains every single x value and every single y value of
        # the source, but never the joint (x, y) tuples.
        t_rows = ([{"row_id": "tx%d" % i, "x": "v%d" % i, "y": 1000 + i}
                   for i in range(5)]
                  + [{"row_id": "ty%d" % i, "x": "vx", "y": i}
                     for i in range(20)])
        _, store, _ = self.make_store([
            ("S", [("x", "text"), ("y", "int64")], s_rows),
            ("T", [("x", "text"), ("y", "int64")], t_rows),
        ])
        mapping = {"source_table": "S", "source_columns": ["x", "y"],
                   "target_table": "T", "target_columns": ["x", "y"],
                   "nulls": "complete"}
        ev = evalmap.evaluate_mapping(store, mapping, want_witnesses=True)
        all_rows = ev["unmatched_rows"]
        self.assertEqual(len(all_rows), 20)
        self.assertEqual([e["row_id"] for e in all_rows],
                         sorted(e["row_id"] for e in all_rows))
        for e in all_rows:
            self.assertEqual(e["per_column_value_present"], [True, True])
            self.assertTrue(e["all_single_values_present"])
        # Paginate and reassemble.
        collected = []
        for offset in range(0, 20, 7):
            page = evalmap.evaluate_mapping(
                store, mapping, want_witnesses=True)["unmatched_rows"][offset:offset + 7]
            collected.extend(page)
        self.assertEqual(collected, all_rows)


class TestIncompleteRuns(Case):
    def test_candidate_limit_marks_incomplete(self):
        _, store, _ = self.make_store([
            ("A", [("x", "text"), ("y", "int64")],
             [{"row_id": i, "x": "a", "y": i} for i in range(10)]),
            ("B", [("x", "text"), ("y", "int64")],
             [{"row_id": i, "x": "a", "y": i} for i in range(10)]),
        ])
        res = self.discover(store, k=2, nulls="complete", candidate_limit=1)
        self.assertEqual(res["status"], "incomplete")
        self.assertTrue(res["uncovered"])
        self.assertEqual(res["stats"]["candidates_evaluated"], 1)
        # A complete run must find strictly more candidates.
        full = self.discover(store, k=2, nulls="complete")
        self.assertEqual(full["status"], "complete")
        self.assertEqual(full["uncovered"], [])
        self.assertGreater(full["stats"]["candidates_evaluated"], 1)
        # Everything listed by the partial run is also listed by the full run.
        self.assertLessEqual(
            {(json.dumps(c["source"], sort_keys=True),
              json.dumps(c["target"], sort_keys=True))
             for c in res["candidates"]},
            {(json.dumps(c["source"], sort_keys=True),
              json.dumps(c["target"], sort_keys=True))
             for c in full["candidates"]})

    def test_time_budget_zero_marks_incomplete(self):
        _, store, _ = self.make_store([
            ("A", [("v", "text")], [{"row_id": 1, "v": "a"}]),
            ("B", [("v", "text")], [{"row_id": 1, "v": "a"}]),
        ])
        res = self.discover(store, k=1, nulls="complete", time_budget_sec=0.0)
        self.assertEqual(res["status"], "incomplete")


class TestSupportPropagation(Case):
    def test_insufficiency_propagates_to_supersets(self):
        # Table A has 2 rows; min_rows=3 -> every source set of A is
        # insufficient, and arity-2/3 sets must be marked as propagated.
        _, store, _ = self.make_store([
            ("A", [("x", "text"), ("y", "int64")],
             [{"row_id": 1, "x": "a", "y": 1}, {"row_id": 2, "x": "b", "y": 2}]),
            ("B", [("x", "text"), ("y", "int64")],
             [{"row_id": i, "x": "a", "y": i} for i in range(5)]),
        ])
        res = self.discover(store, k=2, nulls="complete", min_rows=3)
        reasons = {(i["table"], tuple(i["columns"])): i["reason"]
                   for i in res["insufficient"]}
        self.assertEqual(reasons[("A", ("x",))], "support_below_min_rows")
        self.assertEqual(reasons[("A", ("x", "y"))],
                         "propagated_from_insufficient_subset")
        # No candidate with source A may be listed.
        self.assertFalse([c for c in res["candidates"]
                          if c["source"]["table"] == "A"])


# --------------------------------------------------------------------------
# CLI-level tests: restart continuity and exit codes
# --------------------------------------------------------------------------

class TestCli(Case):
    def run_cli(self, *argv, cwd=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = PROJECT_ROOT
        return subprocess.run(
            [sys.executable, "-m", "reldisc", *argv],
            cwd=cwd or self.dir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_empty_dir_full_cycle_across_processes(self):
        run_dir = os.path.join(self.dir, "run")
        os.makedirs(run_dir)
        manifest = write_manifest(run_dir, [
            ("S", MAIN_COLS, MAIN_S),
            ("T", MAIN_COLS, MAIN_T),
        ])
        workdir = os.path.join(run_dir, "w")

        p = self.run_cli("prepare", "--manifest", manifest, "--workdir", workdir)
        self.assertEqual(p.returncode, 0, p.stderr)

        # New process, only the prepared workdir is needed from now on.
        p = self.run_cli("discover", "--workdir", workdir, "--k", "2",
                         "--nulls", "complete", "--min-rows", "1",
                         "--max-bad", "2")
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["status"], "complete")
        hit = [c for c in out["candidates"]
               if c["source"]["table"] == "S"
               and c["source"]["columns"] == ["x", "y"]]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["unmatched"], 2)

        p = self.run_cli("check", "--workdir", workdir, "--mapping-json",
                         json.dumps({"source_table": "S",
                                     "source_columns": ["y", "x"],
                                     "target_table": "T",
                                     "target_columns": ["y", "x"],
                                     "nulls": "complete",
                                     "min_rows": 1, "max_bad": 2}))
        self.assertEqual(p.returncode, 0, p.stderr)
        checked = json.loads(p.stdout)
        self.assertEqual(checked["verdict"], "accepted")
        # Source columns were canonicalised (y,x) -> (x,y) with the target
        # permutation preserved.
        self.assertEqual(checked["mapping"]["source_columns"], ["x", "y"])
        self.assertEqual(checked["mapping"]["target_columns"], ["x", "y"])

        p = self.run_cli("witnesses", "--workdir", workdir, "--mapping-json",
                         json.dumps({"source_table": "S",
                                     "source_columns": ["x", "y"],
                                     "target_table": "T",
                                     "target_columns": ["x", "y"],
                                     "nulls": "complete"}),
                         "--offset", "0", "--limit", "1")
        self.assertEqual(p.returncode, 0, p.stderr)
        page = json.loads(p.stdout)
        self.assertEqual(page["unmatched_total"], 2)
        self.assertEqual(len(page["entries"]), 1)
        self.assertEqual(page["entries"][0]["row_id"], "r2")

    def test_data_error_exit_code(self):
        run_dir = os.path.join(self.dir, "run2")
        os.makedirs(run_dir)
        manifest = write_manifest(run_dir, [
            ("A", [("v", "int64")], [{"row_id": 1, "v": True}]),
            ("B", [("v", "int64")], [{"row_id": 1, "v": 1}]),
        ])
        p = self.run_cli("prepare", "--manifest", manifest,
                         "--workdir", os.path.join(run_dir, "w"))
        self.assertEqual(p.returncode, 3)
        self.assertIn("data error", p.stderr)

    def test_unprepared_workdir_exit_code(self):
        p = self.run_cli("check", "--workdir",
                         os.path.join(self.dir, "nothing"),
                         "--mapping-json", "{}")
        self.assertEqual(p.returncode, 2)


# --------------------------------------------------------------------------
# Randomized item-by-item cross-check against the brute-force enumerator
# --------------------------------------------------------------------------

def random_tables(rng, n_tables=4, n_cols=5, n_rows=28):
    text_domain = ["s%d" % i for i in range(6)]
    int_domain = list(range(8))
    tables = []
    for t in range(n_tables):
        cols = [("c%d" % i, "text" if i % 2 == 0 else "int64")
                for i in range(n_cols)]
        rows = []
        for r in range(n_rows):
            row = {"row_id": "t%dr%d" % (t, r)}
            for cname, ctype in cols:
                if rng.random() < 0.15:
                    row[cname] = None
                elif ctype == "text":
                    row[cname] = rng.choice(text_domain)
                else:
                    row[cname] = rng.choice(int_domain)
            rows.append(row)
        tables.append((cols, rows))
    # Plant a couple of inclusions so accepted candidates exist.
    s_cols, s_rows = tables[0]
    t_cols, t_rows = tables[1]
    for r, row in enumerate(s_rows):
        if r % 3 != 0 and t_rows:
            donor = t_rows[r % len(t_rows)]
            row["c0"] = donor["c0"]
            row["c1"] = donor["c1"]
    return tables


class TestCrossCheck(Case):
    def compare_with_brute(self, manifest, store, k, nulls, min_rows, max_bad):
        got = self.discover(store, k=k, nulls=nulls,
                            min_rows=min_rows, max_bad=max_bad)
        self.assertEqual(got["status"], "complete")
        want = brute.brute_discover(manifest, k, nulls, min_rows, max_bad)
        got_items = [
            (c["source"]["table"], tuple(c["source"]["columns"]),
             c["target"]["table"], tuple(c["target"]["columns"]),
             c["participating"], c["excluded"], c["matched"], c["unmatched"])
            for c in got["candidates"]]
        want_items = [
            (c["source"]["table"], tuple(c["source"]["columns"]),
             c["target"]["table"], tuple(c["target"]["columns"]),
             c["participating"], c["excluded"], c["matched"], c["unmatched"])
            for c in want]
        self.assertEqual(got_items, want_items,
                         "nulls=%s min_rows=%d max_bad=%d"
                         % (nulls, min_rows, max_bad))
        # Evidence samples: stable, sorted by row_id, one entry per row.
        for gc, wc in zip(got["candidates"], want):
            ev = gc["evidence"]
            self.assertEqual([e["row_id"] for e in ev],
                             wc["unmatched_row_ids"][:len(ev)])
            self.assertEqual([e["tuple"] for e in ev],
                             wc["unmatched_tuples"][:len(ev)])
            self.assertLessEqual(len(ev), 10)

    def test_randomized_crosscheck(self):
        for seed in (1, 2, 3):
            rng = random.Random(seed)
            tables = random_tables(rng)
            data_dir = os.path.join(self.dir, "rnd%d" % seed)
            os.makedirs(data_dir)
            manifest = write_manifest(data_dir, [
                ("T%d" % i, cols, rows) for i, (cols, rows) in enumerate(tables)])
            workdir = os.path.join(self.dir, "wrnd%d" % seed)
            prepare.run_prepare(manifest, workdir)
            store = Store(workdir)
            for nulls in ("complete", "equal"):
                for min_rows, max_bad in ((1, 0), (3, 2)):
                    self.compare_with_brute(manifest, store, 3, nulls,
                                            min_rows, max_bad)

    def test_main_exercise_matches_brute(self):
        manifest, store, _ = self.make_store([
            ("S", MAIN_COLS, MAIN_S), ("T", MAIN_COLS, MAIN_T)])
        for nulls in ("complete", "equal"):
            for max_bad in (0, 1, 2, 3):
                got = self.discover(store, k=2, nulls=nulls, max_bad=max_bad)
                want = brute.brute_discover(manifest, 2, nulls, 1, max_bad)
                self.assertEqual(
                    [(c["source"]["table"], tuple(c["source"]["columns"]),
                      c["target"]["table"], tuple(c["target"]["columns"]),
                      c["unmatched"]) for c in got["candidates"]],
                    [(c["source"]["table"], tuple(c["source"]["columns"]),
                      c["target"]["table"], tuple(c["target"]["columns"]),
                      c["unmatched"]) for c in want])


if __name__ == "__main__":
    unittest.main(verbosity=2)
