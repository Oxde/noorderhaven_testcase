# Decisions

## What was built

An end-to-end slice across all seven parts. It runs on the real KIE.ai LLM with the mock API's chaos on.

| Part | Built | How strong |
|---|---|---|
| 01 Intake | LLM fact extraction into a validated schema; stdlib PDF text; deterministic fallback extractor; injection regex | strong |
| 02 Validation | policy by number, else by sender email; active on loss date; peril covered; missing info; identity check | strong |
| 03 Fraud signals | duplicate invoice or event, 3+ claims in 12 months, loss within 30 days of start, other IBAN, instructions to the system | strong for the listed indicators only |
| 04 Decision | net payout, auto / handler / senior bands, reason citing the rulebook section, rule-by-rule trace | strong |
| 05 Communication | NL/EN templates; info requests and auto-approval letters sent; neutral acknowledgements; rejections held (customer-facing reason in the customer's language); replies merged and the original request marked answered; open info requests listed with reminder drafts | medium |
| 06 Human review | local web page: queue, filters, rule trace, facts, email, draft letter; approve / edit / reject; releases held letters; every action logged | medium–strong |
| Extra: dry-run tester | "Try an email" tab in the review UI + `python3 -m agent.try`: one email through extraction, policy lookup, rules and letter drafting; API writes are blocked in code (`ReadOnlyApiClient` raises), nothing written to `output/` | medium |
| 07 Monitoring | audit trail per step, run summary, `metrics.json` + Monitoring view (automation rate, API retry rate, LLM-vs-keyword agreement, handler override rate, open requests, alerts), eval with 5 safety checks, blind holdout, robustness runs | medium–strong |

**Final run** (15:20:52–15:22:27, chaos on, final code; this is the run the `mock_api/runtime/` files and `output/` come from, and the server was not reset or restarted afterwards). LLM answers were replayed from the cache recorded by the live run at 15:14–15:16 (40 real KIE calls with deepseek-v4-1-flash, 1.19 credits, same prompt and model chain), because the code changes in between did not touch extraction:
- Actions: auto_approve 9, request_info 4, escalate_handler 8, escalate_senior 3, reject_draft 7, fraud_review 6, route_other 3.
- Mock API: 170 calls, 31 retries, **0 failed writes**, no duplicate records.
- Eval on the 40 case claims: **100% action match, 100% payout match, all 5 safety checks at 0**.
- Read that 100% carefully. The key was written before the code, but two rules were adjusted after the first eval on these same 40 claims (C-033 and the exclusion order, see below). The first untuned run scored 97.5%.
- **That is why a blind holdout was added:** 12 new, deliberately tricky claims written by an agent that never saw the code, with expected answers from the rulebook alone (`eval/holdout/`). Result: **91.7% strict (11/12), 100% counting the author's accepted alternatives, payouts 5/5, safety 0.** The one strict miss (H-009, no photos above €500) went to a handler where the author preferred asking the customer for photos. A reworded Dutch injection, a reused invoice number, an identity mismatch and a loss abroad were all caught.
- **Robustness** (heavy API failure, KIE down, idempotent re-run, no-LLM mode): see `docs/robustness.md`.

## Core design choice

**The LLM extracts, code decides.** One LLM call per email returns a fact sheet. Everything after that is deterministic Python: coverage, payout, routing, who gets what letter. Reasons:

- **Safety:** an email that talks the LLM into anything can at most produce wrong *facts*. It cannot approve, change a payout or send data. Injection is also caught by a regex that does not depend on the LLM. C-005 and C-020 both go to fraud review.
- **Correctness you can test:** 35 unit tests on the rules alone. Each decision carries a trace a handler can read.
- **Cost and speed:** about 0.03 credits and 3–5 s per claim on `deepseek-v4-1-flash`.

## How the rulebook gaps were read (assumptions)

| Gap | Reading | Claims |
|---|---|---|
| Email amount ≠ document amount: is that a §4 "contradiction"? | No. §2.6 explicitly says the document counts, so it is expected, not suspicious. Flagged `amount_mismatch_email_vs_document`; auto-approval still allowed. | C-004 |
| "Photos are expected" above 500 (§2.5) | Missing photos block auto-approval (escalate_handler with `photos_missing`) but never reject; a handler can waive. Not applied to theft, where there is nothing to photograph. | C-025, C-031, C-034 |
| Late report (§2.3, "in principle 30 days") | More than 30 days blocks auto-approval (handler), never rejects. | C-034 |
| "Sudden" (§2.2) | Damage described as developing over weeks or months (damp, mould, slow leak) is gradual: rejection drafted, held for a handler. | C-019, C-024 |
| A clear exclusion but missing info | Exclusions that need no missing fact (peril not covered, gradual, outside Europe, accidental own damage on contents) are decided before asking for missing info. Asking a customer for an invoice we will reject anyway wastes their time. | C-010, C-019, C-024 |
| Policy number wrong or missing | Look it up by sender email. If exactly one policy matches, that sender is verified (§5). A claim resolved this way is never auto-approved. | C-003, C-015, C-017, C-023, C-036 |
| Vague email from an unknown sender | Ask for identification (§5), not triage. | C-033 |
| Theft vs burglary (motor `theft`, contents `burglary_theft`) | The same peril family for the cover check. | C-005, C-022 |
| Escalated claims: tell the customer anything? | Yes, a neutral acknowledgement with no amount, no promise and no fraud wording. §6 restricts *decision* letters, and this is not one. | escalations, fraud_review |
| Auto-approval letter | Sent immediately (§6: handlers sample next morning). | 9 claims |
| Third-party liability claim (C-038) | route_other, neutral acknowledgement confirming nothing about the insured (§5). | C-038 |
| Complaint about an existing claim (C-026) | route_other with `complaint` and `urgent` (Kifid threat); acknowledgement sent; no new claim registered. | C-026 |
| The LLM is down (all models fail) | **Nikita's decision:** no automatic decision at all. Every claim goes to escalate_handler with `llm_unavailable` and `facts_unverified`, no payout, no letter; the keyword fallback's facts are only a pre-fill for the handler. A keyword reader can misread a fact silently, and acting on it would be misleading. (`--no-llm` remains a labelled developer/test mode that does decide.) | any, when KIE fails |
| A reply from a different sender than the original | Not linked to that thread (an In-Reply-To header is trivially forged); processed on its own. | — |

## What was cut, and why

- **Vision on photos.** The photos are captioned placeholders, so we only count them. A vision model would add cost and risk for no signal on this data.
- **A real mailbox and automatic reminders.** Replies are merged and the original request is marked answered; open requests are listed with reminder letters *drafted*, but nothing polls an inbox and reminders are not sent automatically (the reminder cadence is a business decision). Still the weakest part (05).
- **LLM-written letters.** A deliberate choice: templates cannot promise money or leak data.
- **Logins and multiple handlers.** The review page is single-user and local. Fine for a demo, not for production.
- **A database.** State is in JSON files; the API's in-memory store is the system of record.
- **Scale infrastructure** (queues, workers, persistent retry queue), described under "200 a day" below.

## What is fake, stubbed or untested

- **Fake by design:** no email is really sent (mock outbox). No money moves. Policies and claims are the mock API's.
- **Review page:** no automated browser test. Checked by hand in a browser; its API, CSRF guard and recipient check were tested with curl; an end-to-end test on an isolated copy approved a held rejection (exactly one mail queued, to the original sender) and posted a handler's edit to the API (recorded as `decided_by: human:…`).
- **Untested live:** the chain reaching `gemini-3-7-flash` or `claude-sonnet-5-5` (the deepseek → gpt switch was tested live; the retry-then-next-model logic, including schema failures, is unit tested).
- **KIE down:** every claim goes to manual review by design (see the assumptions table); tested in docs/robustness.md.
- **Known edge:** if a decision is posted as `approved` but a later write for that claim fails, `decisions.json` downgrades to escalate_handler while the API record stays `approved`. No corrected decision is posted. This did not happen in the final run (0 failed writes).
- **LLM cache:** responses are cached in `.cache/llm/` (not committed). The final run replayed answers recorded live 6 minutes earlier with the same prompt and chain. A fresh clone always makes real KIE calls.
- **Cost of the last-resort model:** `claude-sonnet-5-5` costs about 0.75 credits per extraction (the prompt is long), about 25x deepseek's 0.03. It is only reached when the three cheap models fail; failed calls are not charged. The reviewer saw transient HTTP 500s from Sonnet on KIE that succeeded on a later retry.

## Where this could cause harm, and how it is limited

| Risk | Limit |
|---|---|
| Wrong automatic payout | Only net ≤ €1,000 with complete documents, a verified sender, a policy found by number, and no risk indicator, no late report, no missing photos. Handlers sample these. Eval safety check: 0 false auto-approvals. |
| Prompt injection | The LLM decides nothing; regex plus LLM detection sends the claim to fraud review; the review page shows the passage with a warning. |
| Data leak | Mail only ever goes to the original sender (enforced twice, in pipeline and review server). Identity requests and acknowledgements carry no policy data. Third parties get nothing. Cross-sender thread links refused. |
| Rejection sent without a human | Rejections are only ever drafts (`send_now: false`); the review page must approve them. Eval safety check: 0. |
| Duplicate claims or mails from retries | Deterministic `Idempotency-Key` on every write. The write log shows each claim and decision once. |
| Review page abused from another website | JSON-only POSTs, Origin and Host checks (CSRF and DNS rebinding). |

## What breaks first at 200 claims a day

1. **LLM rate limit and latency.** KIE allows 20 requests per 10 s, and upstream 429/500/524 errors happen. With 4 parallel extractions this is fine for 200 a day, but a burst of 200 at 09:00 queues for minutes. Next step: a persistent work queue with per-provider token buckets.
2. **The flaky policy API.** About 20% of calls retry. That is fine, but writes need a durable outbox so a crash mid-run resumes rather than restarts (idempotency keys already make a restart safe).
3. **Handler capacity.** On this data roughly 65% of claims need a human. The value comes from tuning *what* is safe to automate, using the review log.
4. **JSON files as state.** Concurrent handlers will clash. Move to a database.

## How to know the decisions are correct, and monitor them

- **Before go-live:** `eval/run_eval.py` against a labelled set, with the safety checks as hard gates in CI. Grow the set from real handled claims.
- **In production:** `output/reviews.jsonl` records every handler approve, edit or reject with before and after values. Track the **override rate per action and per rule**, the **auto-approval sample error rate**, the **LLM extraction disagreement rate** (LLM vs fallback on the same email is a cheap drift signal), and **API retry and failure rates** from `run_summary.json`. Alert when any moves.

## Next steps, in order

0. A persistent per-claim ledger (or reading decision state back from the API) consulted before deciding, so a re-run never auto-approves a claim an earlier run left for a handler after a failed write (robustness finding 1; only partly mitigated by `api_state_mismatch`).

1. Reminder flow for request_info, with real inbox threading by Message-ID and sender (part 05).
2. Persistent queue, a database for state, and multi-user review with logins (scale and part 06).
3. A larger labelled eval set from handled claims; CI gate on the safety checks.
4. An LLM second-opinion check on the "gradual" and "outside Europe" judgments where evidence is thin, shown to the handler, never deciding.
5. A vision check on real photos (damage present, matches description).

## Time log (case received 14:04, due 16:04)

| Time | What |
|---|---|
| 14:04–14:15 | Read the briefing, rulebook, API and all 40 claims; wrote the answer key and key insights |
| 14:15–14:25 | Brainstorm: scope, stack, KIE, repo; design doc plus reviewer check; skills from skills.sh |
| 14:25–14:30 | Shared contract (`models.py`), build plan |
| 14:30–14:43 | Five parallel builder agents; lead verified, committed and relayed interfaces; fixed 2 security findings and 1 rule-order issue |
| 14:43–14:50 | Integration: full run on KIE with chaos (97.5%), fixed C-033, rerun (100%), checked the review page in a browser |
| 14:50–14:52 | Documentation (README, DECISIONS, PROMPTS, PROCESS), first zip |
| 14:52–15:10 | Strengthening (Nikita's choice), 4 more agents in parallel: blind holdout set, robustness runs, part 05 thread closing + open requests, part 07 metrics + monitoring view. Nikita's rule: AI down → manual review. Review page end-to-end test |
| 15:10–15:16 | Independent reviewer findings fixed (fallback could still decide on no key or extraction errors; validation inside the retry loop + Sonnet as last model; thread provenance; cross-type duplicates; Dutch rejection reasons). Fresh final run with live KIE calls |
| 15:16–15:22 | Holdout re-run on final code (unchanged result); robustness results; fixed 2 robustness bugs plus a replay-mismatch guard; final run 15:20:52–15:22:27 |
| 15:22–15:40 | *(plan)* docs update, final checks |
| 15:40–16:04 | *(plan)* zip, and push to GitHub (Oxde) once the repo exists |

## Independent review

A separate Claude session reviewed the code and the holdout behaviour, and found that the keyword fallback could still make automatic decisions on two paths: a missing API key, and an extraction error outside the LLM retry loop. On unseen emails the fallback had auto-approved a reworded Dutch injection and an IBAN-change request that the LLM correctly sent to fraud review. Fixed (commit `193a1ac`): any claim not read by the LLM is escalated in normal mode; schema validation runs inside the retry loop, with `claude-sonnet-5-5` as a stronger last model (Nikita's idea); merged threads keep the weaker provenance; duplicate events match policy + date regardless of loss type; rejection letters give the customer a reason in their own language. Each fix has a test (`tests/test_fallback_safety.py`).

## How AI tools were used

Claude Code (Claude Opus 5.5) was the lead engineer and orchestrator; six subagents did the parallel work (see [PROCESS.md](PROCESS.md)). A separate session tested KIE models live. What was checked or corrected by hand, not just accepted:

- The **answer key** was written from the rulebook before any code, so the eval is independent of the implementation.
- Every subagent's tests were **re-run by the lead** before committing; nothing was committed unseen.
- **Security fixes after automated review:** cross-sender thread linking (a forged In-Reply-To could have merged another customer's claim) and CSRF/DNS rebinding on the review server.
- **Rule fixes after the first eval:** exclusions before missing info (C-010, C-019, C-024), and unknown-sender vague emails ask for identity (C-033).
- **Registration fix:** claims from unverified senders are no longer registered against someone else's policy.
- **Checked the prompt by hand on live output:** the extraction agent tightened it after deepseek tagged an unknown hit-and-run as "third party involved" and gpt listed a photo as a document.
