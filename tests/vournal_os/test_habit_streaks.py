"""Habit maths, ported from vournalOS's habit-streaks.spec.ts (same cases, same numbers)."""

import unittest

from app.os.habit_streaks import (
    completion_between, compute_habit, compute_streaks, count_map, day_num, day_str, week_start_of, weekday_of,
)


def daily(**over):
    return {"frequency": "DAILY", "daysOfWeek": [], "timesPerWeek": None, "targetPerDay": 1, "startDate": "2026-09-01", **over}


def logs(*dates):
    return [{"date": d, "count": 1} for d in dates]


def date_range(start, end):
    return [day_str(n) for n in range(day_num(start), day_num(end) + 1)]


def streaks(h, l, today, ws=1):
    return compute_streaks(h, count_map(l), day_num(today), ws)


class DayHelpers(unittest.TestCase):
    def test_round_trips_dates_and_knows_weekdays(self):
        self.assertEqual(day_str(day_num("2026-10-08")), "2026-10-08")
        self.assertEqual(weekday_of(day_num("2026-10-08")), 4)  # Thursday
        self.assertEqual(weekday_of(day_num("2026-10-04")), 0)  # Sunday
        self.assertEqual(day_str(week_start_of(day_num("2026-10-08"), 1)), "2026-10-05")
        self.assertEqual(day_str(week_start_of(day_num("2026-10-08"), 0)), "2026-10-04")
        self.assertEqual(day_str(week_start_of(day_num("2026-10-08"), 6)), "2026-10-03")


class Daily(unittest.TestCase):
    def test_counts_consecutive_days_including_today(self):
        self.assertEqual(streaks(daily(), logs("2026-10-06", "2026-10-07", "2026-10-08"), "2026-10-08"), {"current": 3, "best": 3})

    def test_today_not_yet_done_does_not_break(self):
        self.assertEqual(streaks(daily(), logs("2026-10-05", "2026-10-06", "2026-10-07"), "2026-10-08")["current"], 3)

    def test_a_missed_yesterday_breaks(self):
        self.assertEqual(streaks(daily(), logs("2026-10-05", "2026-10-06"), "2026-10-08")["current"], 0)

    def test_best_survives_a_break(self):
        l = logs(*date_range("2026-09-01", "2026-09-10"), "2026-10-07", "2026-10-08")
        self.assertEqual(streaks(daily(), l, "2026-10-08"), {"current": 2, "best": 10})

    def test_respects_target_per_day(self):
        l = [{"date": "2026-10-06", "count": 8}, {"date": "2026-10-07", "count": 5}, {"date": "2026-10-08", "count": 9}]
        self.assertEqual(streaks(daily(targetPerDay=8), l, "2026-10-08"), {"current": 1, "best": 1})

    def test_backfilled_logs_extend_history(self):
        self.assertEqual(streaks(daily(startDate="2026-10-07"), logs("2026-10-05", "2026-10-06", "2026-10-07"), "2026-10-08")["current"], 3)

    def test_no_logs(self):
        self.assertEqual(streaks(daily(), [], "2026-10-08"), {"current": 0, "best": 0})


class Custom(unittest.TestCase):
    h = daily(frequency="CUSTOM", daysOfWeek=[1, 3, 5])

    def test_unscheduled_days_neither_add_nor_break(self):
        self.assertEqual(streaks(self.h, logs("2026-10-02", "2026-10-05", "2026-10-07"), "2026-10-08"), {"current": 3, "best": 3})

    def test_missing_a_scheduled_day_breaks(self):
        self.assertEqual(streaks(self.h, logs("2026-09-30", "2026-10-05", "2026-10-07"), "2026-10-08")["current"], 2)

    def test_unscheduled_check_is_ignored(self):
        self.assertEqual(streaks(self.h, logs("2026-10-06"), "2026-10-08")["current"], 0)

    def test_scheduled_today_pending_keeps_streak(self):
        self.assertEqual(streaks(self.h, logs("2026-10-05", "2026-10-07"), "2026-10-09")["current"], 2)


class Weekly(unittest.TestCase):
    h = daily(frequency="WEEKLY", timesPerWeek=3, startDate="2026-09-07")

    def test_consecutive_weeks_current_pending(self):
        l = logs("2026-09-14", "2026-09-16", "2026-09-18", "2026-09-21", "2026-09-22", "2026-09-27",
                 "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-06")
        self.assertEqual(streaks(self.h, l, "2026-10-08"), {"current": 3, "best": 3})

    def test_current_week_counts_once_met(self):
        l = logs("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-05", "2026-10-06", "2026-10-07")
        self.assertEqual(streaks(self.h, l, "2026-10-08")["current"], 2)

    def test_week_below_quota_breaks(self):
        l = logs("2026-09-21", "2026-09-22", "2026-09-23", "2026-09-28", "2026-10-05", "2026-10-06", "2026-10-07")
        self.assertEqual(streaks(self.h, l, "2026-10-08"), {"current": 1, "best": 1})

    def test_honours_week_starts_on(self):
        l = logs("2026-10-04", "2026-10-05", "2026-10-06")
        self.assertEqual(streaks(self.h, l, "2026-10-10", 0)["current"], 1)
        self.assertEqual(streaks(self.h, l, "2026-10-10", 1)["current"], 0)


class Completion(unittest.TestCase):
    def test_daily_today_pending_not_missed(self):
        h = daily(startDate="2026-10-05")
        c = completion_between(h, count_map(logs("2026-10-05", "2026-10-07")), day_num("2026-10-05"), day_num("2026-10-08"), day_num("2026-10-08"), 1)
        self.assertEqual(c, {"achieved": 2, "expected": 3, "rate": 66.7})

    def test_weekly_only_impossible_days_missed(self):
        h = daily(frequency="WEEKLY", timesPerWeek=3, startDate="2026-09-28")
        counts = count_map(logs("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-05"))
        c = completion_between(h, counts, day_num("2026-09-28"), day_num("2026-10-11"), day_num("2026-10-11"), 1)
        self.assertEqual(c, {"achieved": 4, "expected": 5, "rate": 80})

    def test_compute_habit(self):
        h = {**daily(startDate="2026-10-01", targetPerDay=2), "isArchived": False}
        r = compute_habit(h, [{"date": "2026-10-06", "count": 2}, {"date": "2026-10-07", "count": 2}, {"date": "2026-10-08", "count": 1}], "2026-10-08", 1)
        self.assertEqual((r["todayCount"], r["doneToday"], r["isScheduledToday"], r["currentStreak"], r["streakUnit"]), (1, False, True, 2, "day"))
        self.assertEqual(len(r["last7Days"]), 7)
        self.assertEqual(r["last7Days"][6], {"date": "2026-10-08", "count": 1, "done": False, "scheduled": True})
        self.assertEqual(r["last7Days"][0], {"date": "2026-10-02", "count": 0, "done": False, "scheduled": True})

    def test_weekly_not_scheduled_once_quota_met(self):
        h = {**daily(frequency="WEEKLY", timesPerWeek=2, startDate="2026-09-01"), "isArchived": False}
        self.assertFalse(compute_habit(h, logs("2026-10-05", "2026-10-06"), "2026-10-08", 1)["isScheduledToday"])
        self.assertTrue(compute_habit(h, logs("2026-10-05"), "2026-10-08", 1)["isScheduledToday"])
        self.assertEqual(compute_habit(h, logs("2026-10-05", "2026-10-06"), "2026-10-08", 1)["weekProgress"], {"done": 2, "target": 2})

    def test_archived_never_scheduled(self):
        self.assertFalse(compute_habit({**daily(), "isArchived": True}, [], "2026-10-08", 1)["isScheduledToday"])


if __name__ == "__main__":
    unittest.main()
