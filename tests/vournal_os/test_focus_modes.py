"""Focus modes (like iOS Focus): saved in settings, one can be on, and while it is on the home
screen's task lists keep only its projects / tasks."""

from vournal_os.support import OsTestCase


class FocusModes(OsTestCase):
    async def test_save_turn_on_and_remove(self):
        client = await self.signed_in()
        work = {"id": "work", "name": " Work ", "icon": "briefcase", "color": "#2563eb", "sections": ["tasks", "projects", "finance", "tasks"],
                "widgets": ["todayTasks", "savings"], "projectIds": [], "taskIds": []}
        saved = await client.patch("/api/settings", json={"focusModes": [work], "activeFocusModeId": "work"})
        self.assertEqual(saved.status_code, 200, saved.text)
        s = saved.json()
        self.assertEqual((s["activeFocusModeId"], s["focusModes"][0]["name"], s["focusModes"][0]["sections"]), ("work", "Work", ["tasks", "projects", "finance"]))
        self.assertEqual((await client.get("/api/auth/me")).json()["settings"]["activeFocusModeId"], "work")

        off = (await client.patch("/api/settings", json={"activeFocusModeId": None})).json()
        self.assertEqual((off["activeFocusModeId"], len(off["focusModes"])), (None, 1))
        missing = await client.patch("/api/settings", json={"activeFocusModeId": "gym"})
        self.assertEqual((missing.status_code, missing.json()["message"]), (404, "Focus mode not found"))

        await client.patch("/api/settings", json={"activeFocusModeId": "work"})
        removed = (await client.patch("/api/settings", json={"focusModes": []})).json()
        self.assertEqual((removed["focusModes"], removed["activeFocusModeId"]), ([], None))  # the one that was on went with it

        for bad in ({"focusModes": [work, work]}, {"focusModes": [{**work, "sections": ["nope"]}]}, {"focusModes": [{**work, "name": ""}]}):
            self.assertEqual((await client.patch("/api/settings", json=bad)).status_code, 400, bad)

    async def test_home_tasks_follow_the_focus_scope(self):
        client = await self.signed_in()
        await client.patch("/api/settings", json={"timezone": "Asia/Beirut"})
        project = (await client.post("/api/projects", json={"name": "Launch"})).json()
        today = (await client.get("/api/dashboard")).json()["today"]
        inside = (await client.post("/api/tasks", json={"title": "Ship it", "projectId": project["id"], "dueAt": today})).json()
        picked = (await client.post("/api/tasks", json={"title": "Call bank", "dueAt": today})).json()
        await client.post("/api/tasks", json={"title": "Groceries", "dueAt": today})
        self.assertEqual(len((await client.get("/api/dashboard")).json()["todayTasks"]["items"]), 3)

        mode = {"id": "work", "name": "Work", "sections": ["tasks"], "widgets": ["todayTasks"], "projectIds": [project["id"]], "taskIds": [picked["id"]]}
        await client.patch("/api/settings", json={"focusModes": [mode], "activeFocusModeId": "work"})
        d = (await client.get("/api/dashboard")).json()
        self.assertEqual(sorted(t["id"] for t in d["todayTasks"]["items"]), sorted([inside["id"], picked["id"]]))
        self.assertEqual(d["todayTasks"]["total"], 2)

        await client.patch("/api/settings", json={"activeFocusModeId": None})
        self.assertEqual((await client.get("/api/dashboard")).json()["todayTasks"]["total"], 3)


from vournal_os.parity import fake_model as fm  # noqa: E402
from vournal_os.test_assistant import ChatBase  # noqa: E402


class FocusTools(ChatBase):
    async def run_tool(self, tool, tool_input):
        fm.script(fm.tools((tool, tool_input)), fm.message(fm.text("Done.")))
        reply = (await self.say("set it up"))["messages"][1]
        result = fm.requests("python")[-1]["body"]["messages"][-1]["content"][0]
        return reply, result

    async def test_save_turn_on_off_and_undo(self):
        project = (await self.client.post("/api/projects", json={"name": "Launch"})).json()
        reply, result = await self.run_tool("save_focus_mode", {"name": "Work", "sections": ["tasks"], "widgets": ["savings", "todayTasks"],
                                                                "project_names": ["launch"], "icon": "briefcase", "turn_on": True})
        self.assertFalse(result.get("is_error"), result)
        s = (await self.client.get("/api/settings")).json()
        mode = s["focusModes"][0]
        self.assertEqual((mode["name"], mode["sections"], mode["projectIds"], s["activeFocusModeId"]), ("Work", ["tasks", "finance"], [project["id"]], mode["id"]))

        off_reply, off = await self.run_tool("set_focus", {"name": None})
        self.assertFalse(off.get("is_error"), off)
        self.assertIsNone((await self.client.get("/api/settings")).json()["activeFocusModeId"])
        await self.client.post(f"/api/assistant/actions/{off_reply['actions'][0]['id']}/undo")
        self.assertEqual((await self.client.get("/api/settings")).json()["activeFocusModeId"], mode["id"])

        _, unknown = await self.run_tool("set_focus", {"name": "Gym"})
        self.assertTrue(unknown.get("is_error"))
        self.assertIn("Work", unknown["content"])

        await self.client.post(f"/api/assistant/actions/{reply['actions'][0]['id']}/undo")
        s = (await self.client.get("/api/settings")).json()
        self.assertEqual((s["focusModes"], s["activeFocusModeId"]), ([], None))
