import unittest

from agent.followup import build_open_requests, build_reminder_letter


def item(cid, received_at, action="request_info", missing=None, thread=None, thread_parent=None, lang="nl"):
    return {"claim_id": cid, "received_at": received_at, "from_email": f"{cid.lower()}@example.com",
            "from_name": "Anna Bakker", "subject": "Schade", "message_id": f"<{cid}@example.com>",
            "facts": {"language": lang, "thread_parent": thread_parent},
            "decision": {"action": action, "missing_items": missing or ["priced_document"]},
            "thread": thread}


class FollowupTest(unittest.TestCase):
    def test_open_request_counts_days(self):
        out = build_open_requests([item("C-001", "2026-10-28T09:15:00")], "2026-11-02")
        self.assertEqual(len(out["items"]), 1)
        it = out["items"][0]
        self.assertEqual(it["requested_on"], "2026-10-28")
        self.assertEqual(it["days_open"], 5)
        self.assertEqual(it["status"], "open")
        self.assertIsNone(it["answered_by"])
        self.assertFalse(it["reminder_due"])
        self.assertEqual((out["open"], out["answered"], out["reminder_due"]), (1, 0, 0))

    def test_answered_request_marked(self):
        q = [item("C-003", "2026-10-20T10:00:00", thread={"answered_by": "C-017", "status": "answered"}),
             item("C-017", "2026-10-29T10:00:00", action="auto_approve", thread={"answers": "C-003"},
                  thread_parent="C-003"),
             item("C-013", "2026-10-21T10:00:00"),   # answered only via the reply's facts.thread_parent
             item("C-027", "2026-10-30T10:00:00", action="escalate_handler", thread_parent="C-013")]
        out = build_open_requests(q, "2026-11-02")
        by_id = {i["claim_id"]: i for i in out["items"]}
        self.assertEqual(set(by_id), {"C-003", "C-013"})       # only request_info items
        self.assertEqual(by_id["C-003"]["status"], "answered")
        self.assertEqual(by_id["C-003"]["answered_by"], "C-017")
        self.assertEqual(by_id["C-013"]["answered_by"], "C-027")
        self.assertFalse(by_id["C-003"]["reminder_due"])      # answered requests never need a reminder
        self.assertEqual((out["open"], out["answered"], out["reminder_due"]), (0, 2, 0))

    def test_reminder_due_after_7_days(self):
        q = [item("C-010", "2026-10-26T08:00:00"), item("C-011", "2026-10-27T08:00:00")]
        out = build_open_requests(q, "2026-11-02")
        by_id = {i["claim_id"]: i for i in out["items"]}
        self.assertEqual(by_id["C-010"]["days_open"], 7)
        self.assertTrue(by_id["C-010"]["reminder_due"])
        self.assertEqual(by_id["C-011"]["days_open"], 6)
        self.assertFalse(by_id["C-011"]["reminder_due"])
        self.assertEqual(out["reminder_due"], 1)

    def test_reminder_letter_recipient_is_sender_and_held(self):
        out = build_open_requests([item("C-033", "2026-10-20T08:00:00", missing=["loss_date", "priced_document"])],
                                  "2026-11-02")
        it = out["items"][0]
        for lang in ("nl", "en"):
            letter = build_reminder_letter(it, lang)
            self.assertEqual(letter.to, "c-033@example.com")
            self.assertFalse(letter.send_now)
            self.assertEqual(letter.language, lang)
            self.assertEqual(letter.in_reply_to, "<C-033@example.com>")
            self.assertIn("2026-10-20", letter.body)
        nl = build_reminder_letter(it, "nl")
        self.assertIn("een factuur of reparatieofferte met het bedrag", nl.body)
        self.assertIn("de datum waarop de schade is ontstaan", nl.body)
        self.assertNotIn("EUR", nl.body)
        en = build_reminder_letter(it, "en")
        self.assertIn("an invoice or repair quote showing the amount", en.body)
        answered = dict(it, status="answered", answered_by="C-040")
        with self.assertRaises(ValueError):
            build_reminder_letter(answered, "nl")


if __name__ == "__main__":
    unittest.main()
