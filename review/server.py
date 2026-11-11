"""Human review UI server (stdlib only).

    python3 review/server.py [--port 8000] [--queue output/review_queue.json]

Serves review/index.html and a small JSON API over the agent's review queue:
  GET  /api/queue                      items (attachment text stripped) + counts + run/eval summaries
  GET  /api/claims/<claim_id>          full item
  GET  /api/metrics                    agent.monitor.compute_metrics(OUTPUT_DIR), computed live
  GET  /api/open-requests              OUTPUT_DIR/open_info_requests.json (or {"items": []})
  POST /api/claims/<claim_id>/review   {action: approve|edit|reject, new_action?, net_payout?, reason?,
                                        letter_body?, reviewer?}
  POST /api/try                        dry run of one email (agent.try_email); writes nothing anywhere

Every review is appended to output/reviews.jsonl ({ts, claim_id, reviewer, action, before, after}) and the
queue file is rewritten atomically. The mock API client (agent.api_client) is imported lazily: if it is
missing or the API is unreachable the review is still recorded and the response carries `api_error`.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.models import ACTION_TO_STATUS, ACTIONS  # noqa: E402  (shared contract, always present)

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", str(ROOT / "output"))).resolve()
INDEX_HTML = HERE / "index.html"
REVIEW_ACTIONS = ("approve", "edit", "reject")
API_MAX_ATTEMPTS = 4          # keep the UI responsive when the API is down

LOCK = threading.Lock()


class ReviewError(Exception):
    """A client error in a review request (HTTP 4xx)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Store:
    """Reads and atomically rewrites the review queue; appends to reviews.jsonl."""

    def __init__(self, queue_path: Path, reviews_path: Path):
        self.queue_path = queue_path
        self.reviews_path = reviews_path
        self._wrapped = False      # queue file was {"items": [...]} rather than a bare list
        self._envelope: dict = {}

    def load(self) -> List[dict]:
        if not self.queue_path.exists():
            raise FileNotFoundError(f"review queue not found: {self.queue_path} "
                                    f"(run `python3 -m agent.run` first, or pass --queue review/sample_queue.json)")
        with open(self.queue_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            self._wrapped = False
            return data
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            self._wrapped = True
            self._envelope = {k: v for k, v in data.items() if k != "items"}
            return data["items"]
        raise ValueError(f"unexpected review queue format in {self.queue_path}: "
                         f"expected a list or {{'items': [...]}}, got {type(data).__name__}")

    def save(self, items: List[dict]) -> None:
        payload = dict(self._envelope, items=items) if self._wrapped else items
        fd, tmp = tempfile.mkstemp(prefix=".review_queue.", suffix=".tmp", dir=str(self.queue_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.queue_path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def review_count(self, claim_id: str) -> int:
        if not self.reviews_path.exists():
            return 0
        n = 0
        with open(self.reviews_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and json.loads(line).get("claim_id") == claim_id:
                    n += 1
        return n

    def append_review(self, record: dict) -> None:
        self.reviews_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.reviews_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_json_if_exists(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def live_metrics() -> Tuple[int, dict]:
    """Monitoring metrics computed on request so review overrides show up immediately."""
    try:
        from agent.monitor import compute_metrics  # noqa: WPS433 (lazy: the queue UI must work without it)
    except Exception as e:
        return 500, {"ok": False, "error": f"monitoring unavailable (cannot import agent.monitor: "
                                           f"{type(e).__name__}: {e})"}
    try:
        return 200, compute_metrics(OUTPUT_DIR)
    except Exception as e:
        return 500, {"ok": False, "error": f"computing metrics from {OUTPUT_DIR} failed: {type(e).__name__}: {e}"}


def open_requests() -> dict:
    data = read_json_if_exists(OUTPUT_DIR / "open_info_requests.json")
    if data is None:
        return {"items": []}
    if isinstance(data, list):
        return {"items": data}
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data
    raise ValueError(f"unexpected format in {OUTPUT_DIR / 'open_info_requests.json'}: "
                     f"expected a list or {{'items': [...]}}, got {type(data).__name__}")


def summarise_item(item: dict) -> dict:
    """Queue list entry: everything except full attachment text."""
    out = dict(item)
    out["attachments"] = [{"filename": a.get("filename"), "content_type": a.get("content_type"),
                           "text_chars": len(a.get("text") or "")} for a in item.get("attachments") or []]
    return out


def counts(items: List[dict]) -> Dict[str, Dict[str, int]]:
    by: Dict[str, Dict[str, int]] = {"action": {}, "review_status": {}, "letter_status": {}}
    for it in items:
        for key, val in (("action", (it.get("decision") or {}).get("action")),
                         ("review_status", it.get("review_status")),
                         ("letter_status", it.get("letter_status"))):
            k = str(val)
            by[key][k] = by[key].get(k, 0) + 1
    return by


def api_call(fn_name: str, *args) -> Tuple[Optional[dict], Optional[str]]:
    """Call ApiClient.<fn_name>; returns (result, error message). Imported lazily (Task 1 module)."""
    try:
        from agent.api_client import ApiClient  # noqa: WPS433 (lazy on purpose)
    except Exception as e:  # module missing or broken: the UI must still work
        return None, f"API client unavailable (cannot import agent.api_client: {type(e).__name__}: {e})"
    try:
        client = ApiClient(max_attempts=API_MAX_ATTEMPTS)
        return getattr(client, fn_name)(*args), None
    except Exception as e:
        parts = [f"{type(e).__name__}: {e}"]
        for attr in ("method", "path", "status", "body"):
            if getattr(e, attr, None) is not None:
                parts.append(f"{attr}={getattr(e, attr)}")
        return None, "Mock API call failed (" + "; ".join(parts) + ")"


def parse_payout(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ReviewError(400, f"net_payout must be a number or null, got {value!r}")
    if num < 0:
        raise ReviewError(400, f"net_payout must be >= 0, got {num}")
    return round(num, 2)


def apply_review(store: Store, claim_id: str, req: dict) -> dict:
    action = req.get("action")
    if action not in REVIEW_ACTIONS:
        raise ReviewError(400, f"action must be one of {list(REVIEW_ACTIONS)}, got {action!r}")
    reviewer = str(req.get("reviewer") or "handler").strip()[:80] or "handler"

    with LOCK:
        items = store.load()
        item = next((it for it in items if it.get("claim_id") == claim_id), None)
        if item is None:
            raise ReviewError(404, f"claim {claim_id!r} not in review queue {store.queue_path}")
        before = copy.deepcopy({k: item.get(k) for k in ("decision", "letter", "letter_status", "review_status")})
        n = store.review_count(claim_id) + 1
        api_error: Optional[str] = None
        api_result: Optional[dict] = None
        notes: List[str] = []
        decision = item.setdefault("decision", {})
        letter = item.get("letter")

        letter_body = req.get("letter_body")
        if isinstance(letter_body, str) and letter_body.strip():
            if letter and item.get("letter_status") == "sent":
                notes.append("letter already sent; text change ignored")
            elif letter:
                if letter_body != letter.get("body"):
                    letter["body"] = letter_body
                    notes.append("letter text edited")
            elif action == "edit":
                item["letter"] = letter = {
                    "kind": "manual", "to": item.get("from_email"), "subject": "Re: " + (item.get("subject") or ""),
                    "body": letter_body, "language": (item.get("facts") or {}).get("language", "nl"),
                    "in_reply_to": item.get("message_id"), "send_now": False, "api_kind": "decision",
                }
                item["letter_status"] = "held"
                notes.append("manual letter drafted (held until approved)")

        if action == "approve":
            item["review_status"] = "approved"
            if letter and item.get("letter_status") in ("held", "failed"):
                to = (letter.get("to") or "").strip().lower()
                sender = (item.get("from_email") or "").strip().lower()
                if not to or to != sender:
                    raise ReviewError(409, f"refusing to send: letter recipient {letter.get('to')!r} is not the "
                                           f"original sender {item.get('from_email')!r}")
                payload = {"to": letter.get("to"), "subject": letter.get("subject"), "body": letter.get("body"),
                           "kind": letter.get("api_kind") or "other", "language": letter.get("language"),
                           "in_reply_to": letter.get("in_reply_to"), "claim_id": item.get("api_claim_id"),
                           "source_claim_id": claim_id}
                api_result, api_error = api_call("queue_email", payload, f"{claim_id}:mail:approved:{n}")
                if api_error:
                    item["letter_status"] = "failed"
                    item["letter_error"] = api_error
                else:
                    item["letter_status"] = "sent"
                    item.pop("letter_error", None)
                    notes.append("held letter queued to the outbox")
            else:
                notes.append("no held letter; nothing sent")

        elif action == "edit":
            new_action = req.get("new_action") or decision.get("action")
            if new_action not in ACTIONS:
                raise ReviewError(400, f"new_action must be one of {list(ACTIONS)}, got {new_action!r}")
            decision["action"] = new_action
            if "net_payout" in req:
                decision["net_payout"] = parse_payout(req.get("net_payout"))
            if isinstance(req.get("reason"), str) and req["reason"].strip():
                decision["reason"] = req["reason"].strip()
            item["review_status"] = "edited"
            status = ACTION_TO_STATUS.get(new_action)
            if item.get("api_claim_id") and status:
                payload = {"status": status, "net_payout": decision.get("net_payout"),
                           "reason": decision.get("reason"), "flags": decision.get("flags") or [],
                           "decided_by": f"human:{reviewer}", "source_claim_id": claim_id}
                api_result, api_error = api_call("post_decision", item["api_claim_id"], payload,
                                                 f"{claim_id}:decision:edit:{n}")
                if not api_error:
                    notes.append(f"corrected decision posted to API as {status}")
            elif not item.get("api_claim_id"):
                notes.append("claim not registered in the API; decision recorded locally only")
            else:
                notes.append(f"{new_action} has no API status; recorded locally only")

        else:  # reject: discard the agent's proposal, send nothing
            item["review_status"] = "rejected"
            if item.get("letter_status") in ("held", "failed"):
                item["letter_status"] = "none"
                notes.append("held letter discarded, nothing sent")
            if isinstance(req.get("reason"), str) and req["reason"].strip():
                item["review_note"] = req["reason"].strip()

        item["reviewed_by"] = reviewer
        item["reviewed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        after = copy.deepcopy({k: item.get(k) for k in ("decision", "letter", "letter_status", "review_status")})
        record = {"ts": item["reviewed_at"], "claim_id": claim_id, "reviewer": reviewer, "action": action,
                  "before": before, "after": after, "api_error": api_error,
                  "reason": req.get("reason")}
        store.append_review(record)
        store.save(items)
        return {"ok": True, "item": item, "api_error": api_error, "api_result": api_result, "notes": notes,
                "review_number": n}


def try_email(req: dict) -> Tuple[int, dict]:
    """Dry run of one email (agent.try_email): read-only API client, nothing written to output/ or the API."""
    try:
        from agent.try_email import dry_run, email_from_dict  # noqa: WPS433 (lazy: queue UI works without it)
    except Exception as e:
        return 500, {"ok": False, "error": f"dry run unavailable (cannot import agent.try_email: "
                                           f"{type(e).__name__}: {e})"}
    try:
        email = email_from_dict(req)
    except (ValueError, TypeError) as e:
        return 400, {"ok": False, "error": f"bad email: {e}"}
    if not (email.body.strip() or email.subject.strip()):
        return 400, {"ok": False, "error": "subject or body is required"}
    try:
        return 200, dict(dry_run(email), ok=True)
    except Exception as e:
        return 500, {"ok": False, "error": f"dry run failed: {type(e).__name__}: {e}"}


def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ClaimsReview/1.0"

        def log_message(self, fmt, *args):
            sys.stderr.write("[review] %s - %s\n" % (self.address_string(), fmt % args))

        def _send(self, code: int, body, content_type: str = "application/json; charset=utf-8") -> None:
            raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def _error(self, code: int, message: str) -> None:
            self._send(code, {"ok": False, "error": message})

        def _allowed_hosts(self) -> set:
            port = self.server.server_address[1]
            return {f"localhost:{port}", f"127.0.0.1:{port}"}

        def _host_ok(self) -> bool:
            """Block DNS rebinding: only accept requests addressed to this local server."""
            if (self.headers.get("Host") or "") in self._allowed_hosts():
                return True
            self._error(403, "request must be addressed to localhost")
            return False

        def do_GET(self):
            if not self._host_ok():
                return
            parts = [unquote(p) for p in urlparse(self.path).path.split("/") if p]
            try:
                if parts in ([], ["index.html"]):
                    return self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
                if parts == ["api", "queue"]:
                    items = store.load()
                    return self._send(200, {
                        "queue_path": str(store.queue_path),
                        "items": [summarise_item(it) for it in items],
                        "counts": counts(items),
                        "run_summary": read_json_if_exists(OUTPUT_DIR / "run_summary.json"),
                        "eval_summary": read_json_if_exists(OUTPUT_DIR / "eval_summary.json"),
                    })
                if parts == ["api", "metrics"]:
                    return self._send(*live_metrics())
                if parts == ["api", "open-requests"]:
                    return self._send(200, open_requests())
                if len(parts) == 3 and parts[:2] == ["api", "claims"]:
                    item = next((it for it in store.load() if it.get("claim_id") == parts[2]), None)
                    if item is None:
                        return self._error(404, f"claim {parts[2]!r} not in review queue")
                    return self._send(200, item)
                return self._error(404, f"not found: GET {self.path}")
            except (FileNotFoundError, ValueError) as e:
                return self._error(500, f"{type(e).__name__}: {e}")

        def do_POST(self):
            parts = [unquote(p) for p in urlparse(self.path).path.split("/") if p]
            is_try = parts == ["api", "try"]
            if not is_try and not (len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "review"):
                return self._error(404, f"not found: POST {self.path}")
            # CSRF guard: a review POST can release held letters, so any other web page the handler has
            # open must not be able to trigger it. Require JSON (forces a CORS preflight, which this
            # server never answers) and a same-origin Origin header when the browser sends one.
            if not self._host_ok():
                return
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._error(415, f"Content-Type must be application/json, got {ctype or 'none'!r}")
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + h for h in self._allowed_hosts()}:
                return self._error(403, f"cross-origin review request refused (Origin {origin!r})")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(req, dict):
                    raise ReviewError(400, "request body must be a JSON object")
            except json.JSONDecodeError as e:
                return self._error(400, f"invalid JSON body: {e}")
            except ReviewError as e:
                return self._error(e.status, e.message)
            if is_try:
                return self._send(*try_email(req))
            try:
                return self._send(200, apply_review(store, parts[2], req))
            except ReviewError as e:
                return self._error(e.status, e.message)
            except (FileNotFoundError, ValueError) as e:
                return self._error(500, f"{type(e).__name__}: {e}")

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description="Claims human review UI")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--queue", default=str(OUTPUT_DIR / "review_queue.json"),
                    help="review queue JSON (default: output/review_queue.json)")
    ap.add_argument("--reviews", default=str(OUTPUT_DIR / "reviews.jsonl"),
                    help="review log to append to (default: output/reviews.jsonl)")
    args = ap.parse_args()
    store = Store(Path(args.queue).resolve(), Path(args.reviews).resolve())
    if not store.queue_path.exists():
        print(f"warning: {store.queue_path} does not exist yet; /api/queue will return an error until it does",
              file=sys.stderr)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store))
    print(f"Review UI on http://localhost:{args.port}  (queue: {store.queue_path})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
