"""Notification texts against NestJS's own notificationTexts()
(tests/vournal_os/golden/notifications_golden.mjs): plurals, when, date and time."""

import json
import unittest
from datetime import datetime
from pathlib import Path

GOLDEN = json.loads((Path(__file__).parent / "golden" / "notifications.json").read_text())


class NotificationTexts(unittest.TestCase):
    def test_match_nestjs(self):
        from app.os.notification_texts import texts

        for c in GOLDEN["plurals"]:
            self.assertEqual(texts(c["lang"]).n(c["key"], c["n"], name="Sara"), c["text"], c)
        for c in GOLDEN["when"]:
            self.assertEqual(texts(c["lang"]).when(c["n"]), c["text"], c)
        for c in GOLDEN["date"]:
            self.assertEqual(texts(c["lang"]).date(c["date"]), c["text"], c)
        for c in GOLDEN["time"]:
            moment = datetime.fromisoformat(c["iso"].replace("Z", "+00:00"))
            self.assertEqual(texts(c["lang"]).time(moment, c["tz"], c["fmt"]), c["text"], c)

    def test_messages_fill_their_values(self):
        from app.os.notification_texts import texts

        self.assertEqual(texts("en").join(["a", "b"]), "a · b")
        self.assertEqual(texts("ar").ltr("12"), "⁦12⁩")
        self.assertEqual(texts("en").ltr("12"), "12")
        self.assertIn("Sara", texts("en").t("birthdayToday", name="Sara"))


if __name__ == "__main__":
    unittest.main()
