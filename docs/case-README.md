# Technical case: claims intake agent for an insurer

**Time box: 2 hours.** Then a 30 minute conversation about what you built.

## The situation

Noorderhaven Verzekeringen (fictional) receives about 200 damage claims per day by email. Each email has free text and often a PDF or photo. Handlers lose most of their day on reading, checking the policy, chasing missing documents, and writing letters. You are asked to build an agentic system that speeds this up, without taking away the control that a claims department needs.

## Read this first: you will not finish

The scope below is deliberately larger than 2 hours. Nobody finishes it, and we do not expect you to. What we look at is **what you choose to build, what you skip, and how you explain those choices**. A thin working slice that you can defend is worth more than one polished part. Fake or stub things on purpose if that helps, but say so.

## What the system could do

1. **Intake**: read the email and attachments, extract policy number, loss date, damage type, amount.
2. **Validation**: check the claim against the policy API (active, covered, deductible) and spot missing information.
3. **Fraud signals**: detect suspicious patterns (duplicates, repeat claimants, risky timing, amounts just under limits, odd instructions).
4. **Decision**: approve small clean claims automatically, escalate bigger or uncertain ones, draft a decision letter.
5. **Customer communication**: ask for missing documents and handle replies in the same thread.
6. **Human review**: a queue or small interface where a handler approves, edits or rejects what the agent proposes.
7. **Monitoring**: logging, audit trail, a way to measure whether the agent is right.

Pick your own priorities.

## What you receive

| Path | Content |
|---|---|
| `claims/C-001 ... C-040/` | 40 claim emails (`email.json`) with attachments (PDFs and synthetic photos). Dutch, English and mixed. Some are messy, incomplete, replies to earlier emails, or not what they seem |
| `docs/policy.md` | The claims rulebook. It has gaps, on purpose |
| `docs/api.md` and `mock_api/` | A mock policy and claims API (Python, no dependencies). It fails and slows down at random, like real systems |

The photos are generated placeholders with a caption, not real damage photos.

## What we ask you to hand in

1. **Your proof of concept (code)**, runnable by us. Any language, any framework, any LLM provider. Tell us in a README how to install and run it. Also include the `mock_api/runtime/` folder from your final run: the server writes every email your agent queued and every write there. Do not restart or reset the server after that final run.
2. **Your prompt list** (`PROMPTS.md`), in two parts: (a) the prompts and system prompts your agent uses at runtime, and (b) the main prompts you gave to AI coding tools while building. A list or an export is fine, it does not need to be complete.
3. **`output/decisions.json`**: one entry per claim you processed. Partial is fine, and expected. Format:

```json
[
  {
    "claim_id": "C-001",
    "action": "auto_approve",
    "net_payout": 370,
    "reason": "Covered parking damage, invoice complete, payout below 1,000.",
    "flags": []
  }
]
```

`action` is one of: `auto_approve`, `request_info`, `escalate_handler`, `escalate_senior`, `reject_draft`, `fraud_review`, `route_other`. Use `null` for `net_payout` when no amount applies. `flags` is a free list of short strings for things a human should know (for example `possible_duplicate`).

4. **`DECISIONS.md`** (short): what you built, what you cut and why, which assumptions you made where the rulebook was unclear, what is fake or untested, and what you would do next, in order. Add a rough time log (how the 2 hours were spent) and a few lines on how you used AI tools and what you checked or corrected yourself.
5. **Tests or an evaluation, if you made any**: a script, a results table, even a handful of cases. Optional, but we look for it.

## Rules of the game

- **AI coding tools are allowed and expected.** We assess your engineering judgment, not your typing speed. You are responsible for every line you hand in.
- You may use any libraries and any LLM. Bring your own API key; if that is a problem, tell us beforehand.
- Work alone. Do not ask others for help.
- Stop after 2 hours. We value an honest "this part is not done" over a README that oversells.
- The data is fictional. Do not send it anywhere you would not send test data.

## In the debrief we will ask about

Why you built X first and skipped Y. What breaks first at 200 claims per day. How you know the decisions are correct. Where the system could cause harm and how you limited it. What you would do with two more days.

Good luck, and have fun with it.
