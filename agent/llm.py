"""KIE.ai LLM client: model chain with retries, error-in-200 detection, JSON parsing and an on-disk cache.

See docs/kie-llm-api.md. Each model family uses its own endpoint and request format. A model is retried up
to 3 times (backoff 1, 2, 4 s; longer on 429), then the next model in the chain is tried. When every model
fails, `LLMError` is raised listing each model's last error.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import config

MAX_ATTEMPTS = 3
BACKOFF_S = (1.0, 2.0, 4.0)
RATE_LIMIT_BACKOFF_S = (5.0, 10.0, 15.0)


class LLMError(Exception):
    """Raised when no model in the chain produced a parseable JSON object."""


class _AttemptError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def parse_json_text(text: str) -> dict:
    """Strip ```json fences and parse the first {...} block. Raises ValueError when none parses."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty model output")
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S | re.I)
    if fence:
        t = fence.group(1).strip()
    start = t.find("{")
    if start < 0:
        raise ValueError("no JSON object in model output: %r" % text[:200])
    # Walk to the matching closing brace (string-aware), so trailing prose is ignored.
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        c = t[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                obj = json.loads(t[start:i + 1])
                if not isinstance(obj, dict):
                    raise ValueError("model output JSON is not an object")
                return obj
    raise ValueError("unterminated JSON object in model output: %r" % text[:200])


def _body_error(body: object) -> Optional[str]:
    """KIE often reports errors with HTTP 200: {"code": 429, "msg": ...} or {"error": {...}}."""
    if not isinstance(body, dict):
        return "response body is not a JSON object"
    if body.get("error"):
        return "error in body: %s" % json.dumps(body.get("error"), ensure_ascii=False)[:300]
    code = body.get("code")
    if code is not None and str(code) != "200":
        return "code %s in body: %s" % (code, str(body.get("msg") or body.get("message") or "")[:300])
    return None


def _body_status(body: object) -> Optional[int]:
    if isinstance(body, dict):
        try:
            return int(body.get("code"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            err = body.get("error")
            if isinstance(err, dict):
                for k in ("code", "status"):
                    try:
                        return int(err.get(k))  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        pass
    return None


def _responses_text(body: dict) -> str:
    if isinstance(body.get("output_text"), str) and body["output_text"].strip():
        return body["output_text"]
    parts: List[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("type") in ("output_text", "text") and isinstance(c.get("text"), str):
                parts.append(c["text"])
    return "".join(parts)


def _chat_text(body: dict) -> str:
    choices = body.get("choices") or []
    if choices and isinstance(choices[0], dict):
        content = (choices[0].get("message") or {}).get("content")
        if isinstance(content, list):
            return "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if isinstance(content, str):
            return content
    return ""


def _claude_text(body: dict) -> str:
    return "".join(c.get("text", "") for c in body.get("content") or []
                   if isinstance(c, dict) and c.get("type") == "text")


def build_request(model: str, system: str, user: str) -> Tuple[str, dict]:
    """Return (path, json body) for a model id, per KIE's per-family formats."""
    if model.startswith("deepseek-"):
        return "/openai/v1/responses", {"model": model, "instructions": system, "input": user,
                                        "reasoning": {"effort": "none"}, "stream": False}
    if model.startswith("gpt-"):
        return "/codex/v1/responses", {"model": model, "instructions": system, "input": user,
                                       "reasoning": {"effort": "none"}, "stream": False}
    if model.startswith("gemini-"):
        return "/%s-openai/v1/chat/completions" % model, {
            "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if model.startswith("claude-"):
        # Haiku returns 500 on a `system` field, so instructions go into the first user message.
        return "/claude/v1/messages", {"model": model, "stream": False, "max_tokens": 2048,
                                       "messages": [{"role": "user", "content": system + "\n\n" + user}]}
    raise LLMError("unknown model family for %r (expected deepseek-, gpt-, gemini- or claude-)" % model)


def extract_text(model: str, body: dict) -> str:
    if model.startswith(("deepseek-", "gpt-")):
        return _responses_text(body)
    if model.startswith("gemini-"):
        return _chat_text(body)
    if model.startswith("claude-"):
        return _claude_text(body)
    raise LLMError("unknown model family for %r" % model)


class LLMClient:
    def __init__(self, models: Optional[List[str]] = None, api_key: Optional[str] = None,
                 base: Optional[str] = None, timeout: Optional[float] = None,
                 cache_dir: Optional[Path] = None, use_cache: bool = True,
                 backoff: Tuple[float, ...] = BACKOFF_S, rate_limit_backoff: Tuple[float, ...] = RATE_LIMIT_BACKOFF_S):
        self.models = list(models if models is not None else config.LLM_MODELS)
        self.api_key = config.KIE_API_KEY if api_key is None else api_key
        if not self.models:
            raise LLMError("LLMClient needs at least one model")
        if not self.api_key:
            raise LLMError("KIE_API_KEY is not set; use the fallback extractor (--no-llm)")
        self.base = (base or config.KIE_BASE).rstrip("/")
        self.timeout = config.LLM_TIMEOUT_S if timeout is None else timeout
        self.cache_dir = (cache_dir or (config.CACHE_DIR / "llm"))
        self.use_cache = use_cache
        self.backoff = backoff
        self.rate_limit_backoff = rate_limit_backoff
        self._lock = threading.Lock()
        self.stats: Dict[str, object] = {"calls": 0, "cache_hits": 0, "failures": 0, "retries": 0,
                                         "per_model": {}, "credits_consumed": 0.0, "latency_s": 0.0}

    # -- stats ------------------------------------------------------------------------------------------------
    def _bump(self, key: str, by: float = 1) -> None:
        with self._lock:
            self.stats[key] = self.stats.get(key, 0) + by  # type: ignore[operator]

    def _bump_model(self, model: str, key: str, by: float = 1) -> None:
        with self._lock:
            pm = self.stats["per_model"].setdefault(model, {"ok": 0, "errors": 0, "credits": 0.0})  # type: ignore[union-attr]
            pm[key] = pm.get(key, 0) + by

    # -- cache ------------------------------------------------------------------------------------------------
    def _cache_path(self, system: str, user: str) -> Path:
        h = hashlib.sha256(json.dumps([self.models, system, user], ensure_ascii=False).encode("utf-8")).hexdigest()
        return self.cache_dir / ("%s.json" % h)

    # -- HTTP -------------------------------------------------------------------------------------------------
    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace") if e.fp else ""
            raise _AttemptError("HTTP %s on POST %s: %s" % (e.code, path, text[:300]), e.code)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise _AttemptError("network error on POST %s: %s" % (path, e))
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise _AttemptError("HTTP %s on POST %s: body is not JSON: %r" % (status, path, raw[:300]), status)
        err = _body_error(parsed)
        if err:
            raise _AttemptError("HTTP %s on POST %s: %s" % (status, path, err), _body_status(parsed))
        return parsed

    def _call_model(self, model: str, system: str, user: str) -> Tuple[dict, dict]:
        path, body = build_request(model, system, user)
        resp = self._post(path, body)
        text = extract_text(model, resp)
        try:
            obj = parse_json_text(text)
        except ValueError as e:
            raise _AttemptError("model %s returned unparseable JSON: %s" % (model, e))
        return obj, resp

    def complete_json(self, system: str, user: str,
                      validate: Optional[Callable[[dict], None]] = None) -> Tuple[dict, str]:
        """Return (parsed JSON object, model id that produced it). Raises LLMError when the chain is exhausted.

        validate: optional schema check; if it raises (ValueError/TypeError/KeyError/AttributeError) the answer
        counts as a failed attempt, so it is retried and then the next model is tried.
        """
        cpath = self._cache_path(system, user)
        if self.use_cache and cpath.exists():
            try:
                cached = json.loads(cpath.read_text(encoding="utf-8"))
                if validate:
                    validate(cached["result"])
                self._bump("cache_hits")
                return cached["result"], cached["model"]
            except (ValueError, KeyError, TypeError, AttributeError):
                pass  # corrupt or invalid cache entry: recompute and overwrite below
        errors: List[str] = []
        for model in self.models:
            last = ""
            for attempt in range(MAX_ATTEMPTS):
                self._bump("calls")
                t0 = time.monotonic()
                try:
                    obj, resp = self._call_model(model, system, user)
                    if validate:
                        try:
                            validate(obj)
                        except (ValueError, KeyError, TypeError, AttributeError) as ve:
                            raise _AttemptError("model %s returned JSON that fails the schema: %s" % (model, ve))
                except _AttemptError as e:
                    last = str(e)
                    self._bump_model(model, "errors")
                    if attempt + 1 < MAX_ATTEMPTS:
                        self._bump("retries")
                        waits = self.rate_limit_backoff if e.status == 429 else self.backoff
                        time.sleep(waits[min(attempt, len(waits) - 1)])
                    continue
                dt = time.monotonic() - t0
                credits = resp.get("credits_consumed")
                self._bump("latency_s", dt)
                self._bump_model(model, "ok")
                if isinstance(credits, (int, float)):
                    self._bump("credits_consumed", float(credits))
                    self._bump_model(model, "credits", float(credits))
                if self.use_cache:
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    tmp = cpath.with_suffix(".tmp%d" % threading.get_ident())
                    tmp.write_text(json.dumps({"model": model, "result": obj, "latency_s": round(dt, 2),
                                               "credits_consumed": credits}, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(cpath)
                return obj, model
            errors.append("%s: %s" % (model, last))
        self._bump("failures")
        msg = "all models failed: " + " | ".join(errors)
        with self._lock:
            self.stats["last_error"] = msg
        raise LLMError(msg)
