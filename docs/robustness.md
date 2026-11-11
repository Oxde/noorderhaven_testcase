# Robustness tests

Run on 2026-11-11 against a frozen snapshot of `HEAD` (commit `0681124`; scenarios 4 and 5 use the newer `agent/rules.py` with the "LLM down, everything to a handler" rule). Every mock API ran from a copy of `mock_api/` on its own port, never from the repo's `mock_api/` and never on 8080. LLM answers came from the local cache (`.cache/llm`), so scenarios 1 to 3 cost no credits. Scoring: `eval/run_eval.py` against `eval/expected.json` (40 claims).

`eval/robustness.py` repeats scenarios 2, 3 and 5 on its own (standard library only, temp copy, free port): `python3 eval/robustness.py`.

## Results

| # | Scenario | Duration | Action acc. strict / lenient | Payout match | Safety checks | API calls / retries / failed | Errors | Write log: claims / decisions / outbox | Duplicates |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Baseline: chaos 0.15, LLM (cached), `--reset` | 104 s | 100% / 100% | 20/20 | all 0 | 170 / 45 / 0 | 0 | 29 / 29 / 32 | none |
| 2 | Heavy chaos: FAIL_RATE 0.5, LLM (cached), `--reset` | 227 s | 97.5% / 97.5% | 20/20 | all 0 | 170 / 173 / 1 | 1 (C-006 decision_post) | 29 / 28 / 32 | none |
| 3 | Re-run on the scenario 2 server, no reset | 165 s | 100% / 100% | 20/20 | all 0 | 169 / 165 / 0 | 0 | **new:** 0 / 1 / 1 | none |
| 4 | KIE down: bogus key, no cache, one model, chaos 0.15, `--reset` | 119 s | 20% / 25% (by design) | 0/0 | all 0 | 140 / 29 / 0 | 0 | 30 / 30 / 0 | none |
| 5 | `--no-llm`, chaos 0.15, `--reset` | 88 s | 100% / 100% | 20/20 | all 0 | 170 / 32 / 0 | 0 | 29 / 29 / 32 | none |

"Duplicates" means: no `source_claim_id` registered twice, no API claim with two decisions, no identical email queued twice.

## What each scenario shows

1. **Baseline.** Same result as the hand-in run: 9 auto-approved, 7 rejections held, 32 emails, 45 retries absorbed, nothing failed.
2. **Heavy chaos.** About half of all calls fail (165 x 503, 9 x 429). Retries absorb all but one: the decision write for C-006 (EUR 337 windscreen, normally auto-approved) failed 8 times in a row. The pipeline handled it as designed: action downgraded from `auto_approve` to `escalate_handler`, flag `api_write_failed`, review status pending, and the customer got an acknowledgement ("a handler is reviewing") instead of an approval letter. No claim with `api_write_failed` or `api_read_failed` ended as `auto_approve`. The 2.5% accuracy loss is that one claim, and it is the safe outcome. The claim stays registered in the API without a decision until a handler finishes it.
3. **Idempotent re-run.** All 29 claim registrations, 28 decisions and 32 emails replayed from their idempotency keys: 0 new claims and no duplicates. The run did write 2 new records, both for C-006. Its decision write, which failed in scenario 2, now went through as `approved` (EUR 337), and because the letter kind changed from acknowledgement to approval, the customer got a second email: "approved, EUR 337.00". So a re-run finishes the claim the earlier run had handed to a handler (see weakness 1).
4. **KIE down.** Every model call got HTTP 401. All 40 claims fell back to the rule-based extractor (`fallback(llm_failed)`), and the new rule sent all 40 to `escalate_handler` with `llm_unavailable` + `facts_unverified`: 0 auto-approvals, 0 payouts, 0 emails, safety 0. **AI down → everything goes to manual review, by design.** The low accuracy (20% strict) is expected: only claims whose correct answer is `escalate_handler` count as matches.
5. **No-LLM mode.** The fallback extractor alone matches the answer key 100% on these 40 claims, with the same write counts as the baseline. That is the dev set it was written against, so it says little about unseen emails.

## Weaknesses found

1. **A re-run can overturn a "handler must finish this" state and send a contradictory second letter.** Idempotency keys are fixed strings per claim and step (`f"{cid}:decision"` at `agent/pipeline.py:269`, `f"{cid}:mail:{letter.kind}"` at `agent/pipeline.py:283`, `f"{cid}:register"` at `agent/pipeline.py:338`). They hold no state from the earlier run. In scenario 3, C-006 went from "pending with a handler, customer told a handler is reviewing" to "auto-approved, approval email sent" with no human involved. Related risk, read from `mock_api/server.py` (not triggered here): the decision key ignores the payload. If a re-run computes a different decision (after a rules change, or a different LLM answer), the server replays the old record with `Idempotent-Replay: true`, the client does not check that header (`agent/api_client.py`), and `output/decisions.json` then disagrees with what the API holds. Suggested fix: before writing, read the claim's current state (or keep a local ledger of the last run's outcome per claim) and never auto-approve a claim that an earlier run left pending with `api_write_failed`. Put a hash of status + payout in the decision key, or compare the replayed record with the payload and flag a mismatch.
2. **The LLM client retries errors that cannot succeed.** `agent/llm.py:244-250` retries every `_AttemptError` up to `MAX_ATTEMPTS = 3` (line 21) per model, including HTTP 401. With a bad key, scenario 4 made 120 calls for 40 claims (one model). With the default three-model chain that would be 9 failing calls per claim, plus backoff sleeps. Suggested fix: do not retry 400/401/403. Move to the next model at once, and after an auth error with the shared KIE key, mark the client as down so the remaining claims go straight to the fallback.
3. **When the LLM is down, non-claim emails are registered as claims.** With every claim forced to `escalate_handler`, the registration branch (`agent/pipeline.py:326-338`, which registers any action other than `route_other` when policy, loss date and loss type are present) registered C-026, a complaint about an existing claim (expected `route_other`), as a new API claim. That is 30 registrations against 29 in the other runs. Suggested fix: skip registration when `llm_unavailable` is in the flags, or register only when the fallback extractor's intent is a new claim, and leave registration to the handler.
4. **The fallback extractor's 100% is measured on the dev set.** It is not evidence that it would hold up on new emails. Scenario 4 avoids relying on it for decisions, which is the right default.
5. **Mock limitation, not an agent bug:** the mock API keeps idempotency keys in memory only. Restarting the server and re-running without `--reset` would create duplicates. A real API must persist keys.


## Fixes applied after these runs (lead, 15:20)

| Finding | Status |
|---|---|
| 2. LLM client retried 401/403/400, which cannot succeed | **Fixed** in `agent/llm.py`: auth and bad-request errors skip straight to the next model (test `test_auth_error_is_not_retried`). |
| 3. With the LLM down, a non-claim (C-026) was registered as an API claim | **Fixed** in `agent/pipeline.py`: no API registration when `llm_unavailable` is flagged. |
| 1. A re-run after a failed write can auto-approve a claim that the earlier run left for a handler; an idempotent replay can silently differ from the new decision | **Partly fixed:** if the API replays a different earlier decision for the same key, the claim gets `api_state_mismatch` and cannot be auto-approved. **Still open:** a re-run does not check whether an earlier run left the claim with `api_write_failed`. The proper fix is a persistent ledger of per-claim outcomes (or reading the decision state back from the API) consulted before deciding. It is listed as a next step in DECISIONS.md. |
| 4. The fallback's 100% only covers the dev set | Accepted, and moot in normal mode: the fallback no longer decides anything unless `--no-llm` is passed explicitly. |
| 5. The mock keeps idempotency keys in memory | Mock limitation; a real API must persist them. |
