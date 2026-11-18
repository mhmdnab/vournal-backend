"""The nudge job (design 2026-11-18): who is due, the cap, AI or rules, write-before-send, dead
tokens and receipts. Expo and the AI writer are mocked; times are frozen."""

import os
from datetime import datetime, time, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from prisma import Json

from app.nudges import run
from app.nudges.rules import Nudge
from app.nudges.writer import WriterFailed
from app.os.common.defaults import DEFAULT_NOTIFICATION_PREFS
from vournal_os.support import OsTestCase

TOKYO = ZoneInfo("Asia/Tokyo")  # UTC+9, no daylight saving
LIFE = {"tasksToday": ["Finish report"], "overdue": 0, "events": 0, "reminders": 0, "billsSoon": [], "habits": [],
        "goals": [], "birthdays": [], "triggers": []}
AI_NUDGE = Nudge("Your day", "Finish the report first.", "life")


def tokyo(hh: int, mm: int, day: int = 18) -> datetime:
    return datetime(2026, 11, day, hh, mm, tzinfo=TOKYO).astimezone(timezone.utc)


class NudgesRun(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.send = AsyncMock(side_effect=lambda msgs: [{"status": "ok", "id": f"t{i}"} for i, _ in enumerate(msgs)])
        self.receipts = AsyncMock(return_value={})
        self.write = AsyncMock(return_value=AI_NUDGE)
        self.life = AsyncMock(return_value=dict(LIFE))
        for target, mock in (("app.push_expo.send", self.send), ("app.push_expo.receipts", self.receipts),
                             ("app.nudges.run.write", self.write), ("app.nudges.run.life_facts", self.life)):
            p = patch(target, mock)
            p.start()
            self.addCleanup(p.stop)
        env = patch.dict(os.environ, {"NUDGES_ENABLED": "1", "NUDGES_AI": "1"})
        env.start()
        self.addCleanup(env.stop)

    async def person(self, plan="plus", consent=True, tz="Asia/Tokyo", tokens=("ExponentPushToken[a]",), **prefs):
        from app.os.profile import ensure_os_profile

        user = await self.make_user(plan=plan)
        await ensure_os_profile(user)
        await self.db.usersettings.update(where={"userId": user.id}, data={
            "notificationPrefs": Json({**DEFAULT_NOTIFICATION_PREFS, "nudges": True, **prefs}),
            "nudgeAiConsentAt": datetime.now(timezone.utc) if consent else None})
        for t in tokens:
            await self.db.pushdevice.create(data={"userId": user.id, "token": t, "timezone": tz})
        return user

    async def logs(self, user):
        return await self.db.nudgelog.find_many(where={"userId": user.id}, order={"createdAt": "asc"})

    async def entry_today(self, user, local_day):
        await self.db.entry.create(data={"userId": user.id, "rawTranscript": "x", "status": "DONE", "inJournal": True,
                                         "createdAt": datetime.combine(local_day, time(12), tzinfo=TOKYO)})

    async def test_morning_sent_once_in_window(self):
        user = await self.person()
        self.assertEqual(await run.tick(tokyo(8, 5)), 1)
        [log] = await self.logs(user)
        self.assertEqual((log.moment, log.source, log.body, log.ticketIds), ("morning", "ai", AI_NUDGE.body, ["t0|ExponentPushToken[a]"]))
        [msgs], _ = self.send.await_args
        self.assertEqual(msgs[0]["data"], {"kind": "nudge", "opens": "life", "prompt": None, "nudgeId": log.id, "userId": user.id})
        self.assertEqual((msgs[0]["title"], msgs[0]["sound"]), ("Your day", "default"))
        self.assertEqual(await run.tick(tokyo(8, 6)), 0)
        self.assertEqual(self.send.await_count, 1)

    async def test_outside_window_nothing(self):
        await self.person()
        self.assertEqual(await run.tick(tokyo(8, 31)), 0)
        self.send.assert_not_awaited()

    async def test_nudges_off_nothing(self):
        await self.person(nudges=False)
        self.assertEqual(await run.tick(tokyo(8, 5)), 0)

    async def test_switch_off_skips_that_moment(self):
        await self.person(nudgePlan=False)
        self.assertEqual(await run.tick(tokyo(8, 5)), 0)

    async def test_custom_morning_time(self):
        user = await self.person(nudgeMorningTime="06:30")
        self.assertEqual(await run.tick(tokyo(6, 40)), 1)
        self.assertEqual((await self.logs(user))[0].moment, "morning")

    async def test_cap_plus_three_free_two(self):
        self.life.return_value = {**LIFE, "triggers": ["Gym streak at risk"]}
        plus = await self.person()
        free = await self.person(plan="free", consent=False, tokens=("ExponentPushToken[f]",))
        for d in range(3):
            await self.db.todo.create(data={"entryId": (await self.db.entry.create(data={"userId": free.id, "rawTranscript": "t", "inJournal": False})).id,
                                            "userId": free.id, "text": f"todo {d}", "dueDate": datetime(2026, 11, 18, tzinfo=timezone.utc)})
        for hh, mm in ((8, 5), (13, 5), (20, 5)):
            await run.tick(tokyo(hh, mm))
        self.assertEqual([l.moment for l in await self.logs(plus)], ["morning", "midday", "evening"])
        self.assertEqual([l.moment for l in await self.logs(free)], ["morning", "evening"])
        # a fourth moment (a morning moved later) never goes past the cap
        await self.db.usersettings.update(where={"userId": plus.id}, data={"notificationPrefs": Json(
            {**DEFAULT_NOTIFICATION_PREFS, "nudges": True, "nudgeMorningTime": "11:00"})})
        await self.db.nudgelog.delete_many(where={"userId": plus.id, "moment": "morning"})
        await self.db.nudgelog.create(data={"userId": plus.id, "date": datetime(2026, 11, 18, tzinfo=timezone.utc), "moment": "x",
                                            "title": "t", "body": "b", "opens": "life", "source": "rules"})
        self.assertEqual(await run.tick(tokyo(11, 5)), 0)

    async def test_free_user_rules_never_ai(self):
        free = await self.person(plan="free", consent=True)
        self.assertEqual(await run.tick(tokyo(20, 5)), 1)
        self.write.assert_not_awaited()
        self.life.assert_not_awaited()
        [log] = await self.logs(free)
        self.assertEqual((log.title, log.body, log.opens, log.source),
                         ("Evening check-in", "No entry yet today. How was your day?", "record", "rules"))

    async def test_plus_without_consent_uses_rules(self):
        user = await self.person(consent=False)
        self.assertEqual(await run.tick(tokyo(8, 5)), 1)
        self.write.assert_not_awaited()
        [log] = await self.logs(user)
        self.assertEqual((log.source, log.body), ("rules", "1 task today"))

    async def test_midday_without_triggers_no_ai_call_no_send(self):
        await self.person()
        self.assertEqual(await run.tick(tokyo(13, 5)), 0)
        self.write.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_plus_morning_empty_day_no_ai_call_no_send(self):
        self.life.return_value = {**LIFE, "tasksToday": []}
        await self.person()
        self.assertEqual(await run.tick(tokyo(8, 5)), 0)
        self.write.assert_not_awaited()

    async def test_ai_send_false_sends_nothing(self):
        self.write.return_value = None
        user = await self.person()
        self.assertEqual(await run.tick(tokyo(8, 5)), 0)
        self.assertEqual(await self.logs(user), [])

    async def test_writer_failure_falls_back_to_rules(self):
        self.write.side_effect = WriterFailed()
        user = await self.person()
        self.assertEqual(await run.tick(tokyo(8, 5)), 1)
        [log] = await self.logs(user)
        self.assertEqual((log.source, log.body), ("rules", "1 task today"))

    async def test_evening_gets_journal_summaries_only_with_permission(self):
        user = await self.person()
        await self.db.entry.create(data={"userId": user.id, "rawTranscript": "words", "summary": "Worried about the exam.",
                                         "status": "DONE", "inJournal": True,
                                         "createdAt": datetime(2026, 11, 17, 12, tzinfo=TOKYO)})
        await run.tick(tokyo(20, 5))
        self.assertEqual(self.write.await_args.kwargs["summaries"], [])
        await self.db.nudgelog.delete_many(where={"userId": user.id})
        await self.db.usersettings.update(where={"userId": user.id}, data={"assistantJournalConsentAt": datetime.now(timezone.utc)})
        await run.tick(tokyo(20, 6))
        self.assertEqual(self.write.await_args.kwargs["summaries"], ["Worried about the exam."])
        self.assertEqual(self.write.await_args.args[0], "evening")

    async def test_two_devices_one_nudge_one_count(self):
        user = await self.person(tokens=("ExponentPushToken[a]", "ExponentPushToken[b]"))
        self.assertEqual(await run.tick(tokyo(8, 5)), 1)
        [log] = await self.logs(user)
        [msgs], _ = self.send.await_args
        self.assertEqual(sorted(m["to"] for m in msgs), ["ExponentPushToken[a]", "ExponentPushToken[b]"])
        self.assertEqual(len(log.ticketIds), 2)

    async def test_plus_lapse_midday_switches_to_free_rules(self):
        self.life.return_value = {**LIFE, "triggers": ["Gym streak at risk"]}
        user = await self.person()
        await run.tick(tokyo(8, 5))
        await self.db.user.update(where={"id": user.id}, data={"plan": "free"})
        self.assertEqual(await run.tick(tokyo(13, 5)), 0)
        self.assertEqual(await run.tick(tokyo(20, 5)), 1)
        logs = await self.logs(user)
        self.assertEqual([(l.moment, l.source) for l in logs], [("morning", "ai"), ("evening", "rules")])
        self.assertEqual(logs[1].body, "No entry yet today. How was your day?")

    async def test_bad_timezone_uses_utc_and_respects_quiet_hours(self):
        await self.person(tz="UTC")
        for hh, mm in ((22, 5), (23, 5), (2, 5), (5, 5), (7, 35)):
            await run.tick(datetime(2026, 11, 18, hh, mm, tzinfo=timezone.utc))
        self.send.assert_not_awaited()
        await self.person(tz="Not/AZone", tokens=("ExponentPushToken[z]",))
        self.assertEqual(await run.tick(datetime(2026, 11, 18, 8, 5, tzinfo=timezone.utc)), 2)

    async def test_device_not_registered_ticket_deletes_device(self):
        self.send.side_effect = lambda msgs: [{"status": "error", "error": "DeviceNotRegistered"}, {"status": "ok", "id": "t1"}]
        user = await self.person(tokens=("ExponentPushToken[a]", "ExponentPushToken[b]"))
        await run.tick(tokyo(8, 5))
        left = await self.db.pushdevice.find_many(where={"userId": user.id})
        [log] = await self.logs(user)
        self.assertEqual(len(left), 1)
        self.assertEqual(log.ticketIds, [f"t1|{left[0].token}"])

    async def test_receipts_checked_after_20_minutes_delete_dead_device(self):
        user = await self.person()
        await run.tick(tokyo(8, 5))
        self.receipts.return_value = {"t0": {"status": "error", "error": "DeviceNotRegistered"}}
        await run.check_receipts(tokyo(8, 10))
        self.receipts.assert_not_awaited()
        await self.db.nudgelog.update_many(where={"userId": user.id}, data={"createdAt": tokyo(8, 5) - timedelta(minutes=1)})
        await run.check_receipts(tokyo(8, 30))
        self.receipts.assert_awaited_once_with(["t0"])
        self.assertEqual(await self.db.pushdevice.count(where={"userId": user.id}), 0)
        self.assertIsNotNone((await self.logs(user))[0].receiptsCheckedAt)
        await run.check_receipts(tokyo(8, 40))
        self.assertEqual(self.receipts.await_count, 1)

    async def test_kill_switches(self):
        await self.person()
        with patch.dict(os.environ, {"NUDGES_ENABLED": "0"}):
            self.assertEqual(await run.tick(tokyo(8, 5)), 0)
        with patch.dict(os.environ, {"NUDGES_AI": "0"}):
            self.assertEqual(await run.tick(tokyo(8, 5)), 1)
        self.write.assert_not_awaited()

    async def test_race_unique_log_prevents_double_send(self):
        user = await self.person()
        await self.db.nudgelog.create(data={"userId": user.id, "date": datetime(2026, 11, 18, tzinfo=timezone.utc), "moment": "morning",
                                            "title": "t", "body": "b", "opens": "life", "source": "rules"})
        self.assertEqual(await run.tick(tokyo(8, 5)), 0)
        self.send.assert_not_awaited()

    async def test_one_failing_person_never_stops_the_rest(self):
        self.life.side_effect = [RuntimeError("dashboard down"), dict(LIFE)]
        await self.person()
        await self.person(tokens=("ExponentPushToken[c]",))
        self.assertEqual(await run.tick(tokyo(8, 5)), 1)

    async def test_cleanup_ages(self):
        user = await self.person()
        old = datetime.now(timezone.utc) - timedelta(days=31)
        await self.db.nudgelog.create(data={"userId": user.id, "date": datetime(2026, 10, 1, tzinfo=timezone.utc), "moment": "morning",
                                            "title": "t", "body": "b", "opens": "life", "source": "rules", "createdAt": old})
        await self.db.pushdevice.create(data={"userId": user.id, "token": "ExponentPushToken[old]",
                                              "lastSeenAt": datetime.now(timezone.utc) - timedelta(days=61)})
        await run.cleanup(datetime.now(timezone.utc))
        self.assertEqual(await self.logs(user), [])
        self.assertEqual([d.token for d in await self.db.pushdevice.find_many(where={"userId": user.id})], ["ExponentPushToken[a]"])


class ReviewFixes(NudgesRun):
    async def sent_and_aged(self):
        user = await self.person()
        await run.tick(tokyo(8, 5))
        await self.db.nudgelog.update_many(where={"userId": user.id}, data={"createdAt": tokyo(8, 5) - timedelta(minutes=1)})
        return user

    async def test_receipts_request_failure_leaves_rows_unchecked(self):
        user = await self.sent_and_aged()
        self.receipts.return_value = None
        await run.check_receipts(tokyo(8, 30))
        self.assertIsNone((await self.logs(user))[0].receiptsCheckedAt)

    async def test_other_receipt_errors_are_logged(self):
        await self.sent_and_aged()
        self.receipts.return_value = {"t0": {"status": "error", "error": "InvalidCredentials"}}
        with self.assertLogs("vournal", level="WARNING") as logs:
            await run.check_receipts(tokyo(8, 30))
        self.assertTrue(any("InvalidCredentials" in line for line in logs.output))

    async def test_ticket_errors_are_logged(self):
        self.send.side_effect = lambda msgs: [{"status": "error", "error": "MessageRateExceeded"}]
        await self.person()
        with self.assertLogs("vournal", level="WARNING") as logs:
            await run.tick(tokyo(8, 5))
        self.assertTrue(any("MessageRateExceeded" in line for line in logs.output))

    async def test_people_outside_every_window_cost_no_settings_read(self):
        user = await self.person()
        actions = type(run.db.usersettings)
        original = actions.find_unique
        reads = []

        async def find_unique(this, *args, **kwargs):
            reads.append(kwargs.get("where", {}).get("userId"))
            return await original(this, *args, **kwargs)

        with patch.object(actions, "find_unique", find_unique):
            await run.tick(tokyo(16, 0))  # 16:00 in Tokyo: no moment could be due
        self.assertNotIn(user.id, reads)
