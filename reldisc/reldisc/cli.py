"""Command line interface: prepare / discover / check / witnesses."""

import argparse
import json
import sys

from . import discover as discover_mod
from . import evalmap, prepare
from .errors import ReldiscError, UsageError
from .store import Store

CHECK_NOTE = ("verdict applies to this mapping under the given parameters; "
              "it is a validation candidate, not a declared permanent "
              "foreign key")


def _json_out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def _load_mapping(args):
    if args.mapping_json is not None:
        text = args.mapping_json
    elif args.mapping_file is not None:
        try:
            with open(args.mapping_file, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            raise UsageError("cannot read mapping file: %s" % exc)
    else:
        raise UsageError("provide --mapping-json or --mapping-file")
    try:
        mapping = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError("mapping is not valid JSON: %s" % exc)
    return mapping


def _thresholds(mapping):
    min_rows = mapping.get("min_rows", 1)
    max_bad = mapping.get("max_bad", 0)
    if not isinstance(min_rows, int) or isinstance(min_rows, bool) or min_rows < 1:
        raise UsageError("min_rows must be a positive integer")
    if not isinstance(max_bad, int) or isinstance(max_bad, bool) or max_bad < 0:
        raise UsageError("max_bad must be a non-negative integer")
    return min_rows, max_bad


def _cmd_prepare(args):
    stats = prepare.run_prepare(args.manifest, args.workdir)
    _json_out({"status": "prepared", "workdir": args.workdir, "stats": stats})
    return 0


def _cmd_discover(args):
    store = Store(args.workdir)
    store.meta()  # fail early if not prepared
    out = discover_mod.run_discover(
        store, k=args.k, nulls=args.nulls, min_rows=args.min_rows,
        max_bad=args.max_bad, time_budget_sec=args.time_budget,
        candidate_limit=args.candidate_limit,
        log=lambda m: print("discover: " + m, file=sys.stderr))
    text = json.dumps(out, ensure_ascii=False, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    return 0


def _cmd_check(args):
    store = Store(args.workdir)
    mapping = _load_mapping(args)
    min_rows, max_bad = _thresholds(mapping)
    ev = evalmap.evaluate_mapping(store, mapping, want_witnesses=True)
    if ev["participating"] < min_rows:
        verdict = "insufficient_evidence"
    elif ev["unmatched"] <= max_bad:
        verdict = "accepted"
    else:
        verdict = "rejected"
    _json_out({
        "mapping": evalmap.canonical_mapping_json(store.meta(), mapping),
        "min_rows": min_rows,
        "max_bad": max_bad,
        "verdict": verdict,
        "participating": ev["participating"],
        "excluded": ev["excluded"],
        "matched": ev["matched"],
        "unmatched": ev["unmatched"],
        "evidence": ev["unmatched_rows"][:args.evidence_limit],
        "note": CHECK_NOTE,
    })
    return 0


def _cmd_witnesses(args):
    store = Store(args.workdir)
    mapping = _load_mapping(args)
    ev = evalmap.evaluate_mapping(store, mapping, want_witnesses=True)
    rows = ev["unmatched_rows"]
    page = rows[args.offset:args.offset + args.limit]
    _json_out({
        "mapping": evalmap.canonical_mapping_json(store.meta(), mapping),
        "participating": ev["participating"],
        "excluded": ev["excluded"],
        "matched": ev["matched"],
        "unmatched": ev["unmatched"],
        "unmatched_total": len(rows),
        "offset": args.offset,
        "limit": args.limit,
        "entries": page,
    })
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="reldisc",
        description="offline cross-table inclusion (reference candidate) "
                    "discovery over JSONL tables")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="validate inputs and build the typed store")
    p.add_argument("--manifest", required=True)
    p.add_argument("--workdir", required=True)
    p.set_defaults(func=_cmd_prepare)

    p = sub.add_parser("discover", help="exhaustive exact candidate search")
    p.add_argument("--workdir", required=True)
    p.add_argument("--k", type=int, required=True, choices=(1, 2, 3))
    p.add_argument("--nulls", required=True, choices=evalmap.NULLS_MODES)
    p.add_argument("--min-rows", type=int, default=1)
    p.add_argument("--max-bad", type=int, default=0)
    p.add_argument("--time-budget", type=float, default=None,
                   help="seconds; exceeding it marks the run incomplete")
    p.add_argument("--candidate-limit", type=int, default=None,
                   help="stop after this many candidate evaluations "
                        "(marks the run incomplete)")
    p.add_argument("--out", default=None, help="also write the JSON result here")
    p.set_defaults(func=_cmd_discover)

    p = sub.add_parser("check", help="exactly evaluate one mapping")
    p.add_argument("--workdir", required=True)
    p.add_argument("--mapping-json", default=None)
    p.add_argument("--mapping-file", default=None)
    p.add_argument("--evidence-limit", type=int, default=10)
    p.set_defaults(func=_cmd_check)

    p = sub.add_parser("witnesses", help="paginate unmatched rows of a mapping")
    p.add_argument("--workdir", required=True)
    p.add_argument("--mapping-json", default=None)
    p.add_argument("--mapping-file", default=None)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=_cmd_witnesses)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ReldiscError as exc:
        print("reldisc: %s: %s" % (exc.label, exc), file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
