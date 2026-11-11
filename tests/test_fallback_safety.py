"""Paths where the keyword fallback must never make an automatic decision (independent review findings)."""
import tempfile
import unittest
from pathlib import Path

from agent import llm as llm_mod
from agent.models import Facts, HistoryClaim, Policy, PolicyResolution
from agent.rules import decide
from agent.threads import merge_thread


def _policy() -> Policy:
    return Policy(policy_number="POL-100231", holder_name="J", holder_email="j@example.com", product="motor_allrisk",
                  status="active", start_date="2022-03-01", covered_perils=["collision", "parking_damage", "glass"],
                  deductible=250.0, glass_deductible=75.0, max_payout=25000.0)


def _clean_facts(extractor: str) -> Facts:
    from agent.models import Document
    return Facts(claim_id="C-900", policy_number="POL-100231", loss_date="2026-10-21", loss_type="parking_damage",
                 documents=[Document("invoice", "INV-1", "2026-10-24", 620.0)], photo_count=1, extractor=extractor)


RES = PolicyResolution(policy=_policy(), how="number", sender_verified=True)


class NonLlmExtractorEscalates(unittest.TestCase):
    def _decide(self, extractor: str):
        return decide(_clean_facts(extractor), RES, [], "2026-11-02", received_at="2026-10-28")

    def test_llm_facts_still_auto_approve(self):
        self.assertEqual(self._decide("llm:deepseek-v4-1-flash").action, "auto_approve")

    def test_no_key_escalates(self):
        d = self._decide("fallback(no_key)")
        self.assertEqual(d.action, "escalate_handler")
        self.assertIsNone(d.letter_kind)

    def test_extract_error_escalates(self):
        self.assertEqual(self._decide("fallback(extract_error)").action, "escalate_handler")

    def test_unknown_future_tag_escalates(self):
        self.assertEqual(self._decide("fallback(something_new)").action, "escalate_handler")

    def test_explicit_no_llm_mode_still_decides(self):
        self.assertEqual(self._decide("fallback").action, "auto_approve")


class ThreadMergeKeepsWeakerProvenance(unittest.TestCase):
    def test_mixed_extractors_merge_to_weaker(self):
        parent = _clean_facts("fallback(llm_failed)")
        reply = _clean_facts("llm:deepseek-v4-1-flash")
        reply.claim_id = "C-901"
        merged = merge_thread(parent, reply)
        self.assertEqual(merged.extractor, "fallback(llm_failed)")
        self.assertEqual(decide(merged, RES, [], "2026-11-02").action, "escalate_handler")


class DuplicateEventAcrossLossTypes(unittest.TestCase):
    def test_same_policy_same_date_different_type_is_duplicate(self):
        hist = [HistoryClaim(claim_id="CLM-1", policy_number="POL-100231", loss_date="2026-10-21",
                             loss_type="collision", source_claim_id="C-899")]
        d = decide(_clean_facts("llm:x"), RES, hist, "2026-11-02", received_at="2026-10-28")
        self.assertEqual(d.action, "fraud_review")
        self.assertIn("duplicate_event", d.flags)


class SchemaValidationInsideRetryLoop(unittest.TestCase):
    def _client(self, answers):
        c = llm_mod.LLMClient(models=["model-a", "model-b"], api_key="test")
        c.use_cache = False
        c.backoff = c.rate_limit_backoff = [0, 0, 0]
        calls = []

        def fake_call(model, system, user):
            calls.append(model)
            return answers(model), {}
        c._call_model = fake_call
        return c, calls

    @staticmethod
    def _validate(obj):
        if not isinstance(obj.get("loss_type"), str):
            raise ValueError("loss_type must be a string")

    def test_bad_shape_retries_then_next_model_succeeds(self):
        c, calls = self._client(lambda m: {"loss_type": ["wrong"]} if m == "model-a" else {"loss_type": "glass"})
        obj, model = c.complete_json("s", "u", validate=self._validate)
        self.assertEqual(model, "model-b")
        self.assertEqual(calls.count("model-a"), llm_mod.MAX_ATTEMPTS)

    def test_whole_chain_failing_raises_llm_error(self):
        c, _ = self._client(lambda m: {"loss_type": 123})
        with self.assertRaises(llm_mod.LLMError):
            c.complete_json("s", "u", validate=self._validate)


if __name__ == "__main__":
    unittest.main()
