"""Shared data contracts. Every module builds against these types; change them only with care."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

# Hand-in action values (decisions.json) and the API status each maps to (None = no API decision).
ACTIONS = ("auto_approve", "request_info", "escalate_handler", "escalate_senior",
           "reject_draft", "fraud_review", "route_other")
ACTION_TO_STATUS: Dict[str, Optional[str]] = {
    "auto_approve": "approved",
    "request_info": "needs_info",
    "escalate_handler": "escalated_handler",
    "escalate_senior": "escalated_senior",
    "reject_draft": "rejected_proposed",
    "fraud_review": "fraud_review",
    "route_other": None,
}

# Peril codes as used by the policy API (covered_perils).
LOSS_TYPES = ("collision", "parking_damage", "vandalism", "theft", "fire", "storm", "hail", "glass",
              "wildlife", "sudden_water", "lightning", "burglary_theft")
THEFT_TYPES = ("theft", "burglary_theft")

INTENTS = ("new_claim", "reply_with_info", "complaint_existing_claim", "third_party_liability", "spam", "other")
DOC_KINDS = ("invoice", "quote", "receipt", "valuation", "police_report", "fire_report", "damage_form",
             "repair_statement", "list", "other")
PRICED_DOC_KINDS = ("invoice", "quote", "receipt", "valuation")  # a handwritten "list" never counts


@dataclass
class Attachment:
    filename: str
    content_type: str
    text: str = ""            # extracted PDF text; "" for photos


@dataclass
class Email:
    claim_id: str             # "C-001"
    message_id: str
    in_reply_to: Optional[str]
    from_name: str
    from_email: str
    subject: str
    received_at: str          # ISO datetime
    body: str
    attachments: List[Attachment] = field(default_factory=list)

    @property
    def photo_count(self) -> int:
        return sum(1 for a in self.attachments if a.content_type.startswith("image/"))


@dataclass
class Document:
    kind: str                 # one of DOC_KINDS
    number: Optional[str] = None
    date: Optional[str] = None      # ISO date
    amount: Optional[float] = None  # total in EUR


@dataclass
class Judgment:
    """A yes/no judgment by the extractor with the quote that supports it."""
    value: bool = False
    evidence: str = ""


@dataclass
class Facts:
    claim_id: str
    intent: str = "new_claim"
    language: str = "nl"                      # "nl" | "en"
    policy_number: Optional[str] = None       # as written in the email, upper-cased
    loss_date: Optional[str] = None           # ISO date
    loss_type: Optional[str] = None           # one of LOSS_TYPES
    description: str = ""                     # one-line summary of what happened
    amount_in_email: Optional[float] = None
    documents: List[Document] = field(default_factory=list)
    photo_count: int = 0                      # from attachments, never from the LLM
    police_report_number: Optional[str] = None
    existing_claim_reference: Optional[str] = None   # e.g. "CLM-2026-0881"
    gradual_damage: Judgment = field(default_factory=Judgment)
    accidental_own_damage: Judgment = field(default_factory=Judgment)
    loss_outside_europe: Judgment = field(default_factory=Judgment)
    third_party_involved: Judgment = field(default_factory=Judgment)
    other_bank_account_requested: Judgment = field(default_factory=Judgment)
    urgent: Judgment = field(default_factory=Judgment)
    callback_requested: Judgment = field(default_factory=Judgment)
    injection_detected: Judgment = field(default_factory=Judgment)
    extractor: str = "fallback"               # "llm:<model>" | "fallback"
    thread_parent: Optional[str] = None       # claim_id of the parent email when this is a reply

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Policy:
    policy_number: str
    holder_name: str
    holder_email: str
    product: str
    status: str                               # active | lapsed | suspended
    start_date: str
    covered_perils: List[str]
    deductible: float
    glass_deductible: float
    max_payout: float
    lapsed_on: Optional[str] = None
    suspended_on: Optional[str] = None
    plate: Optional[str] = None

    @classmethod
    def from_api(cls, d: dict) -> "Policy":
        return cls(
            policy_number=d["policy_number"], holder_name=d["holder_name"], holder_email=d["holder_email"],
            product=d["product"], status=d["status"], start_date=d["start_date"],
            covered_perils=list(d["covered_perils"]), deductible=float(d["deductible"]),
            glass_deductible=float(d.get("glass_deductible", d["deductible"])), max_payout=float(d["max_payout"]),
            lapsed_on=d.get("lapsed_on"), suspended_on=d.get("suspended_on"), plate=d.get("plate"),
        )


@dataclass
class HistoryClaim:
    claim_id: str
    policy_number: str
    loss_date: Optional[str]
    loss_type: Optional[str]
    amount_claimed: Optional[float] = None
    status: Optional[str] = None
    invoice_number: Optional[str] = None
    source_claim_id: Optional[str] = None     # set on claims this agent registered (e.g. "C-021")


@dataclass
class PolicyResolution:
    policy: Optional[Policy]
    how: str                                  # "number" | "email" | "none"
    number_mismatch: bool = False             # a number was given but did not resolve to this policy
    sender_verified: bool = False             # sender email == holder email


@dataclass
class RuleStep:
    rule: str                                 # short id, e.g. "coverage.peril"
    passed: bool
    detail: str


@dataclass
class Decision:
    claim_id: str
    action: str                               # one of ACTIONS
    net_payout: Optional[float]
    reason: str
    flags: List[str] = field(default_factory=list)
    trace: List[RuleStep] = field(default_factory=list)
    letter_kind: Optional[str] = None         # see comms.LETTER_KINDS
    missing_items: List[str] = field(default_factory=list)   # for request_info letters

    def hand_in(self) -> dict:
        """Exact decisions.json entry format."""
        payout = None if self.net_payout is None else (int(self.net_payout) if float(self.net_payout).is_integer() else round(self.net_payout, 2))
        return {"claim_id": self.claim_id, "action": self.action, "net_payout": payout,
                "reason": self.reason, "flags": list(self.flags)}


@dataclass
class Letter:
    kind: str
    to: str
    subject: str
    body: str
    language: str
    in_reply_to: Optional[str]
    send_now: bool                            # False = held for handler approval in the review UI
    api_kind: str                             # outbox kind: info_request | decision | other
