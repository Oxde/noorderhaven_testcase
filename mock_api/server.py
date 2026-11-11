#!/usr/bin/env python3
"""Mock policy API for the Noorderhaven claims case. Standard library only (Python 3.9+).

Run:   python mock_api/server.py            (chaos ON: this is how the case is evaluated)
       CHAOS=0 python mock_api/server.py    (chaos OFF: handy while developing)
Port:  8080 (override with PORT)
"""
import json, os, random, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
POLICIES = json.load(open(os.path.join(HERE, "data", "policies.json"), encoding="utf-8"))
HISTORY = json.load(open(os.path.join(HERE, "data", "claim_history.json"), encoding="utf-8"))
CHAOS = os.environ.get("CHAOS", "1") != "0"
FAIL_RATE = float(os.environ.get("FAIL_RATE", "0.15"))
LOCK = threading.Lock()
CREATED, DECISIONS, OUTBOX, IDEMPOTENT = [], [], [], {}
RUNTIME = os.path.join(HERE, "runtime")
os.makedirs(RUNTIME, exist_ok=True)


def persist(kind, rec):
    """Called with LOCK held. Keeps files the reviewers can read afterwards."""
    with open(os.path.join(RUNTIME, "write_log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, "record": rec}, ensure_ascii=False) + "\n")
    if kind == "outbox":
        with open(os.path.join(RUNTIME, "outbox.json"), "w", encoding="utf-8") as f:
            json.dump(OUTBOX, f, ensure_ascii=False, indent=2)


def reset_state():
    with LOCK:
        CREATED.clear(); DECISIONS.clear(); OUTBOX.clear(); IDEMPOTENT.clear(); COUNTER[0] = 1000
        for n in ("write_log.jsonl", "outbox.json"):
            p = os.path.join(RUNTIME, n)
            if os.path.exists(p):
                os.remove(p)

COUNTER = [1000]


reset_state()


def chaos():
    """Returns an error tuple (status, body) or None. Adds latency."""
    if not CHAOS:
        return None
    time.sleep(random.choice([0.05, 0.1, 0.2, 0.4, 0.8, 1.5, 3.0]))
    r = random.random()
    if r < FAIL_RATE:
        return 503, {"error": "service_unavailable", "message": "Upstream policy system timed out, try again"}
    if r < FAIL_RATE + 0.03:
        return 429, {"error": "rate_limited", "message": "Too many requests", "retry_after_seconds": 2}
    return None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, headers=None):
        raw = json.dumps(body, ensure_ascii=False, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return None

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        parts = [p for p in u.path.split("/") if p]
        if parts == ["health"]:
            return self.send(200, {"status": "ok", "chaos": CHAOS})
        e = chaos()
        if e:
            return self.send(e[0], e[1], {"Retry-After": "2"} if e[0] == 429 else None)
        if len(parts) == 2 and parts[0] == "policies":
            p = POLICIES.get(parts[1].upper())
            return self.send(200, p) if p else self.send(404, {"error": "not_found", "message": f"No policy {parts[1]}"})
        if parts == ["policies"] and "email" in q:
            em = q["email"][0].lower()
            hits = [p for p in POLICIES.values() if p["holder_email"].lower() == em]
            return self.send(200, {"results": hits})
        if parts == ["claims"] and "policy_number" in q:
            pn = q["policy_number"][0].upper()
            with LOCK:
                items = [c for c in HISTORY if c["policy_number"] == pn] + [c for c in CREATED if c["policy_number"] == pn]
            return self.send(200, {"policy_number": pn, "claims": items})
        if parts == ["outbox"]:
            return self.send(200, {"messages": OUTBOX})
        return self.send(404, {"error": "not_found"})

    def do_POST(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        data = self.body()
        if data is None:
            return self.send(400, {"error": "invalid_json"})
        if parts == ["admin", "reset"]:
            reset_state()
            return self.send(200, {"status": "reset"})
        e = chaos()
        if e:
            return self.send(e[0], e[1], {"Retry-After": "2"} if e[0] == 429 else None)
        key = self.headers.get("Idempotency-Key")
        with LOCK:
            if key and key in IDEMPOTENT:
                return self.send(200, IDEMPOTENT[key], {"Idempotent-Replay": "true"})
            if parts == ["claims"]:
                miss = [f for f in ("policy_number", "loss_date", "loss_type") if not data.get(f)]
                if miss:
                    return self.send(422, {"error": "validation_failed", "missing": miss})
                COUNTER[0] += 1
                rec = {"claim_id": f"CLM-2026-{COUNTER[0]}", "status": "registered", **data}
                CREATED.append(rec)
                persist("claim", rec)
                if key:
                    IDEMPOTENT[key] = rec
                return self.send(201, rec)
            if len(parts) == 3 and parts[0] == "claims" and parts[2] == "decision":
                allowed = {"approved", "rejected_proposed", "escalated_handler", "escalated_senior", "needs_info", "fraud_review"}
                if data.get("status") not in allowed:
                    return self.send(422, {"error": "validation_failed", "allowed_status": sorted(allowed)})
                rec = {"claim_id": parts[1], **data, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                DECISIONS.append(rec)
                persist("decision", rec)
                if key:
                    IDEMPOTENT[key] = rec
                return self.send(201, rec)
            if parts == ["outbox"]:
                if not data.get("to") or not data.get("body"):
                    return self.send(422, {"error": "validation_failed", "missing": ["to", "body"]})
                rec = {"message_id": "MSG-" + uuid.uuid4().hex[:8], "state": "queued", **data}
                OUTBOX.append(rec)
                persist("outbox", rec)
                if key:
                    IDEMPOTENT[key] = rec
                return self.send(201, rec)
        return self.send(404, {"error": "not_found"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"Mock API on http://localhost:{port} (chaos={'ON' if CHAOS else 'OFF'})")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
