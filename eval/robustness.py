"""Robustness checks for the claims agent (standard library only).

Runs three scenarios against an isolated copy of the repo (never the repo's own
mock_api/runtime, never port 8080):
  heavy_chaos   FAIL_RATE=0.5 on the mock API, LLM (cached answers if present), --reset
  rerun         the same server again without reset: idempotency replay, expect 0 new writes
  no_llm        --no-llm, default chaos, --reset

Usage: python3 eval/robustness.py [--keep]   (--keep leaves the temp dir for inspection)
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
COPY = ("agent", "claims", "eval", "mock_api", ".env", ".cache")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    if port == 8080:
        raise RuntimeError("refusing to use port 8080")
    return port


def make_copy(dst: Path) -> None:
    for name in COPY:
        src = ROOT / name
        if not src.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, dst / name, ignore=shutil.ignore_patterns("__pycache__", "runtime"))
        else:
            shutil.copy2(src, dst / name)
    (dst / "mock_api" / "runtime").mkdir()


def start_server(work: Path, port: int, fail_rate: str) -> subprocess.Popen:
    env = dict(os.environ, PORT=str(port), FAIL_RATE=fail_rate)
    proc = subprocess.Popen([sys.executable, str(work / "mock_api" / "server.py")], cwd=work / "mock_api",
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                if r.status == 200:
                    return proc
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError(f"mock API did not start on port {port}")


def write_log(work: Path) -> List[dict]:
    p = work / "mock_api" / "runtime" / "write_log.jsonl"
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def run_agent(work: Path, port: int, out: Path, args: List[str]) -> Tuple[int, float]:
    env = dict(os.environ, CLAIMS_API_BASE=f"http://127.0.0.1:{port}", OUTPUT_DIR=str(out))
    t0 = time.time()
    rc = subprocess.run([sys.executable, "-m", "agent.run", *args], cwd=work, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    return rc, round(time.time() - t0, 1)


def evaluate(work: Path, out: Path) -> Dict[str, object]:
    subprocess.run([sys.executable, str(work / "eval" / "run_eval.py"), "--decisions", str(out / "decisions.json"),
                    "--outbox", str(work / "mock_api" / "runtime" / "outbox.json"),
                    "--queue", str(out / "review_queue.json"), "--out-dir", str(out / "eval")],
                   cwd=work, check=True, stdout=subprocess.DEVNULL)
    return json.loads((out / "eval" / "eval_summary.json").read_text(encoding="utf-8"))


def summarize(name: str, work: Path, out: Path, rc: int, secs: float, log_before: int) -> Dict[str, object]:
    log = write_log(work)
    new = log[log_before:]
    kinds = collections.Counter(r["kind"] for r in new)
    src = collections.Counter(r["record"].get("source_claim_id") for r in new if r["kind"] == "claim")
    dec = collections.Counter(r["record"].get("claim_id") for r in new if r["kind"] == "decision")
    rs = json.loads((out / "run_summary.json").read_text(encoding="utf-8"))
    ds = json.loads((out / "decisions.json").read_text(encoding="utf-8"))
    bad = ("api_write_failed", "api_read_failed", "processing_error")
    failed = [d for d in ds if any(f in d["flags"] for f in bad)]
    ev = evaluate(work, out)
    return {
        "scenario": name, "rc": rc, "secs": secs,
        "strict": ev.get("strict_accuracy"), "lenient": ev.get("lenient_accuracy"),
        "payout": ev.get("payout_match"), "safety": ev.get("safety"),
        "calls": rs["api"]["calls"], "retries": rs["api"]["retries"], "failures": rs["api"]["failures"],
        "errors": len(rs["errors"]),
        "new_claims": kinds.get("claim", 0), "new_decisions": kinds.get("decision", 0), "new_outbox": kinds.get("outbox", 0),
        "dup_claims": sum(v - 1 for v in src.values() if v > 1), "dup_decisions": sum(v - 1 for v in dec.values() if v > 1),
        "failed_claims": len(failed), "failed_auto_approved": sum(1 for d in failed if d["action"] == "auto_approve"),
    }


def fmt(v: object) -> str:
    if isinstance(v, dict):
        return "PASS" if all(x == 0 for x in v.values()) else "FAIL " + json.dumps(v)
    if isinstance(v, float) and v <= 1:
        return f"{v:.0%}"
    return str(v)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="claims-robust-"))
    rows: List[Dict[str, object]] = []
    procs: List[subprocess.Popen] = []
    try:
        chaos = tmp / "chaos"; chaos.mkdir(); make_copy(chaos)
        port = free_port()
        procs.append(start_server(chaos, port, "0.5"))
        rc, secs = run_agent(chaos, port, chaos / "out_heavy", ["--reset"])
        rows.append(summarize("heavy_chaos (FAIL_RATE=0.5)", chaos, chaos / "out_heavy", rc, secs, 0))
        before = len(write_log(chaos))
        rc, secs = run_agent(chaos, port, chaos / "out_rerun", [])
        rows.append(summarize("rerun, no reset (idempotency)", chaos, chaos / "out_rerun", rc, secs, before))

        nollm = tmp / "nollm"; nollm.mkdir(); make_copy(nollm)
        port = free_port()
        procs.append(start_server(nollm, port, "0.15"))
        rc, secs = run_agent(nollm, port, nollm / "out_nollm", ["--reset", "--no-llm"])
        rows.append(summarize("no-llm (FAIL_RATE=0.15)", nollm, nollm / "out_nollm", rc, secs, 0))
    finally:
        for p in procs:
            p.terminate()
            p.wait(timeout=10)
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"kept: {tmp}")

    cols = list(rows[0].keys()) if rows else []
    print(" | ".join(cols))
    for r in rows:
        print(" | ".join(fmt(r[c]) for c in cols))
    ok = all(r["safety"] and all(v == 0 for v in r["safety"].values()) and r["failed_auto_approved"] == 0 for r in rows)
    ok = ok and len(rows) == 3 and rows[1]["new_claims"] == 0 and rows[1]["new_decisions"] == 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
