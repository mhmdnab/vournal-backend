"""Small leftovers from the parts 1–3 reviews (2026-11-18): an opt-in for question entries, the
journal tools behind the permission in the runner too, the quota given back only in its own month,
an /ask throttle, an atomic prefs merge, failed pushes sent again, Arabic rule-based nudges, no
dashboard read for a midday that can't be written, no settings row on a read, and the dashboard's
overdue count without the app's to-dos. Fake models only."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.nudges import run
from app.nudges.rules import Nudge, free_text, plus_fallback_text
from vournal_os.parity import fake_model as fm
from vournal_os.support import OsTestCase
# modules, not classes: a class imported here would run all its tests again in this file
from vournal_os import test_ask_routes as ask_tests, test_entry_actions as action_tests, test_nudges_run as nudge_tests

QUESTION, LIFE, tokyo = ask_tests.QUESTION, nudge_tests.LIFE, nudge_tests.tokyo


# ----- B1: only an app that knows about questions gets them held back ----------------------


class QuestionOptIn(action_tests.EntryActionsBase):
    result = {**QUESTION, "wantsActions": True}

    async def test_an_older_app_still_gets_actions_on_a_question(self):
        fm.script(fm.message(fm.text("Nothing to add.")))
        entry = await self.record("how much did I spend on food?")  # no "ask": an older build
        self.assertNotIn("askable", entry.proposal)
        self.assertNotEqual(fm.requests("python"), [])

    async def test_a_newer_app_gets_the_question_held_back(self):
        fm.script()
        r = await self.app.post("/entries", json={"transcript": "how much did I spend on food?", "review": True, "ask": True})
        self.assertEqual(r.status_code, 200, r.text)
        entry = await self.db.entry.find_unique(where={"id": r.json()["id"]})
        self.assertIs(entry.proposal["askable"], True)
        self.assertEqual(fm.requests("python"), [])

    async def test_audio_upload_takes_the_opt_in_too(self):
        import inspect

        from app import main

        self.assertIn("ask", inspect.signature(main.create_audio_entry).parameters)
        self.assertIn("ask", inspect.signature(main.process_audio_entry).parameters)


# ----- B2: the journal tools need the permission in the runner, and imports never get them ---


class JournalToolGate(OsTestCase):
    async def ctx(self, **attrs):
        from app.os import assistant_tools

        user = await self.make_user()
        ctx = assistant_tools.Ctx(user, "Asia/Beirut", assistant_tools.local_day(datetime.now(timezone.utc), "Asia/Beirut"), "auto")
        for k, v in attrs.items():
            setattr(ctx, k, v)
        return ctx

    async def test_run_refuses_a_journal_tool_without_permission(self):
        from app.os import assistant_tools

        out = await assistant_tools.run("search_journal", {"query": "Ali"}, await self.ctx())
        self.assertTrue(out.get("isError"))
        self.assertIn("permission", out["content"].lower())

    async def test_run_allows_a_journal_tool_with_permission(self):
        from app.os import assistant_tools

        out = await assistant_tools.run("search_journal", {"query": "Ali"}, await self.ctx(journal=True))
        self.assertFalse(out.get("isError"), out)

    def test_an_import_gets_only_the_import_tools(self):
        from app.os.assistant import tools_for
        from app.os.assistant_tools import IMPORT_TOOLS

        names = {t["name"] for t in tools_for(True, importing=True)}
        self.assertTrue(names)
        self.assertLessEqual(names, set(IMPORT_TOOLS))
        self.assertNotIn("search_journal", names)
        self.assertIn("search_journal", {t["name"] for t in tools_for(True)})


# ----- B4 and B5: an /ask throttle; a question from last month gives back last month's entry --


class AskLimits(ask_tests.AskBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app import rate_limit

        p = patch.dict(rate_limit._hits, {}, clear=True)
        p.start()
        self.addCleanup(p.stop)

    async def test_too_many_asks_in_a_row_are_refused(self):
        from app import ask_routes

        for _ in range(ask_routes.ASK_LIMIT):
            entry = await self.record()
            await self.ask(entry.id)
        entry = await self.record()
        r = await self.app.post(f"/entries/{entry.id}/ask", json={})
        self.assertEqual(r.status_code, 429, r.text)
        self.assertEqual(await self.state(entry.id), "needs_review")

    async def test_a_question_from_last_month_leaves_this_month_alone(self):
        entry = await self.record()
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        await self.db.entry.update(where={"id": entry.id}, data={"createdAt": month_start - timedelta(days=2)})
        await self.db.user.update(where={"id": self.user.id}, data={"usagePeriodStart": month_start, "usagePeriodEntries": 4})
        await self.ask(entry.id)
        self.assertEqual(await self.used(), 4)

    async def test_a_question_from_this_month_gives_its_entry_back(self):
        entry = await self.record()
        before = await self.used()
        await self.ask(entry.id)
        self.assertEqual(await self.used(), before - 1)


# ----- B6 and B10: the prefs -------------------------------------------------------------


class PrefsWrites(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.app = await self.mobile()

    async def test_reading_prefs_makes_no_settings_row(self):
        r = await self.app.get("/nudges/prefs")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["nudges"], False)
        self.assertIsNone(await self.db.usersettings.find_unique(where={"userId": self.app.user.id}))

    async def test_a_stale_read_never_drops_another_patch(self):
        """The app and the website patch at the same moment: each keeps the other's change."""
        from app.os.profile import ensure_os_profile

        await ensure_os_profile(self.app.user)
        web = await self.signed_in(self.app.user)
        stale = await self.db.usersettings.find_unique(where={"userId": self.app.user.id})
        # both requests read the row before either writes
        with patch("app.nudge_routes.ensure_os_profile", AsyncMock(return_value=stale)):
            r = await self.app.patch("/nudges/prefs", json={"nudges": True})
            self.assertEqual(r.status_code, 200, r.text)
        actions = type(self.db.usersettings)
        original = actions.find_unique

        async def stale_read(this, *args, **kwargs):
            row = await original(this, *args, **kwargs)
            return row.model_copy(update={"notificationPrefs": stale.notificationPrefs}) if row else row

        with patch.object(actions, "find_unique", stale_read):
            r = await web.patch("/api/settings", json={"notificationPrefs": {"bills": False}})
            self.assertEqual(r.status_code, 200, r.text)
        prefs = (await original(self.db.usersettings, where={"userId": self.app.user.id})).notificationPrefs
        self.assertEqual((prefs["nudges"], prefs["bills"]), (True, False))


# ----- B7, B9, B11: the nudge job ---------------------------------------------------------


class NudgeJob(nudge_tests.NudgesRun):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.send.side_effect = lambda msgs, **kw: [{"status": "ok", "id": f"t{i}"} for i, _ in enumerate(msgs)]

    async def test_a_failed_send_is_sent_again_with_the_same_text(self):
        user = await self.person()
        self.send.side_effect = lambda msgs, **kw: [{"status": "error", "error": None} for _ in msgs]
        with self.assertLogs("vournal", level="WARNING"):
            await run.tick(tokyo(8, 5))
        self.send.side_effect = lambda msgs, **kw: [{"status": "ok", "id": "t9"} for _ in msgs]
        self.assertEqual(await run.tick(tokyo(8, 7)), 0)  # too soon to try again
        self.assertEqual(await run.tick(tokyo(8, 11)), 1)
        self.assertEqual(self.write.await_count, 1)  # the text is reused: no second AI call
        [log] = await self.logs(user)
        self.assertEqual(log.ticketIds, ["t9|ExponentPushToken[a]"])
        self.assertEqual(self.send.await_args_list[0].args[0][0]["body"], self.send.await_args_list[-1].args[0][0]["body"])

    async def fail_with(self, error):
        self.send.side_effect = lambda msgs, **kw: [{"status": "error", "error": error} for _ in msgs]

    async def test_a_failed_send_is_retried_once_only(self):
        """Final review: a retry can duplicate a push Expo took after all, so there's one."""
        await self.person()
        await self.fail_with(None)
        with self.assertLogs("vournal", level="WARNING"):
            await run.tick(tokyo(8, 5))
            await run.tick(tokyo(8, 11))
            self.assertEqual(await run.tick(tokyo(8, 17)), 0)
        self.assertEqual(self.send.await_count, 2)

    async def test_an_unclear_answer_or_a_message_error_is_not_resent(self):
        for error in ("NoAnswer", "MessageRateExceeded"):
            await self.fail_with(error)
            await self.person(tokens=(f"ExponentPushToken[{error}]",))
            with self.assertLogs("vournal", level="WARNING"):
                await run.tick(tokyo(8, 5))
        before = self.send.await_count
        self.assertEqual(await run.tick(tokyo(8, 11)), 0)
        self.assertEqual(self.send.await_count, before)

    async def test_an_evening_retry_is_dropped_once_they_recorded(self):
        user = await self.person()
        await self.fail_with(None)
        with self.assertLogs("vournal", level="WARNING"):
            await run.tick(tokyo(20, 5))
        await self.db.entry.create(data={"userId": user.id, "rawTranscript": "x", "status": "DONE", "inJournal": True,
                                         "createdAt": tokyo(20, 7)})
        self.send.side_effect = lambda msgs, **kw: [{"status": "ok", "id": "t9"} for _ in msgs]
        self.assertEqual(await run.tick(tokyo(20, 11)), 0)
        self.assertEqual(self.send.await_count, 1)

    async def test_a_time_zone_postgres_lacks_still_counts_the_streak(self):
        from zoneinfo import ZoneInfo

        from app.nudges import rules

        user = await self.make_user()
        cls = type(rules.db)
        real = cls.query_raw
        calls = []

        async def picky(this, sql, *args):
            calls.append(args[-1])
            if args[-1] != "UTC":
                raise RuntimeError('time zone "Asia/Tokyo" not recognized')
            return await real(this, sql, *args)

        with patch.object(cls, "query_raw", picky):
            facts = await rules.app_facts(user, ZoneInfo("Asia/Tokyo"), datetime(2026, 11, 18).date())
        self.assertEqual(facts["streak"], 0)
        self.assertEqual(calls, ["Asia/Tokyo", "UTC"])

    async def test_a_delivered_nudge_is_never_sent_again(self):
        await self.person()
        await run.tick(tokyo(8, 5))
        self.assertEqual(await run.tick(tokyo(8, 15)), 0)
        self.assertEqual(self.send.await_count, 1)

    async def test_a_missed_window_is_not_retried(self):
        await self.person()
        self.send.side_effect = lambda msgs, **kw: [{"status": "error", "error": None} for _ in msgs]
        with self.assertLogs("vournal", level="WARNING"):
            await run.tick(tokyo(8, 5))
        self.assertEqual(await run.tick(tokyo(8, 40)), 0)
        self.assertEqual(self.send.await_count, 1)

    async def test_midday_without_ai_agreement_reads_no_dashboard(self):
        self.life.return_value = {**LIFE, "triggers": ["Rent is due tomorrow"]}
        await self.person(consent=False)
        self.assertEqual(await run.tick(tokyo(13, 5)), 0)
        self.life.assert_not_awaited()

    async def test_one_http_client_for_the_whole_tick(self):
        await self.person()
        await self.person(tokens=("ExponentPushToken[b]",))
        await run.tick(tokyo(8, 5))
        clients = [c.kwargs.get("client") for c in self.send.await_args_list]
        self.assertEqual(len(clients), 2)
        self.assertIsNotNone(clients[0])
        self.assertIs(clients[0], clients[1])

    async def test_settings_are_read_once_for_everyone_due(self):
        await self.person()
        await self.person(tokens=("ExponentPushToken[b]",))
        actions = type(run.db.usersettings)
        calls = {"find_unique": 0, "find_many": 0}
        originals = {name: getattr(actions, name) for name in calls}

        def counting(name):
            async def wrapper(this, *args, **kwargs):
                calls[name] += 1
                return await originals[name](this, *args, **kwargs)
            return wrapper

        with patch.object(actions, "find_unique", counting("find_unique")), patch.object(actions, "find_many", counting("find_many")):
            await run.tick(tokyo(8, 5))
        self.assertEqual(calls, {"find_unique": 0, "find_many": 1})

    async def test_streak_counts_local_days(self):
        from zoneinfo import ZoneInfo

        from app.nudges.rules import app_facts

        user = await self.make_user()
        tz = ZoneInfo("Asia/Tokyo")
        today = datetime(2026, 11, 18, 12, tzinfo=tz).date()
        for days_ago, hour in ((0, 1), (1, 23), (1, 8), (2, 0)):
            day = today - timedelta(days=days_ago)
            await self.db.entry.create(data={"userId": user.id, "rawTranscript": "x", "status": "DONE", "inJournal": True,
                                             "createdAt": datetime(day.year, day.month, day.day, hour, 30, tzinfo=tz)})
        await self.db.entry.create(data={"userId": user.id, "rawTranscript": "x", "status": "DONE", "inJournal": False,
                                         "createdAt": datetime(2026, 11, 15, 12, tzinfo=tz)})
        facts = await app_facts(user, tz, today)
        self.assertEqual((facts["recordedToday"], facts["streak"]), (True, 3))


# ----- B8: rule-based nudges in Arabic -------------------------------------------------------


class ArabicRules(OsTestCase):
    def test_free_text_in_arabic(self):
        morning = free_text("morning", {"todosDue": 3, "recordedToday": False, "streak": 0}, "ar")
        self.assertEqual(morning, Nudge("اليوم", "3 مهام مستحقة اليوم.", "life"))
        self.assertEqual(free_text("morning", {"todosDue": 1, "recordedToday": False, "streak": 0}, "ar").body, "مهمة واحدة مستحقة اليوم.")
        evening = free_text("evening", {"todosDue": 0, "recordedToday": False, "streak": 5}, "ar")
        self.assertEqual(evening.title, "مراجعة المساء")
        self.assertIn("5", evening.body)
        self.assertEqual(free_text("evening", {"todosDue": 0, "recordedToday": False, "streak": 0}, "ar").body,
                         "لا يوجد تسجيل اليوم بعد. كيف كان يومك؟")

    def test_plus_morning_in_arabic(self):
        life = {**LIFE, "tasksToday": ["Report", "Gym"], "billsSoon": [{"name": "Rent", "inDays": 1}]}
        n = plus_fallback_text("morning", {"todosDue": 1}, life, "ar")
        self.assertEqual(n.title, "يومك")
        self.assertEqual(n.body, "3 مهام اليوم · Rent مستحق غدًا")

    def test_english_unchanged(self):
        self.assertEqual(free_text("morning", {"todosDue": 1, "recordedToday": False, "streak": 0}).body, "1 to-do due today.")


class NudgeJobArabic(nudge_tests.NudgesRun):
    async def test_rules_follow_the_account_language(self):
        user = await self.person(consent=False)
        await self.db.usersettings.update(where={"userId": user.id}, data={"language": "ar"})
        await run.tick(tokyo(8, 5))
        [log] = await self.logs(user)
        self.assertEqual((log.title, log.body), ("يومك", "مهمة واحدة اليوم"))


# ----- dashboard: the overdue count without the app's to-dos ----------------------------------


class OverdueTaskCount(OsTestCase):
    async def test_task_count_leaves_out_app_todos(self):
        web = await self.signed_in()
        now = datetime.now(timezone.utc)
        await self.db.task.create(data={"userId": web.user.id, "title": "Old task", "dueAt": now - timedelta(days=3), "status": "TODO"})
        home = (await web.get("/api/dashboard")).json()
        self.assertEqual(home["overdueTasks"]["taskCount"], 1)
