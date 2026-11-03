"""The Python repeat engine gives the same answers as vournalOS's own (JS `rrule`).

Every case in golden/rrule.json was computed by the NestJS RecurrenceService and
rrule.util (golden/rrule_golden.mjs regenerates it). Any difference is a Python bug.
"""

import json
import pathlib
import unittest
from datetime import datetime, timezone

GOLDEN = json.loads((pathlib.Path(__file__).parent / "golden" / "rrule.json").read_text())
# NestJS wraps the JS library's own error text in this; only NestJS's part is ours to match.
LIBRARY_ERROR = "is not a valid recurrence rule ("


def _instant(value):
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(moment):
    if moment is None:
        return None
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _options(opts):
    opts = dict(opts or {})
    return {
        "tz": opts.get("tz"),
        "date_only": bool(opts.get("dateOnly")),
        "until": _instant(opts.get("until")),
        **({"inclusive": bool(opts["inclusive"])} if "inclusive" in opts else {}),
    }


class Golden(unittest.TestCase):
    def test_every_golden_case(self):
        from app.os.common import rrule

        for case in GOLDEN:
            with self.subTest(case["id"], rule=case.get("rule")):
                op, expected = case["op"], case["result"]
                if op == "validate":
                    got = rrule.validate_rrule(case["rule"])
                    if isinstance(expected, str) and expected.startswith(LIBRARY_ERROR) and "must include FREQ" not in expected:
                        self.assertIsInstance(got, str)
                        self.assertTrue(got.startswith(LIBRARY_ERROR), got)
                    else:
                        self.assertEqual(got, expected)
                elif op == "normalize":
                    self.assertEqual(rrule.normalize_rrule(case["rule"]), expected)
                elif op == "next":
                    got = rrule.next_occurrence(
                        case["rule"], _instant(case["anchor"]), _instant(case["after"]), **_options(case.get("opts"))
                    )
                    self.assertEqual(_iso(got), expected)
                elif op == "between":
                    opts = _options(case.get("opts"))
                    opts.pop("inclusive", None)
                    got = rrule.between(
                        case["rule"],
                        _instant(case["anchor"]),
                        _instant(case["start"]),
                        _instant(case["end"]),
                        [_instant(d) for d in case.get("exDates", [])],
                        **opts,
                    )
                    self.assertEqual([_iso(d) for d in got], expected)
                elif op == "monthlyFactor":
                    self.assertEqual(rrule.monthly_factor(case["rule"]), expected)
                elif op == "describe":
                    self.assertEqual(rrule.describe(case["rule"], case["lang"]), expected)
                else:
                    self.fail(f"unknown op {op}")


if __name__ == "__main__":
    unittest.main()
