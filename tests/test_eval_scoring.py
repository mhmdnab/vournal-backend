"""Scoring for the extraction evaluation (scripts/eval_extraction.py). Pure — no API."""

import importlib.util
import pathlib
import unittest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "eval_extraction.py"
_spec = importlib.util.spec_from_file_location("eval_extraction", _PATH)
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)


def _todo(text, **kw):
    base = dict(text=text, dueDate=None, time=None, urgency=0, urgencyUnclear=False,
                location=None, recurrence=None, recurrenceWeekdays=[], recurrenceUnclear=False,
                kind="task", questions=[])
    base.update(kw)
    return base


CASE = {
    "id": "en-x", "lang": "en",
    "expect": {
        "todos": [
            {"match": ["ahmad"], "dueDate": "2026-10-29", "time": "15:00", "urgency": 0,
             "location": "office", "recurrence": None, "kind": "event"},
            {"match": ["rent"], "dueDate": None, "time": None, "urgency": "ask",
             "location": None, "recurrence": None, "kind": "task"},
        ],
        "questions": True,
    },
}


class Scoring(unittest.TestCase):
    def test_match_by_keywords(self):
        got = [
            _todo("Pay the rent", urgencyUnclear=True, questions=["urgency"]),
            _todo("Meeting with Ahmad", dueDate="2026-10-29", time="15:00", location="the office", kind="event"),
        ]
        score = ev.score_case(CASE, got)
        self.assertEqual(score["fields"]["dueDate"], [2, 2])
        self.assertEqual(score["fields"]["time"], [2, 2])
        self.assertEqual(score["fields"]["location"], [2, 2])
        self.assertEqual(score["fields"]["urgency"], [2, 2])
        self.assertEqual((score["missing"], score["extra"]), (0, 0))

    def test_unmatched_expected_todo_scores_zero(self):
        score = ev.score_case(CASE, [_todo("Something else")])
        self.assertEqual(score["missing"], 2)
        self.assertEqual(score["extra"], 1)
        self.assertEqual(score["fields"]["time"], [0, 2])

    def test_urgency_ask_matches_unclear(self):
        got = [_todo("Pay rent", urgency=2)]
        score = ev.score_case(CASE, got)
        self.assertEqual(score["fields"]["urgency"][0], 0)  # rent expected "ask"

    def test_questions_expectation(self):
        none = ev.score_case(CASE, [_todo("Meeting with Ahmad")])
        self.assertFalse(none["questions"])
        asked = ev.score_case(CASE, [_todo("Pay rent", questions=["urgency"])])
        self.assertTrue(asked["questions"])

    def test_wildcard_field_is_not_scored(self):
        case = {"id": "x", "lang": "en", "expect": {"todos": [
            {"match": ["gym"], "dueDate": "*", "time": "07:00", "urgency": 0, "location": None,
             "recurrence": {"freq": "weekly", "weekdays": ["MO", "TH"]}, "kind": "task"}],
            "questions": False}}
        got = [_todo("Gym", dueDate="2026-11-02", time="07:00", recurrence="weekly",
                     recurrenceWeekdays=["MO", "TH"])]
        score = ev.score_case(case, got)
        self.assertEqual(score["fields"]["dueDate"], [0, 0])
        self.assertEqual(score["fields"]["recurrence"], [1, 1])

    def test_repeat_interval_is_scored_when_labelled(self):
        case = {"id": "x", "lang": "en", "expect": {"todos": [
            {"match": ["sync"], "dueDate": "*", "time": "11:00", "urgency": 0, "location": None,
             "recurrence": {"freq": "weekly", "weekdays": ["TU"], "interval": 2}, "kind": "event"}],
            "questions": False}}
        ok = _todo("Team sync", time="11:00", recurrence="weekly", recurrenceWeekdays=["TU"],
                   recurrenceInterval=2, kind="event")
        self.assertEqual(ev.score_case(case, [ok])["fields"]["recurrence"], [1, 1])
        ok["recurrenceInterval"] = 1
        self.assertEqual(ev.score_case(case, [ok])["fields"]["recurrence"], [0, 1])

    def test_summary_per_language(self):
        results = [
            {"lang": "en", "score": ev.score_case(CASE, [
                _todo("Meeting with Ahmad", dueDate="2026-10-29", time="15:00", location="office", kind="event"),
                _todo("Rent", urgencyUnclear=True, questions=["urgency"])])},
            {"lang": "ar", "score": ev.score_case(CASE, [])},
        ]
        table = ev.summarize(results)
        self.assertEqual(table["en"]["time"], 100.0)
        self.assertEqual(table["ar"]["time"], 0.0)
        self.assertEqual(table["en"]["recall"], 100.0)


if __name__ == "__main__":
    unittest.main()
