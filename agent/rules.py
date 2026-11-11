"""Deterministic claim decisions. The LLM only extracts facts; every decision is made here.

decide() follows docs/design.md "Decision rules" 1-10 in order. Every check appends a RuleStep to the
trace so a claims handler can see what passed and what failed. Rulebook sections (docs/policy.md) are
cited in the reason.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import List, Optional, Tuple

from agent.models import (PRICED_DOC_KINDS, THEFT_TYPES, Decision, Facts, HistoryClaim, Policy,
                          PolicyResolution, RuleStep)

AUTO_LIMIT = 1000.0      # §3: automatic approval allowed up to and including 1,000
SENIOR_LIMIT = 5000.0    # §3: above 5,000 goes to a senior handler
LATE_DAYS = 30           # §2.3
NEW_POLICY_DAYS = 30     # §4
REPEAT_WINDOW_DAYS = 365  # §4 "last 12 months"
REPEAT_THRESHOLD = 3     # §4 "3 or more claims ... counting the new one"
PHOTO_THRESHOLD = 500.0  # §2.5

MISSING_LOSS_DATE = "loss_date"
MISSING_DESCRIPTION = "description"
MISSING_PRICED_DOCUMENT = "priced_document"
MISSING_POLICE_REPORT = "police_report"
MISSING_IDENTITY = "identity"


def _d(s: str) -> date:
    try:
        return date.fromisoformat(s[:10])
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid ISO date {s!r}: {e}") from e


def _eur(x: float) -> str:
    return f"EUR {x:,.2f}"


def _same_peril(a: Optional[str], b: Optional[str]) -> bool:
    """theft (motor) and burglary_theft (contents) are one peril family."""
    if a is None or b is None:
        return False
    if a in THEFT_TYPES and b in THEFT_TYPES:
        return True
    return a == b


def _peril_covered(loss_type: str, policy: Policy) -> bool:
    return any(_same_peril(loss_type, p) for p in policy.covered_perils)


def document_amount(facts: Facts) -> Tuple[Optional[float], Optional[str]]:
    """Largest priced document (invoice/quote/receipt/valuation) total, and its kind."""
    best: Optional[float] = None
    kind: Optional[str] = None
    for doc in facts.documents:
        if doc.kind in PRICED_DOC_KINDS and doc.amount is not None and doc.amount > 0:
            if best is None or doc.amount > best:
                best, kind = float(doc.amount), doc.kind
    return best, kind


def net_payout(amount: float, policy: Policy, loss_type: Optional[str]) -> Tuple[float, float]:
    """§2.6: max(0, min(amount, max_payout) - deductible). Returns (net, deductible used)."""
    deductible = policy.glass_deductible if loss_type == "glass" else policy.deductible
    return max(0.0, min(amount, policy.max_payout) - deductible), deductible


def _active_on(policy: Policy, loss: date) -> Tuple[bool, str]:
    if policy.lapsed_on and _d(policy.lapsed_on) <= loss:
        return False, f"§2.1 policy lapsed on {policy.lapsed_on}, on or before the loss on {loss.isoformat()}"
    if policy.suspended_on and _d(policy.suspended_on) <= loss:
        return False, f"§2.1 policy suspended on {policy.suspended_on}, on or before the loss on {loss.isoformat()}"
    if policy.status != "active" and not policy.lapsed_on and not policy.suspended_on:
        return False, (f"§2.1 policy status is '{policy.status}' with no end date in the record; "
                       f"interpreted as not active on the loss date {loss.isoformat()}")
    if loss < _d(policy.start_date):
        return False, f"§2.1 loss on {loss.isoformat()} is before the policy start date {policy.start_date}"
    return True, f"policy active on {loss.isoformat()} (status {policy.status})"


def _extra_flags(facts: Facts, flags: List[str]) -> None:
    """Rule 10: informational flags that may be added to any decision."""
    if facts.third_party_involved.value and "possible_recourse" not in flags:
        flags.append("possible_recourse")
    if facts.urgent.value and "urgent" not in flags:
        flags.append("urgent")
    if facts.callback_requested.value and "callback_requested" not in flags:
        flags.append("callback_requested")


def _finish(facts: Facts, action: str, net: Optional[float], reason: str, flags: List[str],
            trace: List[RuleStep], letter_kind: Optional[str], missing: Optional[List[str]] = None,
            extra: bool = True) -> Decision:
    if extra:
        _extra_flags(facts, flags)
    if facts.thread_parent and "thread_parent" not in flags:
        flags.append("thread_parent")
    return Decision(claim_id=facts.claim_id, action=action, net_payout=net, reason=reason, flags=flags,
                    trace=trace, letter_kind=letter_kind, missing_items=list(missing or []))


def decide(facts: Facts, res: PolicyResolution, history: List[HistoryClaim], today: str,
           received_at: Optional[str] = None) -> Decision:
    """Decide one claim.

    history: other claims on the resolved policy (excluding this one; claims registered earlier in this
    run included). today: run date (ISO). received_at: date (or datetime) the email was received; used for
    the late-report check (§2.3). The pipeline passes email.received_at[:10]; when omitted, `today` is used
    and the trace says so.
    """
    trace: List[RuleStep] = []
    flags: List[str] = []

    # Rule 0 (Nikita's decision): when the LLM was meant to read the email but every model failed, the
    # keyword-based fallback may misread a fact without knowing it, and the rules would act on that with
    # full confidence. So nothing is decided automatically: the claim goes to a handler with the reason,
    # no letter is sent, and the fallback's facts are only a pre-fill for the handler (marked unverified).
    # Explicit --no-llm runs (extractor == "fallback") are a developer/test mode and still decide.
    if facts.extractor == "fallback(llm_failed)":
        trace.append(RuleStep("intake.llm_available", False,
                              "all LLM models failed; facts below come from the keyword reader and are unverified"))
        flags.extend(["llm_unavailable", "facts_unverified"])
        if facts.injection_detected.value:
            flags.append("injection")
        return _finish(facts, "escalate_handler", None,
                       "AI extraction was unavailable (all LLM models failed), so no automatic decision was made. "
                       "A handler must read the email; the extracted facts are an unverified keyword pre-fill.",
                       flags, trace, None, extra=False)

    # Rule 1: intents that are not a new claim for this agent.
    if facts.intent == "spam":
        trace.append(RuleStep("intent.spam", False, "email classified as spam / not a claim"))
        return _finish(facts, "route_other", None, "Not an insurance claim (classified as spam); no reply sent.",
                       flags + ["spam"], trace, None, extra=False)
    if facts.intent == "complaint_existing_claim":
        flags.append("complaint")
        if facts.existing_claim_reference:
            flags.append(f"existing_claim:{facts.existing_claim_reference}")
        trace.append(RuleStep("intent.complaint", False,
                              f"complaint about existing claim {facts.existing_claim_reference or '(no reference)'}"))
        ref = facts.existing_claim_reference or "an existing claim (no reference given)"
        return _finish(facts, "route_other", None,
                       f"Complaint about {ref}, not a new claim; routed to the claims team with an acknowledgement.",
                       flags, trace, "complaint_ack")
    if facts.intent == "third_party_liability":
        flags += ["third_party_claim", "privacy_no_disclosure"]
        trace.append(RuleStep("intent.third_party", False, "liability claim from a third party against an insured"))
        return _finish(facts, "route_other", None,
                       "Third-party liability claim against an insured; routed to the liability team. §5: nothing about "
                       "the insured or the policy may be disclosed, so only a neutral acknowledgement is sent.",
                       flags, trace, "third_party_ack", extra=False)
    # A vague "my car is damaged" from an unknown sender (C-033) is often classified "other". When no policy
    # matches the sender, §5 says ask for identification, so let it fall through to rule 2 (lead's call).
    if facts.intent == "other" and res.policy is not None:
        flags.append("needs_triage")
        trace.append(RuleStep("intent.other", False, "email is not a claim, complaint or third-party claim"))
        return _finish(facts, "route_other", None,
                       "Email is not a claim; routed to the general inbox for manual triage.", flags, trace, None)
    trace.append(RuleStep("intent", True, f"intent {facts.intent}"))

    # Rule 2: no policy resolved.
    policy = res.policy
    if policy is None:
        if facts.injection_detected.value or facts.other_bank_account_requested.value:
            if facts.injection_detected.value:
                flags.append("injection")
            if facts.other_bank_account_requested.value:
                flags.append("other_bank_account")
            flags.append("no_policy_found")
            trace.append(RuleStep("policy.resolve", False, "no policy found by number or sender email"))
            trace.append(RuleStep("fraud.no_policy", False, "risk indicator without any matching policy: "
                                  + ", ".join(f for f in flags if f != "no_policy_found")))
            return _finish(facts, "fraud_review", None,
                           "No policy matches the number or sender address, and the email shows §4 risk indicators ("
                           + ", ".join(f for f in flags if f != "no_policy_found") + "); sent to fraud review.",
                           flags, trace, "acknowledgement")
        flags.append("no_policy_found")
        trace.append(RuleStep("policy.resolve", False,
                              f"no policy found for number {facts.policy_number or '(none given)'} or sender email"))
        return _finish(facts, "request_info", None,
                       f"§5: the sender cannot be matched to a policy (number {facts.policy_number or 'not given'}, "
                       "sender address unknown); asking for identification and the policy number.",
                       flags, trace, "identity_request", [MISSING_IDENTITY])
    trace.append(RuleStep("policy.resolve", True, f"policy {policy.policy_number} found by {res.how}"))

    # Rule 3: sender is not the policyholder.
    if not res.sender_verified:
        flags.append("identity_unverified")
        trace.append(RuleStep("identity.verified", False, "sender address differs from the holder email on the policy"))
        return _finish(facts, "request_info", None,
                       f"§5: the sender address does not match the holder email of {policy.policy_number}; asking for "
                       "identification and disclosing nothing about the policy.",
                       flags, trace, "identity_request", [MISSING_IDENTITY])
    trace.append(RuleStep("identity.verified", True, "sender email matches holder email"))

    loss: Optional[date] = _d(facts.loss_date) if facts.loss_date else None

    # Rule 4: fraud indicators (collect all).
    fraud: List[str] = []
    details: List[str] = []
    if facts.injection_detected.value:
        fraud.append("injection")
        details.append(f"instructions addressed to the processing system ({facts.injection_detected.evidence[:80]!r})")
    trace.append(RuleStep("fraud.injection", not facts.injection_detected.value,
                          facts.injection_detected.evidence or "no instructions to the system found"))
    if facts.other_bank_account_requested.value:
        fraud.append("other_bank_account")
        details.append("payout requested to a bank account other than the one on file")
    trace.append(RuleStep("fraud.bank_account", not facts.other_bank_account_requested.value,
                          facts.other_bank_account_requested.evidence or "no other bank account requested"))

    own_hist = [h for h in history
                if h.policy_number == policy.policy_number
                and not (facts.thread_parent and h.source_claim_id == facts.thread_parent)
                and h.source_claim_id != facts.claim_id]
    doc_numbers = {d.number.strip().upper() for d in facts.documents
                   if d.number and d.kind in PRICED_DOC_KINDS}
    dup_inv = [h for h in history
               if h.invoice_number and h.invoice_number.strip().upper() in doc_numbers
               and not (facts.thread_parent and h.source_claim_id == facts.thread_parent)
               and h.source_claim_id != facts.claim_id]
    if dup_inv:
        fraud.append("duplicate_invoice")
        details.append(f"invoice {dup_inv[0].invoice_number} already used in claim {dup_inv[0].claim_id}")
    trace.append(RuleStep("fraud.duplicate_invoice", not dup_inv,
                          f"invoice already in claim {dup_inv[0].claim_id}" if dup_inv else
                          f"document numbers {sorted(doc_numbers) or 'none'} not seen before"))
    dup_evt = [h for h in own_hist if loss and h.loss_date and h.loss_type
               and _d(h.loss_date) == loss and _same_peril(h.loss_type, facts.loss_type)]
    if dup_evt:
        fraud.append("duplicate_event")
        details.append(f"same loss event ({facts.loss_type} on {facts.loss_date}) already claimed in {dup_evt[0].claim_id}")
    trace.append(RuleStep("fraud.duplicate_event", not dup_evt,
                          f"same event in {dup_evt[0].claim_id}" if dup_evt else "no claim with the same loss type and date"))
    if loss:
        recent = [h for h in own_hist if h.loss_date
                  and loss - timedelta(days=REPEAT_WINDOW_DAYS) <= _d(h.loss_date) <= loss]
        count = len(recent) + 1
        if count >= REPEAT_THRESHOLD:
            fraud.append("repeat_claimant_3_in_12m")
            details.append(f"{count} claims in the 12 months up to {facts.loss_date} including this one ("
                           + ", ".join(h.claim_id for h in recent) + ")")
        trace.append(RuleStep("fraud.repeat_claimant", count < REPEAT_THRESHOLD,
                              f"{count} claim(s) in the 12 months before the loss, counting this one"))
        start = _d(policy.start_date)
        if start <= loss < start + timedelta(days=NEW_POLICY_DAYS):
            fraud.append("new_policy_within_30d")
            details.append(f"loss on {facts.loss_date} is {(loss - start).days} days after policy start {policy.start_date}")
            trace.append(RuleStep("fraud.new_policy", False, f"loss {(loss - start).days} days after start"))
        else:
            trace.append(RuleStep("fraud.new_policy", True, f"policy started {policy.start_date}"))
    if fraud:
        flags += fraud
        return _finish(facts, "fraud_review", None,
                       "§4 risk indicators, no automatic decision: " + "; ".join(details) + ".",
                       flags, trace, "acknowledgement")

    # Rule 4b (lead's call): clear exclusions that do not depend on the missing facts. Asking a customer for
    # a loss date or invoice is pointless when the claim is excluded anyway (C-010 dropped phone, C-019/C-024
    # gradual damage). The policy-active check still needs a loss date, so it stays in rule 6.
    early: Optional[Tuple[str, str]] = None
    if facts.loss_type and not _peril_covered(facts.loss_type, policy):
        early = ("peril_not_covered", f"§1: {facts.loss_type} is not a covered peril for {policy.product} policy "
                 f"{policy.policy_number} (covered: {', '.join(policy.covered_perils)}).")
    elif facts.gradual_damage.value:
        ev = f" (\"{facts.gradual_damage.evidence[:100]}\")" if facts.gradual_damage.evidence else ""
        early = ("gradual_damage", f"§2.2: the damage appears gradual rather than sudden{ev}, which is excluded.")
    elif policy.product == "contents" and facts.loss_outside_europe.value:
        early = ("outside_europe", "§1: contents cover applies in Europe only and the loss happened outside Europe.")
    elif policy.product == "contents" and facts.accidental_own_damage.value:
        early = ("accidental_damage_not_covered", "§1: contents policies have no accidental damage cover (drops, spills)"
                 + (f"; \"{facts.accidental_own_damage.evidence[:80]}\"" if facts.accidental_own_damage.evidence else "")
                 + ".")
    if early:
        trace.append(RuleStep("coverage.exclusion", False, early[1]))
        flags.append(early[0])
        return _finish(facts, "reject_draft", None, early[1] + " Draft held for a handler to confirm.",
                       flags, trace, "rejection_draft")

    # Rule 5: missing essentials.
    missing: List[str] = []
    if not loss:
        missing.append(MISSING_LOSS_DATE)
    if not facts.loss_type:
        missing.append(MISSING_DESCRIPTION)
    amount, amount_kind = document_amount(facts)
    if amount is None:
        missing.append(MISSING_PRICED_DOCUMENT)
    if facts.loss_type in THEFT_TYPES and not facts.police_report_number \
            and not any(d.kind == "police_report" for d in facts.documents):
        missing.append(MISSING_POLICE_REPORT)
    trace.append(RuleStep("essentials", not missing, "missing: " + ", ".join(missing) if missing else "complete"))
    if missing:
        labels = {MISSING_LOSS_DATE: "the date of the loss", MISSING_DESCRIPTION: "what happened",
                  MISSING_PRICED_DOCUMENT: "an invoice or repair quote with an amount (§2.5)",
                  MISSING_POLICE_REPORT: "a police report number (§2.4)"}
        flags.append("missing_info")
        return _finish(facts, "request_info", None,
                       "Claim cannot be assessed yet; missing " + ", ".join(labels[m] for m in missing) + ".",
                       flags, trace, "info_request", missing)
    assert loss is not None and amount is not None and facts.loss_type is not None

    # Rule 6: coverage.
    active, why = _active_on(policy, loss)
    trace.append(RuleStep("coverage.active", active, why))
    if not active:
        flags.append("policy_not_active")
        return _finish(facts, "reject_draft", None, why[0].upper() + why[1:] + "; no cover. Draft held for a handler.",
                       flags, trace, "rejection_draft")
    covered = _peril_covered(facts.loss_type, policy)
    trace.append(RuleStep("coverage.peril", covered,
                          f"{facts.loss_type} {'in' if covered else 'not in'} covered perils {policy.covered_perils}"))
    if not covered:
        flags.append("peril_not_covered")
        return _finish(facts, "reject_draft", None,
                       f"§1: {facts.loss_type} is not a covered peril for {policy.product} policy {policy.policy_number} "
                       f"(covered: {', '.join(policy.covered_perils)}). Draft held for a handler.",
                       flags, trace, "rejection_draft")
    trace.append(RuleStep("coverage.sudden", not facts.gradual_damage.value,
                          facts.gradual_damage.evidence or "no sign of gradual damage"))
    if facts.gradual_damage.value:
        flags.append("gradual_damage")
        ev = f" (\"{facts.gradual_damage.evidence[:100]}\")" if facts.gradual_damage.evidence else ""
        return _finish(facts, "reject_draft", None,
                       f"§2.2: the damage appears gradual rather than sudden{ev}, which is excluded. Draft held for a "
                       "handler to confirm.", flags, trace, "rejection_draft")
    if policy.product == "contents":
        trace.append(RuleStep("coverage.europe", not facts.loss_outside_europe.value,
                              facts.loss_outside_europe.evidence or "loss in Europe"))
        if facts.loss_outside_europe.value:
            flags.append("outside_europe")
            return _finish(facts, "reject_draft", None,
                           "§1: contents cover applies in Europe only and the loss happened outside Europe"
                           + (f" (\"{facts.loss_outside_europe.evidence[:80]}\")" if facts.loss_outside_europe.evidence else "")
                           + ". Draft held for a handler.", flags, trace, "rejection_draft")
        trace.append(RuleStep("coverage.accidental", not facts.accidental_own_damage.value,
                              facts.accidental_own_damage.evidence or "no accidental own damage"))
        if facts.accidental_own_damage.value:
            flags.append("accidental_damage_not_covered")
            return _finish(facts, "reject_draft", None,
                           "§1: contents policies have no accidental damage cover (drops, spills)"
                           + (f"; \"{facts.accidental_own_damage.evidence[:80]}\"" if facts.accidental_own_damage.evidence else "")
                           + ". Draft held for a handler.", flags, trace, "rejection_draft")

    # Rule 7: amount and net payout.
    net, deductible = net_payout(amount, policy, facts.loss_type)
    capped = amount > policy.max_payout
    calc = (f"{amount_kind} {_eur(amount)}" + (f" capped at {_eur(policy.max_payout)}" if capped else "")
            + f" minus {'glass ' if facts.loss_type == 'glass' else ''}deductible {_eur(deductible)} = {_eur(net)}")
    trace.append(RuleStep("amount.net", True, calc))
    notes: List[str] = []
    if facts.amount_in_email is not None and abs(facts.amount_in_email - amount) > 0.01:
        flags.append("amount_mismatch_email_vs_document")
        notes.append(f"email states {_eur(facts.amount_in_email)} but the document says {_eur(amount)}; per §2.6 the "
                     "document counts (read as anticipated by the rulebook, not as a §4 contradiction)")
        trace.append(RuleStep("amount.email_vs_document", False, notes[-1]))

    # Rule 8: soft checks that block automatic approval.
    blocks: List[str] = []
    rec_s = received_at or today
    rec = _d(rec_s)
    days = (rec - loss).days
    late = days > LATE_DAYS
    trace.append(RuleStep("soft.late_report", not late,
                          f"reported {days} days after the loss ({'received ' + rec_s[:10] if received_at else 'run date ' + today + ' used, no received date'})"))
    if late:
        flags.append("late_report")
        blocks.append(f"reported {days} days after the loss, beyond the §2.3 30-day guideline (interpreted as a "
                      "reason for a handler to look, not as a rejection)")
    no_photos = facts.loss_type not in THEFT_TYPES and amount > PHOTO_THRESHOLD and facts.photo_count == 0
    trace.append(RuleStep("soft.photos", not no_photos, f"{facts.photo_count} photo(s), document amount {_eur(amount)}"))
    if no_photos:
        flags.append("photos_missing")
        blocks.append(f"no photos for damage of {_eur(amount)} (§2.5 expects photos above EUR 500)")
    if res.how == "email":
        flags.append("policy_resolved_by_email")
        blocks.append(f"policy {policy.policy_number} was found only via the sender's email address")
    if res.number_mismatch:
        flags.append("policy_number_mismatch")
        blocks.append(f"the email names policy {facts.policy_number or '(none)'}, which does not match "
                      f"{policy.policy_number}")
    trace.append(RuleStep("soft.policy_match", res.how == "number" and not res.number_mismatch,
                          f"resolved by {res.how}, number mismatch {res.number_mismatch}"))

    # Rule 9: decision limits (§3).
    if net > SENIOR_LIMIT:
        action, band = "escalate_senior", f"net {_eur(net)} is above EUR 5,000, so §3 requires a senior handler"
    elif net > AUTO_LIMIT:
        action, band = "escalate_handler", f"net {_eur(net)} is above EUR 1,000, so §3 requires a claims handler"
    elif blocks:
        action, band = "escalate_handler", f"net {_eur(net)} is within the automatic limit, but automatic approval is blocked"
    else:
        action, band = "auto_approve", f"net {_eur(net)} is within the §3 automatic limit of EUR 1,000 with documents complete and no risk indicators"
    trace.append(RuleStep("routing.band", action == "auto_approve", band))

    reason = f"Covered {facts.loss_type} on {facts.loss_date}: {calc}; {band}."
    if blocks:
        reason += " Flags: " + "; ".join(blocks) + "."
    if notes:
        reason += " Note: " + "; ".join(notes) + "."
    letter = "approval" if action == "auto_approve" else "acknowledgement"
    return _finish(facts, action, round(net, 2), reason, flags, trace, letter)
