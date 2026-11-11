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

## Integration, test and hand-in

_(filled in as it happens)_
