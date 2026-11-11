"""Task 2 tests: PDF text, amount parsing, injection detection, fallback extraction, KIE client errors, normalise."""
import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import config  # noqa: E402
from agent.extract import (detect_injection, extract_facts, fallback_extract, find_loss_date,  # noqa: E402
                           load_email, normalise, parse_amount)
from agent.llm import LLMClient, LLMError, parse_json_text  # noqa: E402
from agent.pdf_text import extract_text  # noqa: E402
from agent.prompts import EXTRACTION_SYSTEM, build_extraction_user  # noqa: E402

CLAIMS = config.CLAIMS_DIR


class _FakeKie(BaseHTTPRequestHandler):
    """Scripted responses: a list of (http_status, body_dict) popped per request; records paths."""
    script = []
    paths = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(n)
        type(self).paths.append(self.path)
        status, body = type(self).script.pop(0) if type(self).script else (500, {"error": "script empty"})
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def _responses_body(text):
    return {"id": "r1", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "credits_consumed": 0.01}


class TestPdfAndParsing(unittest.TestCase):
    def test_pdf_text_c001_total(self):
        text = extract_text(CLAIMS / "C-001" / "document_1.pdf")
        self.assertIn("Totaal incl. BTW: EUR 620.00", text)
        self.assertIn("Factuurnummer: INV-2026-0447", text)

    def test_parse_amount_formats(self):
        self.assertEqual(parse_amount("1.650 euro"), 1650.0)
        self.assertEqual(parse_amount("EUR 1,650.00"), 1650.0)
        self.assertEqual(parse_amount("3.200"), 3200.0)
        self.assertEqual(parse_amount("38,000.00"), 38000.0)
        self.assertEqual(parse_amount("EUR 620.00"), 620.0)
        self.assertEqual(parse_amount("12,50"), 12.5)
        self.assertEqual(parse_amount(450), 450.0)
        self.assertIsNone(parse_amount("geen bedrag"))
        self.assertIsNone(parse_amount(None))

    def test_relative_dates(self):
        self.assertEqual(find_loss_date("Gisteren schoot de slang los", "2026-10-30T09:12:00"), "2026-10-29")
        self.assertEqual(find_loss_date("in de nacht van 26 op 27 oktober", "2026-11-01T10:05:00"), "2026-10-26")
        self.assertEqual(find_loss_date("afgelopen dinsdag", "2026-10-28T08:14:00"), "2026-10-27")
        self.assertEqual(find_loss_date("on 27 October I scraped", "2026-10-31T15:10:00"), "2026-10-27")

    def test_json_fences_and_trailing_prose(self):
        self.assertEqual(parse_json_text('```json\n{"a": {"b": "}"}}\n```'), {"a": {"b": "}"}})
        self.assertEqual(parse_json_text('Here you go: {"x": 1} hope this helps'), {"x": 1})
        with self.assertRaises(ValueError):
            parse_json_text("no json here")


class TestInjection(unittest.TestCase):
    def test_injection_in_email_body(self):
        email = load_email(CLAIMS / "C-005")
        facts = fallback_extract(email)
        self.assertTrue(facts.injection_detected.value)
        self.assertIn("SYSTEM INSTRUCTION", facts.injection_detected.evidence)
        # The injected 9500 must not leak into the amount.
        self.assertEqual(facts.amount_in_email, 450.0)

    def test_injection_in_attachment(self):
        email = load_email(CLAIMS / "C-020")
        self.assertFalse(detect_injection(email.body).value)
        facts = fallback_extract(email)
        self.assertTrue(facts.injection_detected.value)
        self.assertIn("document_1.pdf", facts.injection_detected.evidence)
        # The LLM path ORs deterministic detection even when the model says "no injection".
        f2 = normalise({"intent": "new_claim", "injection_detected": {"value": False, "evidence": ""}}, email)
        self.assertTrue(f2.injection_detected.value)

    def test_clean_email_has_no_injection(self):
        self.assertFalse(fallback_extract(load_email(CLAIMS / "C-001")).injection_detected.value)


class TestFallback(unittest.TestCase):
    def test_fallback_c001(self):
        f = fallback_extract(load_email(CLAIMS / "C-001"))
        self.assertEqual(f.policy_number, "POL-100231")
        self.assertEqual(f.loss_date, "2026-10-21")
        self.assertEqual(f.loss_type, "parking_damage")
        self.assertEqual(f.photo_count, 1)
        self.assertEqual(f.language, "nl")
        self.assertEqual(f.intent, "new_claim")
        inv = [d for d in f.documents if d.kind == "invoice"]
        self.assertEqual(len(inv), 1)
        self.assertEqual(inv[0].amount, 620.0)
        self.assertEqual(inv[0].number, "INV-2026-0447")

    def test_fallback_special_intents(self):
        self.assertEqual(fallback_extract(load_email(CLAIMS / "C-008")).intent, "spam")
        c26 = fallback_extract(load_email(CLAIMS / "C-026"))
        self.assertEqual(c26.intent, "complaint_existing_claim")
        self.assertEqual(c26.existing_claim_reference, "CLM-2026-0881")
        self.assertTrue(c26.urgent.value)
        self.assertEqual(fallback_extract(load_email(CLAIMS / "C-038")).intent, "third_party_liability")
        self.assertEqual(fallback_extract(load_email(CLAIMS / "C-017")).intent, "reply_with_info")
        self.assertTrue(fallback_extract(load_email(CLAIMS / "C-040")).other_bank_account_requested.value)
        self.assertTrue(fallback_extract(load_email(CLAIMS / "C-037")).loss_outside_europe.value)
        self.assertTrue(fallback_extract(load_email(CLAIMS / "C-024")).gradual_damage.value)

    def test_extract_facts_without_llm_is_fallback(self):
        f = extract_facts(load_email(CLAIMS / "C-001"), None)
        self.assertEqual(f.extractor, "fallback")


class TestNormalise(unittest.TestCase):
    def test_normalise_rejects_unknown_loss_type(self):
        email = load_email(CLAIMS / "C-001")
        f = normalise({"intent": "make_money", "language": "Dutch", "loss_type": "car crash",
                       "loss_date": "21-10-2026 maybe", "amount_in_email": "EUR 1,650.00",
                       "documents": [{"kind": "bill", "amount": "620,00"}, "junk"],
                       "urgent": "yes", "gradual_damage": {"value": "true", "evidence": "x"},
                       "policy_number": " pol-100231 "}, email)
        self.assertIsNone(f.loss_type)
        self.assertEqual(f.intent, "new_claim")
        self.assertIn(f.language, ("nl", "en"))
        self.assertEqual(f.amount_in_email, 1650.0)
        self.assertEqual(f.documents[0].kind, "other")
        self.assertEqual(f.documents[0].amount, 620.0)
        self.assertEqual(f.policy_number, "POL-100231")
        self.assertTrue(f.gradual_damage.value)
        self.assertFalse(f.urgent.value)          # a bare string is not a valid judgment
        self.assertEqual(f.photo_count, 1)        # from attachments, not the LLM

    def test_normalise_garbage_does_not_crash(self):
        email = load_email(CLAIMS / "C-001")
        f = normalise("not a dict", email)  # type: ignore[arg-type]
        self.assertEqual(f.claim_id, "C-001")
        self.assertIsNone(f.loss_type)

    def test_prompt_wraps_untrusted_blocks(self):
        email = load_email(CLAIMS / "C-020")
        user = build_extraction_user(email)
        self.assertIn("received_at: 2026-11-01T09:00:00", user)
        self.assertIn('<attachment name="document_1.pdf"', user)
        self.assertIn("<email>", user)
        self.assertIn("untrusted", EXTRACTION_SYSTEM)


class TestLLMClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _FakeKie)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _FakeKie.script = []
        _FakeKie.paths = []
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _client(self, models):
        return LLMClient(models=models, api_key="test-key", base=self.base, timeout=5, cache_dir=self.tmp,
                         backoff=(0, 0, 0), rate_limit_backoff=(0, 0, 0))

    def test_kie_error_in_200_body_is_failure(self):
        _FakeKie.script = [
            (200, {"code": 429, "msg": "rate limited"}),
            (200, {"error": {"type": "server_error", "message": "upstream"}}),
            (200, {"code": 500, "msg": "boom"}),
            (200, {"choices": [{"message": {"content": '```json\n{"intent": "new_claim"}\n```'}}],
                   "credits_consumed": 0.01}),
        ]
        c = self._client(["deepseek-v4-1-flash", "gemini-3-7-flash"])
        obj, model = c.complete_json("sys", "user")
        self.assertEqual(obj, {"intent": "new_claim"})
        self.assertEqual(model, "gemini-3-7-flash")
        self.assertEqual(_FakeKie.paths[:3], ["/openai/v1/responses"] * 3)
        self.assertEqual(_FakeKie.paths[3], "/gemini-3-7-flash-openai/v1/chat/completions")
        self.assertEqual(c.stats["per_model"]["deepseek-v4-1-flash"]["errors"], 3)
        self.assertAlmostEqual(c.stats["credits_consumed"], 0.01)

    def test_all_models_fail_raises_and_extract_falls_back(self):
        _FakeKie.script = [(500, {"error": "x"})] * 3 + [(200, _responses_body("not json at all"))] * 3
        c = self._client(["deepseek-v4-1-flash", "gpt-5-6-luna"])
        with self.assertRaises(LLMError) as ctx:
            c.complete_json("sys", "user")
        self.assertIn("deepseek-v4-1-flash", str(ctx.exception))
        self.assertIn("gpt-5-6-luna", str(ctx.exception))
        self.assertEqual(_FakeKie.paths[-1], "/codex/v1/responses")
        _FakeKie.script = [(503, {"error": "down"})] * 6
        f = extract_facts(load_email(CLAIMS / "C-020"), c)
        self.assertEqual(f.extractor, "fallback(llm_failed)")
        self.assertTrue(f.injection_detected.value)

    def test_success_is_cached(self):
        _FakeKie.script = [(200, _responses_body('{"intent": "spam"}'))]
        c = self._client(["deepseek-v4-1-flash"])
        self.assertEqual(c.complete_json("s", "u"), ({"intent": "spam"}, "deepseek-v4-1-flash"))
        self.assertEqual(c.complete_json("s", "u"), ({"intent": "spam"}, "deepseek-v4-1-flash"))
        self.assertEqual(len(_FakeKie.paths), 1)
        self.assertEqual(c.stats["cache_hits"], 1)

    def test_claude_has_no_system_field(self):
        from agent.llm import build_request
        path, body = build_request("claude-haiku-5-5", "SYS", "USER")
        self.assertEqual(path, "/claude/v1/messages")
        self.assertNotIn("system", body)
        self.assertFalse(body["stream"])
        self.assertTrue(body["messages"][0]["content"].startswith("SYS"))


if __name__ == "__main__":
    unittest.main()
