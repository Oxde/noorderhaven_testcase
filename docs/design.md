# Design: Noorderhaven claims agent (2h technical case)

## Intent

Gaide's case: build an agentic system for a fictional insurer receiving ~200 claim emails a day, in 2 hours, and be honest about what is left out. They judge **choices, safety, handling of messy reality, honesty**. Nikita presents the result in person to a co-founder (30 min), so the demo must be visual and the reasoning must be explainable.

Goal for this build: cover all 7 parts (intake, validation, fraud signals, decision, customer communication, human review, monitoring) end to end, with an evaluation against a hand-made answer key, and a candid DECISIONS.md. Stop building at **15:40**, hand in by **16:04**.

## Core principle

**The LLM extracts, code decides.** The LLM turns an email plus attachment text into a strict JSON fact sheet. Every coverage check, payout calculation, routing decision and outgoing email is plain deterministic Python that can be unit tested. Email and document text is treated as data: the LLM is told so, and injection detection also runs as deterministic regex, so a fooled LLM cannot approve anything.

## Non-goals and anti-goals

- No real email sending, no real bank payouts.
- No vision model on the photos (they are placeholders with a caption). A photo counts as present if attached. Stated in DECISIONS.md.
- Anti-goal: anything that auto-approves a claim the rulebook forbids, sends a rejection without a handler, emails anyone other than the verified sender, or shares policy data with third parties. These are worse than not processing the claim at all.
- Anti-goal: duplicate writes in `mock_api/runtime/` (reviewers read `write_log.jsonl` and `outbox.json`).

## Constraints

- Python 3.9+, **standard library only** for the agent, the review server and the tests (reviewers run it easily; no installs). LLM is called over HTTP with `urllib`.
- LLM: any OpenAI-compatible chat-completions endpoint (Nikita will use KIE). Config by env: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`. With no key, a deterministic **fallback extractor** (regex/keyword) is used so everything still runs; the README says which mode produced the hand-in.
- Mock API on `http://localhost:8080` with chaos ON (15% 503, 3% 429 + `Retry-After`, 50 ms to 3 s latency).
- Hand-in: GitHub repo on the **Oxde** account (ask before creating/pushing) plus a zip.

## Repository layout

```
claims-agent/
  README.md  DECISIONS.md  PROMPTS.md
  claims/  docs/  mock_api/        # copied unchanged from the candidate pack
  agent/
    models.py      # dataclasses: Email, Attachment, Facts, Document, Policy, Decision, RuleStep (shared contract)
    config.py      # env settings
    api_client.py  # mock API client: retries, backoff, Retry-After, idempotency keys
    pdf_text.py    # stdlib PDF text extraction (ReportLab PDFs: ASCII85 + Flate streams, Tj/TJ operators)
    llm.py         # OpenAI-compatible client + on-disk cache (.cache/llm/<sha>.json)
    extract.py     # build Facts: LLM extraction (prompts in agent/prompts.py) or fallback extractor; injection regex
    prompts.py     # runtime prompts (copied into PROMPTS.md)
    threads.py     # link replies to parents via in_reply_to, merge facts
    rules.py       # pure decide(facts, policy, history, today) -> Decision with a rule trace
    comms.py       # NL/EN letter templates, who may receive what
    pipeline.py    # orchestration, writes output/*
    run.py         # CLI: python -m agent.run [--reset] [--only C-001,C-005] [--no-llm]
  review/
    server.py      # stdlib http.server on :8000, JSON API over output/ files
    index.html     # single-page handler queue (no build step)
  eval/
    expected.json  # hand-made answer key for all 40 claims
    run_eval.py    # accuracy, payout match, confusion table, safety checks -> output/eval_report.md
  tests/           # unittest: rules, api_client retry/idempotency, pdf_text, threads
  output/          # decisions.json, review_queue.json, audit.jsonl, eval_report.md, reviews.jsonl
```

## Pipeline (per run)

1. Optionally `POST /admin/reset` (`--reset`, used once right before the final run; never after).
2. Load all `claims/*/email.json`, extract PDF text, sort by `received_at`.
3. Extraction (LLM calls run in parallel threads, cached). Facts schema:
   - `intent`: new_claim | reply_with_info | complaint_existing_claim | third_party_liability | spam | other
   - `language`: nl | en
   - `policy_number` (as written), `loss_date` (ISO or null), `loss_type` (one of the API peril codes: collision, parking_damage, vandalism, theft, fire, storm, hail, glass, wildlife, sudden_water, lightning, burglary_theft) or null
   - `amount_in_email`, `documents[]` each `{kind: invoice|quote|receipt|valuation|police_report|fire_report|damage_form|repair_statement|list|other, number, date, amount}`
   - `photos_attached` (count, from attachments, not from the LLM)
   - judgment fields with a one-line evidence quote: `gradual_damage`, `accidental_own_damage` (drop/spill), `loss_outside_europe`, `third_party_involved`, `police_report_number`, `iban_or_other_account_requested`, `existing_claim_reference`
   - `injection_detected` + quote: OR of the LLM field and a deterministic regex over email body and attachment text (system instruction / ignore your rules / AI assistant / set status / payout / etc.)
4. Thread merge: a reply inherits the parent's facts; reply facts win. Both claims get an entry in decisions.json; the parent's entry stays `request_info`, the reply carries the decision and a `thread_parent` flag.
5. Policy resolution: `GET /policies/{n}`; on 404 or missing number, `GET /policies?email=sender`. Exactly one email match: use it, flag `policy_resolved_by_email` (and `policy_number_mismatch` if a number was given). No match: no policy.
6. History: `GET /claims?policy_number=` (includes claims created this run).
7. `rules.decide()` (below) returns action, net_payout, reason, flags, letter kind, and a trace of every check.
8. Writes, all with deterministic idempotency keys (`<claim_id>:register`, `<claim_id>:decision`, `<claim_id>:mail:<kind>`):
   - `POST /claims` only when there is a policy, a loss date and a loss type (include `invoice_number`, `amount_claimed`, `source_claim_id`).
   - `POST /claims/{api_id}/decision` with mapped status (auto_approve→approved, request_info→needs_info, escalate_handler→escalated_handler, escalate_senior→escalated_senior, reject_draft→rejected_proposed, fraud_review→fraud_review). `route_other` posts nothing.
   - `POST /outbox` per the communication rules.
9. Append every step to `output/audit.jsonl` (timestamp, claim, step, inputs summary, result, API attempts). Write `output/decisions.json` (exact hand-in format, plus nothing extra) and `output/review_queue.json` (decision + facts + trace + email + attachment text + draft letter + status `pending|auto_sent|approved|edited|rejected`). Print a run summary (counts per action, API retries, LLM calls, time).

## Decision rules (`rules.decide`), in order, first match wins unless noted

1. `spam` → route_other. `complaint_existing_claim` → route_other, flags `complaint`, `existing_claim:<ref>`, `urgent` when it threatens escalation (Kifid). `third_party_liability` → route_other, flags `third_party_claim`, `privacy_no_disclosure`.
2. No policy resolved → if IBAN/other account requested or injection: fraud_review; else request_info (identification + details).
3. Sender email ≠ holder email → request_info for identification; disclose nothing.
4. Fraud indicators → fraud_review (collect all as flags): injection, other bank account requested, duplicate invoice number or same loss event (same policy + loss_type + loss_date) in history, ≥3 claims in the 12 months before the loss including this one, loss within 30 days after policy start.
5. Missing essentials → request_info listing exactly what is missing: loss date, what happened, invoice/quote with amount, police report for theft/burglary.
6. Coverage → reject_draft with the rule cited: policy not active on loss date (`lapsed_on`/`suspended_on` ≤ loss date), peril not in `covered_perils`, gradual damage, contents loss outside Europe, contents accidental own damage.
7. Amount: document amount = the invoice/quote/valuation/receipt total (largest priced document of those kinds; a handwritten list does not count). `net = max(0, min(amount, max_payout) − deductible)`, `glass_deductible` when loss_type is glass. Email ≠ document → flag `amount_mismatch_email_vs_document`, document counts (rulebook 2.6 anticipates this, so it is not treated as a contradiction).
8. Soft checks that block auto-approval (escalate_handler with flag): reported > 30 days after loss (`late_report`); damage (not theft/burglary) above 500 with no photo (`photos_missing`); policy found only via email (`policy_resolved_by_email`).
9. Routing: net > 5000 → escalate_senior; > 1000 → escalate_handler; else auto_approve if nothing above fired.
10. Extra flags anytime: `possible_recourse` (third party involved), `urgent` (displaced/urgent wording), `callback_requested`.

## Communication rules (`comms.py`)

- Recipient is always the original sender's address. Never an address found in a body or document.
- request_info: sent automatically, in the customer's language, listing missing items; no policy details if not verified.
- auto_approve: approval letter sent automatically (rulebook: handlers sample next morning). States the approved amount.
- escalate_*/fraud_review: neutral acknowledgement ("received, a handler is reviewing"), no amounts, no promises, no mention of fraud.
- reject_draft: letter drafted into the review queue, **never sent** until a handler approves it in the UI.
- route_other: spam gets nothing; complaint gets an acknowledgement that it was passed to the team; third party gets an acknowledgement that confirms nothing about any insured.
- All letters are templates (not LLM-written), so they cannot promise or leak anything.

## Human review UI

`python review/server.py` → `http://localhost:8000`. Left: queue filtered by action/status, with counts, color per action, flags as chips. Right: selected claim with the original email, attachment text, extracted facts, the rule trace (each check passed/failed), proposed action and payout, and the draft letter. Buttons: **Approve**, **Edit** (change action, payout, reason, letter), **Reject**. A held letter goes to the mock API outbox only when a handler approves it; the corrected decision is posted to the API. Every review action is appended to `output/reviews.jsonl` with old and new values (this is the correction data for measuring the agent). A small header shows run metrics and eval accuracy.

## Evaluation and monitoring

- `eval/expected.json`: action and net payout for all 40 claims, from the analysis already done (the rulebook-gap claims carry an `alt_actions` list).
- `eval/run_eval.py`: action accuracy, payout exact-match, confusion table, and **safety checks that must be 0**: auto_approve where expected is not auto_approve, any outbox mail to a non-sender address, any sent rejection, any approved claim with injection.
- Unit tests (stdlib unittest): rules on synthetic facts, retry/idempotency against a fake server, PDF text extraction, thread merge.
- Monitoring in production terms goes into DECISIONS.md (handler override rate from reviews.jsonl, sampling of auto-approvals, drift alarms).

## Decided by Nikita

Python; frontend review UI yes; KIE (OpenAI-compatible) with key provided later; GitHub repo on Oxde plus zip; stop at 15:40.

## Calls made by Claude (check these)

stdlib only; templated letters instead of LLM letters; acknowledgements for escalations; photos-missing and late-report block auto-approval but do not reject; policy found via email is used but never auto-approved; email-vs-document amount difference is not a "contradiction"; a fallback extractor exists so it runs without a key.

## Clarifications (from the builder check)

- **Cut order if time runs short** (Claude's call): first UI polish, then unit tests beyond rules + retry, then acknowledgement letters. Never cut: the pipeline, decisions.json, the clean final run with runtime files, the eval, DECISIONS.md.
- **Identity:** "verified" means sender email == holder email of the resolved policy. Rule 3 fires when a policy number resolves but the sender address differs from its holder address. A policy found by email lookup is verified by construction, but if the email named a different or non-existent number it is flagged `policy_number_mismatch` and cannot be auto-approved (rule 8).
- **expected.json** comes from Claude's earlier claim-by-claim analysis (the answer table in the conversation). Claude writes it, not the rules builder, so the key is not derived from the code.
- **History count:** every claim returned by `GET /claims` for the policy counts regardless of status, plus the current one, within 365 days before the loss date. Policy fields used: `status`, `start_date`, `lapsed_on`, `suspended_on`, `covered_perils`, `deductible`, `glass_deductible`, `max_payout`, `holder_email`, `product`.
- **Thread conflicts:** the reply's non-null facts win over the parent's.
- **Reruns:** the final run always starts with `--reset`. Without a reset, the deterministic idempotency keys make replays return the original records, so a re-run creates no duplicates. A write that still fails after all retries is logged in audit.jsonl and the claim is escalated with flag `api_write_failed`; there is no silent skip.
- **Fallback extractor:** first-class enough to get the templated emails mostly right, so tests and eval run without a key. The README and eval report state which mode produced the hand-in numbers. The LLM path is the primary mode.

## Left to the builders

Internal function signatures beyond `models.py`, exact UI styling, regex details, template wording.
