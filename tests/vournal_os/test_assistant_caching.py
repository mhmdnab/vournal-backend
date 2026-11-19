"""Prompt caching (2026-11-19): the system prompt (and the tools before it) is cached at its own
breakpoint, so a new conversation reads it instead of writing it again; the growing conversation
keeps the automatic breakpoint."""

from vournal_os.parity import fake_model as fm
from vournal_os.test_assistant import ChatBase


class Caching(ChatBase):
    async def test_system_prompt_has_its_own_cache_breakpoint(self):
        from app.os import assistant

        fm.script(fm.message(fm.text("Hi!")))
        await self.say("hello")
        body = fm.requests("python")[-1]["body"]
        self.assertEqual(body["cache_control"], {"type": "ephemeral"})  # the conversation tail, as before
        self.assertEqual(body["system"], [{"type": "text", "text": assistant.SYSTEM, "cache_control": {"type": "ephemeral"}}])
