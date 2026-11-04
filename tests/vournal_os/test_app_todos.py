"""App to-dos on the web (spec "Shared to-dos"): one list edited on both sides through the
same rules, shown in the calendar with the app's repeat rule, found by search, and counted
on the home screen and in insights. Nothing of another account."""

import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as T
from zoneinfo import ZoneInfo

import httpx

from vournal_os.support import OsTestCase

BEIRUT = ZoneInfo("Asia/Beirut")


def todo(**extra):
    base = {"done": False, "dueDate": datetime(2026, 11, 2, tzinfo=timezone.utc), "startAt": None, "recurrence": None,
            "recurrenceInterval": 1, "recurrenceWeekdays": [], "recurrenceUntil": None}
    return T(**{**base, **extra})


def shown(t, start: str, count: int) -> list[str]:
    """The days a to-do is on, as the app's calendar shows them (calendarRepeats.test.ts)."""
    from app.os.app_todos import occurs_on_day
    from app.todo_rules import local_day

    first = date.fromisoformat(start)
    days = [first + timedelta(days=i) for i in range(count)]
    return [d.isoformat() for d in days if local_day(t, BEIRUT) == d or occurs_on_day(t, d, BEIRUT)]


class Repeats(unittest.TestCase):
    """The app's own cases for occursOnDay."""

    def test_daily_once_done_undated_interval_until(self):
        self.assertEqual(shown(todo(recurrence="daily"), "2026-11-01", 5), ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"])
        self.assertEqual(shown(todo(), "2026-11-01", 5), ["2026-11-02"])
        self.assertEqual(shown(todo(recurrence="daily", done=True), "2026-11-02", 3), ["2026-11-02"])
        self.assertEqual(shown(todo(recurrence="daily", dueDate=None), "2026-11-02", 3), [])
        self.assertEqual(shown(todo(recurrence="daily", recurrenceInterval=2), "2026-11-02", 5), ["2026-11-02", "2026-11-04", "2026-11-06"])
        self.assertEqual(shown(todo(recurrence="daily", recurrenceUntil=datetime(2026, 11, 3, 12, tzinfo=BEIRUT)), "2026-11-02", 5),
                         ["2026-11-02", "2026-11-03"])

    def test_weekly_monthly_yearly(self):
        from app.os.app_todos import occurs_on_day

        self.assertEqual(shown(todo(recurrence="weekly"), "2026-11-02", 15), ["2026-11-02", "2026-11-09", "2026-11-16"])
        self.assertEqual(shown(todo(recurrence="weekly", recurrenceWeekdays=["MO", "TH"]), "2026-11-02", 8), ["2026-11-02", "2026-11-05", "2026-11-09"])
        self.assertEqual(shown(todo(recurrence="weekly", recurrenceInterval=2, recurrenceWeekdays=["MO", "TH"]), "2026-11-02", 15),
                         ["2026-11-02", "2026-11-05", "2026-11-16"])
        monthly = todo(recurrence="monthly", dueDate=datetime(2026, 10, 31, tzinfo=timezone.utc))
        self.assertEqual((occurs_on_day(monthly, date(2026, 11, 30), BEIRUT), occurs_on_day(monthly, date(2026, 12, 31), BEIRUT)), (False, True))
        yearly = todo(recurrence="yearly")
        self.assertEqual((occurs_on_day(yearly, date(2027, 11, 2), BEIRUT), occurs_on_day(yearly, date(2027, 11, 3), BEIRUT)), (True, False))


class AppTodosOnTheWeb(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app import security
        from app.main import app

        self.web = await self.signed_in(await self.make_user(timezone="Asia/Beirut"))
        await self.web.patch("/api/settings", json={"timezone": "Asia/Beirut"})
        user = self.web.user
        token = security.create_access_token(user.id, user.sessionGeneration)
        self.app = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"})
        self.addAsyncCleanup(self.app.aclose)
        self.entry = await self.db.entry.create(data={"userId": user.id, "rawTranscript": "Things to do", "emotions": []})
        self.today = datetime.now(BEIRUT).date()

    async def make(self, text, **fields):
        due = fields.pop("due", self.today)
        return await self.db.todo.create(data={"entryId": self.entry.id, "userId": self.web.user.id, "text": text,
                                               "dueDate": datetime(due.year, due.month, due.day, tzinfo=timezone.utc) if due else None, **fields})

    async def test_web_and_app_edits_give_the_same_rows(self):
        web_twin = await self.make("Water plants", recurrence="daily")
        app_twin = await self.make("Water plants", recurrence="daily")
        web = await self.web.patch(f"/api/app-todos/{web_twin.id}", json={"done": True, "advance": True})
        self.assertEqual(web.status_code, 200, web.text)
        await self.app.patch(f"/todos/{app_twin.id}", json={"done": True, "advance": True})
        details = {"date": (self.today + timedelta(days=3)).isoformat(), "time": "18:30", "urgency": 2}
        await self.web.patch(f"/api/app-todos/{web_twin.id}/details", json=details)
        await self.app.patch(f"/todos/{app_twin.id}/details", json=details)
        a = await self.db.todo.find_unique(where={"id": web_twin.id})
        b = await self.db.todo.find_unique(where={"id": app_twin.id})
        same = ("done", "dueDate", "startAt", "urgency", "urgencyUnclear", "recurrence", "dismissedQuestions")
        self.assertEqual({k: getattr(a, k) for k in same}, {k: getattr(b, k) for k in same})
        self.assertEqual(a.startAt.astimezone(BEIRUT).strftime("%H:%M"), "18:30")
        refused = await self.web.patch(f"/api/app-todos/{web_twin.id}/details", json={"date": None, "time": "09:00"})
        self.assertEqual(refused.json(), {"statusCode": 422, "message": "Choose a date before adding a time.", "error": "Unprocessable Entity"})
        listed = (await self.web.get("/api/app-todos?done=false")).json()
        self.assertEqual({(t["text"], t["source"], t["entryId"]) for t in listed["items"]}, {("Water plants", "app", self.entry.id)})
        self.assertEqual((await self.web.delete(f"/api/app-todos/{web_twin.id}")).json(), {"ok": True})
        self.assertIsNone(await self.db.todo.find_unique(where={"id": web_twin.id}))

    async def test_another_accounts_todo_is_not_found(self):
        mine = await self.make("Mine")
        other = await self.signed_in()
        for method, path, body in (("PATCH", "", {"done": True}), ("PATCH", "/details", {"urgency": 1}), ("DELETE", "", None)):
            response = await other.request(method, f"/api/app-todos/{mine.id}{path}", json=body)
            self.assertEqual((response.status_code, response.json()["message"]), (404, "Todo not found"))
        self.assertEqual((await other.get("/api/app-todos")).json(), {"items": [], "total": 0})
        self.assertEqual((await other.get("/api/search?q=Mine")).json(), {"results": []})

    async def test_calendar_search_dashboard_and_insights(self):
        yoga = await self.make("Yoga", recurrence="weekly", recurrenceWeekdays=["MO", "TH"], due=date(2026, 11, 2))
        await self.make("Call the bank", due=self.today, urgency=3)
        await self.make("Renew passport", due=self.today - timedelta(days=4))
        await self.make("Someday maybe", due=None)
        feed = (await self.web.get("/api/calendar/feed?from=2026-11-01&to=2026-11-16&types=appTodo")).json()
        yoga_days = [i["start"] for i in feed["items"] if i["sourceId"] == yoga.id]
        self.assertEqual(yoga_days, ["2026-11-02", "2026-11-05", "2026-11-09", "2026-11-12", "2026-11-16"])
        first = next(i for i in feed["items"] if i["sourceId"] == yoga.id)
        self.assertEqual((first["sourceType"], first["link"], first["meta"]["entryId"]), ("appTodo", f"/journal/{self.entry.id}", self.entry.id))
        found = (await self.web.get("/api/search?q=bank")).json()["results"]
        self.assertEqual([(r["type"], r["title"]) for r in found], [("todo", "Call the bank")])
        home = (await self.web.get("/api/dashboard")).json()
        today_titles = [t["title"] for t in home["todayTasks"]["items"]]
        self.assertIn("Call the bank", today_titles)
        # a to-do counts on its own day (ticking a repeat moves that day on): Yoga, unticked since 2 November, is overdue
        self.assertEqual((home["overdueTasks"]["count"], home["counts"]["tasksToday"]), (2, 1))
        insights = {i["id"]: i for i in (await self.web.get("/api/insights")).json()}
        self.assertEqual(insights["tasks-overdue"]["title"], "You have 2 overdue tasks")

    async def test_free_account_sees_plus_required(self):
        free = await self.signed_in(await self.make_user(plan="free"))
        self.assertEqual((await free.get("/api/app-todos")).status_code, 403)


if __name__ == "__main__":
    unittest.main()
