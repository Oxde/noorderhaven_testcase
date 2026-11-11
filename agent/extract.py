"""Intake: load claim emails, extract a Facts sheet (LLM or deterministic fallback), detect prompt injection.

The LLM only fills in facts. `normalise` validates every value it returns against the enums in
`agent/models.py`, and deterministic injection detection is OR-ed in on every path, so a fooled or broken
LLM cannot switch off the injection flag.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import List, Optional, Tuple

from . import config
from .models import (DOC_KINDS, INTENTS, LOSS_TYPES, Attachment, Document, Email, Facts, Judgment)
from .pdf_text import extract_text

# ---------------------------------------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------------------------------------


def load_email(claim_dir: Path) -> Email:
    path = claim_dir / "email.json"
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError("cannot read %s: %s" % (path, e)) from e
    atts: List[Attachment] = []
    for a in d.get("attachments") or []:
        ctype = a.get("content_type", "")
        text = ""
        if ctype == "application/pdf" or a.get("filename", "").lower().endswith(".pdf"):
            text = extract_text(claim_dir / a["filename"])
        atts.append(Attachment(filename=a["filename"], content_type=ctype, text=text))
    sender = d.get("from") or {}
    return Email(claim_id=d["claim_id"], message_id=d.get("message_id", ""), in_reply_to=d.get("in_reply_to"),
                 from_name=sender.get("name", ""), from_email=sender.get("email", ""), subject=d.get("subject", ""),
                 received_at=d["received_at"], body=d.get("body", ""), attachments=atts)


def load_emails(claims_dir: Path = config.CLAIMS_DIR) -> List[Email]:
    """All claims/*/email.json with PDF text filled in, sorted by received_at (then claim_id)."""
    dirs = sorted(p for p in Path(claims_dir).iterdir() if (p / "email.json").exists())
    if not dirs:
        raise FileNotFoundError("no claims/*/email.json found under %s" % claims_dir)
    emails = [load_email(p) for p in dirs]
    emails.sort(key=lambda e: (e.received_at, e.claim_id))
    return emails


# ---------------------------------------------------------------------------------------------------------
# Small parsers
# ---------------------------------------------------------------------------------------------------------

_NUM_RE = re.compile(r"\d[\d.,]*\d|\d")


def parse_amount(value: object) -> Optional[float]:
    """Parse Dutch and English money formats: '1.650 euro', 'EUR 1,650.00', '3.200', '38,000.00', '12,50'."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _NUM_RE.search(str(value))
    if not m:
        return None
    s = m.group(0)
    if "." in s and "," in s:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        thou = "," if dec == "." else "."
        s = s.replace(thou, "").replace(dec, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        groups = s.split(sep)
        if len(groups) > 1 and all(len(g) == 3 for g in groups[1:]):
            s = "".join(groups)                      # thousands separators: 1.650 / 38,000
        else:
            s = "".join(groups[:-1]) + "." + groups[-1]   # decimal: 620.00 / 12,50
    try:
        return float(s)
    except ValueError:
        return None


_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6, "juli": 7, "augustus": 8,
    "september": 9, "oktober": 10, "november": 11, "december": 12,
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7, "august": 8, "october": 10,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_NIGHT_RE = re.compile(r"(?:nacht\s+van|night\s+of)\s+(\d{1,2})\s+(?:op|to|on|-)\s+(\d{1,2})\s+(%s)" % _MONTH_ALT, re.I)
_DAY_MONTH_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(%s)\b" % _MONTH_ALT, re.I)
_MONTH_DAY_RE = re.compile(r"\b(%s)\s+(\d{1,2})(?:st|nd|rd|th)?\b" % _MONTH_ALT, re.I)
_ISO_RE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
_WEEKDAYS = {"maandag": 0, "dinsdag": 1, "woensdag": 2, "donderdag": 3, "vrijdag": 4, "zaterdag": 5, "zondag": 6,
             "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}


def _received_date(received_at: str) -> _dt.date:
    return _dt.date.fromisoformat(received_at[:10])


def _mk_date(day: int, month: int, received: _dt.date) -> Optional[str]:
    try:
        d = _dt.date(received.year, month, day)
    except ValueError:
        return None
    if d > received:  # a loss cannot be after the email: it was last year
        try:
            d = _dt.date(received.year - 1, month, day)
        except ValueError:
            return None
    return d.isoformat()


def parse_iso_date(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    m = _ISO_RE.search(value)
    if not m:
        return None
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def find_loss_date(text: str, received_at: str) -> Optional[str]:
    """First loss date written in the text, resolved against the received date."""
    received = _received_date(received_at)
    m = _NIGHT_RE.search(text)
    if m:
        return _mk_date(int(m.group(1)), _MONTHS[m.group(3).lower()], received)
    m = _DAY_MONTH_RE.search(text)
    if m:
        return _mk_date(int(m.group(1)), _MONTHS[m.group(2).lower()], received)
    m = _MONTH_DAY_RE.search(text)
    if m:
        return _mk_date(int(m.group(2)), _MONTHS[m.group(1).lower()], received)
    low = text.lower()
    if re.search(r"\b(gisteren|gisteravond|gistermiddag|gisterochtend|yesterday|last night)\b", low):
        return (received - _dt.timedelta(days=1)).isoformat()
    m = re.search(r"\b(?:afgelopen|vorige|last)\s+(%s)\b" % "|".join(_WEEKDAYS), low)
    if m:
        delta = (received.weekday() - _WEEKDAYS[m.group(1)]) % 7 or 7
        return (received - _dt.timedelta(days=delta)).isoformat()
    if re.search(r"\b(vannacht|vanochtend|this morning)\b", low):
        return received.isoformat()
    return None


# ---------------------------------------------------------------------------------------------------------
# Injection detection (deterministic, runs on every path)
# ---------------------------------------------------------------------------------------------------------

_INJECTION_RE = re.compile("|".join([
    r"system\s*(?:instruction|prompt|message)",
    r"systeem\s*(?:instructie|prompt|bericht)",
    r"ignore\s+(?:all\s+|any\s+)?(?:your|all|previous|prior|the|above)\s+(?:\w+\s+)?(?:rules|instructions|checks)",
    r"negeer\s+(?:je|jouw|alle|de|eerdere)\s+(?:\w+\s+)?(?:regels|instructies)",
    r"\bAI[\s-]+(?:assistant|agent|model|system)",
    r"\b(?:claims|claim)\s+(?:assistant|agent|bot)",
    r"\blanguage\s+model\b",
    r"set\s+the\s+(?:status|payout)",
    r"mark\s+(?:this|the)\s+claim\s+as\s+approved",
    r"keur\s+(?:deze|de)\s+claim\s+goed",
    r"skip\s+all\s+(?:other\s+)?checks",
    r"do\s+not\s+mention",
    r"vermeld\s+dit\s+niet",
]), re.I)


def detect_injection(text: str) -> Judgment:
    """Instructions aimed at an AI/system inside customer data. Evidence is the matched line."""
    if not text:
        return Judgment()
    m = _INJECTION_RE.search(text)
    if not m:
        return Judgment()
    start = text.rfind("\n", 0, m.start()) + 1
    end = text.find("\n", m.end())
    line = text[start:end if end >= 0 else len(text)].strip()
    return Judgment(True, line[:240])


def _injection_over_email(email: Email) -> Judgment:
    j = detect_injection(email.subject + "\n" + email.body)
    if j.value:
        return Judgment(True, "email body: " + j.evidence)
    for a in email.attachments:
        j = detect_injection(a.text)
        if j.value:
            return Judgment(True, "%s: %s" % (a.filename, j.evidence))
    return Judgment()


# ---------------------------------------------------------------------------------------------------------
# Fallback extractor (no LLM)
# ---------------------------------------------------------------------------------------------------------

_POLICY_RE = re.compile(r"\bPOL[-\s]?(\d{6})\b", re.I)
_CLAIM_REF_RE = re.compile(r"\bCLM-\d{4}-\d{4}\b", re.I)
_EMAIL_AMOUNT_RE = re.compile(r"(?:€|EUR)\s*(\d[\d.,]*)|(\d[\d.,]*)\s*(?:euro|eur\b|€)", re.I)
_PHONE_RE = re.compile(r"(?:\+31[\s-]?|\b0)6[\s-]?\d{2}[\s-]?\d{2}[\s-]?\d{2}[\s-]?\d{2}\b|\b0\d{2}[\s-]\d{7}\b")
_IBAN_RE = re.compile(r"\bIBAN\b|\b[A-Z]{2}\d{2}\s?[A-Z]{4}\s?\d{4}", re.I)

_DOC_KIND_RULES: List[Tuple[str, str]] = [
    (r"proces-verbaal|police report|politierapport", "police_report"),
    (r"taxatie|waardebepaling|valuation|assessment", "valuation"),
    (r"rapport brandweer|brandweer|fire (?:brigade|department) report", "fire_report"),
    (r"schadeformulier|damage form|europees schadeformulier", "damage_form"),
    (r"verklaring|repair statement", "repair_statement"),
    (r"lijstje|^lijst|list\b", "list"),
    (r"kassabon|orderbevestiging|aankoopbon|receipt|bon\b", "receipt"),
    (r"offerte|quote|estimate", "quote"),
    (r"factuur|invoice", "invoice"),
]

# (loss_type, pattern) checked in order; first hit wins. Run on subject + body only.
_LOSS_RULES: List[Tuple[str, str]] = [
    ("fire", r"\bbrand\b|\bfire\b|brandweer|\bvuur\b"),
    ("lightning", r"bliksem|lightning|onweer"),
    ("hail", r"hagel|\bhail"),
    ("storm", r"\bstorm"),
    ("wildlife", r"\bree\b|\bhert\b|everzwijn|wild zwijn|\bdeer\b|\bwildlife|overstekend wild"),
    ("burglary_theft", r"ingebroken|inbraak|burglary|break-in|broken into|afgesloten schuur"),
    ("theft", r"gestolen|diefstal|stolen|\btheft\b|gezaagd|opengebroken"),
    ("sudden_water", r"waterschade|lekkage|\blek\b|lekkende|afvoerslang|wasmachineslang|leiding|water damage|\bleak|burst pipe"),
    ("glass", r"voorruit|windscreen|windshield|steenslag|stone chip|\bruit\b|\braam\b|glasschade|\bglas\b|window"),
    ("collision", r"\bi scraped|ik reed|pillar|paal|rear-ended|botsing|kettingbotsing|achteruit reed"),
    ("parking_damage", r"parkeer|geparkeerd|car park|parking|doorgereden|weggereden|uitparkeren"),
    ("vandalism", r"vandal|bekrast|afgerukt|ripped off|vernield"),
    ("collision", r"aanrijding|geraakt|collision|\bhit\b|deuk|geschraapt|scraped|crash|accident|ongeluk|ongeval|aan gereden"),
]

_NON_EUROPE = r"\bbali\b|indonesi|thailand|\bthai\b|\bbangkok\b|\bphuket\b|\bmexico\b|\bcanc[uú]n\b|\bbrazil|brazili|\begypt|egypte|\bmorocco|marokko|\bturkey\b|\bdubai\b|\bvs\b|\busa\b|united states|amerika|\bindia\b|\bchina\b|\bjapan\b|\bvietnam\b|\bcuraçao|curacao|\baruba\b|\bsuriname\b|\bkenya\b|\btanzania|\bsouth africa|zuid-afrika|australi|new zealand|nieuw-zeeland|\bcanada\b|\bdominican|dominicaanse|\bsri lanka\b|\bmaldiv|malediven"
_DUTCH_STOP = {"de", "het", "een", "en", "is", "ik", "mijn", "van", "op", "niet", "met", "zijn", "voor", "bij", "er", "heb",
               "dat", "die", "aan", "groet", "beste", "hallo", "hoi", "graag", "polisnummer", "naar", "ook", "wel", "nog"}
_ENGLISH_STOP = {"the", "a", "an", "and", "is", "i", "my", "of", "on", "not", "with", "was", "for", "at", "have", "to",
                 "that", "it", "in", "please", "hi", "hello", "dear", "thanks", "regards", "policy", "attached", "from"}


def detect_language(text: str) -> str:
    words = re.findall(r"[a-zà-ÿ]+", text.lower())
    nl = sum(1 for w in words if w in _DUTCH_STOP)
    en = sum(1 for w in words if w in _ENGLISH_STOP)
    return "en" if en > nl else "nl"


def _kw(pattern: str, text: str) -> Judgment:
    m = re.search(pattern, text, re.I)
    if not m:
        return Judgment()
    # Evidence: the sentence (or line) around the hit.
    start = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
    ends = [i for i in (text.find(".", m.end()), text.find("\n", m.end())) if i >= 0]
    end = min(ends) if ends else len(text)
    return Judgment(True, text[start:end].strip()[:200])


def parse_document(att: Attachment, received_at: str) -> Optional[Document]:
    text = att.text or ""
    if not text.strip():
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    first = lines[0]
    kind = "other"
    for pat, k in _DOC_KIND_RULES:
        if re.search(pat, first, re.I):
            kind = k
            break
    number = None
    m = re.search(r"(?:nummer|number|report no|no\.?)\s*:\s*(\S+)", text, re.I)
    if m:
        number = m.group(1)
    else:
        m = re.search(r"\b([A-Z]{1,4}(?:[-/][A-Z0-9]+)+|\d{4}-\d{3,})\b", first)
        number = m.group(1) if m else None
    date = None
    m = re.search(r"(?:datum|date)[^:\n]*:\s*(\d{4}-\d{2}-\d{2})", text, re.I)
    if m:
        date = parse_iso_date(m.group(1))
    amount = None
    m = re.search(r"(?:totaal|total)[^:\n]*:\s*(?:EUR|€)?\s*(\d[\d.,]*)", text, re.I)
    if m:
        amount = parse_amount(m.group(1))
    return Document(kind=kind, number=number, date=date, amount=amount)


def fallback_extract(email: Email) -> Facts:
    """Regex/keyword extraction tuned for the templated claim emails. Used without a key or when the LLM fails."""
    body = email.body or ""
    # Keep injected lines out of fact matching (amounts such as "9500 euro" in an injection must not count).
    inj = _injection_over_email(email)
    clean_body = "\n".join(ln for ln in body.splitlines() if not detect_injection(ln).value)
    text = email.subject + "\n" + clean_body
    low = text.lower()
    f = Facts(claim_id=email.claim_id)
    f.language = detect_language(clean_body or email.subject)

    m = _POLICY_RE.search(text)
    f.policy_number = ("POL-" + m.group(1)) if m else None
    m = _CLAIM_REF_RE.search(text)
    f.existing_claim_reference = m.group(0).upper() if m else None

    # Intent
    spam = re.search(r"korting|discount|leads\b|klik hier|click here|unsubscribe|aanbieding|special offer|webinar", low)
    if spam and not f.policy_number:
        f.intent = "spam"
    elif f.existing_claim_reference and re.search(r"klacht|geen reactie|onacceptabel|kifid|complaint|no response|nog steeds|still no", low):
        f.intent = "complaint_existing_claim"
    elif re.search(r"uw verzekerde|your (?:insured|policyholder)|verzekerden|op uw verzekerde verhalen", low):
        f.intent = "third_party_liability"
    elif email.in_reply_to or re.match(r"\s*(re|antw|aw)\s*:", email.subject, re.I):
        f.intent = "reply_with_info"
    else:
        f.intent = "new_claim"

    f.loss_date = find_loss_date(clean_body, email.received_at)
    for lt, pat in _LOSS_RULES:
        if re.search(pat, low):
            f.loss_type = lt
            break
    first_line = next((ln.strip() for ln in clean_body.splitlines() if ln.strip() and not re.match(
        r"^(beste|hallo|hoi|hi|hello|dear|goedemorgen|goedemiddag|geachte)\b", ln.strip(), re.I)), "")
    f.description = (email.subject + ": " + first_line)[:240] if first_line else email.subject

    m = _EMAIL_AMOUNT_RE.search(clean_body)
    f.amount_in_email = parse_amount(m.group(1) or m.group(2)) if m else None

    for att in email.attachments:
        if att.content_type.startswith("image/"):
            continue
        doc = parse_document(att, email.received_at)
        if doc:
            f.documents.append(doc)
            if doc.kind == "police_report" and doc.number and not f.police_report_number:
                f.police_report_number = doc.number
    f.photo_count = email.photo_count

    f.gradual_damage = _kw(r"al sinds|paar weken|al weken|maanden|schimmel|vocht|for weeks|for months|mould|mold|damp", clean_body)
    f.accidental_own_damage = _kw(r"gevallen|laten vallen|omgestoten|gemorst|dropped|spilled|knocked over", clean_body)
    f.loss_outside_europe = _kw(_NON_EUROPE, text)
    f.third_party_involved = _kw(r"fietser|bovenburen|buren|wederpartij|tegenpartij|rear-ended|vrachtwagen|other driver|"
                                 r"uw verzekerde|kenteken|plate", clean_body)
    f.other_bank_account_requested = _kw(r"\bIBAN\b|ander(?:e)? (?:bank)?rekening|other (?:bank )?account|transfer the money to", clean_body)
    f.urgent = _kw(r"urgent|spoed|as soon as possible|asap|zo snel mogelijk|z\.s\.m\.|kifid|staying with family|"
                   r"logeren|vandaag nog|onacceptabel", text)
    cb = _kw(r"call me|bel me|bel mij|terugbellen|phone me|ring me", clean_body)
    if not cb.value:
        pm = _PHONE_RE.search(clean_body)
        if pm:
            cb = Judgment(True, pm.group(0))
    f.callback_requested = cb
    f.injection_detected = inj
    f.extractor = "fallback"
    return f


# ---------------------------------------------------------------------------------------------------------
# Normalising LLM output
# ---------------------------------------------------------------------------------------------------------


def _judgment(raw: object) -> Judgment:
    if isinstance(raw, Judgment):
        return raw
    if isinstance(raw, bool):
        return Judgment(raw, "")
    if isinstance(raw, dict):
        v = raw.get("value")
        if isinstance(v, str):
            v = v.strip().lower() in ("true", "yes", "1", "ja")
        ev = raw.get("evidence")
        return Judgment(bool(v) if isinstance(v, (bool, int)) else False, str(ev)[:240] if ev else "")
    return Judgment()


def _str_or_none(v: object) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.lower() not in ("null", "none", "unknown", "n/a", "-") else None


def _date(v: object, received_at: str) -> Optional[str]:
    s = _str_or_none(v)
    if not s:
        return None
    return parse_iso_date(s) or find_loss_date(s, received_at)


def normalise(raw: dict, email: Email) -> Facts:
    """Turn raw LLM JSON into a valid Facts. Unknown enum values become None (or a safe default); never raises."""
    if not isinstance(raw, dict):
        raw = {}
    f = Facts(claim_id=email.claim_id)
    intent = _str_or_none(raw.get("intent"))
    f.intent = intent.lower() if intent and intent.lower() in INTENTS else "new_claim"  # safest: full rule set applies
    lang = (_str_or_none(raw.get("language")) or "").lower()[:2]
    f.language = lang if lang in ("nl", "en") else detect_language(email.body)
    pol = _str_or_none(raw.get("policy_number"))
    f.policy_number = re.sub(r"\s+", "", pol).upper() if pol else None
    f.loss_date = _date(raw.get("loss_date"), email.received_at)
    lt = (_str_or_none(raw.get("loss_type")) or "").lower()
    f.loss_type = lt if lt in LOSS_TYPES else None
    f.description = str(raw.get("description") or "")[:300]
    f.amount_in_email = parse_amount(raw.get("amount_in_email"))
    docs = raw.get("documents")
    if isinstance(docs, list):
        for d in docs:
            if not isinstance(d, dict):
                continue
            kind = (_str_or_none(d.get("kind")) or "other").lower()
            f.documents.append(Document(kind=kind if kind in DOC_KINDS else "other",
                                        number=_str_or_none(d.get("number")),
                                        date=parse_iso_date(_str_or_none(d.get("date")) or ""),
                                        amount=parse_amount(d.get("amount"))))
    f.photo_count = email.photo_count  # from attachments, never from the LLM
    f.police_report_number = _str_or_none(raw.get("police_report_number"))
    ref = _str_or_none(raw.get("existing_claim_reference"))
    f.existing_claim_reference = ref.upper() if ref else None
    for name in ("gradual_damage", "accidental_own_damage", "loss_outside_europe", "third_party_involved",
                 "other_bank_account_requested", "urgent", "callback_requested", "injection_detected"):
        setattr(f, name, _judgment(raw.get(name)))
    det = _injection_over_email(email)
    if det.value:
        llm_ev = f.injection_detected.evidence if f.injection_detected.value else ""
        f.injection_detected = Judgment(True, det.evidence if not llm_ev else "%s | llm: %s" % (det.evidence, llm_ev[:120]))
    f.extractor = "llm"
    return f


# ---------------------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------------------


def extract_facts(email: Email, llm: Optional["LLMClient"] = None) -> Facts:  # noqa: F821
    """LLM extraction when a client is given, else the fallback. On LLMError, falls back and says so."""
    if llm is None:
        return fallback_extract(email)
    from .llm import LLMError
    from .prompts import EXTRACTION_SYSTEM, build_extraction_user
    try:
        raw, model = llm.complete_json(EXTRACTION_SYSTEM, build_extraction_user(email))
    except LLMError:
        f = fallback_extract(email)
        f.extractor = "fallback(llm_failed)"
        return f
    f = normalise(raw, email)
    f.extractor = "llm:%s" % model
    return f
