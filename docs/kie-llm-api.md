# KIE.ai: LLM API notes

Source: https://docs.kie.ai (read 2026-11-11). Live checks were made with our key on the same day.

## Basics

- Base URL: `https://api.kie.ai`
- Auth header on every call: `Authorization: Bearer $KIE_API_KEY` (key lives in `.env` as `KIE_API_KEY`)
- One credit balance covers every model. Prices are at https://kie.ai/pricing, and each task's cost shows at https://kie.ai/logs.
- Chat models are **synchronous**: the answer comes back in the response, with no taskId or polling. (Image, video and music are async.)
- Every chat response includes `credits_consumed`. For streams it arrives in the last chunk, before `data: [DONE]`.
- Rate limit: 20 new requests per 10 s per account. Going over returns code `429`, and the request is dropped, not queued.
- Logs are kept for 2 months.
- Never call it from frontend code. Use it server-side only.

### Errors: always check the body, not just the HTTP status

Errors often come back as **HTTP 200** with an error code in the body:

```json
{"code": 401, "msg": "You do not have access permissions"}   // 402 = out of credits, 429 = rate limited
```

Some endpoints use `{"error": {"type": "...", "message": "..."}}` instead. Handle both shapes.

### Balance

```bash
curl -s https://api.kie.ai/api/v1/chat/credit -H "Authorization: Bearer $KIE_API_KEY"
# {"code":200,"msg":"success","data":6213.25}   <- data = credits left
```

### List models (free)

```bash
curl -s https://api.kie.ai/anthropic/v1/models -H "Authorization: Bearer $KIE_API_KEY"   # Claude
curl -s https://api.kie.ai/openai/v1/models    -H "Authorization: Bearer $KIE_API_KEY"   # GPT
curl -s https://api.kie.ai/xai/v1/models       -H "Authorization: Bearer $KIE_API_KEY"   # Grok
```

Models returned on 2026-11-11:
- Claude: fable-5, opus-5-5, opus-5, sonnet-5-5, **haiku-5-5**, sonnet-5, opus-4-5…4-8, sonnet-4-5/4-6, haiku-4-5
- GPT: 6.1-sol, 6.1-terra, 6-astra, 6-sol, **6-luna**, 5.6-sol/terra/**luna**, 5.5, 5.4
- Grok: 4-7, 4-6, 4-5, 4-3
- Docs only (not in the lists above): Gemini 2.5 Pro / 3 Pro / 3.1 Pro / **3.5–3.8 Flash** / 4 Argon / 4 Carbon, **DeepSeek V4.1 Flash**, Kimi K3 / K3.1 Pro

## Cheap models to try first

Use these for most calls and move up only when the output isn't good enough. Check the actual price per call with `credits_consumed`. I haven't compared prices yet.

| Model id | Endpoint | Format | Notes |
|---|---|---|---|
| `claude-haiku-5-5` | `POST /claude/v1/messages` | Anthropic Messages | tools, streaming, `thinkingFlag` |
| `gemini-3-8-flash` | `POST /gemini-3-8-flash-openai/v1/chat/completions` | OpenAI Chat Completions | text, image, video, audio and PDF input; Google Search; marked "chat only" |
| `deepseek-v4-1-flash` | `POST /openai/v1/responses` | OpenAI Responses | thinks by default (turn it off to save money); JSON output; no `temperature`/`max_output_tokens` |
| `gpt-6-luna` / `gpt-5-6-luna` | `POST /codex/v1/responses` | OpenAI Responses | web search **or** function tools, never both in one request |

"Chat only" in the docs means the model works for plain requests but isn't tuned for agent and tool loops.

## Test results (2026-11-11)

Each model got the same prompt ("In exactly two sentences, explain what an insurance claim is."), non-streaming.

| Model | Result | Credits | Time | Notes |
|---|---|---|---|---|
| `claude-haiku-5-5` | ✅ | 0.01 | ~15 s | answer was good but ran past the requested two sentences (~150 output tokens) |
| `claude-haiku-5-5` + `system` | ❌ 500 | 0 | | fails every time, on both `/claude` and `/anthropic` paths, with a string or block `system` |
| `claude-sonnet-5-5` + `system` | ✅ | **0.13** | ~16 s | `system` works here; about 13x Haiku's cost |
| `deepseek-v4-1-flash` (effort `none`) | ✅ | 0.01 | **~3 s** | fastest; answer was tight and accurate |
| `gpt-5-6-luna` | ✅ | 0.01 | ~9 s | good answer |
| `gpt-6-luna` | ❌ 429 | 0 | | "All available accounts are currently rate-limited" (KIE's upstream capacity, not our limit); failed twice |
| `gemini-3-8-flash` | ⚠️ | 0.01 | ~27 s | 1st call failed `{"code":524,"msg":"Interal error: HTTP 403"}`; retry worked |
| `gemini-3-7-flash` | ✅ | 0.01 | ~9 s | good answer |

Takeaways:
- The cheap models all cost **0.01 credits** (likely the minimum charge) for a short call.
- **Default pick: `deepseek-v4-1-flash`**, which was fastest and cheap. Fallbacks: `gpt-5-6-luna`, `gemini-3-7-flash`.
- Haiku 5.5 can't take a `system` prompt. To use it, put instructions in the first user message, or switch to Sonnet when a system prompt matters.
- KIE's upstream sometimes errors (500, 524, 429), so **build in retries and a fallback model**.

## The four request formats

### 1. Claude: Anthropic Messages (`/claude/v1/messages`)

```bash
curl -s https://api.kie.ai/claude/v1/messages \
  -H "Authorization: Bearer $KIE_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"claude-haiku-5-5","stream":false,"max_tokens":1024,
       "messages":[{"role":"user","content":"Hello"}]}'
```

- Fields: `model`, `messages`, `tools` (`name`, `description`, `input_schema`), `thinkingFlag` (bool), `stream`, `max_tokens`
- ⚠️ `stream` **defaults to `true`**. Set `"stream": false` to get plain JSON back.
- `max_tokens` defaults to 4096.
- There's no `system` field in their schema. In testing it **works on sonnet-5-5 and returns a 500 on haiku-5-5** (see test results).
- Structured output (documented on sonnet-4-5): `"output_config":{"format":{"type":"json_schema","schema":{...}}}`
- The response is in Anthropic format (`content[]`, `stop_reason`, `usage`) plus `credits_consumed`.
- **Anthropic SDK:** set `base_url="https://api.kie.ai/anthropic"`. If the SDK sends the key as `x-api-key`, it must be `"Bearer <key>"`.

### 2. GPT / DeepSeek / Kimi / Grok: OpenAI Responses

| Family | Path |
|---|---|
| GPT 5.5+ | `/codex/v1/responses` |
| DeepSeek, Kimi | `/openai/v1/responses` |
| Grok | `/grok/v1/responses` |

```bash
curl -s https://api.kie.ai/openai/v1/responses \
  -H "Authorization: Bearer $KIE_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-1-flash","stream":false,
       "reasoning":{"effort":"none"},
       "input":"Introduce Amsterdam in one sentence"}'
```

- Fields: `model`, `input` (a string or an array of items), `instructions` (DeepSeek, Kimi), `stream`, `reasoning.effort`, `tools`, `tool_choice`, `text` (JSON format)
- `reasoning.effort` values: GPT `none…max`, Grok `low…xhigh`, DeepSeek `none/minimal/low/medium/high/xhigh/max`, Kimi `low/high/max`
- Function tool shape: `{"type":"function","name":...,"description":...,"parameters":{...}}` with `"tool_choice":"auto"`. Leave out `tool_choice` when there are no tools.
- GPT: `{"type":"web_search"}` and function tools are mutually exclusive.
- GPT response `model` fields use dots (`gpt-6.1-terra`). Requests accept either spelling.

### 3. Gemini, OpenAI-compatible (`/<model>-openai/v1/chat/completions`)

```bash
curl -s https://api.kie.ai/gemini-3-8-flash-openai/v1/chat/completions \
  -H "Authorization: Bearer $KIE_API_KEY" -H "Content-Type: application/json" \
  -d '{"stream":false,"messages":[{"role":"user","content":"Hello"}]}'
```

- The model is chosen by the **URL path**, not a `model` field. The Pro models use dots in the path: `/gemini-3.1-pro/v1/...`, `/gemini-2.5-pro/v1/...`.
- Fields: `messages`, `stream`, `tools`, `include_thoughts`, `reasoning_effort`. 2.5 Pro and 3 Pro also take `response_format` / `json_schema`.
- Every media type (image, video, audio, PDF) uses the image shape: `{"type":"image_url","image_url":{"url":"https://..."}}`
- GPT 5.2 also uses this format at `/gpt-5-2/v1/chat/completions` (chat only, `web_search` function tool).

### 4. Gemini native (`/gemini/v1/models/<model>:streamGenerateContent`)

- Gemini `contents` format: `tools.googleSearch`, `tools.functionDeclarations`, `generationConfig.thinkingConfig.{includeThoughts, thinkingLevel: low|high}`
- Streaming only. Use the `-openai` variant unless you need native Gemini features.

## Claude Code / coding agents on KIE

```bash
export ANTHROPIC_BASE_URL="https://api.kie.ai/anthropic"   # no /v1/messages suffix
export ANTHROPIC_AUTH_TOKEN="<kie key>"                    # or ANTHROPIC_API_KEY="Bearer <kie key>"
```

KIE also documents Codex CLI and Grok Build setups at https://docs.kie.ai/ai-agent/overview.

## Open questions

- How the cheap models compare on long prompts (short calls all cost the 0.01 minimum).
- How many dollars one credit costs (see https://kie.ai/pricing).
