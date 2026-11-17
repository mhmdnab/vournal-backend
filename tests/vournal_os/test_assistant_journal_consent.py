"""Permission for the assistant to read the journal (design 2026-11-17, Ask vournal): asked once,
stored on the server so the app and the website share it. No model calls."""

from vournal_os.support import OsTestCase

URL = "/api/assistant/journal-consent"


class JournalConsent(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.user = await self.make_user()
        self.client = await self.signed_in(self.user)

    async def settings(self):
        return await self.db.usersettings.find_unique(where={"userId": self.user.id})

    async def test_fresh_user_has_not_been_asked(self):
        r = await self.client.get(URL)
        self.assertEqual((r.status_code, r.json()), (200, {"allowed": False, "asked": False}))

    async def test_allow_turns_ai_on(self):
        r = await self.client.post(URL, json={"allow": True})
        self.assertEqual((r.status_code, r.json()), (200, {"allowed": True, "asked": True}))
        s = await self.settings()
        self.assertTrue(s.aiEnabled)
        self.assertIsNotNone(s.assistantJournalConsentAt)
        self.assertIsNotNone(s.assistantJournalAskedAt)

    async def test_not_now_after_allow_takes_it_back_and_keeps_ai_on(self):
        await self.client.post(URL, json={"allow": True})
        r = await self.client.post(URL, json={"allow": False})
        self.assertEqual(r.json(), {"allowed": False, "asked": True})
        self.assertTrue((await self.settings()).aiEnabled)
        self.assertEqual((await self.client.get(URL)).json(), {"allowed": False, "asked": True})

    async def test_not_now_leaves_ai_switch_alone(self):
        r = await self.client.post(URL, json={"allow": False})
        self.assertEqual(r.json(), {"allowed": False, "asked": True})
        self.assertFalse((await self.settings()).aiEnabled)

    async def test_allow_must_be_a_real_bool(self):
        self.assertEqual((await self.client.post(URL, json={"allow": "yes"})).status_code, 400)
        self.assertEqual((await self.client.post(URL, json={})).status_code, 400)

    async def test_plus_only(self):
        free = await self.signed_in(await self.make_user(plan="free"))
        self.assertEqual((await free.get(URL)).status_code, 403)
        self.assertEqual((await free.post(URL, json={"allow": True})).status_code, 403)

    async def test_app_token_works_too(self):
        app = await self.mobile(self.user)
        self.assertEqual((await app.get(URL)).json(), {"allowed": False, "asked": False})
