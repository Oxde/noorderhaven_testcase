"""Part 07 tests: monitoring metrics (override rate, retry alert, LLM vs fallback extraction agreement)."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import monitor  # noqa: E402


def _facts(cid: str, **over) -> dict:
    f = {"claim_id": cid, "intent": "new_claim", "policy_number": "POL-100231", "loss_date": "2026-10-21",
         "loss_type": "parking_damage", "documents": [], "injection_detected": {"value": False, "evidence": ""},
         "extractor": "fallback", "thread_parent": None}
    f.update(over)
    return f


def _item(cid: str, action: str = "auto_approve", review_status: str = "auto", **facts) -> dict:
    return {"claim_id": cid, "decision": {"action": action}, "letter_status": "sent",
            "review_status": review_status, "facts": _facts(cid, **facts)}


def _review(cid: str, action: str, original: str = "escalate_handler") -> dict:
    return {"ts": "2026-11-02T10:00:00", "claim_id": cid, "reviewer": "t", "action": action,
            "before": {"decision": {"action": original}}, "after": {}}


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="monitor_test_"))
        self.out = self.tmp / "output"
        self.claims = self.tmp / "claims"
        self.out.mkdir()
        self.claims.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _write(self, queue, run=None, reviews=None):
        (self.out / "review_queue.json").write_text(json.dumps(queue), encoding="utf-8")
        run = run or {"mode": "fallback", "api": {"calls": 10, "retries": 0, "failures": 0}, "llm": {}}
        (self.out / "run_summary.json").write_text(json.dumps(run), encoding="utf-8")
        if reviews is not None:
            (self.out / "reviews.jsonl").write_text("".join(json.dumps(r) + "\n" for r in reviews), encoding="utf-8")

    def test_override_rate(self):
        queue = [_item("C-%03d" % i, "escalate_handler", "pending") for i in range(1, 7)]
        reviews = [_review("C-001", "approve"), _review("C-002", "approve"), _review("C-003", "approve"),
                   _review("C-004", "edit"), _review("C-005", "reject", "reject_draft"),
                   _review("C-006", "edit"), _review("C-006", "approve")]   # latest review per claim counts
        self._write(queue, reviews=reviews)
        m = monitor.compute_metrics(self.out, self.claims)
        rv = m["reviews"]
        self.assertEqual((rv["total"], rv["approve"], rv["edit"], rv["reject"]), (6, 4, 1, 1))
        self.assertAlmostEqual(rv["override_rate"], 2 / 6, places=3)
        self.assertEqual(rv["by_action"]["reject_draft"], {"approve": 0, "edit": 0, "reject": 1})
        self.assertTrue(any("override rate" in a for a in m["alerts"]), m["alerts"])
        self.assertEqual(m["automation"], {"auto_handled": 0, "needs_human": 6, "auto_rate": 0.0})

    def test_override_rate_none_without_reviews(self):
        self._write([_item("C-001")])
        m = monitor.compute_metrics(self.out, self.claims)
        self.assertIsNone(m["reviews"]["override_rate"])
        self.assertIsNone(m["open_info_requests"])
        self.assertEqual(m["alerts"], [])

    def test_alert_on_high_retry_rate(self):
        run = {"mode": "fallback", "api": {"calls": 100, "retries": 31, "failures": 2}, "llm": {}}
        self._write([_item("C-001")], run=run)
        m = monitor.compute_metrics(self.out, self.claims)
        self.assertAlmostEqual(m["api"]["retry_rate"], 0.31)
        self.assertTrue(any("retry rate" in a for a in m["alerts"]), m["alerts"])
        self.assertTrue(any("failed after retries" in a for a in m["alerts"]), m["alerts"])
        run["api"]["retries"], run["api"]["failures"] = 30, 0
        self._write([_item("C-001")], run=run)
        self.assertEqual(monitor.compute_metrics(self.out, self.claims)["alerts"], [])

    def test_agreement_counts_disagreement(self):
        cdir = self.claims / "C-001"
        cdir.mkdir()
        (cdir / "email.json").write_text(json.dumps({
            "claim_id": "C-001", "message_id": "<c1@x>", "in_reply_to": None,
            "from": {"name": "Jan", "email": "jan@example.com"}, "subject": "Schade aan bumper",
            "received_at": "2026-10-28T08:14:00",
            "body": "Beste,\n\nOp 21 oktober is mijn auto op de parkeerplaats geraakt.\n\nPolisnummer POL-100231.\n",
            "attachments": []}), encoding="utf-8")
        llm_item = _item("C-001", extractor="llm:test", policy_number="POL-999999")
        self._write([llm_item], run={"mode": "llm:test", "api": {"calls": 1, "retries": 0, "failures": 0},
                                     "llm": {"calls": 1}})
        m = monitor.compute_metrics(self.out, self.claims)
        ag = m["extraction_agreement"]
        self.assertEqual(ag["claims_compared"], 1)
        self.assertEqual([(d["field"], d["llm"], d["fallback"]) for d in ag["disagreements"]],
                         [("policy_number", "POL-999999", "POL-100231")])
        self.assertEqual(ag["per_field"]["policy_number"], 0.0)
        self.assertEqual(ag["per_field"]["loss_date"], 1.0)
        self.assertAlmostEqual(ag["agreement_rate"], 5 / 6, places=3)
        self.assertEqual(m["llm"]["fallback_used"], 0)

    def test_write_metrics(self):
        self._write([_item("C-001")])
        (self.out / "open_info_requests.json").write_text(json.dumps({"items": [
            {"claim_id": "C-003", "status": "answered"}, {"claim_id": "C-009", "status": "open", "reminder_due": True}]}))
        path = monitor.write_metrics(self.out)
        m = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(m["open_info_requests"], {"open": 1, "answered": 1, "reminder_due": 1})
        self.assertTrue(any("reminder" in a for a in m["alerts"]))


if __name__ == "__main__":
    unittest.main()
