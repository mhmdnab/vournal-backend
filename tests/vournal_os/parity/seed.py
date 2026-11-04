"""The same life on both backends, for the overview sections (insights, dashboard, reports,
search): money this month and last, budgets, bills, tasks, documents, maintenance, people,
habits, goals and projects, all relative to today in Beirut."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

BEIRUT = ZoneInfo("Asia/Beirut")


def today() -> date:
    return datetime.now(BEIRUT).date()


def day(offset: int) -> str:
    return (today() + timedelta(days=offset)).isoformat()


def at(day_iso: str, hh: int, mm: int = 0) -> str:
    """A Beirut wall-clock time on a day, as an ISO instant."""
    local = datetime.combine(date.fromisoformat(day_iso), time(hh, mm), BEIRUT)
    return local.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def last_month(day_of_month: int) -> str:
    first = today().replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    return prev.replace(day=min(day_of_month, 28)).isoformat()


class SeedMixin:
    """For a ParityCase: `await self.seed_life()` once the account is in Beirut time."""

    async def on_each(self, method, path, body=None, ids=None):
        """The request on both sides; `{id}` in path/body values is replaced by each side's id."""
        out = []
        for side, (client, prefix) in enumerate(((self.nest, ""), (self.py, "/api"))):
            p = path.format(*(i[side] for i in ids)) if ids else path
            b = body(side) if callable(body) else body
            out.append(await client.request(method, f"{prefix}{p}", json=b))
        self.assertParity(*out, ignore_keys=("mentionCount",))  # Python's people count journal mentions
        return tuple(r.json().get("id") if r.content and isinstance(r.json(), dict) else None for r in out)

    async def id_pair(self, path, name):
        found = []
        for client, prefix in ((self.nest, ""), (self.py, "/api")):
            items = (await client.get(f"{prefix}{path}")).json()["items"]
            found.append(next(i["id"] for i in items if i["name"] == name))
        return tuple(found)

    async def seed_life(self) -> dict:
        food = await self.id_pair("/categories?scope=EXPENSE", "Food")
        shopping = await self.id_pair("/categories?scope=EXPENSE", "Shopping")
        salary = await self.id_pair("/categories?scope=INCOME", "Salary")
        cash = await self.id_pair("/accounts", "Cash")
        card = await self.on_each("POST", "/accounts", {"name": "Visa", "type": "CREDIT_CARD", "currency": "USD"})
        tx = lambda **b: (lambda side: {**{k: (v[side] if isinstance(v, tuple) else v) for k, v in b.items()}})  # noqa: E731
        for body in (
            tx(type="INCOME", title="Salary", amount=2000, categoryId=salary, accountId=cash, date=at(day(0), 0, 30)),
            tx(type="EXPENSE", title="Groceries", amount=82.4, categoryId=food, accountId=cash, date=at(day(0), 0, 40)),
            tx(type="EXPENSE", title="Sneakers", amount=310, categoryId=shopping, accountId=card, date=at(day(0), 0, 50)),
            tx(type="EXPENSE", title="Dinner", amount=45, currency="EUR", categoryId=food, accountId=cash, date=at(day(-2), 20)),
            tx(type="EXPENSE", title="Coffee", amount=4.5, categoryId=food, date=at(day(-6), 9)),
            tx(type="EXPENSE", title="Snack", amount=7, categoryId=food, date=at(day(-20), 9)),
            tx(type="EXPENSE", title="Book", amount=21, categoryId=shopping, date=at(day(-25), 9)),
            tx(type="EXPENSE", title="Taxi", amount=12, date=at(day(-30), 9)),
            tx(type="INCOME", title="Salary", amount=2000, categoryId=salary, date=at(last_month(1), 9)),
            tx(type="EXPENSE", title="Rent share", amount=150, categoryId=food, date=at(last_month(1), 10)),
            tx(type="EXPENSE", title="Jacket", amount=60, categoryId=shopping, date=at(last_month(1), 11)),
        ):
            await self.on_each("POST", "/transactions", body)
        await self.on_each("POST", "/budgets", lambda s: {"amount": 100, "categoryId": food[s], "alertThreshold": 70})
        await self.on_each("POST", "/budgets", {"amount": 5000, "period": "YEARLY", "name": "Everything"})
        await self.on_each("POST", "/recurring-payments", {"name": "Rent", "amount": 700, "startDate": day(-40), "nextDate": day(-2)})
        await self.on_each("POST", "/recurring-payments", {"name": "Netflix", "amount": 12.99, "kind": "SUBSCRIPTION", "rrule": "FREQ=WEEKLY",
                                                          "startDate": day(3), "currency": "EUR"})
        project = await self.on_each("POST", "/projects", {"name": "Website", "deadline": day(6)})
        for body in ({"title": "Pay rent", "dueAt": day(-4), "priority": "HIGH"}, {"title": "Call bank", "dueAt": day(0)},
                     {"title": "Old thing", "dueAt": day(-12)}, {"title": "Plan week", "dueAt": at(day(1), 9)},
                     {"title": "Someday"}):
            await self.on_each("POST", "/tasks", body)
        await self.on_each("POST", "/tasks", lambda s: {"title": "Wireframes", "projectId": project[s], "dueAt": day(2)})
        done = await self.on_each("POST", "/tasks", {"title": "Done today", "dueAt": day(0)})
        await self.on_each("PATCH", "/tasks/{}", {"status": "COMPLETED"}, ids=[done])
        await self.on_each("POST", "/documents", {"name": "Passport", "type": "PASSPORT", "expiryDate": day(10)})
        await self.on_each("POST", "/documents", {"name": "Licence", "type": "DRIVERS_LICENSE", "expiryDate": day(-3)})
        await self.on_each("POST", "/maintenance", {"title": "Oil change", "area": "CAR", "rrule": "FREQ=MONTHLY;INTERVAL=6", "nextDueAt": day(-5)})
        await self.on_each("POST", "/people", {"name": "Sara", "birthday": f"1995-{day(3)[5:]}", "relationship": "Sister"})
        await self.on_each("POST", "/people", {"name": "Omar", "anniversary": f"2016-{day(9)[5:]}"})
        await self.on_each("POST", "/important-dates", {"title": "Exam", "date": day(5), "repeatsYearly": False})
        habit = await self.on_each("POST", "/habits", {"name": "Read", "startDate": day(-20)})
        for offset in range(-9, 0):
            await self.on_each("POST", "/habits/{}/check", {"date": day(offset)}, ids=[habit])
        await self.on_each("POST", "/goals", {"name": "Car fund", "type": "FINANCIAL", "targetValue": 10000, "startDate": day(-200), "deadline": day(100)})
        await self.on_each("POST", "/goals", {"name": "Marathon", "type": "FITNESS", "targetValue": 40, "currentValue": 37, "unit": "km"})
        await self.on_each("POST", "/events", {"title": "Dentist", "type": "APPOINTMENT", "startAt": at(day(1), 15), "endAt": at(day(1), 16)})
        await self.on_each("POST", "/events", {"title": "Gym", "startAt": at(day(-7), 18), "endAt": at(day(-7), 19), "rrule": "FREQ=WEEKLY"})
        return {"food": food, "cash": cash, "card": card, "project": project, "habit": habit}
