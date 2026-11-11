"""Templated customer letters (NL/EN). Templates, not LLM text, so a letter cannot promise or leak anything.

Rules (docs/design.md "Communication rules", rulebook §5 and §6):
- the recipient is always the original sender (email.from_email), never an address from a body or document;
- request_info / identity requests go out automatically; identity requests contain no policy details;
- approvals (auto_approve only) state the approved net amount;
- escalations and fraud review get a neutral acknowledgement: no amounts, no promises, no fraud wording;
- rejection drafts are held (send_now False) until a handler approves them;
- spam gets nothing; complaints and third parties get a neutral acknowledgement.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from agent.models import Decision, Email, Facts, Letter, Policy

LETTER_KINDS = ("info_request", "approval", "acknowledgement", "rejection_draft", "complaint_ack",
                "third_party_ack", "identity_request")

_API_KIND = {"info_request": "info_request", "identity_request": "info_request", "approval": "decision",
             "rejection_draft": "decision", "acknowledgement": "other", "complaint_ack": "other",
             "third_party_ack": "other"}

_MISSING_LABELS: Dict[str, Dict[str, str]] = {
    "nl": {"loss_date": "de datum waarop de schade is ontstaan",
           "description": "een korte beschrijving van wat er is gebeurd",
           "priced_document": "een factuur of reparatieofferte met het bedrag",
           "police_report": "het nummer van het proces-verbaal (aangifte bij de politie)",
           "identity": "uw polisnummer en uw volledige naam zoals bekend bij ons, verstuurd vanaf het e-mailadres "
                       "dat bij ons bekend is"},
    "en": {"loss_date": "the date the loss or damage happened",
           "description": "a short description of what happened",
           "priced_document": "an invoice or repair quote showing the amount",
           "police_report": "the police report number",
           "identity": "your policy number and your full name as known to us, sent from the email address we have "
                       "on file"},
}

_SIGN = {"nl": "Met vriendelijke groet,\n\nTeam Schadebehandeling\nNoorderhaven Verzekeringen",
         "en": "Kind regards,\n\nClaims Team\nNoorderhaven Verzekeringen"}


def _eur(x: float, lang: str) -> str:
    s = f"{x:,.2f}"
    if lang == "nl":
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"EUR {s}"


def _greeting(email: Email, lang: str) -> str:
    name = (email.from_name or "").strip()
    if lang == "nl":
        return f"Beste {name}," if name else "Beste heer/mevrouw,"
    return f"Dear {name}," if name else "Dear Sir or Madam,"


def _bullets(items: List[str]) -> str:
    return "\n".join(f"- {i}" for i in items)


def _body(kind: str, lang: str, decision: Decision, facts: Facts, policy: Optional[Policy]) -> str:
    nl = lang == "nl"
    if kind == "info_request":
        items = [_MISSING_LABELS[lang].get(m, m) for m in decision.missing_items] or (
            ["aanvullende informatie over uw schade"] if nl else ["more information about your claim"])
        ref = f" (polis {policy.policy_number})" if policy else ""
        if nl:
            return (f"Bedankt voor uw schademelding{ref}. Om uw schade te kunnen beoordelen hebben wij nog het "
                    f"volgende van u nodig:\n\n{_bullets(items)}\n\nU kunt dit sturen door op deze e-mail te "
                    "antwoorden. Zodra wij alles hebben, nemen wij uw melding verder in behandeling.")
        return (f"Thank you for reporting your claim{ref}. To assess it we still need the following from you:\n\n"
                f"{_bullets(items)}\n\nYou can simply reply to this email. As soon as we have everything, we will "
                "continue handling your claim.")
    if kind == "identity_request":
        items = [_MISSING_LABELS[lang]["identity"]]
        if nl:
            return ("Bedankt voor uw bericht. Wij kunnen uw e-mail nog niet koppelen aan een polis op uw naam. Om uw "
                    f"privacy te beschermen hebben wij eerst het volgende nodig:\n\n{_bullets(items)}\n\n"
                    "Zodra wij uw gegevens hebben kunnen controleren, nemen wij uw bericht in behandeling.")
        return ("Thank you for your message. We cannot yet link your email to a policy in your name. To protect "
                f"your privacy we first need the following:\n\n{_bullets(items)}\n\n"
                "Once we have been able to check your details, we will handle your message.")
    if kind == "approval":
        amount = _eur(decision.net_payout or 0.0, lang)
        if nl:
            return (f"Goed nieuws: wij hebben uw schademelding beoordeeld en goedgekeurd. Wij keren {amount} uit, "
                    "dit is het gedocumenteerde bedrag min uw eigen risico. Het bedrag wordt overgemaakt naar het "
                    "rekeningnummer dat bij ons bekend is.\n\nHeeft u vragen? Antwoord dan gerust op deze e-mail.")
        return (f"Good news: we have assessed and approved your claim. We will pay {amount}, which is the documented "
                "amount minus your deductible. The payment will be made to the bank account we have on file.\n\n"
                "If you have any questions, just reply to this email.")
    if kind == "acknowledgement":
        if nl:
            return ("Bedankt voor uw schademelding. Wij hebben uw melding en de bijlagen goed ontvangen. Een "
                    "schadebehandelaar bekijkt uw melding en neemt zo snel mogelijk contact met u op.")
        return ("Thank you for your claim. We have received your message and attachments. A claims handler is "
                "reviewing your claim and will get back to you as soon as possible.")
    if kind == "rejection_draft":
        # Customer-facing reason in the customer's language; the internal reason (English, § refs) stays
        # in the review queue for the handler.
        why = _customer_reason(decision.flags, nl) or decision.reason.split(" Draft held")[0]
        if nl:
            return ("Bedankt voor uw schademelding. Wij hebben uw melding zorgvuldig bekeken. Helaas kunnen wij deze "
                    f"schade niet vergoeden.\n\nReden: {why}\n\nBent u het niet eens met deze beslissing, "
                    "of heeft u informatie die wij over het hoofd hebben gezien? Antwoord dan op deze e-mail; een "
                    "schadebehandelaar kijkt er dan opnieuw naar.")
        return ("Thank you for your claim. We have looked at it carefully. Unfortunately we are unable to cover this "
                f"loss.\n\nReason: {why}\n\nIf you disagree, or have information we may have missed, "
                "please reply to this email and a claims handler will look at it again.")
    if kind == "complaint_ack":
        if nl:
            return ("Bedankt voor uw bericht. Wij hebben het doorgestuurd naar het team dat uw lopende schade "
                    "behandelt. Een collega neemt zo snel mogelijk persoonlijk contact met u op.")
        return ("Thank you for your message. We have passed it on to the team handling your existing claim. A "
                "colleague will contact you personally as soon as possible.")
    if kind == "third_party_ack":
        if nl:
            return ("Bedankt voor uw bericht. Wij hebben het doorgestuurd naar de juiste afdeling, die uw bericht "
                    "beoordeelt. Om privacyredenen kunnen wij geen informatie geven over verzekerden of polissen.")
        return ("Thank you for your message. We have forwarded it to the right department, which will review it. "
                "For privacy reasons we cannot share any information about insured persons or policies.")
    raise ValueError(f"unknown letter kind {kind!r} for claim {decision.claim_id}")


_CUSTOMER_REASONS = {
    "policy_not_active": ("Uw verzekering was op de schadedatum niet actief.",
                          "Your policy was not active on the date of the loss."),
    "peril_not_covered": ("Dit type schade valt niet onder de dekking van uw verzekering.",
                          "This type of damage is not covered by your policy."),
    "gradual_damage": ("De schade is geleidelijk ontstaan (bijvoorbeeld door vocht, schimmel of een langdurige lekkage). "
                       "Geleidelijk ontstane schade is uitgesloten van dekking.",
                       "The damage developed gradually (for example damp, mould or a long-term leak). Gradual damage "
                       "is excluded from cover."),
    "outside_europe": ("Uw inboedelverzekering dekt alleen schade binnen Europa.",
                       "Your contents policy only covers losses within Europe."),
    "accidental_damage_not_covered": ("Uw inboedelverzekering dekt geen schade door eigen toedoen, zoals laten vallen of morsen.",
                                      "Your contents policy does not cover accidental damage such as dropping or spilling."),
}


def _customer_reason(flags, nl: bool):
    for flag, (nl_text, en_text) in _CUSTOMER_REASONS.items():
        if flag in flags:
            return nl_text if nl else en_text
    return None


def build_letter(decision: Decision, facts: Facts, email: Email, policy: Optional[Policy]) -> Optional[Letter]:
    """Letter for this decision, or None when nothing should be sent (spam / no letter kind)."""
    kind = decision.letter_kind
    if kind is None or "spam" in decision.flags:
        return None
    if kind not in LETTER_KINDS:
        raise ValueError(f"unknown letter kind {kind!r} for claim {decision.claim_id}")
    if kind == "approval" and (decision.action != "auto_approve" or decision.net_payout is None):
        raise ValueError(f"approval letter requested for claim {decision.claim_id} with action {decision.action} "
                         f"and net_payout {decision.net_payout}; refusing to promise an unapproved payout")
    lang = facts.language if facts.language in ("nl", "en") else "nl"
    # Policy details are only ever mentioned to a verified policyholder.
    shown_policy = policy if kind in ("info_request", "approval") else None
    body = f"{_greeting(email, lang)}\n\n{_body(kind, lang, decision, facts, shown_policy)}\n\n{_SIGN[lang]}"
    subject = email.subject or ""
    subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    return Letter(kind=kind, to=email.from_email, subject=subject, body=body, language=lang,
                  in_reply_to=email.message_id, send_now=kind != "rejection_draft", api_kind=_API_KIND[kind])
