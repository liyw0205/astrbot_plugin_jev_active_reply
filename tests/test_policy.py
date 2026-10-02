import json
import tempfile
import unittest
from pathlib import Path

from ..policy import decide, pack_state
from ..state import Ledger, Room, RoomBook


class PolicyTests(unittest.TestCase):
    def test_same_person_is_not_automatic_reply(self):
        self.assertFalse(
            decide(
                dict(addressed=0.2, continuation=0.2, worthwhile=0.3, intrusive=0.1),
                True,
                0,
            ).speak
        )

    def test_followup_need_not_be_question(self):
        verdict = decide(
            dict(addressed=0.5, continuation=0.9, worthwhile=0.7, intrusive=0.1),
            True,
            0,
        )
        self.assertEqual(verdict.mode, "continue")

    def test_intrusion_always_blocks(self):
        self.assertFalse(
            decide(
                dict(addressed=0.99, continuation=0.99, worthwhile=0.99, intrusive=0.8),
                True,
                0,
            ).speak
        )

    def test_more_initiations_raise_threshold(self):
        values = dict(addressed=0.1, continuation=0.1, worthwhile=0.82, intrusive=0.1)
        self.assertTrue(decide(values, False, 0).speak)
        self.assertFalse(decide(values, False, 2).speak)

    def test_context_budget_in_utf8_bytes(self):
        state = pack_state(
            "设" * 300,
            [dict(sender_name="甲", content="话" * 5000) for _ in range(96)]
            + [dict(sender_name="甲", content="话" * 100) for _ in range(4)],
            dict(content="说" * 600),
            {},
            max_bytes=8000,
        )
        self.assertLessEqual(len(json.dumps(state, ensure_ascii=False).encode()), 8000)
        self.assertTrue(state["truncated"])
        self.assertEqual(state["target_message"]["content"], "说" * 600)
        self.assertEqual(state["bot_persona"], "设" * 300)

    def test_protected_persona_is_not_silently_cut(self):
        with self.assertRaisesRegex(ValueError, "protected_context"):
            pack_state("设" * 9000, [], {"content": "原消息"}, {}, max_bytes=8000)

    def test_no_media_url_in_state(self):
        state = pack_state(
            "",
            [
                dict(
                    content="[图片]",
                    image_urls=["secret-url"],
                    image_local_paths=["private-path"],
                )
            ],
            {},
            {},
        )
        self.assertNotIn("secret-url", str(state))
        self.assertNotIn("private-path", str(state))

    def test_message_dedup_and_ticket_release(self):
        room = Room()
        self.assertTrue(room.observe({}, "1"))
        self.assertFalse(room.observe({}, "1"))
        room.pending = "new"
        room.release("old")
        self.assertEqual(room.pending, "new")

    def test_rooms_bounded(self):
        rooms = RoomBook(2)
        rooms.get("a")
        rooms.get("b")
        rooms.get("c")
        self.assertEqual(len(rooms.rooms), 2)

    def test_counters_survive_restart_without_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.sqlite3"
            ledger = Ledger(path)
            ledger.bump("room", "sent")
            ledger.record(
                "room", "ok", {"content": "private chat", "api_key": "SECRET"}
            )
            ledger.close()
            ledger = Ledger(path)
            self.assertEqual(ledger.count("room", "sent"), 1)
            self.assertNotIn("private chat", path.read_bytes().decode(errors="ignore"))
            self.assertNotIn("SECRET", path.read_bytes().decode(errors="ignore"))
            ledger.close()
