"""Shared helpers for the tests of the assistant's vournal life section tools (design 2026-11-18).
No tests here, so importing it doesn't run anything twice."""

import json

from vournal_os.parity import fake_model as fm
from vournal_os.test_assistant import ChatBase

ALL_SECTIONS = ["calendar", "tasks", "projects", "finance", "goals", "habits", "maintenance", "documents", "shopping", "meals",
                "health", "notes", "study", "work", "travel", "people", "reminders", "reports"]


class LifeBase(ChatBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        r = await self.client.patch("/api/settings", json={"enabledSections": ALL_SECTIONS})
        self.assertEqual(r.status_code, 200, r.text)
        self.uid = self.client.user.id

    async def act(self, tool: str, data: dict) -> dict:
        """One tool call through the chat (Auto): its action card."""
        fm.script(fm.tools((tool, data)), fm.message(fm.text("Done.")))
        actions = (await self.say("do it"))["messages"][1]["actions"]
        self.assertEqual(len(actions), 1, actions)
        self.assertNotEqual(actions[0]["status"], "failed", actions[0])
        return actions[0]

    async def act_error(self, tool: str, data: dict) -> str:
        """The tool result text of a call that fails."""
        from app.os import assistant_tools

        out = await assistant_tools.run(tool, data, self.ctx())
        self.assertTrue(out.get("isError"), out)
        return out["content"]

    async def result(self, tool: str, data: dict) -> dict:
        """The JSON a successful call gives the model."""
        from app.os import assistant_tools

        out = await assistant_tools.run(tool, data, self.ctx())
        self.assertFalse(out.get("isError"), out)
        return json.loads(out["content"])

    def ctx(self, mode="auto"):
        from datetime import datetime, timezone

        from app.os import assistant_tools

        return assistant_tools.Ctx(self.client.user, "Asia/Beirut", assistant_tools.local_day(datetime.now(timezone.utc), "Asia/Beirut"), mode)

    async def undo(self, action: dict):
        r = await self.client.post(f"/api/assistant/actions/{action['id']}/undo")
        self.assertEqual(r.status_code, 200, r.text)
        return r

    async def undo_refused(self, action: dict) -> str:
        r = await self.client.post(f"/api/assistant/actions/{action['id']}/undo")
        self.assertNotEqual(r.status_code, 200, r.text)
        return r.text

    async def delete(self, kind: str, id: str, label: str = "it") -> dict:
        fm.script(fm.tools(("delete_item", {"type": kind, "id": id, "label": label})), fm.message(fm.text("Done.")))
        return (await self.say("delete it"))["messages"][1]["actions"][0]

    async def approve(self, action: dict) -> dict:
        r = await self.client.post(f"/api/assistant/actions/{action['id']}/approve")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    async def post(self, path, body, method="post"):
        r = await getattr(self.client, method)(path, json=body)
        self.assertIn(r.status_code, (200, 201), r.text)
        return r.json()
