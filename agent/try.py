"""Dry-run one email: python3 -m agent.try path/to/email.json [--json] [--no-llm]

Reads claims/*/email.json (PDF attachments next to it are text-extracted) or a flat form JSON. Never writes.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent.extract import load_email
from agent.try_email import dry_run, email_from_dict


def main() -> int:
    ap = argparse.ArgumentParser(description="Dry-run one claim email (nothing is written)")
    ap.add_argument("path", help="email.json (claims schema or flat form dict)")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--no-llm", action="store_true", help="keyword fallback extractor only")
    args = ap.parse_args()
    path = Path(args.path)
    d = json.loads(path.read_text(encoding="utf-8"))
    email = load_email(path.parent) if isinstance(d.get("from"), dict) and path.name == "email.json" else email_from_dict(d)
    r = dry_run(email, use_llm=not args.no_llm)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    dec = r["decision"]
    print("DRY RUN - nothing was written")
    print("action:    %s" % dec["action"])
    print("payout:    %s" % ("-" if dec["net_payout"] is None else "EUR %.2f" % dec["net_payout"]))
    print("reason:    %s" % dec["reason"])
    print("flags:     %s" % (", ".join(dec["flags"]) or "-"))
    print("policy:    %s (resolved by %s)" % ((r["policy"] or {}).get("policy_number", "-"), r["resolution"]))
    print("extractor: %s   llm credits: %s" % (r["extractor"], r["llm"].get("credits")))
    if r.get("api_error"):
        print("api_error: %s" % r["api_error"])
    failed = [s for s in dec["trace"] if not s["passed"]]
    print("failed checks:%s" % ("" if failed else " none"))
    for s in failed:
        print("  x %s: %s" % (s["rule"], s["detail"]))
    let = r["letter"]
    if let:
        print("\nletter (%s): to %s\nsubject: %s\n\n%s" % ("would be sent" if let["send_now"] else "would be held",
                                                       let["to"], let["subject"], let["body"]))
    else:
        print("\nletter: none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
