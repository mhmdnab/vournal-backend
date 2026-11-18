"""Facts and rule-based nudge text (design 2026-11-18). Free users get only these; Plus users get
them when the AI can't write (no agreement, a failure, NUDGES_AI=0). No amounts of money ever."""

import json
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app.nudges.rules import Nudge, app_facts, free_text, life_facts, plus_fallback_text
from vournal_os.support import OsTestCase

TZ = ZoneInfo("Asia/Beirut")


def dashboard(**over):
    base = {
        "todayTasks": {"items": [{"title": "Finish report", "status": "TODO"}, {"title": "Done one", "status": "COMPLETED"}]},
        "overdueTasks": {"count": 0},
        "counts": {"eventsToday": 1, "remindersToday": 0},
        "upcomingBills": {"items": [{"name": "Rent", "daysUntil": 2, "isNext": True, "amountDefault": 600.0, "amount": 600}]},
        "habits": {"items": [{"name": "Gym", "currentStreak": 5, "doneToday": False, "frequency": "DAILY"}]},
        "goals": {"items": [{"name": "Save for car", "progress": 40}]},
        "birthdays": {"items": [{"title": "Sara's birthday", "daysUntil": 4}]},
        "budgets": {"items": [{"name": "Food", "percentUsed": 50, "isOver": False, "spent": 120.5}]},
    }
    base.update(over)
    return base


class Rules(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.user = await self.make_user()
        self.today = datetime.now(TZ).date()

    async def todo(self, day: date | None, done=False, title="Call mom"):
        due = datetime.combine(day, time(0), tzinfo=timezone.utc) if day else None
        if not hasattr(self, "parent"):
            self.parent = await self.db.entry.create(data={"userId": self.user.id, "rawTranscript": "todos", "inJournal": False})
        await self.db.todo.create(data={"entryId": self.parent.id, "userId": self.user.id, "text": title, "dueDate": due, "done": done})

    async def entry(self, days_ago: int, **data):
        when = datetime.combine(self.today - timedelta(days=days_ago), time(12), tzinfo=TZ)
        await self.db.entry.create(data={"userId": self.user.id, "rawTranscript": "a day", "status": "DONE",
                                         "inJournal": True, "createdAt": when, **data})

    async def facts(self):
        return await app_facts(self.user, TZ, self.today)

    async def test_todos_due_counts_today_only_and_not_done(self):
        await self.todo(self.today)
        await self.todo(self.today, title="Second")
        await self.todo(self.today, done=True)
        await self.todo(self.today + timedelta(days=1))
        await self.todo(None)
        self.assertEqual((await self.facts())["todosDue"], 2)

    async def test_streak_counts_back_from_today_or_yesterday(self):
        for d in (0, 1, 2):
            await self.entry(d)
        f = await self.facts()
        self.assertEqual((f["recordedToday"], f["streak"]), (True, 3))
        await self.db.entry.delete_many(where={"userId": self.user.id})
        for d in (1, 2):
            await self.entry(d)
        f = await self.facts()
        self.assertEqual((f["recordedToday"], f["streak"]), (False, 2))
        await self.db.entry.delete_many(where={"userId": self.user.id})
        for d in (0, 2, 3):
            await self.entry(d)
        self.assertEqual((await self.facts())["streak"], 1)

    async def test_hidden_entries_dont_count(self):
        await self.entry(0, reviewState="asked")
        f = await self.facts()
        self.assertEqual((f["recordedToday"], f["streak"]), (False, 0))

    async def test_free_text_copy(self):
        self.assertEqual(free_text("morning", {"todosDue": 1, "recordedToday": False, "streak": 0}),
                         Nudge("Today", "1 to-do due today.", "life"))
        self.assertEqual(free_text("morning", {"todosDue": 3, "recordedToday": False, "streak": 0}).body, "3 to-dos due today.")
        self.assertIsNone(free_text("morning", {"todosDue": 0, "recordedToday": False, "streak": 0}))
        self.assertEqual(free_text("evening", {"todosDue": 0, "recordedToday": False, "streak": 5}),
                         Nudge("Evening check-in", "No entry yet today. Keep your 5-day streak going.", "record"))
        self.assertEqual(free_text("evening", {"todosDue": 0, "recordedToday": False, "streak": 1}).body,
                         "No entry yet today. How was your day?")
        self.assertIsNone(free_text("evening", {"todosDue": 0, "recordedToday": True, "streak": 9}))
        self.assertIsNone(free_text("midday", {"todosDue": 4, "recordedToday": False, "streak": 9}))

    async def life(self, data, insights=()):
        with patch("app.nudges.rules.get_dashboard", AsyncMock(return_value=data)), \
             patch("app.nudges.rules.insights_for", AsyncMock(return_value=list(insights))):
            return await life_facts(self.user)

    async def test_life_facts_has_no_amounts(self):
        facts = await self.life(dashboard())
        text = json.dumps(facts)
        self.assertNotIn("600", text)
        self.assertNotIn("120.5", text)
        self.assertEqual(facts["tasksToday"], ["Finish report"])
        self.assertEqual(facts["billsSoon"], [{"name": "Rent", "inDays": 2}])
        self.assertEqual(facts["birthdays"], [])  # 4 days away

    async def test_midday_triggers(self):
        facts = await self.life(dashboard(), insights=[{"id": "habit-risk:h1", "title": "Gym streak at risk: 5 days"}])
        self.assertEqual(len(facts["triggers"]), 1)
        quiet = await self.life(dashboard())
        self.assertEqual(quiet["triggers"], [])
        busy = await self.life(dashboard(
            upcomingBills={"items": [{"name": "Internet", "daysUntil": 1, "isNext": True, "amountDefault": 30.0}]},
            birthdays={"items": [{"title": "Sara's birthday", "daysUntil": 1}]},
            budgets={"items": [{"name": "Food", "percentUsed": 95, "isOver": False}]},
        ), insights=[{"id": "tasks-overdue", "title": "2 overdue tasks"}, {"id": "goal-behind:g1", "title": "Save for car is behind"}])
        # 4, not 5: "tasks-overdue" fires for any old overdue task; follow-ups are only tasks overdue since yesterday
        self.assertEqual(len(busy["triggers"]), 4)
        self.assertNotIn("30", json.dumps(busy))

    async def test_sections_off_give_empty_values(self):
        facts = await self.life({k: None for k in dashboard()} | {"counts": {"eventsToday": 0, "remindersToday": 0}})
        self.assertEqual((facts["tasksToday"], facts["billsSoon"], facts["habits"], facts["triggers"]), ([], [], [], []))

    async def test_plus_morning_fallback_none_on_empty_day(self):
        empty = {"tasksToday": [], "overdue": 0, "events": 0, "reminders": 0, "billsSoon": [], "habits": [], "goals": [],
                 "birthdays": [], "triggers": []}
        app = {"todosDue": 0, "recordedToday": False, "streak": 0}
        self.assertIsNone(plus_fallback_text("morning", app, empty))
        full = await self.life(dashboard())
        n = plus_fallback_text("morning", app, full)
        self.assertEqual((n.title, n.opens), ("Your day", "life"))
        self.assertIn("1 task today", n.body)
        self.assertLessEqual(len(n.body), 140)
        self.assertIsNone(plus_fallback_text("midday", app, full))
        self.assertEqual(plus_fallback_text("evening", app, full), free_text("evening", app))


class ReviewFixes(Rules):
    async def test_plus_morning_counts_app_todos(self):
        empty = {"tasksToday": [], "overdue": 0, "events": 0, "reminders": 0, "billsSoon": [], "habits": [], "goals": [],
                 "birthdays": [], "triggers": []}
        n = plus_fallback_text("morning", {"todosDue": 3, "recordedToday": False, "streak": 0}, empty)
        self.assertIsNotNone(n)
        self.assertIn("3 to-dos today", n.body)

    async def test_only_recently_overdue_tasks_are_a_trigger(self):
        today = date(2026, 11, 18)
        old = {"title": "Old thing", "dueAt": "2026-10-01T09:00:00.000Z", "isOverdue": True}
        app_todo = {"title": "Buy milk", "dueAt": "2026-11-17T09:00:00.000Z", "isOverdue": True, "source": "app"}
        base = dashboard(today=today.isoformat(), timezone="UTC", overdueTasks={"count": 2, "items": [old, app_todo]})
        quiet = await self.life(base, insights=[{"id": "tasks-overdue", "title": "2 overdue tasks"}])
        self.assertEqual(quiet["triggers"], [])
        fresh = {"title": "Finish report", "dueAt": "2026-11-17T15:00:00.000Z", "isOverdue": True}
        busy = await self.life(dashboard(today=today.isoformat(), timezone="UTC", overdueTasks={"count": 3, "items": [old, fresh]}))
        self.assertEqual(busy["triggers"], ["Finish report is overdue"])
