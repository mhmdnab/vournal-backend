"""Prayer anchors before the prayer section exists: every account is "no city set"."""

import unittest

from fastapi import HTTPException

from vournal_os.support import OsTestCase


class Anchors(OsTestCase):
    def test_read_anchor(self):
        from app.os.common.prayer import read_anchor

        self.assertEqual(
            read_anchor({"start": {"prayer": "maghrib", "offset": 10}, "extra": 1}),
            {"start": {"prayer": "maghrib", "offset": 10}, "end": None},
        )
        self.assertEqual(
            read_anchor({"start": {"prayer": "fajr", "offset": 0}, "end": {"prayer": "sunrise", "offset": -5}})["end"],
            {"prayer": "sunrise", "offset": -5},
        )
        for raw in (None, {}, {"start": {"prayer": "noon", "offset": 0}}, {"start": {"prayer": "isha"}}, "x"):
            self.assertIsNone(read_anchor(raw))

    async def test_no_city_set(self):
        from app.os.common.prayer import SETTINGS_MISSING, resolve_or_fail, resolver

        user = await self.make_user()
        resolve = await resolver(user.id)
        self.assertIsNone(resolve("2026-11-03", {"start": {"prayer": "maghrib", "offset": 10}}))
        with self.assertRaises(HTTPException) as caught:
            await resolve_or_fail(user.id, "2026-11-03", {"start": {"prayer": "maghrib", "offset": 10}})
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail["message"], SETTINGS_MISSING)

    def test_anchor_validation(self):
        from pydantic import ValidationError

        from app.os.common.prayer import TimeAnchor

        TimeAnchor.model_validate({"start": {"prayer": "asr", "offset": -240}})
        for bad in ({"start": {"prayer": "asr", "offset": 241}}, {"start": {"prayer": "noon", "offset": 0}}, {"start": {"prayer": "asr", "offset": "5"}}):
            with self.assertRaises(ValidationError):
                TimeAnchor.model_validate(bad)


if __name__ == "__main__":
    unittest.main()
