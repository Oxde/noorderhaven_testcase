"""Client for the mock policy API: retries with backoff, Retry-After, idempotency keys, call stats.

Writes are only retried with an Idempotency-Key, so a replay after a timeout returns the original
record instead of creating a duplicate. Reads and writes share one retry loop.
"""
from __future__ import annotations

import json
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

from agent import config

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER_S = 30.0


class ApiError(Exception):
    """A request that failed for good (non-retryable 4xx, or retries exhausted)."""

    def __init__(self, method: str, path: str, status: Optional[int], body: str, attempts: int = 1):
        self.method = method
        self.path = path
        self.status = status
        self.body = body
        self.attempts = attempts
        super().__init__(f"{method} {path} failed: status={status} attempts={attempts} body={body[:300]!r}")


class ApiClient:
    def __init__(self, base: str = config.API_BASE, max_attempts: int = 8, timeout: float = 10.0):
        self.base = base.rstrip("/")
        self.max_attempts = max_attempts
        self.timeout = timeout
        self._lock = threading.Lock()
        self._local = threading.local()
        self._sleep = time.sleep          # replaceable in tests
        self.stats: Dict[str, object] = {"calls": 0, "retries": 0, "failures": 0, "by_status": {}}

    # ---- stats -------------------------------------------------------------------------------
    def _count(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.stats[key] = int(self.stats[key]) + n  # type: ignore[arg-type]

    def _count_status(self, status: str) -> None:
        with self._lock:
            by: Dict[str, int] = self.stats["by_status"]  # type: ignore[assignment]
            by[status] = by.get(status, 0) + 1

    @property
    def last_attempts(self) -> int:
        """Attempts used by the most recent call made from the current thread."""
        return int(getattr(self._local, "attempts", 0))

    # ---- core --------------------------------------------------------------------------------
    def _backoff(self, n: int) -> float:
        return min(4.0, 0.25 * 2 ** n) + random.uniform(0, 0.25)

    def _request(self, method: str, path: str, payload: Optional[dict] = None,
                 idempotency_key: Optional[str] = None, allow_404: bool = False) -> Tuple[int, dict]:
        url = self.base + path
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        self._count("calls")
        last_status: Optional[int] = None
        last_body = ""
        attempt = 0
        for attempt in range(1, self.max_attempts + 1):
            self._local.attempts = attempt
            wait: Optional[float] = None
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    self._count_status(str(resp.status))
                    try:
                        return resp.status, (json.loads(raw) if raw else {})
                    except json.JSONDecodeError as e:
                        self._count("failures")
                        raise ApiError(method, path, resp.status, f"invalid JSON: {e}: {raw[:300]}", attempt)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                self._count_status(str(e.code))
                last_status, last_body = e.code, body
                if e.code == 404 and allow_404:
                    return 404, {}
                if e.code not in RETRY_STATUSES:
                    self._count("failures")
                    raise ApiError(method, path, e.code, body, attempt)
                if e.code == 429:
                    ra = e.headers.get("Retry-After") if e.headers else None
                    try:
                        wait = min(MAX_RETRY_AFTER_S, float(ra)) if ra is not None else None
                    except ValueError:
                        wait = None
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
                reason = getattr(e, "reason", e)
                kind = "timeout" if isinstance(e, (socket.timeout, TimeoutError)) or isinstance(reason, (socket.timeout, TimeoutError)) else "network_error"
                self._count_status(kind)
                last_status, last_body = None, f"{kind}: {reason}"
            if attempt < self.max_attempts:
                self._count("retries")
                self._sleep(wait if wait is not None else self._backoff(attempt - 1))
        self._count("failures")
        raise ApiError(method, path, last_status, f"retries exhausted; last: {last_body}", attempt)

    # ---- endpoints ---------------------------------------------------------------------------
    def health(self) -> dict:
        return self._request("GET", "/health")[1]

    def get_policy(self, number: str) -> Optional[dict]:
        status, body = self._request("GET", "/policies/" + urllib.parse.quote(number.strip(), safe=""), allow_404=True)
        return None if status == 404 else body

    def find_policies_by_email(self, email: str) -> List[dict]:
        body = self._request("GET", "/policies?" + urllib.parse.urlencode({"email": email.strip()}))[1]
        return list(body.get("results", []))

    def get_claims(self, policy_number: str) -> List[dict]:
        body = self._request("GET", "/claims?" + urllib.parse.urlencode({"policy_number": policy_number}))[1]
        return list(body.get("claims", []))

    def register_claim(self, payload: dict, idempotency_key: str) -> dict:
        return self._request("POST", "/claims", payload, idempotency_key)[1]

    def post_decision(self, api_claim_id: str, payload: dict, idempotency_key: str) -> dict:
        path = "/claims/" + urllib.parse.quote(api_claim_id, safe="") + "/decision"
        return self._request("POST", path, payload, idempotency_key)[1]

    def queue_email(self, payload: dict, idempotency_key: str) -> dict:
        return self._request("POST", "/outbox", payload, idempotency_key)[1]

    def reset(self) -> None:
        self._request("POST", "/admin/reset", {})
