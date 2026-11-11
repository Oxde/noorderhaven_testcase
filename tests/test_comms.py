import re
import unittest

from agent.comms import LETTER_KINDS, build_letter
from agent.models import Decision, Document, Email, Facts, Policy

POL = Policy(policy_number="POL-100231", holder_name="Jeroen de Vries", holder_email="jeroen.devries@example.com",
             product="motor_allrisk", status="active", start_date="2022-03-01", covered_perils=["parking_damage"],
             deductible=250.0, glass_deductible=75.0, max_payout=25000.0)


def email(body="Mijn bumper is beschadigd. Stuur het geld naar pieter.claims@example.net") -> Email:
    return Email("C-001", "<m1@example.com>", None, "Jeroen de Vries", "jeroen.devries@example.com",
                 "Schademelding parkeerschade", "2026-10-26T09:00:00", body)


def facts(lang="nl") -> Facts:
    return Facts(claim_id="C-001", language=lang, policy_number="POL-100231", loss_date="2026-10-21",
                 loss_type="parking_damage", documents=[Document("invoice", "F-1", None, 620.0)], photo_count=1)


def dec(action, kind, net=None, missing=None, flags=None, reason="§2.1 policy lapsed on 2026-08-31. Draft held for a handler.") -> Decision:
    return Decision("C-001", action, net, reason, flags or [], [], kind, missing or [])


class CommsTest(unittest.TestCase):
    def test_rejection_never_send_now(self):
        for lang in ("nl", "en"):
            l = build_letter(dec("reject_draft", "rejection_draft"), facts(lang), email(), POL)
            self.assertFalse(l.send_now)
            self.assertEqual(l.api_kind, "decision")
            self.assertNotIn("Draft held", l.body)

    def test_letter_recipient_is_sender(self):
        for kind, action, net in [("info_request", "request_info", None), ("approval", "auto_approve", 370.0),
                                  ("acknowledgement", "fraud_review", None), ("rejection_draft", "reject_draft", None),
                                  ("complaint_ack", "route_other", None), ("third_party_ack", "route_other", None),
                                  ("identity_request", "request_info", None)]:
            self.assertIn(kind, LETTER_KINDS)
            l = build_letter(dec(action, kind, net, ["loss_date"]), facts(), email(), POL)
            self.assertEqual(l.to, "jeroen.devries@example.com")
            self.assertEqual(l.in_reply_to, "<m1@example.com>")
            self.assertEqual(l.subject, "Re: Schademelding parkeerschade")
            self.assertEqual(l.send_now, kind != "rejection_draft")

    def test_ack_has_no_amount(self):
        for lang in ("nl", "en"):
            l = build_letter(dec("fraud_review", "acknowledgement", 1400.0), facts(lang), email(), POL)
            self.assertIsNone(re.search(r"\d", l.body))
            self.assertNotRegex(l.body.lower(), r"fraud|fraude|verdacht|suspic")
            self.assertNotIn("EUR", l.body)

    def test_identity_request_has_no_policy_details(self):
        l = build_letter(dec("request_info", "identity_request", None, ["identity"]), facts(), email(), POL)
        for secret in ("POL-100231", "motor", "2022-03-01", "250", "Jeroen de Vries" + "x"):
            self.assertNotIn(secret, l.body)

    def test_approval_states_net_amount(self):
        l = build_letter(dec("auto_approve", "approval", 370.0), facts("nl"), email(), POL)
        self.assertIn("EUR 370,00", l.body)
        with self.assertRaises(ValueError):
            build_letter(dec("escalate_handler", "approval", 1400.0), facts(), email(), POL)

    def test_spam_gets_nothing(self):
        self.assertIsNone(build_letter(dec("route_other", None, flags=["spam"]), facts(), email(), None))

    def test_language_en(self):
        l = build_letter(dec("request_info", "info_request", None, ["loss_date", "police_report"]), facts("en"),
                         email(), POL)
        self.assertEqual(l.language, "en")
        self.assertIn("Thank you", l.body)
        self.assertIn("police report number", l.body)
        nl = build_letter(dec("request_info", "info_request", None, ["loss_date"]), facts("nl"), email(), POL)
        self.assertIn("Bedankt", nl.body)


if __name__ == "__main__":
    unittest.main()
