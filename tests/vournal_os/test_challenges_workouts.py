"""Personal challenges (habit groups over N days), workout plans and sessions, their dashboard
widgets, and the assistant tools that set them up from a plan the user brings."""

import unittest

from vournal_os.parity import fake_model as fm
from vournal_os.support import OsTestCase
from vournal_os.test_assistant import ChatBase


class ChallengeMath(unittest.TestCase):
    def test_perfect_days_streak_and_completion(self):
        from app.os.challenges import compute_challenge

        def habit(id, freq="DAILY", days=()):
            return {"id": id, "name": id, "frequency": freq, "daysOfWeek": list(days), "timesPerWeek": 3 if freq == "WEEKLY" else None,
                    "targetPerDay": 1, "startDate": "2026-11-01"}

        # 2026-11-01 is a Sunday; gym is Mon/Wed/Fri; swim is weekly (never on a perfect day)
        habits = [habit("read"), habit("gym", "CUSTOM", (1, 3, 5)), habit("swim", "WEEKLY")]
        logs = {"read": [{"date": f"2026-11-0{d}", "count": 1} for d in range(1, 6)],
                "gym": [{"date": "2026-11-02", "count": 1}, {"date": "2026-11-04", "count": 1}],
                "swim": [{"date": "2026-11-03", "count": 1}]}
        c = compute_challenge("2026-11-01", 30, habits, logs, "2026-11-06", 1)
        self.assertEqual((c["status"], c["endDate"], c["dayNumber"], c["daysLeft"]), ("active", "2026-11-30", 6, 24))
        self.assertEqual((c["perfectDays"], c["currentStreak"], c["bestStreak"]), (5, 5, 5))  # today not done yet is pending
        self.assertEqual((c["todayDone"], c["todayTotal"]), (0, 2))
        self.assertEqual([d["scheduled"] for d in c["days"][:7]], [1, 2, 1, 2, 1, 2, 1])
        self.assertEqual({h["id"]: h["completion"] for h in c["habits"]}, {"read": 100.0, "gym": 100.0, "swim": 50.0})
        upcoming = compute_challenge("2026-12-01", 7, habits[:1], logs, "2026-11-06", 1)
        self.assertEqual((upcoming["status"], upcoming["dayNumber"], upcoming["daysLeft"], upcoming["completionRate"]), ("upcoming", 0, 7, None))
        done = compute_challenge("2026-11-01", 3, habits[:1], logs, "2026-11-06", 1)
        self.assertEqual((done["status"], done["daysLeft"], done["completionRate"]), ("completed", 0, 100.0))


class WorkoutMath(unittest.TestCase):
    def test_weekly_series_bests_and_streak(self):
        from app.os.workouts import compute_stats

        bench = lambda reps, kg, done=True: {"exerciseId": "bench-press", "name": "Bench press", "reps": reps, "weightKg": kg, "done": done}
        sessions = [{"date": "2026-11-02", "volumeKg": 980, "sets": [bench(10, 50), bench(8, 60), bench(8, 100, done=False)]},
                    {"date": "2026-10-27", "volumeKg": 400, "sets": [{"exerciseId": None, "name": "Cable fly", "reps": 10, "weightKg": 40, "done": True}]}]
        s = compute_stats(sessions, "2026-11-06", 1, weeks=3)
        self.assertEqual((s["totalSessions"], s["thisWeekSessions"], s["weekStreak"]), (2, 1, 2))
        self.assertEqual([(w["weekStart"], w["sessions"], w["volumeKg"]) for w in s["weekly"]],
                         [("2026-10-19", 0, 0.0), ("2026-10-26", 1, 400.0), ("2026-11-02", 1, 980.0)])
        b = next(e for e in s["exercises"] if e["key"] == "bench-press")
        self.assertEqual((b["sets"], b["bestWeightKg"], b["best1RmKg"], b["history"]), (2, 60, 76.0, [{"date": "2026-11-02", "weightKg": 60}]))
        self.assertTrue(any(e["key"] == "cable fly" for e in s["exercises"]))  # the user's own exercise, keyed by name


class Challenges(OsTestCase):
    async def test_crud_progress_and_ownership(self):
        owner, other = await self.signed_in(), await self.signed_in()
        read = (await owner.post("/api/habits", json={"name": "Read"})).json()
        walk = (await owner.post("/api/habits", json={"name": "Walk"})).json()
        made = await owner.post("/api/challenges", json={"name": "30 days", "durationDays": 30, "habitIds": [read["id"], walk["id"]]})
        self.assertEqual(made.status_code, 201, made.text)
        c = made.json()
        self.assertEqual((c["status"], c["dayNumber"], c["todayTotal"], c["habitIds"]), ("active", 1, 2, [read["id"], walk["id"]]))
        await owner.post(f"/api/habits/{read['id']}/check", json={})
        await owner.post(f"/api/habits/{walk['id']}/check", json={})
        c = (await owner.get(f"/api/challenges/{c['id']}")).json()
        self.assertEqual((c["todayDone"], c["perfectDays"], c["currentStreak"]), (2, 1, 1))

        edited = (await owner.patch(f"/api/challenges/{c['id']}", json={"habitIds": [walk["id"]], "name": "Walk month"})).json()
        self.assertEqual((edited["name"], edited["habitIds"]), ("Walk month", [walk["id"]]))
        self.assertEqual([x["id"] for x in (await owner.get("/api/challenges")).json()["items"]], [c["id"]])

        foreign = (await other.post("/api/habits", json={"name": "Theirs"})).json()
        self.assertEqual((await owner.post("/api/challenges", json={"name": "x", "durationDays": 5, "habitIds": [foreign["id"]]})).status_code, 404)
        for method in ("GET", "PATCH", "DELETE"):
            response = await other.request(method, f"/api/challenges/{c['id']}", json={} if method == "PATCH" else None)
            self.assertEqual((response.status_code, response.json()["message"]), (404, "Challenge not found"), method)
        bad = await owner.post("/api/challenges", json={"name": "x", "durationDays": 0, "habitIds": []})
        self.assertEqual(bad.status_code, 400)

        # deleting a habit takes it out of the challenge; deleting the challenge keeps the habits
        await owner.delete(f"/api/habits/{walk['id']}")
        self.assertEqual((await owner.get(f"/api/challenges/{c['id']}")).json()["habitIds"], [])
        self.assertEqual((await owner.delete(f"/api/challenges/{c['id']}")).json(), {"ok": True})
        self.assertEqual((await owner.get(f"/api/habits/{read['id']}")).status_code, 200)


class Workouts(OsTestCase):
    async def test_plan_session_and_stats(self):
        client, other = await self.signed_in(), await self.signed_in()
        plan = await client.post("/api/workouts/plans", json={"name": "PPL", "days": [
            {"name": "Push", "weekday": 1, "exercises": [{"exerciseId": "bench-press", "name": "Bench press", "sets": 3, "reps": "8-12", "weightKg": 60}]},
            {"name": "Legs", "exercises": [{"name": "Hack squat", "sets": 2, "reps": "10"}]},
        ]})
        self.assertEqual(plan.status_code, 201, plan.text)
        p = plan.json()
        self.assertEqual([(d["name"], d["weekday"], len(d["exercises"])) for d in p["days"]], [("Push", 1, 1), ("Legs", None, 1)])
        push = p["days"][0]

        started = await client.post("/api/workouts/sessions", json={"planDayId": push["id"], "date": "2026-11-02"})
        self.assertEqual(started.status_code, 201, started.text)
        s = started.json()
        self.assertEqual((s["name"], s["planName"], s["setCount"], s["doneSets"]), ("PPL · Push", "PPL", 3, 0))
        self.assertEqual([(x["reps"], x["weightKg"]) for x in s["sets"]], [(8, 60.0)] * 3)  # "8-12" → 8

        sets = [{**{k: x[k] for k in ("exerciseId", "name", "reps", "weightKg")}, "done": True} for x in s["sets"]]
        sets[2]["weightKg"] = 65
        logged = (await client.patch(f"/api/workouts/sessions/{s['id']}", json={"sets": sets, "completed": True, "durationMin": 45})).json()
        self.assertEqual((logged["doneSets"], logged["volumeKg"], logged["durationMin"], bool(logged["completedAt"])), (3, 1480.0, 45, True))

        stats = (await client.get("/api/workouts/stats")).json()
        bench = next(e for e in stats["exercises"] if e["key"] == "bench-press")
        self.assertEqual((stats["totalSessions"], bench["bestWeightKg"]), (1, 65.0))

        # editing the plan replaces its days; the logged session keeps its sets
        await client.patch(f"/api/workouts/plans/{p['id']}", json={"days": [{"name": "Full body"}]})
        kept = (await client.get(f"/api/workouts/sessions/{s['id']}")).json()
        self.assertEqual((kept["planDayId"], kept["setCount"]), (None, 3))

        for path in (f"/api/workouts/plans/{p['id']}", f"/api/workouts/sessions/{s['id']}"):
            self.assertEqual((await other.get(path)).status_code, 404)
        self.assertEqual((await other.post("/api/workouts/sessions", json={"planDayId": push["id"]})).status_code, 404)
        bad = await client.post("/api/workouts/plans", json={"name": "x", "days": [{"name": "d", "exercises": [{"exerciseId": "nope", "name": "x"}]}]})
        self.assertEqual(bad.status_code, 400)
        self.assertGreater(len((await client.get("/api/workouts/exercises")).json()["items"]), 30)

    async def test_dashboard_widgets_appear_only_with_data(self):
        client = await self.signed_in()
        await client.patch("/api/settings", json={"enabledSections": ["tasks", "habits", "health"]})
        first = (await client.get("/api/dashboard")).json()
        self.assertEqual((first["challenges"], first["workouts"]), (None, None))
        habit = (await client.post("/api/habits", json={"name": "Read"})).json()
        await client.post("/api/challenges", json={"name": "Month", "durationDays": 30, "habitIds": [habit["id"]]})
        await client.post("/api/workouts/plans", json={"name": "Plan", "days": [{"name": "A"}]})
        d = (await client.get("/api/dashboard")).json()
        self.assertEqual((d["challenges"]["total"], d["challenges"]["items"][0]["name"]), (1, "Month"))
        self.assertEqual((d["workouts"]["totalSessions"], d["workouts"]["thisWeekSessions"]), (0, 0))


class PlanTools(ChatBase):
    async def run_tool(self, tool, tool_input):
        fm.script(fm.tools((tool, tool_input)), fm.message(fm.text("Done.")))
        reply = (await self.say("set me up"))["messages"][1]
        result = fm.requests("python")[-1]["body"]["messages"][-1]["content"][0]
        self.assertFalse(result.get("is_error"), result)
        return reply["actions"][0] if reply["actions"] else None

    async def undo(self, action):
        response = await self.client.post(f"/api/assistant/actions/{action['id']}/undo")
        self.assertEqual(response.status_code, 200, response.text)

    async def test_dashboard_layout_and_its_undo(self):
        before = (await self.client.get("/api/settings")).json()
        action = await self.run_tool("set_dashboard_layout", {"widgets": ["habits", "workouts", "todayTasks"]})
        self.assertEqual((action["entityType"], action["canUndo"]), ("dashboard", True))
        s = (await self.client.get("/api/settings")).json()
        self.assertEqual([w["id"] for w in s["dashboardWidgets"] if w["visible"]], ["habits", "workouts", "todayTasks"])
        self.assertIn("health", s["enabledSections"])  # the workouts widget needs it
        await self.undo(action)
        s = (await self.client.get("/api/settings")).json()
        self.assertEqual((s["dashboardWidgets"], s["enabledSections"]), (before["dashboardWidgets"], before["enabledSections"]))

    async def test_bad_widget_goes_back_to_the_model(self):
        fm.script(fm.tools(("set_dashboard_layout", {"widgets": ["weather"]})), fm.message(fm.text("Hm.")))
        await self.say("show weather")
        result = fm.requests("python")[-1]["body"]["messages"][-1]["content"][0]
        self.assertTrue(result.get("is_error"))
        self.assertIn("Unknown widget weather", result["content"])

    async def test_challenge_reuses_habits_and_undo_removes_only_new_ones(self):
        read = (await self.client.post("/api/habits", json={"name": "Read"})).json()
        action = await self.run_tool("create_challenge", {"name": "Morning month", "duration_days": 30,
                                                          "habits": [{"name": "read"}, {"name": "Cold shower"}, {"name": "Gym", "frequency": "CUSTOM", "days_of_week": [1, 3, 5]}]})
        c = (await self.client.get(f"/api/challenges/{action['entityId']}")).json()
        self.assertEqual([h["name"] for h in c["habits"]], ["Read", "Cold shower", "Gym"])
        self.assertEqual(c["habitIds"][0], read["id"])
        await self.undo(action)
        self.assertEqual((await self.client.get(f"/api/challenges/{c['id']}")).status_code, 404)
        self.assertEqual([h["name"] for h in (await self.client.get("/api/habits")).json()["items"]], ["Read"])

    async def test_bad_challenge_habit_leaves_nothing_behind(self):
        fm.script(fm.tools(("create_challenge", {"name": "x", "duration_days": 10, "habits": [{"name": "Ok"}, {"name": "Bad", "frequency": "CUSTOM", "days_of_week": []}]})),
                  fm.message(fm.text("Hm.")))
        await self.say("challenge")
        self.assertTrue(fm.requests("python")[-1]["body"]["messages"][-1]["content"][0].get("is_error"))
        self.assertEqual((await self.client.get("/api/habits")).json()["items"], [])
        self.assertEqual((await self.client.get("/api/challenges")).json()["items"], [])

    async def test_workout_plan_maps_catalog_names(self):
        action = await self.run_tool("create_workout_plan", {"name": "Upper/Lower", "days": [
            {"name": "Upper", "weekday": 1, "exercises": [{"name": "Bench press", "sets": 4, "reps": "6-8", "weight_kg": 70}, {"name": "My band thing", "reps": 15}]},
            {"name": "Lower", "weekday": 4, "exercises": [{"name": "squat", "sets": 5, "reps": 5}]},
        ]})
        p = (await self.client.get(f"/api/workouts/plans/{action['entityId']}")).json()
        self.assertEqual([(e["exerciseId"], e["sets"], e["reps"]) for e in p["days"][0]["exercises"]], [("bench-press", 4, "6-8"), (None, 3, "15")])
        self.assertEqual(p["days"][1]["exercises"][0]["exerciseId"], "squat")
        await self.undo(action)
        self.assertEqual((await self.client.get("/api/workouts/plans")).json()["items"], [])

    async def test_manual_mode_waits_then_creates(self):
        await self.client.patch("/api/settings", json={"assistantMode": "manual"})
        fm.script(fm.tools(("create_habit", {"name": "Meditate", "reminder_time": "07:00"})), fm.message(fm.text("Approve below.")))
        action = (await self.say("add meditate"))["messages"][1]["actions"][0]
        self.assertEqual((action["status"], action["entityType"], action["label"]), ("pending", "habit", "Meditate"))
        self.assertEqual((await self.client.get("/api/habits")).json()["items"], [])
        done = await self.client.post(f"/api/assistant/actions/{action['id']}/approve")
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual([h["reminderTime"] for h in (await self.client.get("/api/habits")).json()["items"]], ["07:00"])

    async def test_long_pasted_plan_is_accepted(self):
        fm.script(fm.message(fm.text("Got it.")))
        self.assertEqual((await self.say("step. " * 1500))["status"], "ok")  # 9000 characters
