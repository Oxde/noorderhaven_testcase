"""Open information requests: which request_info claims are still waiting on the customer, and drafted reminders.

`build_open_requests` runs at the end of every pipeline run (output/open_info_requests.json).
`build_reminder_letter` is for a separate reminder job: reminders are drafted and held (send_now False) because
the reminder cadence is a business decision, not something the agent sends on its own.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional

from agent.comms import _MISSING_LABELS, _SIGN, _bullets
from agent.models import Letter

REMINDER_AFTER_DAYS = 7


def _parse_day(value: str, what: str, claim_id: Optional[str]) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as ex:
        raise ValueError(f"cannot parse {what} {value!r} for claim {claim_id}: {ex}") from ex


def _answered_by(item: dict, children: Dict[str, str]) -> Optional[str]:
    thread = item.get("thread") or {}
    return thread.get("answered_by") or children.get(item["claim_id"])


def build_open_requests(queue_items: List[dict], today: str) -> dict:
    """Summarise every request_info item in the review queue as open or answered.

    A request counts as answered when a reply in the same thread was decided: the item carries
    thread.answered_by, or another queue item has facts.thread_parent pointing at it.
    """
    today_d = _parse_day(today, "today", None)
    children: Dict[str, str] = {}
    for q in queue_items:
        parent = (q.get("facts") or {}).get("thread_parent")
        if parent and parent not in children:
            children[parent] = q["claim_id"]

    items: List[dict] = []
    for q in queue_items:
        decision = q.get("decision") or {}
        if decision.get("action") != "request_info":
            continue
        cid = q["claim_id"]
        if not q.get("received_at"):
            raise ValueError(f"request_info item {cid} has no received_at; cannot compute days open")
        requested = _parse_day(q["received_at"], "received_at", cid)
        days_open = (today_d - requested).days
        answered_by = _answered_by(q, children)
        status = "answered" if answered_by else "open"
        items.append({
            "claim_id": cid, "from_email": q.get("from_email"), "subject": q.get("subject"),
            "requested_on": requested.isoformat(), "missing_items": list(decision.get("missing_items") or []),
            "days_open": days_open,
            "reminder_due": status == "open" and days_open >= REMINDER_AFTER_DAYS,
            "status": status, "answered_by": answered_by,
            "from_name": q.get("from_name"), "message_id": q.get("message_id"),
            "language": (q.get("facts") or {}).get("language"),
        })
    items.sort(key=lambda i: (i["status"] != "open", i["requested_on"], i["claim_id"]))
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"), "today": today_d.isoformat(),
        "items": items,
        "open": sum(1 for i in items if i["status"] == "open"),
        "answered": sum(1 for i in items if i["status"] == "answered"),
        "reminder_due": sum(1 for i in items if i["reminder_due"]),
    }


def _reminder_body(lang: str, items: List[str], requested_on: str) -> str:
    if lang == "nl":
        return (f"Op {requested_on} hebben wij u gevraagd om aanvullende informatie over uw schademelding. "
                f"Wij hebben die nog niet van u ontvangen. Om uw schade te kunnen beoordelen hebben wij nog het "
                f"volgende nodig:\n\n{_bullets(items)}\n\nU kunt dit sturen door op deze e-mail te antwoorden. "
                "Heeft u het al gestuurd? Dan kunt u deze herinnering als niet verzonden beschouwen.")
    return (f"On {requested_on} we asked you for more information about your claim. We have not received it yet. "
            f"To assess your claim we still need the following:\n\n{_bullets(items)}\n\nYou can simply reply to "
            "this email. If you have already sent it, please disregard this reminder.")


def build_reminder_letter(item: dict, language: str) -> Letter:
    """Polite reminder for one open request (an entry of build_open_requests()["items"]).

    The recipient is always the original sender of the claim email. Held for a handler (send_now False).
    """
    cid = item.get("claim_id")
    if language not in ("nl", "en"):
        raise ValueError(f"unsupported reminder language {language!r} for claim {cid}; expected 'nl' or 'en'")
    if item.get("status") != "open":
        raise ValueError(f"claim {cid} has status {item.get('status')!r}; reminders are only for open requests")
    to = (item.get("from_email") or "").strip()
    if not to:
        raise ValueError(f"claim {cid} has no from_email; a reminder can only go to the original sender")
    labels = _MISSING_LABELS[language]
    missing = [labels.get(m, m) for m in item.get("missing_items") or []] or (
        ["aanvullende informatie over uw schade"] if language == "nl" else ["more information about your claim"])
    name = (item.get("from_name") or "").strip()
    if language == "nl":
        greeting = f"Beste {name}," if name else "Beste heer/mevrouw,"
    else:
        greeting = f"Dear {name}," if name else "Dear Sir or Madam,"
    body = f"{greeting}\n\n{_reminder_body(language, missing, item.get('requested_on') or '')}\n\n{_SIGN[language]}"
    subject = item.get("subject") or ""
    prefix = "Herinnering" if language == "nl" else "Reminder"
    subject = f"{prefix}: {subject}" if subject else prefix
    return Letter(kind="info_reminder", to=to, subject=subject, body=body, language=language,
                  in_reply_to=item.get("message_id"), send_now=False, api_kind="info_request")
