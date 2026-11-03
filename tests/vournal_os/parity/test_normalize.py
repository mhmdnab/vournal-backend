"""normalize() makes a NestJS answer and a Python answer comparable: ids and
timestamps made "now" differ between two databases, everything else must not."""

import unittest
from datetime import datetime, timedelta, timezone

from vournal_os.parity.harness import normalize

ID_A = "clx0abcdefghijklmnopqrstu"
ID_B = "clx0zyxwvutsrqponmlkjihgf"


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


class Normalize(unittest.TestCase):
    def test_same_id_gets_same_placeholder(self):
        self.assertEqual(
            normalize({"id": ID_A, "parentId": ID_A}),
            {"id": "<id:1>", "parentId": "<id:1>"},
        )

    def test_different_ids_are_numbered_by_first_appearance(self):
        self.assertEqual(
            normalize([{"id": ID_B}, {"id": ID_A}, {"ref": ID_B}]),
            [{"id": "<id:1>"}, {"id": "<id:2>"}, {"ref": "<id:1>"}],
        )

    def test_timestamps_from_now_are_masked(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(normalize({"createdAt": _iso(now - timedelta(seconds=3))}), {"createdAt": "<now>"})

    def test_timestamps_whole_days_from_now_are_masked_by_their_day_count(self):
        now = datetime.now(timezone.utc)
        body = normalize([_iso(now + timedelta(days=1, milliseconds=15)), _iso(now - timedelta(days=7)), _iso(now + timedelta(days=1, hours=3))])
        self.assertEqual(body[:2], ["<now+1d>", "<now-7d>"])
        self.assertNotIn("<now", body[2])  # a day and three hours is a real time, not "now"

    def test_fixed_dates_are_kept(self):
        self.assertEqual(
            normalize({"dueAt": "2020-01-01T00:00:00.000Z"}), {"dueAt": "2020-01-01T00:00:00.000Z"}
        )

    def test_validation_messages_are_masked(self):
        body = normalize({"statusCode": 400, "message": ["a", "b"], "error": "Bad Request"})
        self.assertEqual(body, {"statusCode": 400, "message": "<validation>", "error": "Bad Request"})

    def test_ids_inside_strings_are_masked(self):
        self.assertEqual(
            normalize([{"id": ID_A, "link": f"/tasks?id={ID_A}"}, {"link": f"/calendar?id={ID_B}"}]),
            [{"id": "<id:1>", "link": "/tasks?id=<id:1>"}, {"link": "/calendar?id=<id:2>"}],
        )

    def test_plain_words_are_kept(self):
        self.assertEqual(normalize({"name": "cash", "code": "c1"}), {"name": "cash", "code": "c1"})


if __name__ == "__main__":
    unittest.main()
