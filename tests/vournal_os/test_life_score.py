"""The weekly life score and ritual streak, ported from vournalOS's life-score.spec.ts."""

import unittest

from app.os.life_score import compute_life_score, level_of, ritual_streak


def part(score, key):
    return next(p for p in score["parts"] if p["key"] == key)


class LifeScore(unittest.TestCase):
    def test_a_strong_week_scores_close_to_100(self):
        s = compute_life_score({"tasks": {"completed": 9, "open": 1}, "habits": {"done": 18, "expected": 20},
                                "money": {"income": 2500, "expenses": 1200}, "rituals": {"planned": 7, "reviewed": 7, "days": 7}, "overdueNow": 0})
        self.assertEqual((s["total"], s["level"]), (94, "great"))
        self.assertEqual({k: part(s, "tasks")[k] for k in ("points", "max", "ratio")}, {"points": 27, "max": 30, "ratio": 0.9})

    def test_rescales_when_a_part_has_nothing_to_measure(self):
        s = compute_life_score({"tasks": {"completed": 1, "open": 1}, "habits": None, "money": {"income": 0, "expenses": 300},
                                "rituals": {"planned": 0, "reviewed": 0, "days": 3}, "overdueNow": 2})
        self.assertEqual((part(s, "tasks")["points"], part(s, "tasks")["max"]), (30, 60))
        self.assertEqual((part(s, "habits")["ratio"], part(s, "habits")["max"]), (None, 0))
        self.assertEqual((part(s, "backlog")["points"], part(s, "backlog")["max"]), (10, 20))
        self.assertEqual((s["total"], s["level"]), (40, "low"))

    def test_overspending_and_a_long_backlog(self):
        s = compute_life_score({"tasks": None, "habits": None, "money": {"income": 1000, "expenses": 2000},
                                "rituals": {"planned": 1, "reviewed": 1, "days": 1}, "overdueNow": None})
        self.assertEqual(s["total"], 67)
        long = compute_life_score({"tasks": {"completed": 0, "open": 0}, "habits": None, "money": None,
                                   "rituals": {"planned": 0, "reviewed": 0, "days": 1}, "overdueNow": 9})
        self.assertEqual((part(long, "backlog")["ratio"], long["total"]), (0, 0))

    def test_no_score_from_rituals_alone(self):
        s = compute_life_score({"tasks": None, "habits": None, "money": None, "rituals": {"planned": 2, "reviewed": 2, "days": 2}, "overdueNow": None})
        self.assertEqual((s["total"], s["level"]), (None, None))

    def test_levels(self):
        self.assertEqual([level_of(85), level_of(70), level_of(50), level_of(49)], ["great", "good", "okay", "low"])

    def test_ritual_streak(self):
        days = {"2026-10-10", "2026-10-11", "2026-10-12"}
        self.assertEqual(ritual_streak(days, "2026-10-13"), 3)
        self.assertEqual(ritual_streak(days | {"2026-10-13"}, "2026-10-13"), 4)
        self.assertEqual(ritual_streak(days, "2026-10-15"), 0)
        self.assertEqual(ritual_streak({"2026-02-28", "2026-03-01"}, "2026-03-01"), 2)


if __name__ == "__main__":
    unittest.main()
