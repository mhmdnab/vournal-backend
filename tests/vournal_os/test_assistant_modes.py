"""The chat's two modes (design 2026-11-06): Manual waits for the user's approval of every
change, Auto acts at once with an Undo. The mode is the user's setting only: no tool can read
or change it. The model is fake_model (no paid calls)."""

import unittest
from pathlib import Path

from vournal_os.support import OsTestCase

BACKEND = Path(__file__).resolve().parents[2]


class ModeSetting(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client = await self.signed_in()

    async def test_mode_defaults_to_manual(self):
        self.assertEqual((await self.client.get("/api/settings")).json()["assistantMode"], "manual")

    async def test_mode_changes_only_to_known_values(self):
        ok = await self.client.patch("/api/settings", json={"assistantMode": "auto"})
        self.assertEqual(ok.json()["assistantMode"], "auto")
        self.assertEqual((await self.client.patch("/api/settings", json={"assistantMode": "yolo"})).status_code, 400)
        self.assertEqual((await self.client.get("/api/settings")).json()["assistantMode"], "auto")


class NoToolTouchesTheMode(unittest.TestCase):
    def test_no_tool_mentions_settings_or_the_mode(self):
        text = (BACKEND / "app/os/data/assistant.json").read_text()
        self.assertNotIn("assistantMode", text)
        self.assertNotIn("aiEnabled", text)


if __name__ == "__main__":
    unittest.main()
