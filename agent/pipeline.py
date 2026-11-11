"""Orchestration: load emails, extract facts, merge threads, resolve policy, decide, write to the API,
and write output/decisions.json, review_queue.json, audit.jsonl, run_summary.json.

Extraction runs in parallel; decisions run sequentially in received_at order so duplicate and
frequency checks see claims registered earlier in the same run.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from agent import config
from agent.api_client import ApiClient, ApiError
from agent.models import (ACTION_TO_STATUS, ACTIONS, PRICED_DOC_KINDS, Decision, Email, Facts, HistoryClaim,
                          Letter, Policy, PolicyResolution, RuleStep)

PENDING_ROUTE_FLAGS = ("complaint", "third_party_claim")
API_CONCURRENCY = 8          # parallel reads / non-history writes against the mock API


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Audit:
    """Thread-safe append-only JSONL audit log."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    def log(self, claim_id: Optional[str], step: str, ok: bool, detail: object,
            api_attempts: Optional[int] = None) -> None:
        rec: Dict[str, object] = {"ts": _now(), "claim_id": claim_id, "step": step, "ok": ok, "detail": detail}
        if api_attempts is not None:
            rec["api_attempts"] = api_attempts
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")


def _write_json(path: Path, data: object) -> None:
    """Atomic write (temp file + rename) so the review UI never reads a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


# ---- policy resolution --------------------------------------------------------------------------

def resolve_policy(api: ApiClient, facts: Facts, sender_email: str) -> PolicyResolution:
    number = (facts.policy_number or "").strip().upper() or None
    if number:
        d = api.get_policy(number)
        if d is not None:
            p = Policy.from_api(d)
            return PolicyResolution(policy=p, how="number", number_mismatch=False,
                                    sender_verified=sender_email.strip().lower() == p.holder_email.strip().lower())
    hits = api.find_policies_by_email(sender_email)
    if len(hits) == 1:
        p = Policy.from_api(hits[0])
        return PolicyResolution(policy=p, how="email",
                                number_mismatch=bool(number) and number != p.policy_number.upper(),
                                sender_verified=sender_email.strip().lower() == p.holder_email.strip().lower())
    return PolicyResolution(policy=None, how="none", number_mismatch=False, sender_verified=False)


def _history(api: ApiClient, policy_number: str, exclude_ids: List[str]) -> List[HistoryClaim]:
    out: List[HistoryClaim] = []
    for c in api.get_claims(policy_number):
        if c.get("source_claim_id") and c["source_claim_id"] in exclude_ids:
            continue
        amt = c.get("amount_claimed")
        out.append(HistoryClaim(
            claim_id=str(c.get("claim_id")), policy_number=str(c.get("policy_number")),
            loss_date=c.get("loss_date"), loss_type=c.get("loss_type"),
            amount_claimed=None if amt is None else float(amt), status=c.get("status"),
            invoice_number=c.get("invoice_number"), source_claim_id=c.get("source_claim_id")))
    return out


def _invoice_number(facts: Facts) -> Optional[str]:
    for d in facts.documents:
        if d.kind in PRICED_DOC_KINDS and d.number:
            return d.number
    return None


def _review_status(decision: Decision, facts: Facts) -> str:
    if any(f in decision.flags for f in ("api_write_failed", "api_read_failed", "processing_error")):
        return "pending"
    if decision.action in ("auto_approve", "request_info"):
        return "auto"
    if decision.action == "route_other":
        if facts.intent == "spam" and not any(f in decision.flags for f in PENDING_ROUTE_FLAGS):
            return "auto"
        return "pending"
    return "pending"


def _fail_write(decision: Decision, audit: Audit, cid: str, step: str, err: ApiError, errors: List[dict]) -> None:
    audit.log(cid, step, False, {"error": str(err), "method": err.method, "path": err.path,
                                 "status": err.status, "body": err.body[:500]}, err.attempts)
    errors.append({"claim_id": cid, "step": step, "error": str(err)})
    if "api_write_failed" not in decision.flags:
        decision.flags.append("api_write_failed")
    if decision.action == "auto_approve":
        decision.action = "escalate_handler"
        decision.letter_kind = "acknowledgement"
        decision.reason += " The API write failed after retries, so a handler must complete this claim."
        decision.trace.append(RuleStep("api.write", False, f"{step} failed: {err.status}; escalated"))


def _error_decision(cid: str, err: BaseException) -> Decision:
    return Decision(claim_id=cid, action="escalate_handler", net_payout=None,
                    reason=f"Automatic processing failed ({type(err).__name__}); a handler must review this claim.",
                    flags=["api_read_failed" if isinstance(err, ApiError) else "processing_error"], trace=[RuleStep("pipeline", False, str(err)[:300])],
                    letter_kind=None)


# ---- main run -----------------------------------------------------------------------------------

def run(only: Optional[List[str]] = None, use_llm: bool = True, reset: bool = False) -> dict:
    from agent import comms, extract, rules, threads  # lazy: built by other tasks

    t0 = time.time()
    started = _now()
    out_dir = config.OUTPUT_DIR
    audit = Audit(out_dir / "audit.jsonl")
    errors: List[dict] = []
    api = ApiClient()

    health = api.health()
    audit.log(None, "health", True, health, api.last_attempts)
    if reset:
        api.reset()
        audit.log(None, "reset", True, "POST /admin/reset", api.last_attempts)

    # 1-2. load
    all_emails = extract.load_emails(config.CLAIMS_DIR)
    by_id: Dict[str, Email] = {e.claim_id: e for e in all_emails}
    links = threads.link_threads(all_emails)      # reply claim_id -> parent claim_id
    selected = set(only) if only else set(by_id)
    unknown = sorted(selected - set(by_id))
    if unknown:
        raise ValueError(f"--only names unknown claim ids: {unknown}")
    for cid in list(selected):                    # a reply needs its parent processed too
        p = links.get(cid)
        while p and p not in selected:
            selected.add(p)
            p = links.get(p)
    emails = [e for e in all_emails if e.claim_id in selected]
    emails.sort(key=lambda e: e.received_at)
    audit.log(None, "load", True, {"emails": len(emails), "threads": links})

    # 3. extraction (parallel)
    llm = None
    mode = "fallback"
    if use_llm:
        if not config.KIE_API_KEY:
            audit.log(None, "llm", False, "use_llm requested but KIE_API_KEY is empty; using fallback extractor")
            errors.append({"claim_id": None, "step": "llm", "error": "KIE_API_KEY empty, fallback extractor used"})
        else:
            from agent import llm as llm_mod
            llm = llm_mod.LLMClient()
            mode = "llm:" + ",".join(config.LLM_MODELS)

    def _extract(e: Email) -> Facts:
        try:
            f = extract.extract_facts(e, llm)
            audit.log(e.claim_id, "extract", True, {"extractor": f.extractor, "intent": f.intent,
                                                    "policy_number": f.policy_number, "loss_date": f.loss_date,
                                                    "loss_type": f.loss_type,
                                                    "injection": f.injection_detected.value})
            return f
        except Exception as ex:  # one bad email must not stop the run; recorded, not silent
            audit.log(e.claim_id, "extract", False, {"error": repr(ex), "trace": traceback.format_exc()[-800:]})
            errors.append({"claim_id": e.claim_id, "step": "extract", "error": repr(ex)})
            f = extract.fallback_extract(e)
            f.extractor = "fallback(extract_error)"
            return f

    with ThreadPoolExecutor(max_workers=max(1, config.LLM_CONCURRENCY)) as pool:
        facts_list = list(pool.map(_extract, emails))
    own_facts: Dict[str, Facts] = {f.claim_id: f for f in facts_list}

    # 4. thread merge (cheap, sequential)
    merged: Dict[str, Facts] = {}
    for email in emails:
        cid = email.claim_id
        parent_id = links.get(cid)
        if parent_id and parent_id in own_facts:
            merged[cid] = threads.merge_thread(own_facts[parent_id], own_facts[cid])
            audit.log(cid, "thread_merge", True, {"parent": parent_id})
        else:
            merged[cid] = own_facts[cid]

    # 5. policy resolution: read-only and independent per claim, so prefetched in parallel
    def _resolve(email: Email):
        try:
            res = resolve_policy(api, merged[email.claim_id], email.from_email)
            audit.log(email.claim_id, "resolve_policy", True,
                      {"how": res.how, "policy": res.policy.policy_number if res.policy else None,
                       "number_mismatch": res.number_mismatch, "sender_verified": res.sender_verified},
                      api.last_attempts)
            return res
        except Exception as ex:
            return ex

    with ThreadPoolExecutor(max_workers=API_CONCURRENCY) as pool:
        resolutions = dict(zip([e.claim_id for e in emails], pool.map(_resolve, emails)))

    api_ids: Dict[str, str] = {}                  # our claim_id -> API claim id

    def _queue_item(email: Email, facts: Facts, res: Optional[PolicyResolution], decision: Decision,
                    letter: Optional[Letter], letter_status: str) -> dict:
        return {
            "claim_id": email.claim_id, "api_claim_id": api_ids.get(email.claim_id),
            "received_at": email.received_at,
            "from_email": email.from_email, "from_name": email.from_name, "subject": email.subject,
            "body": email.body, "message_id": email.message_id,
            "attachments": [{"filename": a.filename, "content_type": a.content_type, "text": a.text}
                            for a in email.attachments],
            "facts": facts.to_dict(),
            "policy": asdict(res.policy) if res and res.policy else None,
            "resolution": {"how": res.how if res else "none",
                           "number_mismatch": res.number_mismatch if res else False,
                           "sender_verified": res.sender_verified if res else False},
            "decision": {"action": decision.action, "net_payout": decision.net_payout, "reason": decision.reason,
                         "flags": list(decision.flags), "letter_kind": decision.letter_kind,
                         "missing_items": list(decision.missing_items),
                         "trace": [asdict(s) for s in decision.trace]},
            "letter": asdict(letter) if letter else None,
            "letter_status": letter_status,
            "review_status": _review_status(decision, facts),
        }

    def _fail(email: Email, facts: Facts, res: Optional[PolicyResolution], step: str, ex: BaseException):
        cid = email.claim_id
        audit.log(cid, step, False, {"error": repr(ex), "trace": traceback.format_exc()[-1200:]})
        errors.append({"claim_id": cid, "step": step, "error": repr(ex)})
        d = _error_decision(cid, ex)
        return d, _queue_item(email, facts, res, d, None, "none")

    # 8b. decision post + letter: they do not influence later claims, so they run in a writer pool
    def _finish(email: Email, facts: Facts, res: PolicyResolution, decision: Decision):
        cid = email.claim_id
        letter: Optional[Letter] = None
        letter_status = "none"
        try:
            api_id = api_ids.get(cid)
            status = ACTION_TO_STATUS[decision.action]
            if api_id and status:
                payload = {"status": status, "payout": decision.net_payout, "reason": decision.reason,
                           "flags": list(decision.flags), "source_claim_id": cid}
                try:
                    api.post_decision(api_id, payload, f"{cid}:decision")
                    audit.log(cid, "decision_post", True, {"api_claim_id": api_id, "status": status},
                              api.last_attempts)
                except ApiError as err:
                    _fail_write(decision, audit, cid, "decision_post", err, errors)

            letter = comms.build_letter(decision, facts, email, res.policy)
            if letter is not None:
                if letter.to.strip().lower() != email.from_email.strip().lower():
                    raise ValueError(f"letter for {cid} addressed to {letter.to!r}, not the sender {email.from_email!r}")
                if letter.send_now:
                    mail = {"to": letter.to, "subject": letter.subject, "body": letter.body,
                            "in_reply_to": letter.in_reply_to, "kind": letter.api_kind, "claim_id": cid}
                    try:
                        api.queue_email(mail, f"{cid}:mail:{letter.kind}")
                        letter_status = "sent"
                        audit.log(cid, "mail", True, {"kind": letter.kind, "to": letter.to}, api.last_attempts)
                    except ApiError as err:
                        letter_status = "failed"
                        was_approval = letter.kind == "approval"
                        _fail_write(decision, audit, cid, "mail", err, errors)
                        if was_approval:   # action was downgraded; keep a letter that matches it for the handler
                            letter = comms.build_letter(decision, facts, email, res.policy)
                else:
                    letter_status = "held"
                    audit.log(cid, "mail", True, {"kind": letter.kind, "held_for_review": True})
        except Exception as ex:  # never crash the run on one claim; surfaced in audit + summary
            return _fail(email, facts, res, "finish", ex)
        return decision, _queue_item(email, facts, res, decision, letter, letter_status)

    # 6-8a. history, decision, registration: sequential in received_at order so duplicate and
    # frequency checks see claims registered earlier in this run
    results: list = []
    with ThreadPoolExecutor(max_workers=API_CONCURRENCY) as writers:
        for email in emails:
            cid = email.claim_id
            facts = merged[cid]
            res = resolutions[cid]
            if isinstance(res, BaseException):
                results.append(_fail(email, facts, None, "resolve_policy", res))
                continue
            try:
                parent_id = links.get(cid)
                history: List[HistoryClaim] = []
                if res.policy:
                    exclude = [cid] + ([parent_id] if parent_id else [])
                    history = _history(api, res.policy.policy_number, exclude)
                    audit.log(cid, "history", True, {"claims": [h.claim_id for h in history]}, api.last_attempts)

                decision = rules.decide(facts, res, history, config.TODAY, received_at=email.received_at[:10])
                if parent_id and not any(f.startswith("thread_parent") for f in decision.flags):
                    decision.flags.append(f"thread_parent:{parent_id}")
                audit.log(cid, "decide", True, {"action": decision.action, "net_payout": decision.net_payout,
                                                "flags": decision.flags})
                if decision.action not in ACTIONS:
                    raise ValueError(f"rules.decide returned unknown action {decision.action!r} for {cid}")

                if decision.action != "route_other":
                    api_id = api_ids.get(parent_id) if parent_id else None
                    if api_id:      # one loss event, one API claim: the reply reuses the parent's record
                        audit.log(cid, "register", True, {"reused_parent_claim": api_id})
                    elif res.policy and facts.loss_date and facts.loss_type:
                        payload = {"policy_number": res.policy.policy_number, "loss_date": facts.loss_date,
                                   "loss_type": facts.loss_type, "invoice_number": _invoice_number(facts),
                                   "amount_claimed": rules.document_amount(facts)[0] or facts.amount_in_email,
                                   "source_claim_id": cid}
                        try:
                            rec = api.register_claim(payload, f"{cid}:register")
                            api_id = str(rec["claim_id"])
                            audit.log(cid, "register", True, {"api_claim_id": api_id}, api.last_attempts)
                        except ApiError as err:
                            _fail_write(decision, audit, cid, "register", err, errors)
                    if api_id:
                        api_ids[cid] = api_id
            except Exception as ex:  # never crash the run on one claim; surfaced in audit + summary
                results.append(_fail(email, facts, res, "pipeline", ex))
                continue
            results.append(writers.submit(_finish, email, facts, res, decision))
        results = [r.result() if hasattr(r, "result") else r for r in results]
    decisions: List[Decision] = [r[0] for r in results]
    queue: List[dict] = [r[1] for r in results]

    # 9. outputs
    _write_json(out_dir / "decisions.json", [d.hand_in() for d in decisions])
    _write_json(out_dir / "review_queue.json", queue)
    counts = {a: 0 for a in ACTIONS}
    for d in decisions:
        counts[d.action] = counts.get(d.action, 0) + 1
    finished = _now()
    summary = {
        "mode": mode, "total_claims": len(decisions), "actions": counts,
        "letters": {s: sum(1 for q in queue if q["letter_status"] == s) for s in ("sent", "held", "none", "failed")},
        "review_pending": sum(1 for q in queue if q["review_status"] == "pending"),
        "api": json.loads(json.dumps(api.stats)),
        "llm": dict(getattr(llm, "stats", {})) if llm is not None else None,
        "extractors": sorted({f.extractor for f in facts_list}),
        "errors": errors, "today": config.TODAY, "api_base": api.base,
        "started": started, "finished": finished, "duration_s": round(time.time() - t0, 2),
    }
    _write_json(out_dir / "run_summary.json", summary)
    audit.log(None, "run_end", not errors, {"actions": counts, "errors": len(errors)})
    return summary
