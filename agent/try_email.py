"""Dry run: push one email through extraction, policy lookup, rules and letter drafting without writing anything.

Writes are blocked in code: `ReadOnlyApiClient` raises on every write method, and nothing here touches
output/. Only GETs reach the mock API (policy lookup and claim history).
"""
from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import asdict
from typing import List, Optional

from agent import config, comms, extract, rules
from agent.api_client import ApiClient
from agent.models import Attachment, Email, HistoryClaim
from agent.pipeline import resolve_policy


class ReadOnlyApiClient(ApiClient):
    """ApiClient whose write methods raise before any request is made."""

    def _blocked(self, method: str):
        raise RuntimeError("dry run: write blocked: %s" % method)

    def register_claim(self, payload: dict, idempotency_key: str) -> dict:
        self._blocked("register_claim")

    def post_decision(self, api_claim_id: str, payload: dict, idempotency_key: str) -> dict:
        self._blocked("post_decision")

    def queue_email(self, payload: dict, idempotency_key: str) -> dict:
        self._blocked("queue_email")

    def reset(self) -> None:
        self._blocked("reset")


def email_from_dict(d: dict) -> Email:
    """Accept the claims/*/email.json schema or a flat form dict (from_name, from_email, ..., attachment_text)."""
    if not isinstance(d, dict):
        raise ValueError("email must be a JSON object, got %s" % type(d).__name__)
    sender = d.get("from") if isinstance(d.get("from"), dict) else {}
    from_name = str(d.get("from_name") or sender.get("name") or "")
    from_email = str(d.get("from_email") or sender.get("email") or "").strip()
    received_at = str(d.get("received_at") or "").strip() or _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    atts: List[Attachment] = []
    for a in d.get("attachments") or []:
        if isinstance(a, dict) and a.get("filename"):
            atts.append(Attachment(filename=str(a["filename"]), content_type=str(a.get("content_type") or ""),
                                   text=str(a.get("text") or "")))
    pasted = str(d.get("attachment_text") or "").strip()
    if pasted:
        atts.append(Attachment("pasted.pdf", "application/pdf", pasted))
    try:
        photos = max(0, min(10, int(d.get("photo_count") or 0)))
    except (TypeError, ValueError) as e:
        raise ValueError("photo_count must be a whole number, got %r" % d.get("photo_count")) from e
    atts.extend(Attachment("photo_%d.jpg" % (i + 1), "image/jpeg", "") for i in range(photos))
    return Email(claim_id=str(d.get("claim_id") or "TRY-1"),
                 message_id=str(d.get("message_id") or "<try-%s@dry-run.local>" % uuid.uuid4().hex[:12]),
                 in_reply_to=d.get("in_reply_to") or None, from_name=from_name, from_email=from_email,
                 subject=str(d.get("subject") or ""), received_at=received_at, body=str(d.get("body") or ""),
                 attachments=atts)


def _history(api: ApiClient, policy_number: str) -> List[HistoryClaim]:
    out: List[HistoryClaim] = []
    for c in api.get_claims(policy_number):
        amt = c.get("amount_claimed")
        out.append(HistoryClaim(
            claim_id=str(c.get("claim_id")), policy_number=str(c.get("policy_number")),
            loss_date=c.get("loss_date"), loss_type=c.get("loss_type"),
            amount_claimed=None if amt is None else float(amt), status=c.get("status"),
            invoice_number=c.get("invoice_number"), source_claim_id=c.get("source_claim_id")))
    return out


def dry_run(email: Email, api: Optional[ApiClient] = None, use_llm: bool = True) -> dict:
    api = api if api is not None else ReadOnlyApiClient(max_attempts=3)
    llm = None
    if use_llm and config.KIE_API_KEY:
        from agent.llm import LLMClient
        llm = LLMClient()
    facts = extract.extract_facts(email, llm)
    if use_llm and llm is None:          # same as the pipeline: no key -> never decide on keyword facts
        facts.extractor = "fallback(no_key)"

    reads = 0
    api_error: Optional[str] = None
    history: List[HistoryClaim] = []
    try:
        res = resolve_policy(api, facts, email.from_email)
        reads += 1 + (0 if res.how == "number" else 1)
        if res.policy:
            history = _history(api, res.policy.policy_number)
            reads += 1
    except RuntimeError:
        raise                                # a blocked write is a bug, never swallowed
    except Exception as e:
        from agent.models import PolicyResolution
        api_error = "%s: %s" % (type(e).__name__, e)
        res = PolicyResolution(policy=None, how="none")

    decision = rules.decide(facts, res, history, config.TODAY, received_at=email.received_at[:10])
    letter = comms.build_letter(decision, facts, email, res.policy)
    llm_stats = {"model": None, "credits": 0.0, "calls": 0}
    if llm is not None:
        llm_stats = {"model": facts.extractor[4:] if facts.extractor.startswith("llm:") else None,
                     "credits": round(float(llm.stats.get("credits_consumed") or 0.0), 4),
                     "calls": int(llm.stats.get("calls") or 0),
                     "cache_hits": int(llm.stats.get("cache_hits") or 0)}
    out = {
        "facts": facts.to_dict(),
        "resolution": res.how,
        "sender_verified": res.sender_verified,
        "number_mismatch": res.number_mismatch,
        "policy": asdict(res.policy) if res.policy else None,
        "history": [asdict(h) for h in history],
        "decision": {"action": decision.action, "net_payout": decision.net_payout, "reason": decision.reason,
                     "flags": list(decision.flags), "letter_kind": decision.letter_kind,
                     "missing_items": list(decision.missing_items),
                     "trace": [asdict(s) for s in decision.trace]},
        "letter": asdict(letter) if letter else None,
        "extractor": facts.extractor,
        "llm": llm_stats,
        "api": {"reads": reads, "writes_blocked": 0},
        "dry_run": True,
    }
    if api_error:
        out["api_error"] = api_error
    return out
