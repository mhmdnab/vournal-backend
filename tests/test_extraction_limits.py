"""A cut-off or refused model reply must fail the entry, never save it empty.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


def _client(stop_reason: str, text: str = '{"summary": "half'):
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=10, output_tokens=10),
    )
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=response)
    return MagicMock(return_value=client)


class ExtractionLimits(unittest.IsolatedAsyncioTestCase):
    async def test_truncated_reply_fails_the_entry(self):
        from app import extraction, extraction_merged

        for module, call in ((extraction, extraction.extract), (extraction_merged, extraction_merged.extract_merged)):
            with patch.object(module, "AsyncAnthropic", _client("max_tokens")):
                with self.assertRaises(extraction.ExtractionTruncated, msg=module.__name__):
                    await call("I need to call the bank tomorrow.")

    async def test_refusal_fails_the_entry(self):
        from app import extraction, extraction_merged

        for module, call in ((extraction, extraction.extract), (extraction_merged, extraction_merged.extract_merged)):
            with patch.object(module, "AsyncAnthropic", _client("refusal", "")):
                with self.assertRaises(extraction.ExtractionRefused, msg=module.__name__):
                    await call("I need to call the bank tomorrow.")

    async def test_complete_reply_still_parses(self):
        from app import extraction

        with patch.object(extraction, "AsyncAnthropic", _client("end_turn", '{"summary": "ok", "todos": []}')):
            result, usage = await extraction.extract("hello")
        self.assertEqual(result["summary"], "ok")
        self.assertEqual(usage, {"inTokens": 10, "outTokens": 10})

    def test_room_for_long_entries(self):
        from app.extraction import MAX_TOKENS

        self.assertGreaterEqual(MAX_TOKENS, 4096)


if __name__ == "__main__":
    unittest.main()
