"""Extraction's intent (design 2026-11-17, Ask vournal): journal, ask or both; anything else is journal.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check test_extraction_intent
"""

import unittest

from app.extraction import _empty_result, _normalize


class IntentNormalizeTest(unittest.TestCase):
    def test_kept_when_valid(self):
        for v in ("journal", "ask", "both"):
            self.assertEqual(_normalize({"intent": v})["intent"], v)

    def test_defaults_to_journal(self):
        for raw in ({}, {"intent": "chat"}, {"intent": 3}, {"intent": None}):
            self.assertEqual(_normalize(raw)["intent"], "journal")

    def test_empty_result_is_journal(self):
        self.assertEqual(_empty_result()["intent"], "journal")


if __name__ == "__main__":
    unittest.main()
