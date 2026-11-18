"""Haiku-written nudges (design 2026-11-18): a small model, structured output, and checks that keep
amounts of money and journal quotes off the lock screen. The model is the local fake (no paid
calls)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.nudges.rules import Nudge
from app.nudges.writer import NUDGE_MODEL, WriterFailed, journal_summaries, passes_checks, write
from vournal_os.parity import fake_model as fm
from vournal_os.support import OsTestCase

TZ = ZoneInfo("UTC")
APP = {"todosDue": 1, "recordedToday": False, "streak": 3}
LIFE = {"tasksToday": ["Finish report"], "overdue": 0, "events": 0, "reminders": 0, "billsSoon": [{"name": "Rent", "inDays": 2}],
        "habits": [], "goals": [], "birthdays": [], "triggers": []}


def reply(**over):
    return fm.structured({"send": True, "title": "Your day", "body": "Finish the report, then rent is due Friday.",
                          "opens": "life", "prompt": None, **over})


class Writer(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app.os import ai

        fm.start()
        fm.script()
        p = patch.object(ai, "_client", fm.python_client())
        p.start()
        self.addCleanup(p.stop)

    async def write(self, moment="morning", summaries=()):
        return await write(moment, app=APP, life=LIFE, recent=["yesterday's nudge"], summaries=list(summaries), language="en")

    async def test_uses_haiku_and_low_effort(self):
        fm.script(reply())
        n = await self.write()
        self.assertEqual(n, Nudge("Your day", "Finish the report, then rent is due Friday.", "life", None))
        [req] = fm.requests("python")
        self.assertEqual(req["body"]["model"], NUDGE_MODEL)
        self.assertEqual(NUDGE_MODEL, "claude-haiku-5-5")
        self.assertEqual(req["body"]["output_config"]["effort"], "low")
        self.assertIn("Finish report", req["body"]["messages"][0]["content"])

    async def test_send_false_returns_none(self):
        fm.script(reply(send=False))
        self.assertIsNone(await self.write())

    async def test_reply_with_amount_falls_back(self):
        fm.script(reply(body="Rent $600 is due Friday"))
        with self.assertRaises(WriterFailed):
            await self.write()

    async def test_reply_quoting_journal_falls_back(self):
        summary = "I promised myself I would finally call my brother this weekend"
        fm.script(reply(opens="record", body="You said you would finally call my brother this weekend. Did you?"))
        with self.assertRaises(WriterFailed):
            await self.write("evening", summaries=[summary])

    async def test_too_long_title_fails(self):
        fm.script(reply(title="x" * 41))
        with self.assertRaises(WriterFailed):
            await self.write()

    async def test_provider_error_raises_writer_failed(self):
        fm.script(fm.error(400))
        with self.assertRaises(WriterFailed):
            await self.write()

    async def test_bad_opens_fails(self):
        fm.script(reply(opens="chat"))
        with self.assertRaises(WriterFailed):
            await self.write()

    async def test_checks(self):
        ok = Nudge("Evening check-in", "How did the meeting with Sara go?", "record", "How did the meeting with Sara go?")
        self.assertTrue(passes_checks(ok, []))
        for body in ("You spent 1,200 this week", "Budget left: 40 USD", "€30 left", "Pay 50$ today"):
            self.assertFalse(passes_checks(Nudge("t", body, "life"), []), body)
        self.assertTrue(passes_checks(Nudge("t", "3 tasks today, day 5 of your gym streak", "life"), []))
        self.assertFalse(passes_checks(Nudge("t", "b", "record", "p" * 121), []))

    async def test_summaries_only_with_journal_permission(self):
        user = await self.make_user()
        from app.os.profile import ensure_os_profile

        settings = await ensure_os_profile(user)
        await self.db.entry.create(data={"userId": user.id, "rawTranscript": "SECRET words", "summary": "A calm day.",
                                         "status": "DONE", "inJournal": True})
        today = datetime.now(TZ).date()
        self.assertEqual(await journal_summaries(user, settings, TZ, today), [])
        settings = await self.db.usersettings.update(where={"userId": user.id},
                                                     data={"assistantJournalConsentAt": datetime.now(timezone.utc)})
        out = await journal_summaries(user, settings, TZ, today)
        self.assertEqual(out, ["A calm day."])

    async def test_summaries_never_include_transcript_or_old_entries(self):
        user = await self.make_user()
        from app.os.profile import ensure_os_profile

        await ensure_os_profile(user)
        settings = await self.db.usersettings.update(where={"userId": user.id},
                                                     data={"assistantJournalConsentAt": datetime.now(timezone.utc)})
        await self.db.entry.create(data={"userId": user.id, "rawTranscript": "SECRET words", "summary": None,
                                         "status": "DONE", "inJournal": True})
        await self.db.entry.create(data={"userId": user.id, "rawTranscript": "old", "summary": "Long ago.", "status": "DONE",
                                         "inJournal": True, "createdAt": datetime.now(timezone.utc) - timedelta(days=5)})
        out = await journal_summaries(user, settings, TZ, datetime.now(TZ).date())
        self.assertEqual(out, [])
