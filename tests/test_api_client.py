"""ApiClient retry, Retry-After, 404 and idempotency behaviour against a fake local server."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent.api_client import ApiClient, ApiError


class FakeServer:
    """Serves a scripted list of (status, body, headers); the last entry repeats. Records requests."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                outer.requests.append({"method": self.command, "path": self.path,
                                       "headers": dict(self.headers), "body": raw.decode()})
                idx = min(len(outer.requests) - 1, len(outer.script) - 1)
                status, body, headers = outer.script[idx]
                out = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(out)

            do_GET = _handle
            do_POST = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"


def client(base, **kw):
    c = ApiClient(base, **kw)
    c.sleeps = []
    c._sleep = c.sleeps.append   # no real waiting; record what the client would have slept
    return c


UNAVAILABLE = (503, {"error": "service_unavailable"}, None)


class ApiClientTests(unittest.TestCase):
    def test_retries_503_then_succeeds(self):
        with FakeServer([UNAVAILABLE, UNAVAILABLE, (200, {"policy_number": "POL-1"}, None)]) as s:
            c = client(s.base)
            self.assertEqual(c.get_policy("POL-1"), {"policy_number": "POL-1"})
            self.assertEqual(c.stats["retries"], 2)
            self.assertEqual(c.stats["by_status"], {"503": 2, "200": 1})
            self.assertEqual(c.last_attempts, 3)
            self.assertEqual(len(c.sleeps), 2)
            self.assertTrue(all(0 < x <= 4.25 for x in c.sleeps))

    def test_retry_after_honoured_on_429(self):
        with FakeServer([(429, {"error": "rate_limited"}, {"Retry-After": "2"}), (200, {"results": []}, None)]) as s:
            c = client(s.base)
            self.assertEqual(c.find_policies_by_email("a@b.nl"), [])
            self.assertEqual(c.sleeps, [2.0])
            self.assertEqual(c.stats["retries"], 1)

    def test_404_policy_returns_none(self):
        with FakeServer([(404, {"error": "not_found"}, None)]) as s:
            c = client(s.base)
            self.assertIsNone(c.get_policy("POL-999999"))
            self.assertEqual(len(s.requests), 1)
            self.assertEqual(c.stats["failures"], 0)

    def test_other_4xx_raises_immediately(self):
        with FakeServer([(422, {"error": "validation_failed"}, None)]) as s:
            c = client(s.base)
            with self.assertRaises(ApiError) as cm:
                c.register_claim({"policy_number": "POL-1"}, "C-001:register")
            self.assertEqual(cm.exception.status, 422)
            self.assertIn("validation_failed", cm.exception.body)
            self.assertEqual(len(s.requests), 1)

    def test_write_exhausted_raises_api_error(self):
        with FakeServer([UNAVAILABLE]) as s:
            c = client(s.base, max_attempts=3)
            with self.assertRaises(ApiError) as cm:
                c.post_decision("CLM-2026-1001", {"status": "approved"}, "C-001:decision")
            e = cm.exception
            self.assertEqual(e.status, 503)
            self.assertEqual(e.method, "POST")
            self.assertEqual(e.path, "/claims/CLM-2026-1001/decision")
            self.assertEqual(e.attempts, 3)
            self.assertEqual(len(s.requests), 3)
            self.assertEqual(c.stats["failures"], 1)

    def test_network_error_retried_then_raises(self):
        c = client("http://127.0.0.1:9", max_attempts=2)   # nothing listens on port 9
        with self.assertRaises(ApiError) as cm:
            c.health()
        self.assertIsNone(cm.exception.status)
        self.assertEqual(c.stats["retries"], 1)

    def test_idempotency_header_sent(self):
        with FakeServer([UNAVAILABLE, (201, {"message_id": "MSG-1"}, None)]) as s:
            c = client(s.base)
            c.queue_email({"to": "x@y.nl", "body": "hi"}, "C-001:mail:approval")
            self.assertEqual(len(s.requests), 2)
            keys = [r["headers"].get("Idempotency-Key") for r in s.requests]
            self.assertEqual(keys, ["C-001:mail:approval", "C-001:mail:approval"])
            self.assertEqual(json.loads(s.requests[1]["body"]), {"to": "x@y.nl", "body": "hi"})


if __name__ == "__main__":
    unittest.main()
