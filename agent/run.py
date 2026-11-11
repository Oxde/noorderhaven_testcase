"""CLI: python3 -m agent.run [--reset] [--only C-001,C-005] [--no-llm]"""
from __future__ import annotations

import argparse
import json
import sys

from agent import config, pipeline


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the Noorderhaven claims agent over claims/*.")
    ap.add_argument("--reset", action="store_true", help="POST /admin/reset before the run (final run only)")
    ap.add_argument("--only", default="", help="comma-separated claim ids, e.g. C-001,C-005")
    ap.add_argument("--no-llm", action="store_true", help="use the deterministic fallback extractor")
    args = ap.parse_args(argv)
    only = [c.strip().upper() for c in args.only.split(",") if c.strip()] or None

    summary = pipeline.run(only=only, use_llm=not args.no_llm, reset=args.reset)

    decisions = json.loads((config.OUTPUT_DIR / "decisions.json").read_text(encoding="utf-8"))
    print(f"{'claim':<7} {'action':<17} {'payout':>9}  flags")
    for d in decisions:
        payout = "" if d["net_payout"] is None else str(d["net_payout"])
        print(f"{d['claim_id']:<7} {d['action']:<17} {payout:>9}  {', '.join(d['flags'])}")
    print()
    print(f"mode: {summary['mode']}   claims: {summary['total_claims']}   duration: {summary['duration_s']}s")
    print("actions: " + ", ".join(f"{k}={v}" for k, v in summary["actions"].items() if v))
    print("letters: " + ", ".join(f"{k}={v}" for k, v in summary["letters"].items() if v)
          + f"   review pending: {summary['review_pending']}")
    a = summary["api"]
    print(f"api: calls={a['calls']} retries={a['retries']} failures={a['failures']} by_status={a['by_status']}")
    if summary.get("llm"):
        print(f"llm: {summary['llm']}")
    if summary["errors"]:
        print(f"ERRORS ({len(summary['errors'])}), see output/audit.jsonl:", file=sys.stderr)
        for e in summary["errors"]:
            print(f"  {e['claim_id']} {e['step']}: {e['error'][:200]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
