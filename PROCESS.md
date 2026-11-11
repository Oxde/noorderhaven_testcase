# How this was built: process log

A record of how the 2-hour case was approached, in order. Times are local (Amsterdam), Wednesday 2026-11-11. The case arrived at 14:04 and was due at 16:04.

## 14:05–14:15 · Read and understand the case

- Read the briefing slides, the candidate pack README, `docs/policy.md`, `docs/api.md`, `mock_api/server.py`, the policy and claim-history data, and all 40 claim emails with their PDF text.
- Wrote a claim-by-claim answer key for all 40 claims (expected action and net payout, with the reasoning and the rulebook gaps). It became `eval/expected.json`. It was written *before* any code, so the eval is not derived from the rules engine.
- Key insights noted at this stage:
  - **The LLM extracts, code decides.** That is the main safety property.
  - There are **prompt-injection traps**: C-005 (email body) and C-020 (inside the PDF).
  - The **mock API is flaky on purpose** (15% 503, 429 with Retry-After, up to 3 s latency). Retries and idempotency keys are needed, because reviewers read `runtime/write_log.jsonl`.
  - **Replies** (C-017→C-003, C-027→C-013, C-036→C-023) must be merged, not registered twice.
  - **Identity and privacy**: policy lookup by email; third-party claimant C-038 gets nothing.
  - **Letter rules**: info requests are automatic; rejections are held for a handler.

## 14:15–14:25 · Brainstorm (superpowers:brainstorming)

- Question: how far can we take it in the time? Decision: aim for all 7 parts, thin but end to end, with an evaluation, and say honestly where each part is weak.
- Choices made with Nikita:
  - **Python**, standard library only, so reviewers can run it with no installs.
  - **A frontend for human review**: a single local web page, no build step.
  - **KIE.ai** as the LLM provider (OpenAI-compatible). A teammate agent tested KIE models live and documented them in `docs/kie-llm-api.md`.
  - **Hand-in** as a GitHub repo on Oxde plus a zip.
  - **Stop building at 15:40.**
- Wrote `docs/design.md` (the spec). A fresh reviewer agent read it as a builder would and raised 6 questions. The answers went into the doc's "Clarifications" section.

## 14:20–14:25 · Skills from skills.sh

Searched the skills.sh registry for 10 topics: LLM agents, OpenAI, structured output, prompt engineering, evals, PDF, Python, FastAPI, agent evaluation, insurance. Read each candidate's SKILL.md before installing. Installed three (plain markdown, no scripts):

| Skill | Source | Installs | Used for |
|---|---|---|---|
| `llm-evaluation` | wshobson/agents | 13k | eval design (accuracy, safety metrics) |
| `prompt-engineering-patterns` | wshobson/agents | 24k | extraction prompt and structured JSON output |
| `ai-prompt-engineering-safety-review` | github/awesome-copilot | 10k | hardening the runtime prompt against injection |

Skipped: FastAPI skills (we use the stdlib), a PDF skill (one was already available), and insurance skills (fewer than 1k installs, unknown authors).

## 14:25–14:30 · Architecture and plan (superpowers:writing-plans)

- Discussed the architecture and the strength of each of the 7 parts (see README, "Architecture" and "The seven parts").
- Wrote the shared contract `agent/models.py` and `agent/config.py` first, so parallel builders code against the same types.
- Wrote `docs/plan.md`: five tasks on disjoint files, each with exact interfaces and named tests.

## 14:30–15:05 · Parallel build (5 agents)

| Agent | Task | Files |
|---|---|---|
| A | API client (retries, backoff, Retry-After, idempotency), pipeline, CLI | `agent/api_client.py`, `agent/pipeline.py`, `agent/run.py` |
| B | Intake: PDF text, KIE LLM client with model fallback, extraction prompt, fallback extractor, injection regex | `agent/pdf_text.py`, `agent/llm.py`, `agent/prompts.py`, `agent/extract.py` |
| C | Decision rules, thread merge, letter templates | `agent/rules.py`, `agent/threads.py`, `agent/comms.py` |
| D | Human review web page | `review/server.py`, `review/index.html` |
| E | Evaluation harness and answer key | `eval/expected.json`, `eval/run_eval.py` |

## Decisions made by Nikita (the human in charge)

| When | Decision | Effect |
|---|---|---|
| 14:15 | Aim for all 7 parts and test them, not just a thin slice | Scope of design.md |
| 14:18 | Build a frontend for human review | `review/` web page |
| 14:18 | Python; KIE.ai as LLM provider (OpenAI-compatible); a separate session tests KIE models live | `agent/llm.py`, `docs/kie-llm-api.md`, model chain deepseek → gpt → gemini |
| 14:18 | Hand in as a GitHub repo (Oxde) plus a zip; stop building at 15:40 | Plan and time box |
| 14:19 | Search skills.sh and install useful skills | 3 skills installed after reading them |
| 14:27 | Approved the design and architecture; build with parallel agents | `docs/plan.md`, 5 builders |
| 14:52 | Use the spare time for more testing and to strengthen the weakest parts (05 communication, 07 monitoring) rather than stop early | Holdout set, robustness runs, thread closing, monitoring panel |
| 14:58 | **If the AI is down, do not let the keyword fallback decide; send every claim to manual review with the reason** | Rule 0 in `agent/rules.py`: `fallback(llm_failed)` → escalate_handler, flags `llm_unavailable`, `facts_unverified`, no payout, no letter |

The last decision overrode Claude's original design. That design let the keyword fallback make full decisions when the LLM failed. Nikita's reasoning: a keyword reader can misread a fact without knowing it, and the rules would then act on it with full confidence, which is misleading. With the AI down, the honest output is "a human must read this". The fallback's facts are still shown to the handler as an unverified pre-fill. The explicit `--no-llm` developer mode still decides, so the pipeline can be tested without a key, and is labelled as a test mode.

## How Nikita evaluated the AI's work

- **Understanding before building.** Before any code, asked Claude to explain the case and list the insights needed to show it was understood. Checked the claim-by-claim answer key, the traps (prompt injection, flaky API, replies, privacy) and the rule interpretations.
- **Explanation before approval.** Asked for the task, plan and architecture to be explained and illustrated, including which of the 7 parts would be weak and why, before approving the design.
- **Asked "did we really do all 7, and which are weak?"** after the first complete run. That surfaced 05 (communication) and 07 (monitoring) as medium, which then got strengthened.
- **Questioned a design choice and overrode it:** the fallback-on-AI-failure behaviour (above).
- **Required a written trail:** the process log, the number of subagents and how they were orchestrated, the architecture in the README, and the decisions with their reasons. The work can be audited afterwards, not just demoed.
- **Kept the human gate on outward actions:** commits and pushes only on request; creating the GitHub repo is Nikita's step.
- **Did not accept the 100% eval at face value:** chose more testing over stopping. That produced a blind holdout set written by an agent that never saw the code, plus robustness runs under heavy API failure and with the LLM down.

## How the agents were orchestrated

**Subagents used so far: 6.**

| # | Agent | Model | Role | Started | Finished |
|---|---|---|---|---|---|
| 1 | Builder check | Sonnet | Read the design as a builder would and listed the open questions | 14:22 | 14:23 |
| 2 | A: API client + pipeline | Opus | Task 1 | 14:30 | 14:42 (7 tests; 40-claim chaos smoke run, 0 errors) |
| 3 | B: Intake + KIE LLM client | Opus | Task 2 | 14:30 | 14:38 (17 tests; live KIE smoke on 4 claims) |
| 4 | C: Rules, threads, letters | Opus | Task 3 | 14:30 | 14:34 (39 tests green) |
| 5 | D: Review web page | Opus | Task 4 | 14:30 | 14:37 (checked with curl, then in a browser by the lead) |
| 6 | E: Evaluation | Opus | Task 5 | 14:30 | 14:33 (7 tests green) |

A separate Claude session tested the KIE.ai models live and wrote `docs/kie-llm-api.md`. This build used its findings: model choice, the Responses format for DeepSeek and GPT, errors inside HTTP 200 bodies, and Haiku failing on system prompts.

**Orchestration pattern: contract first, disjoint files, one integrator.**

1. **Contract before code.** The lead (main session) wrote `agent/models.py` (every shared dataclass) and `agent/config.py` before launching anyone. All five builders code against the same types, so their parts fit together without coordination.
2. **Plan with exact interfaces.** `docs/plan.md` gives each task its files, function signatures, the JSON schema that crosses task boundaries (`review_queue.json`) and named tests. Each builder only needs its own section.
3. **Disjoint file ownership.** No two agents touch the same file, so they could all run at once in the same folder without merge conflicts. Builders import their neighbours' modules lazily, so each module works and is testable before the others exist.
4. **All launched in one message**, running in the background.
5. **The lead integrates.** Builders never commit. When one reports back, the lead re-runs its tests, commits it, and relays any interface detail other agents need. For example, the rules agent added a `received_at` parameter to `decide()`, and the lead passed that to the pipeline agent while it was still working.
6. **Isolation for live systems.** Builders that start the mock API use their own port (8091) and delete `mock_api/runtime/` afterwards, so the lead's final run starts clean.

All five builders finished between 14:33 and 14:42, well inside their 15:05 deadline. Total: **6 subagents** (1 reviewer + 5 builders), plus the separate KIE testing session.

## Integration, test and hand-in

- **14:35–14:42, while builders were still working:**
  - **Fix 1, from the automated security review:** thread linking trusted the In-Reply-To header alone, so a stranger could have merged another customer's claim. Replies now only link to an email from the same sender.
  - **Fix 2, from the automated security review:** the review server accepted cross-origin POSTs, so any website open in the handler's browser could have released held letters. It now requires JSON and checks Origin and Host. Tested with curl.
  - **Fix 3, raised by the intake agent:** claims with a clear exclusion but no loss date (C-010, C-019, C-024) would have got "please send the date". Exclusions are now checked first.
  - **Fix 4, from the pipeline agent's notes:** claims from unverified senders are no longer registered against the matched policy.
- **14:43:** first full run with the real KIE LLM and chaos on. 40 claims in 127 s; 39 LLM calls, all answered by deepseek-v4-1-flash for 1.31 credits; 169 API calls, 33 retries, 0 failures.
- **14:45:** first eval: **97.5%** actions, 100% payouts, 0 safety violations. The one miss was C-033, a vague email from an unknown sender that the LLM labelled "other" and that was routed to triage. Fixed: with no matching policy it now asks for identification (§5).
- **14:47:** rerun: **100%** actions, 100% payouts, safety 0. (Caveat in DECISIONS.md: the rules were adjusted on the same 40 claims.)
- **14:48:** checked the review page in a browser with real data: the queue, the rule trace, and C-005's injection warning with its neutral Dutch acknowledgement.
- **14:50:** wrote DECISIONS.md, PROMPTS.md (runtime prompt generated from the source) and the README run guide.
