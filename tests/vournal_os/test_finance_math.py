"""The cash-flow forecast and spending-plan maths, ported from vournalOS's
forecast.spec.ts and plan.spec.ts (same inputs, same numbers)."""

import math
import unittest

from app.os.finance_forecast import add_days, average_daily_spend, build_forecast
from app.os.finance_plan import build_plan, resolve_period, schedule_rrule


def item(date, amount, kind=None):
    kind = kind or ("income" if amount > 0 else "bill")
    return {"date": date, "amount": amount, "kind": kind, "title": kind, "source": "recurringPayment", "sourceId": f"{kind}-{date}"}


def js_round2(n):
    return math.floor(n * 100 + 0.5) / 100


class Subset(unittest.TestCase):
    def assertSubset(self, actual: dict, expected: dict):
        self.assertEqual({k: actual.get(k) for k in expected}, expected)


class Forecast(Subset):
    def test_walks_the_balance_forward_with_scheduled_items_and_the_everyday_estimate(self):
        f = build_forecast(
            today="2026-10-13", days=30, start_balance=1000, daily_variable=10,
            items=[item("2026-10-15", -600), item("2026-10-31", 2500), item("2026-11-01", -600)],
        )
        self.assertEqual(len(f["series"]), 31)
        self.assertSubset(f["series"][0], {"date": "2026-10-13", "balance": 1000, "committed": 1000, "variable": 0})
        # 15 Oct: two days of everyday spending + rent
        self.assertSubset(f["series"][2], {"date": "2026-10-15", "balance": 380, "committed": 400, "scheduledOut": 600})
        # lowest point is the day before the salary
        self.assertEqual(f["lowest"], {"date": "2026-10-30", "balance": 230})
        self.assertEqual(f["endOfMonth"], {"date": "2026-10-31", "balance": 2720, "committed": 2900})
        self.assertIsNone(f["negativeOn"])
        self.assertEqual(f["safeToSpend"], 230)
        self.assertSubset(f["nextIncome"], {"date": "2026-10-31", "amount": 2500})
        self.assertEqual(f["totals"], {"income": 2500, "scheduledOut": 1200, "variable": 300})
        self.assertEqual(f["endBalance"], 1000 + 2500 - 1200 - 300)

    def test_flags_the_first_negative_day_and_leaves_nothing_safe_to_spend(self):
        f = build_forecast(today="2026-10-13", days=10, start_balance=100, daily_variable=0, items=[item("2026-10-16", -250)])
        self.assertEqual(f["negativeOn"], "2026-10-16")
        self.assertEqual(f["lowest"], {"date": "2026-10-16", "balance": -150})
        self.assertEqual(f["safeToSpend"], 0)

    def test_overdue_items_are_due_today_and_items_beyond_the_horizon_are_dropped(self):
        f = build_forecast(
            today="2026-10-13", days=7, start_balance=500, daily_variable=0,
            items=[item("2026-10-01", -200), item("2026-12-01", -999)],
        )
        self.assertEqual(len(f["items"]), 1)
        self.assertSubset(f["items"][0], {"date": "2026-10-13", "overdue": True})
        self.assertEqual(f["series"][0]["balance"], 300)
        self.assertEqual(f["endBalance"], 300)

    def test_no_end_of_month_point_on_the_last_day_and_month_ends_cross(self):
        f = build_forecast(today="2026-02-28", days=3, start_balance=0, daily_variable=0, items=[])
        self.assertIsNone(f["endOfMonth"])
        self.assertEqual([p["date"] for p in f["series"]], ["2026-02-28", "2026-03-01", "2026-03-02", "2026-03-03"])
        self.assertEqual(add_days("2024-02-28", 1), "2024-02-29")

    def test_only_estimates_everyday_spending_with_enough_history(self):
        self.assertEqual(average_daily_spend(600, 60), 10)
        self.assertEqual(average_daily_spend(600, 10), 0)
        self.assertEqual(average_daily_spend(0, 60), 0)


class Period(Subset):
    def test_runs_payday_to_the_day_before_the_next_payday(self):
        self.assertEqual(
            resolve_period("2026-11-02", {"last": "2026-10-25", "next": "2026-11-25"}, "auto"),
            {"start": "2026-10-25", "end": "2026-11-24", "basis": "payday"},
        )

    def test_falls_back_to_the_calendar_month(self):
        self.assertEqual(resolve_period("2026-11-02", None, "auto"), {"start": "2026-11-01", "end": "2026-11-30", "basis": "month"})
        self.assertEqual(
            resolve_period("2026-02-10", {"last": "2026-01-25", "next": "2026-02-25"}, "month"),
            {"start": "2026-02-01", "end": "2026-02-28", "basis": "month"},
        )
        # weekly pay is not a "payday month"
        self.assertEqual(resolve_period("2026-11-02", {"last": "2026-10-30", "next": "2026-11-06"}, "auto")["basis"], "month")
        # salary not received yet
        self.assertEqual(resolve_period("2026-11-02", {"last": None, "next": "2026-11-25"}, "auto")["basis"], "month")


BASE = {
    "today": "2026-11-11",
    "period": {"start": "2026-11-01", "end": "2026-11-30", "basis": "month"},
    "income": [{"title": "Salary", "amount": 1500, "date": "2026-11-01", "done": True}],
    "commitments": [
        {"kind": "bill", "title": "Rent", "amount": 500, "date": "2026-11-03", "done": True},
        {"kind": "installment", "title": "Car loan", "amount": 200, "date": "2026-11-20", "done": False},
    ],
    "savings": {"mode": "percent", "value": 10},
    "everydaySpent": 220,
    "spentToday": 20,
}


class Plan(Subset):
    def test_spreads_what_is_left_over_the_remaining_days(self):
        p = build_plan(BASE)
        self.assertSubset(p, {"daysTotal": 30, "daysLeft": 20, "incomeTotal": 1500, "commitmentsTotal": 700, "commitmentsPaid": 500, "savingsTarget": 150, "freeToSpend": 650, "leftToSpend": 430})
        # this morning 450 were left for 20 days
        self.assertEqual(p["dailyAllowance"], 22.5)
        self.assertEqual(p["todayLeft"], 2.5)
        self.assertEqual(p["status"], "ok")

    def test_flags_overspending_today_and_in_the_period(self):
        self.assertEqual(build_plan({**BASE, "everydaySpent": 250, "spentToday": 50})["status"], "overToday")
        over = build_plan({**BASE, "everydaySpent": 700, "spentToday": 10})
        self.assertEqual(over["status"], "over")
        self.assertEqual(over["dailyAllowance"], 0)
        self.assertEqual(over["fromTomorrow"], 0)
        # a big purchase today: this morning's allowance stands, the rest of the period pays for it
        big = build_plan({**BASE, "everydaySpent": 700, "spentToday": 500})
        self.assertSubset(big, {"status": "over", "dailyAllowance": 22.5, "todayLeft": -477.5, "fromTomorrow": 0})
        self.assertEqual(build_plan(BASE)["fromTomorrow"], 22.63)

    def test_fixed_savings_no_savings_and_no_income(self):
        self.assertEqual(build_plan({**BASE, "savings": {"mode": "amount", "value": 300}})["savingsTarget"], 300)
        self.assertEqual(build_plan({**BASE, "savings": {"mode": "none", "value": 50}})["savingsTarget"], 0)
        self.assertEqual(build_plan({**BASE, "income": []})["status"], "noIncome")

    def test_counts_the_last_day_as_one_day_left(self):
        self.assertEqual(build_plan({**BASE, "today": "2026-11-30"})["daysLeft"], 1)


def _wages(start, end, done):
    return [{"title": "Wage", "amount": 10, "date": f"2026-11-{d:02d}", "done": done} for d in range(start, end + 1)]


# daily wage of 10, half the month gone, then a one-off 100
DAILY = {
    "today": "2026-11-16",
    "period": {"start": "2026-11-01", "end": "2026-11-30", "basis": "month"},
    "income": [*_wages(1, 15, True), *_wages(16, 30, False)],
    "commitments": [
        {"kind": "subscription", "title": "Netflix", "amount": 20, "date": "2026-11-05", "done": True},
        {"kind": "bill", "title": "Rent", "amount": 150, "date": "2026-11-30", "done": False},
    ],
    "savings": {"mode": "none", "value": 0},
    "everydaySpent": 60,
    "spentToday": 0,
    "debts": [{"id": "d1", "title": "Loan from Sami", "left": 300, "paidInPeriod": 0, "target": "2027-04-30"}],
    "extraSplit": {"debts": 50, "savings": 20},
}
WITH_EXTRA = {**DAILY, "income": [*DAILY["income"], {"title": "Freelance", "amount": 100, "date": "2026-11-15", "done": True, "extra": True}]}


class PlanDebtsAndExtra(Subset):
    def test_sets_aside_an_even_share_of_a_long_debt_each_period(self):
        p = build_plan(DAILY)
        # 300 over 181 days (Nov 1 → Apr 30), 30 of them this period
        self.assertSubset(p["debtPlan"][0], {"perDay": 1.66, "share": 49.72, "extra": 0, "remaining": 49.72})
        self.assertSubset(p, {"regularIncome": 300, "extraIncome": 0, "debtPlanTotal": 49.72, "freeToSpend": 80.28, "extra": None})
        # 150 + 49.72 still needed, 70 in hand (150 earned − 20 − 60 spent)
        self.assertSubset(p["advice"], {"stillNeeded": 199.72, "cashOnHand": 70, "keepAside": 70, "gap": 129.72, "setAsidePerDay": 8.65, "expectedIncome": 150, "shortfall": 0})
        self.assertAlmostEqual(p["advice"]["incomeShare"], 0.865, places=3)

    def test_splits_extra_income_between_debts_savings_and_spending(self):
        p = build_plan(WITH_EXTRA)
        self.assertEqual(p["extra"], {"total": 100, "cover": 0, "debts": 50, "savings": 20, "spend": 30, "perDay": 2, "latest": {"title": "Freelance", "amount": 100, "date": "2026-11-15"}})
        self.assertSubset(p["debtPlan"][0], {"extra": 50, "remaining": 99.72})
        self.assertSubset(p, {"savingsTarget": 20, "debtPlanTotal": 99.72, "freeToSpend": 110.28})
        # only the "spend" part reaches the daily allowance: +30 over 15 days
        self.assertEqual(p["dailyAllowance"], js_round2(build_plan(DAILY)["dailyAllowance"] + 2))

    def test_first_covers_what_regular_income_cannot(self):
        electricity = {"kind": "bill", "title": "Electricity", "amount": 160, "date": "2026-11-25", "done": False}
        p = build_plan({**WITH_EXTRA, "commitments": [*WITH_EXTRA["commitments"], electricity]})
        # regular: 300 − 330 − 49.72 = −79.72
        self.assertSubset(p["extra"], {"cover": 79.72, "debts": 10.14, "savings": 4.06, "spend": 6.08})

    def test_moves_the_debt_part_to_savings_when_nothing_is_left_to_pay_early(self):
        self.assertSubset(build_plan({**WITH_EXTRA, "debts": []})["extra"], {"debts": 0, "savings": 70, "spend": 30})
        none = build_plan({**WITH_EXTRA, "extraSplit": {"debts": 0, "savings": 0}})
        self.assertSubset(none["extra"], {"debts": 0, "savings": 0, "spend": 100})

    def test_caps_early_payments_at_what_is_owed_and_counts_payments_already_made(self):
        small = build_plan({**WITH_EXTRA, "debts": [{"id": "d1", "title": "Small", "left": 30, "paidInPeriod": 0, "target": "2027-04-30"}]})
        # share 4.97, room 25.03; the other 24.97 goes to savings
        self.assertSubset(small["debtPlan"][0], {"share": 4.97, "extra": 25.03})
        self.assertSubset(small["extra"], {"debts": 25.03, "savings": 44.97, "spend": 30})
        # due inside this period: all of it is this period's share
        soon = build_plan({**DAILY, "debts": [{"id": "d1", "title": "Soon", "left": 120, "paidInPeriod": 80, "target": "2026-11-20"}]})
        self.assertSubset(soon["debtPlan"][0], {"share": 200, "paid": 80, "remaining": 120})
        # paid more than its share: the payment is what counts
        over = build_plan({**DAILY, "debts": [{"id": "d1", "title": "Loan", "left": 200, "paidInPeriod": 100, "target": "2027-04-30"}]})
        self.assertSubset(over["debtPlan"][0], {"share": 49.72, "paid": 100, "remaining": 0})
        self.assertEqual(over["debtPlanTotal"], 100)

    def test_applies_a_savings_percentage_to_regular_income_only(self):
        self.assertEqual(build_plan({**WITH_EXTRA, "savings": {"mode": "percent", "value": 10}})["savingsTarget"], 50)

    def test_reports_a_shortfall_expected_income_cannot_cover(self):
        repair = {"kind": "bill", "title": "Car repair", "amount": 400, "date": "2026-11-28", "done": False}
        p = build_plan({**DAILY, "commitments": [*DAILY["commitments"], repair]})
        self.assertEqual(p["advice"]["shortfall"], 379.72)
        self.assertEqual(p["advice"]["incomeShare"], 1)


class Schedule(unittest.TestCase):
    def test_builds_rules_for_each_schedule(self):
        self.assertEqual(schedule_rrule({"type": "monthly", "day": 25}), "FREQ=MONTHLY;BYMONTHDAY=25")
        self.assertEqual(schedule_rrule({"type": "monthly", "day": -1}), "FREQ=MONTHLY;BYMONTHDAY=-1")
        self.assertEqual(schedule_rrule({"type": "weekly", "weekday": 5}), "FREQ=WEEKLY;BYDAY=FR")
        self.assertEqual(schedule_rrule({"type": "daily", "weekdays": [1, 2, 3, 4, 5, 6]}), "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR,SA")
        self.assertEqual(schedule_rrule({"type": "daily", "weekdays": [0, 1, 2, 3, 4, 5, 6]}), "FREQ=DAILY")


if __name__ == "__main__":
    unittest.main()


class PlanFromBalance(Subset):
    """With accounts, the plan starts from the money in them, not from income alone
    (owner, 2026-11-09): what is left = balance + income still expected − bills still
    to pay − debt payoff still due − savings."""

    BAL = {**BASE, "balance": 900}

    def test_starts_from_the_money_in_the_accounts(self):
        p = build_plan(self.BAL)
        # 900 − 200 still due for the car loan − 150 savings
        self.assertSubset(p, {"basis": "balance", "balance": 900, "leftToSpend": 550, "freeToSpend": 770, "status": "ok"})
        # this morning 570 were left for 20 days
        self.assertSubset(p, {"dailyAllowance": 28.5, "todayLeft": 8.5})
        self.assertSubset(p["advice"], {"cashOnHand": 900, "stillNeeded": 350, "keepAside": 350, "gap": 0, "shortfall": 0})

    def test_adds_income_still_expected(self):
        bonus = {"title": "Freelance", "amount": 400, "date": "2026-11-25", "done": False}
        p = build_plan({**self.BAL, "income": [*BASE["income"], bonus]})
        # 900 + 400 − 200 car loan − 190 savings (10% of 1900)
        self.assertEqual(p["leftToSpend"], 910)

    def test_money_without_income_is_a_plan(self):
        p = build_plan({**self.BAL, "income": [], "balance": 300, "spentToday": 0})
        # no income: a percentage saves nothing; 300 − 200 car loan, over 20 days
        self.assertSubset(p, {"status": "ok", "savingsTarget": 0, "leftToSpend": 100, "dailyAllowance": 5})

    def test_less_money_than_what_is_due_is_over(self):
        p = build_plan({**self.BAL, "balance": 100})
        self.assertSubset(p, {"status": "over", "leftToSpend": -250, "dailyAllowance": 0})
        self.assertSubset(p["advice"], {"cashOnHand": 100, "gap": 250, "shortfall": 250})

    def test_without_accounts_the_plan_is_from_income(self):
        p = build_plan(BASE)
        self.assertSubset(p, {"basis": "income", "balance": None, "leftToSpend": 430})
