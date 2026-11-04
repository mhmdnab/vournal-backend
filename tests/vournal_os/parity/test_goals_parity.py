"""Goals and milestones answer like NestJS did: value, milestone and status progress,
financial goals with contributions in other currencies and a linked account, linked
tasks, projects and habits, the summary, and every milestone route."""

import unittest
from datetime import datetime, timedelta, timezone

from vournal_os.parity.harness import ParityCase, copy_reference_market_rates


def day(offset: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset)).date().isoformat()


class GoalsParity(ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await copy_reference_market_rates()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))
        self.assertParity(*await self.both("PUT", "/exchange-rates", {"rates": {"LBP": 89500, "EUR": 0.9}}))

    async def pair(self, method, path, body=None, ids=None):
        out = []
        for i, (client, prefix) in enumerate(((self.nest, ""), (self.py, "/api"))):
            payload = {**(body or {}), **{k: v[i] for k, v in (ids or {}).items()}}
            p = path if isinstance(path, str) else path[i]
            out.append(await client.request(method, f"{prefix}{p}", json=payload))
        self.assertParity(*out)
        return tuple(r.json().get("id") for r in out)

    async def both_get(self, paths):
        self.assertParity(await self.nest.get(paths[0]), await self.py.get(f"/api{paths[1]}"))

    async def test_goals(self):
        books = await self.pair("POST", "/goals", {"name": " Books ", "type": "EDUCATION", "targetValue": 24, "unit": "books", "startDate": day(-100), "deadline": day(200)})
        trip = await self.pair("POST", "/goals", {"name": "Trip fund", "type": "FINANCIAL", "targetValue": 3000, "currency": "EUR", "deadline": day(90)})
        account = await self.pair("POST", "/accounts", {"name": "Savings", "initialBalance": 1200})
        car = await self.pair("POST", "/goals", {"name": "Car", "type": "FINANCIAL", "targetValue": 10000, "deadline": day(-10)}, {"linkedAccountId": account})
        habit_goal = await self.pair("POST", "/goals", {"name": "Fit", "type": "FITNESS"})
        await self.pair("POST", "/goals", {"name": "Old", "status": "ABANDONED"})
        await self.pair("POST", "/transactions", {"type": "INCOME", "amount": 500, "title": "save"}, {"goalId": trip})
        await self.pair("POST", "/transactions", {"type": "INCOME", "amount": 8950000, "currency": "LBP", "title": "save lira"}, {"goalId": trip})
        await self.pair("POST", "/transactions", {"type": "EXPENSE", "amount": 200, "title": "spent"}, {"accountId": account})
        for title, extra in (("m1", {"targetValue": 6}), ("m2", {"targetValue": 12, "dueDate": day(-5)}), ("m3", {"dueDate": day(30)})):
            await self.pair("POST", (f"/goals/{books[0]}/milestones", f"/goals/{books[1]}/milestones"), {"title": title, **extra})
        await self.pair("POST", (f"/goals/{habit_goal[0]}/milestones", f"/goals/{habit_goal[1]}/milestones"), {"title": "Week one"})
        await self.pair("POST", "/tasks", {"title": "Read Dune", "dueAt": day(-2)}, {"goalId": books})
        await self.pair("POST", "/projects", {"name": "Library"}, {"goalId": books})
        habit = await self.pair("POST", "/habits", {"name": "Pushups", "startDate": day(-5)}, {"goalId": habit_goal})
        for offset in (-3, -2, -1):
            await self.pair("POST", (f"/habits/{habit[0]}/check", f"/habits/{habit[1]}/check"), {"date": day(offset)})
        for query in ("", "?status=ACTIVE", "?type=FINANCIAL,EDUCATION", "?q=car", "?status=DONE"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/goals{query}"))
        self.assertParity(*await self.both("GET", "/goals/summary"))
        for ids in (books, trip, car, habit_goal):
            await self.both_get((f"/goals/{ids[0]}", f"/goals/{ids[1]}"))
        for body in ({"delta": 5}, {"value": 13}, {"delta": -20}, {"value": 30}, {}, {"delta": 1, "value": 1}):
            with self.subTest(body=body):
                await self.pair("POST", (f"/goals/{books[0]}/progress", f"/goals/{books[1]}/progress"), body)
        await self.pair("POST", (f"/goals/{car[0]}/progress", f"/goals/{car[1]}/progress"), {"delta": 1})
        await self.pair("POST", (f"/goals/{trip[0]}/progress", f"/goals/{trip[1]}/progress"), {"delta": 1000.5})
        for body in ({"status": "PAUSED"}, {"status": "ACTIVE", "deadline": None}, {"type": "PERSONAL"}, {"name": None}, {"startDate": day(10), "deadline": day(5)}):
            with self.subTest(body=body):
                await self.pair("PATCH", (f"/goals/{trip[0]}", f"/goals/{trip[1]}"), body)
        ms = [m["id"] for m in (await self.nest.get(f"/goals/{books[0]}/milestones")).json()["items"]], \
             [m["id"] for m in (await self.py.get(f"/api/goals/{books[1]}/milestones")).json()["items"]]
        await self.both_get((f"/goals/{books[0]}/milestones", f"/goals/{books[1]}/milestones"))
        for i, (method, suffix, body) in enumerate((("POST", "/toggle", {}), ("POST", "/toggle", {"completed": True}), ("PATCH", "", {"completed": False, "title": "renamed"}), ("PATCH", "", {"dueDate": None}))):
            with self.subTest(step=i):
                await self.pair(method, (f"/goals/{books[0]}/milestones/{ms[0][2]}{suffix}", f"/goals/{books[1]}/milestones/{ms[1][2]}{suffix}"), body)
        self.assertParity(
            await self.nest.post(f"/goals/{books[0]}/milestones/reorder", json={"items": [{"id": m, "sortOrder": 9 - n} for n, m in enumerate(ms[0])]}),
            await self.py.post(f"/api/goals/{books[1]}/milestones/reorder", json={"items": [{"id": m, "sortOrder": 9 - n} for n, m in enumerate(ms[1])]}),
        )
        self.assertParity(await self.nest.delete(f"/goals/{books[0]}/milestones/{ms[0][0]}"), await self.py.delete(f"/api/goals/{books[1]}/milestones/{ms[1][0]}"))
        await self.both_get((f"/goals/{books[0]}", f"/goals/{books[1]}"))
        self.assertParity(await self.nest.delete(f"/goals/{books[0]}"), await self.py.delete(f"/api/goals/{books[1]}"))

    async def test_validation(self):
        for body in ({}, {"name": "x", "type": "HOBBY"}, {"name": "x", "targetValue": -1}, {"name": "x", "deadline": "2026-1-1"},
                     {"name": "x", "startDate": day(5), "deadline": day(1)}, {"name": "x", "currency": "BTC"}, {"name": "x", "extra": 1}):
            with self.subTest(body=body):
                self.assertParity(*await self.both("POST", "/goals", body))


if __name__ == "__main__":
    unittest.main()
