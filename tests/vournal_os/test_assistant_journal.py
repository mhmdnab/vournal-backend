"""The assistant reads the journal (design 2026-11-17, Ask vournal): two read-only tools, only
after the user allowed it; and the app's "always ask" messages. The model is fake_model."""

import json
from datetime import datetime, timedelta, timezone

from vournal_os.parity import fake_model as fm
from vournal_os.test_assistant import ChatBase

JOURNAL = {"search_journal", "get_journal_entries"}


class JournalBase(ChatBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.user = self.client.user

    async def entry(self, text, days_ago=0, user=None, **data):
        made = await self.db.entry.create(data={
            "userId": (user or self.user).id, "rawTranscript": text, "status": "DONE",
            "createdAt": datetime.now(timezone.utc) - timedelta(days=days_ago), **data})
        return made

    async def tool(self, name, payload, user=None):
        from app.os import assistant_tools

        u = user or self.user
        ctx = assistant_tools.Ctx(u, "Asia/Beirut", assistant_tools.local_day(datetime.now(timezone.utc), "Asia/Beirut"), "manual")
        ctx.journal = True  # the person allowed it (the runner refuses these tools otherwise)
        out = await assistant_tools.run(name, payload, ctx)
        self.assertFalse(out.get("isError"), out)
        return json.loads(out["content"])


class JournalTools(JournalBase):
    async def test_search_only_finds_the_users_own_entries(self):
        other = await self.make_user()
        await self.entry("tired after the gym", user=other)
        mine = await self.entry("so tired today")
        found = await self.tool("search_journal", {"query": "TIRED"})
        self.assertEqual([e["id"] for e in found], [mine.id])

    async def test_hidden_and_unsaved_entries_never_come_back(self):
        await self.entry("tired, waiting for review", reviewState="needs_review")
        await self.entry("tired, asked vournal", reviewState="asked")
        await self.entry("tired, kept out of the journal", reviewState="saved", inJournal=False)
        kept = await self.entry("tired and saved", reviewState="saved")
        self.assertEqual([e["id"] for e in await self.tool("search_journal", {"query": "tired"})], [kept.id])

    async def test_search_matches_people_topics_and_places(self):
        e = await self.entry("long walk")
        person = await self.db.person.create(data={"userId": self.user.id, "name": "Sara", "normalizedName": "sara"})
        await self.db.entryperson.create(data={"entryId": e.id, "personId": person.id})
        topic = await self.db.topic.create(data={"userId": self.user.id, "name": "sleep", "normalizedName": "sleep"})
        await self.db.entrytopic.create(data={"entryId": e.id, "topicId": topic.id})
        place = await self.db.place.create(data={"userId": self.user.id, "name": "Hamra", "normalizedName": "hamra"})
        await self.db.entryplace.create(data={"entryId": e.id, "placeId": place.id})
        for q in ("sara", "Sleep", "hamra"):
            self.assertEqual([x["id"] for x in await self.tool("search_journal", {"query": q})], [e.id], q)

    async def test_limit_is_capped_at_20_and_default_is_10(self):
        for n in range(25):
            await self.entry(f"walked {n}", days_ago=n)
        self.assertEqual(len(await self.tool("search_journal", {"query": "walked", "limit": 50})), 20)
        self.assertEqual(len(await self.tool("search_journal", {"query": "walked"})), 10)

    async def test_entries_in_a_range_newest_first(self):
        await self.entry("old", days_ago=30)
        a = await self.entry("three days ago", days_ago=3)
        b = await self.entry("yesterday", days_ago=1)
        today = datetime.now(timezone.utc)
        frm, to = (today - timedelta(days=7)).date().isoformat(), today.date().isoformat()
        got = await self.tool("get_journal_entries", {"from": frm, "to": to})
        self.assertEqual([e["id"] for e in got], [b.id, a.id])

    async def test_entry_with_missing_parts_still_reads(self):
        e = await self.entry("")
        got = await self.tool("get_journal_entries", {"from": "2000-01-01", "to": "2100-01-01"})
        self.assertEqual(got, [{"id": e.id, "date": got[0]["date"], "mood": None, "summary": None, "excerpt": ""}])

    async def test_excerpt_is_cut_at_600_characters(self):
        await self.entry("x" * 900, summary="a long one", moodScore=4)
        got = (await self.tool("get_journal_entries", {"from": "2000-01-01", "to": "2100-01-01"}))[0]
        self.assertEqual((len(got["excerpt"]), got["summary"], got["mood"]), (600, "a long one", 4))


class JournalGating(JournalBase):
    def tool_names(self):
        return {t["name"] for t in fm.requests("python")[-1]["body"]["tools"]}

    async def test_tools_hidden_until_the_user_allows_it(self):
        fm.script(fm.message(fm.text("Hi")))
        await self.say("hello")
        self.assertFalse(JOURNAL & self.tool_names())
        await self.client.post("/api/assistant/journal-consent", json={"allow": True})
        fm.script(fm.message(fm.text("Hi")))
        await self.say("hello again")
        self.assertEqual(JOURNAL & self.tool_names(), JOURNAL)

    async def test_entry_actions_never_get_journal_tools(self):
        from app import entry_actions

        self.assertFalse(JOURNAL & {t["name"] for t in entry_actions.TOOLS_FOR_ENTRY})

    async def test_imports_cannot_use_journal_tools(self):
        from app.os import assistant_tools

        self.assertFalse(JOURNAL & assistant_tools.IMPORT_TOOLS)
        self.assertEqual(JOURNAL & assistant_tools.READ_TOOLS, JOURNAL)


class AlwaysAsk(JournalBase):
    async def test_always_ask_waits_even_in_auto_mode(self):
        fm.script(fm.tools(("create_task", {"title": "Gym"})), fm.message(fm.text("Approve below.")))
        r = await self.client.post("/api/assistant/chat", json={"message": "add gym", "alwaysAsk": True})
        self.assertEqual(r.status_code, 200, r.text)
        action = r.json()["messages"][1]["actions"][0]
        self.assertEqual(action["status"], "pending")
        self.assertEqual((await self.client.get("/api/tasks")).json()["items"], [])

    async def test_always_ask_must_be_a_bool(self):
        r = await self.client.post("/api/assistant/chat", json={"message": "hi", "alwaysAsk": "yes"})
        self.assertEqual(r.status_code, 400)
