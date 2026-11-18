"""Nudge settings for the app (design 2026-11-18): free and Plus, stored in notificationPrefs next
to the website's keys, plus the agreement to AI-written nudges (nudgeAiConsentAt)."""

from vournal_os.support import OsTestCase


class NudgeRoutes(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.app = await self.mobile()

    async def get(self):
        r = await self.app.get("/nudges/prefs")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    async def patch(self, status=200, **body):
        r = await self.app.patch("/nudges/prefs", json=body)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    async def settings(self):
        return await self.db.usersettings.find_unique(where={"userId": self.app.user.id})

    async def test_free_user_gets_defaults_without_a_settings_row(self):
        free = await self.mobile(await self.make_user(plan="free"))
        self.assertIsNone(await self.db.usersettings.find_unique(where={"userId": free.user.id}))
        r = await free.get("/nudges/prefs")
        self.assertEqual(r.json(), {"nudges": False, "morningTime": "08:00", "plan": True, "checkIns": True,
                                    "suggestions": True, "aiWriting": False, "plus": False})
        # a read makes no row (2026-11-18 leftovers); the first change does
        self.assertIsNone(await self.db.usersettings.find_unique(where={"userId": free.user.id}))
        await free.patch("/nudges/prefs", json={"nudges": True})
        self.assertIsNotNone(await self.db.usersettings.find_unique(where={"userId": free.user.id}))

    async def test_patch_keeps_web_prefs(self):
        from prisma import Json

        await self.patch()  # makes the settings row (a read no longer does)
        await self.db.usersettings.update(where={"userId": self.app.user.id},
                                          data={"notificationPrefs": Json({"bills": False, "dailyDigestTime": "07:00"})})
        out = await self.patch(nudges=True, morningTime="07:15", suggestions=False)
        self.assertEqual((out["nudges"], out["morningTime"], out["suggestions"]), (True, "07:15", False))
        prefs = (await self.settings()).notificationPrefs
        self.assertEqual((prefs["bills"], prefs["dailyDigestTime"], prefs["nudgeMorningTime"]), (False, "07:00", "07:15"))

    async def test_bad_morning_time_is_422(self):
        for bad in ("23:00", "04:59", "8:00", "11:31"):
            await self.patch(status=422, morningTime=bad)

    async def test_unknown_key_is_422(self):
        await self.patch(status=422, bills=False)

    async def test_ai_writing_sets_and_clears_consent(self):
        out = await self.patch(aiWriting=True)
        self.assertTrue(out["aiWriting"])
        first = (await self.settings()).nudgeAiConsentAt
        self.assertIsNotNone(first)
        await self.patch(aiWriting=True)
        self.assertEqual((await self.settings()).nudgeAiConsentAt, first)
        out = await self.patch(aiWriting=False)
        self.assertFalse(out["aiWriting"])
        self.assertIsNone((await self.settings()).nudgeAiConsentAt)

    async def test_plus_flag_follows_effective_plan(self):
        self.assertTrue((await self.get())["plus"])
        await self.db.user.update(where={"id": self.app.user.id}, data={"plan": "free"})
        self.assertFalse((await self.get())["plus"])
