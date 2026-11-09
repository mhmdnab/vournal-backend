"""The spending plan starts from the money in the accounts the user counts (owner,
2026-11-09). Run through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check vournal_os.test_plan_balance
"""

from datetime import datetime, timedelta, timezone

from vournal_os.support import OsTestCase


class PlanBalance(OsTestCase):
    async def account(self, user, name, balance, **extra):
        return await self.db.account.create(data={"userId": user.id, "name": name, "type": "CASH", "currency": "USD", "initialBalance": balance, **extra})

    async def spend(self, user, amount, when, account=None):
        await self.db.transaction.create(data={
            "userId": user.id, "type": "EXPENSE", "amount": amount, "currency": "USD", "date": when,
            **({"accountId": account.id} if account else {}),
        })

    async def test_counts_the_money_in_the_accounts_now(self):
        web = await self.signed_in()
        user = web.user
        now = datetime.now(timezone.utc)
        cash = await self.account(user, "Cash", 500)
        await self.account(user, "Old", 1000, isArchived=True)
        await self.account(user, "Savings", 1000, includeInTotal=False)
        await self.spend(user, 50, now - timedelta(minutes=5), cash)
        # spent without an account this period: it came out of the user's money too
        await self.spend(user, 30, now - timedelta(minutes=5))
        # before this period, without an account: the balances already say what is left
        await self.spend(user, 100, now - timedelta(days=40))
        # planned for later: not spent yet
        await self.spend(user, 70, now + timedelta(days=2), cash)

        p = (await web.get("/api/finance/plan")).json()
        self.assertEqual((p["basis"], p["balance"]), ("balance", 420))
        self.assertEqual(p["leftToSpend"], 420)
        # this morning 500 were left (80 went today)
        self.assertEqual(p["dailyAllowance"], round(500 / p["daysLeft"], 2))

    async def test_an_unused_account_leaves_the_plan_on_income(self):
        # every account starts with an empty Cash account
        web = await self.signed_in()
        p = (await web.get("/api/finance/plan")).json()
        self.assertEqual((p["basis"], p["balance"], p["status"]), ("income", None, "noIncome"))
