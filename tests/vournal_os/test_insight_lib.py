"""The insights slice's pure code against NestJS's own answers
(tests/vournal_os/golden/insights_golden.mjs): every rule over the fixtures in both
languages, the habit helpers, money() and relDay()."""

import json
import unittest
from datetime import datetime
from pathlib import Path

GOLDEN = json.loads((Path(__file__).parent / "golden" / "insights.json").read_text())


class InsightLib(unittest.TestCase):
    maxDiff = None

    def test_rules_match_nestjs(self):
        from app.os.insight_rules import run_insight_rules

        for case in GOLDEN["rules"]:
            with self.subTest(lang=case["input"].get("lang"), sections=case["input"]["sections"]):
                self.assertEqual(run_insight_rules(case["input"]), case["output"])

    def test_habit_helpers_match_nestjs(self):
        from app.os import insight_habits as h

        defs = {d["id"]: {**d, "startDate": datetime.fromisoformat(d["startDate"].replace("Z", "+00:00"))} for d in GOLDEN["habits"]["defs"]}
        logs = [{**l, "date": datetime.fromisoformat(l["date"].replace("Z", "+00:00"))} for l in GOLDEN["habits"]["logs"]]
        idx = h.index_habit_logs(logs)
        for a in GOLDEN["habits"]["answers"]:
            habit, today, ws = defs[a["habit"]], a["today"], a["weekStartsOn"]
            with self.subTest(habit=a["habit"], today=today, ws=ws):
                got = {
                    "dueToday": h.is_due_today(habit, idx, today, ws),
                    "doneToday": h.is_done_on(habit, idx, today),
                    "currentStreak": h.current_streak(habit, idx, today, ws, 400),
                    "best": h.best_streak_in_range(habit, idx, "2026-09-10", today, ws),
                    "completion": h.completion_in_range(habit, idx, "2026-10-01", "2026-10-31", today),
                    "completionToday": h.completion_in_range(habit, idx, "2026-11-01", "2026-11-30", today),
                }
                self.assertEqual(got, {k: a[k] for k in got})

    def test_money_and_relative_days(self):
        from app.os.insight_text import money, rel_day

        for case in GOLDEN["money"]:
            self.assertEqual(money(case["lang"], case["amount"], case["code"]), case["text"], case)
        for case in GOLDEN["rel"]:
            self.assertEqual(rel_day(case["lang"], case["n"]), case["text"], case)

    def test_arabic_dates(self):
        from app.os.insight_text import fmt_day, month_short

        self.assertEqual(fmt_day("ar", "2026-10-05", month=True, day=True), "5 تشرين الأول")
        self.assertEqual(fmt_day("ar", "2026-01-31", month=True, day=True, year=True), "31 كانون الثاني 2026")
        self.assertEqual(month_short("ar", 2026, 2), "شباط")


if __name__ == "__main__":
    unittest.main()
