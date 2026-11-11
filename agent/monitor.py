"""Run monitoring: one metrics.json with automation, API health, LLM drift, human overrides and alerts.

    python3 -m agent.monitor            # reads config.OUTPUT_DIR (env OUTPUT_DIR), writes metrics.json

Stdlib only. Reads what the pipeline, the review UI and the eval already write:
  review_queue.json, run_summary.json, eval_summary.json, reviews.jsonl, open_info_requests.json.

The drift signal is extraction agreement: for every claim the LLM extracted, the deterministic regex reader
(extract.fallback_extract) reads the same email and the two are compared on a few decision-relevant fields.
A falling agreement rate means the model, the prompt or the incoming mail changed and someone should look.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

from agent import config

# Alert thresholds.
ALERT_RETRY_RATE = 0.30          # API retries / calls
ALERT_OVERRIDE_RATE = 0.20       # (edits + rejects) / reviews
ALERT_OVERRIDE_MIN_REVIEWS = 5   # do not alert on a handful of reviews
ALERT_AGREEMENT_RATE = 0.80      # LLM vs fallback extraction agreement

AGREEMENT_FIELDS = ["policy_number", "loss_date", "loss_type", "priced_amount", "intent", "injection"]
PRICED_DOC_KINDS = {"invoice", "quote", "receipt", "valuation"}


def _read_json(path: Path) -> Optional[object]:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _items(data: object) -> List[dict]:
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    raise ValueError("expected a list or {'items': [...]}, got %s" % type(data).__name__)


def _rate(num: int, den: int) -> Optional[float]:
    return round(num / den, 4) if den else None


def _priced_amount(documents: List[object]) -> Optional[float]:
    """Largest invoice/quote/receipt/valuation amount; accepts dicts (queue JSON) or Document objects."""
    best: Optional[float] = None
    for d in documents or []:
        kind = d.get("kind") if isinstance(d, dict) else getattr(d, "kind", None)
        amount = d.get("amount") if isinstance(d, dict) else getattr(d, "amount", None)
        if kind in PRICED_DOC_KINDS and isinstance(amount, (int, float)):
            best = float(amount) if best is None else max(best, float(amount))
    return round(best, 2) if best is not None else None


def _llm_view(facts: dict) -> Dict[str, object]:
    return {
        "policy_number": facts.get("policy_number"),
        "loss_date": facts.get("loss_date"),
        "loss_type": facts.get("loss_type"),
        "priced_amount": _priced_amount(facts.get("documents") or []),
        "intent": facts.get("intent"),
        "injection": bool((facts.get("injection_detected") or {}).get("value")),
    }


def _fallback_view(facts) -> Dict[str, object]:  # facts: agent.models.Facts
    return {
        "policy_number": facts.policy_number,
        "loss_date": facts.loss_date,
        "loss_type": facts.loss_type,
        "priced_amount": _priced_amount(facts.documents),
        "intent": facts.intent,
        "injection": bool(facts.injection_detected.value),
    }


def extraction_agreement(queue: List[dict], claims_dir: Path) -> dict:
    """Compare LLM-extracted facts in the queue with a fallback_extract re-read of the same email(s)."""
    llm_items = [it for it in queue if str((it.get("facts") or {}).get("extractor") or "").startswith("llm")]
    out: dict = {"claims_compared": 0, "fields": list(AGREEMENT_FIELDS), "agreement_rate": None,
                 "per_field": {f: None for f in AGREEMENT_FIELDS}, "disagreements": []}
    if not llm_items:
        return out
    from agent.extract import fallback_extract, load_email   # lazy: keeps the module light for the UI
    from agent.threads import merge_thread

    cache: Dict[str, object] = {}

    def fb(cid: str):
        if cid not in cache:
            claim_dir = Path(claims_dir) / cid
            if not (claim_dir / "email.json").exists():
                raise FileNotFoundError("no email.json for %s under %s" % (cid, claims_dir))
            cache[cid] = fallback_extract(load_email(claim_dir))
        return cache[cid]

    agree = {f: 0 for f in AGREEMENT_FIELDS}
    for it in llm_items:
        facts = it["facts"]
        cid = it.get("claim_id") or facts.get("claim_id")
        ref = fb(cid)
        parent = facts.get("thread_parent")
        if parent:   # the queue holds merged thread facts: merge the fallback reads the same way
            ref = merge_thread(fb(parent), ref)
        mine, theirs = _llm_view(facts), _fallback_view(ref)
        for f in AGREEMENT_FIELDS:
            if mine[f] == theirs[f]:
                agree[f] += 1
            else:
                out["disagreements"].append({"claim_id": cid, "field": f, "llm": mine[f], "fallback": theirs[f]})
    n = len(llm_items)
    out["claims_compared"] = n
    out["per_field"] = {f: round(agree[f] / n, 4) for f in AGREEMENT_FIELDS}
    out["agreement_rate"] = round(sum(agree.values()) / (n * len(AGREEMENT_FIELDS)), 4)
    return out


def review_metrics(reviews_path: Path) -> dict:
    """Latest review per claim from reviews.jsonl; by_action keys on the agent's original action."""
    latest: Dict[str, dict] = {}
    original: Dict[str, str] = {}
    records = 0
    if reviews_path.exists():
        with open(reviews_path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    raise ValueError("bad JSON in %s line %d: %s" % (reviews_path, lineno, e)) from e
                cid = rec.get("claim_id")
                if not cid:
                    continue
                records += 1
                latest[cid] = rec
                if cid not in original:
                    original[cid] = str((((rec.get("before") or {}).get("decision")) or {}).get("action"))
    tally = {"approve": 0, "edit": 0, "reject": 0}
    by_action: Dict[str, Dict[str, int]] = {}
    for cid, rec in latest.items():
        act = rec.get("action")
        if act not in tally:
            continue
        tally[act] += 1
        row = by_action.setdefault(original[cid], {"approve": 0, "edit": 0, "reject": 0})
        row[act] += 1
    total = sum(tally.values())
    return dict(total=total, records=records, **tally,
                override_rate=_rate(tally["edit"] + tally["reject"], total), by_action=by_action)


def open_requests_metrics(output_dir: Path) -> Optional[dict]:
    data = _read_json(output_dir / "open_info_requests.json")
    if data is None:
        return None
    items = _items(data)
    answered = sum(1 for r in items if str(r.get("status") or "").lower() == "answered")
    due = sum(1 for r in items if r.get("reminder_due") and str(r.get("status") or "").lower() != "answered")
    return {"open": len(items) - answered, "answered": answered, "reminder_due": due}


def compute_metrics(output_dir: Path, claims_dir: Path = config.CLAIMS_DIR) -> dict:
    output_dir = Path(output_dir)
    queue = _items(_read_json(output_dir / "review_queue.json"))
    if not queue and not (output_dir / "review_queue.json").exists():
        raise FileNotFoundError("review_queue.json not found in %s (run `python3 -m agent.run` first)" % output_dir)
    run = _read_json(output_dir / "run_summary.json") or {}
    ev = _read_json(output_dir / "eval_summary.json")

    actions: Dict[str, int] = {}
    letters = {"sent": 0, "held": 0, "none": 0, "failed": 0}
    auto = 0
    fallback_used = 0
    for it in queue:
        a = str((it.get("decision") or {}).get("action"))
        actions[a] = actions.get(a, 0) + 1
        ls = str(it.get("letter_status") or "none")
        letters[ls] = letters.get(ls, 0) + 1
        if it.get("review_status") == "auto":
            auto += 1
        if str((it.get("facts") or {}).get("extractor") or "").startswith("fallback"):
            fallback_used += 1
    n = len(queue)

    api_run = run.get("api") or {}
    calls, retries, failures = int(api_run.get("calls") or 0), int(api_run.get("retries") or 0), int(api_run.get("failures") or 0)
    llm_run = run.get("llm") or {}
    mode = str(run.get("mode") or "unknown")

    eval_block = None
    if isinstance(ev, dict):
        eval_block = {"strict_accuracy": ev.get("strict_accuracy"), "payout_match": ev.get("payout_match"),
                      "safety_total": sum(int(v or 0) for v in (ev.get("safety") or {}).values())}

    m = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "claims": n,
        "actions": actions,
        "automation": {"auto_handled": auto, "needs_human": n - auto, "auto_rate": _rate(auto, n)},
        "letters": letters,
        "api": {"calls": calls, "retries": retries, "failures": failures, "retry_rate": _rate(retries, calls)},
        "llm": {"mode": mode, "calls": int(llm_run.get("calls") or 0), "cache_hits": int(llm_run.get("cache_hits") or 0),
                "credits_consumed": float(llm_run.get("credits_consumed") or 0.0), "fallback_used": fallback_used},
        "extraction_agreement": extraction_agreement(queue, claims_dir),
        "reviews": review_metrics(output_dir / "reviews.jsonl"),
        "open_info_requests": open_requests_metrics(output_dir),
        "eval": eval_block,
    }
    m["alerts"] = alerts(m)
    return m


def alerts(m: dict) -> List[str]:
    out: List[str] = []
    api = m["api"]
    if api["retry_rate"] is not None and api["retry_rate"] > ALERT_RETRY_RATE:
        out.append("API retry rate %.0f%% (%d of %d calls) is above %.0f%%"
                   % (api["retry_rate"] * 100, api["retries"], api["calls"], ALERT_RETRY_RATE * 100))
    if api["failures"]:
        out.append("%d API call(s) failed after retries" % api["failures"])
    rv = m["reviews"]
    if rv["override_rate"] is not None and rv["total"] >= ALERT_OVERRIDE_MIN_REVIEWS and rv["override_rate"] > ALERT_OVERRIDE_RATE:
        out.append("Handler override rate %.0f%% (%d edits + %d rejects of %d reviews) is above %.0f%%"
                   % (rv["override_rate"] * 100, rv["edit"], rv["reject"], rv["total"], ALERT_OVERRIDE_RATE * 100))
    ag = m["extraction_agreement"]
    if ag["agreement_rate"] is not None and ag["agreement_rate"] < ALERT_AGREEMENT_RATE:
        out.append("LLM vs fallback extraction agreement %.0f%% is below %.0f%% (%d disagreements)"
                   % (ag["agreement_rate"] * 100, ALERT_AGREEMENT_RATE * 100, len(ag["disagreements"])))
    ev = m["eval"]
    if ev and ev["safety_total"] > 0:
        out.append("Eval safety checks failed: %d (must be 0)" % ev["safety_total"])
    oir = m["open_info_requests"]
    if oir and oir["reminder_due"] > 0:
        out.append("%d open info request(s) are due a reminder" % oir["reminder_due"])
    llm = m["llm"]
    if llm["fallback_used"] > 0 and llm["mode"].startswith("llm"):
        out.append("%d claim(s) fell back to the regex extractor in LLM mode" % llm["fallback_used"])
    return out


def write_metrics(output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    m = compute_metrics(output_dir)
    path = output_dir / "metrics.json"
    fd, tmp = tempfile.mkstemp(prefix=".metrics.", suffix=".tmp", dir=str(output_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else "%.0f%%" % (v * 100)


def main() -> int:
    out_dir = config.OUTPUT_DIR
    path = write_metrics(out_dir)
    m = json.loads(path.read_text(encoding="utf-8"))
    ag, rv, api = m["extraction_agreement"], m["reviews"], m["api"]
    print("claims %d | auto %s (%d) | needs human %d" % (m["claims"], _pct(m["automation"]["auto_rate"]),
                                                       m["automation"]["auto_handled"], m["automation"]["needs_human"]))
    print("api %d calls, retry rate %s, %d failures | llm %s, fallback used %d"
          % (api["calls"], _pct(api["retry_rate"]), api["failures"], m["llm"]["mode"], m["llm"]["fallback_used"]))
    print("extraction agreement %s over %d claims, %d disagreements"
          % (_pct(ag["agreement_rate"]), ag["claims_compared"], len(ag["disagreements"])))
    print("reviews %d, override rate %s" % (rv["total"], _pct(rv["override_rate"])))
    if m["eval"]:
        print("eval strict %s, payout %s, safety %d" % (_pct(m["eval"]["strict_accuracy"]),
                                                       _pct(m["eval"]["payout_match"]), m["eval"]["safety_total"]))
    for a in m["alerts"]:
        print("ALERT: " + a)
    print("wrote " + str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
