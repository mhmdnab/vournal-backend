"""The app's push tokens (design 2026-11-18): one row per phone, moved to whoever signs in on it,
removed on sign-out, and gone with the account."""

from datetime import datetime, timedelta, timezone

from vournal_os.support import OsTestCase

TOKEN = "ExponentPushToken[abc123]"


class PushDevices(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.app = await self.mobile()

    async def register(self, client=None, token=TOKEN, tz="Asia/Beirut", status=204, **extra):
        r = await (client or self.app).post("/devices/push-token", json={"token": token, "timezone": tz, **extra})
        self.assertEqual(r.status_code, status, r.text)

    async def rows(self, token=TOKEN):
        return await self.db.pushdevice.find_many(where={"token": token})

    async def test_register_creates_row(self):
        await self.register(appVersion="1.9")
        [row] = await self.rows()
        self.assertEqual((row.userId, row.timezone, row.appVersion, row.platform), (self.app.user.id, "Asia/Beirut", "1.9", "ios"))
        self.assertIsNotNone(row.lastSeenAt)

    async def test_register_again_updates_timezone_and_last_seen(self):
        await self.register()
        old = datetime.now(timezone.utc) - timedelta(days=3)
        await self.db.pushdevice.update_many(where={"token": TOKEN}, data={"lastSeenAt": old})
        await self.register(tz="Europe/Paris")
        [row] = await self.rows()
        self.assertEqual(row.timezone, "Europe/Paris")
        self.assertGreater(row.lastSeenAt, old)

    async def test_token_moves_to_new_account(self):
        await self.register()
        other = await self.mobile()
        await self.register(client=other)
        [row] = await self.rows()
        self.assertEqual(row.userId, other.user.id)
        r = await self.app.request("DELETE", "/devices/push-token", json={"token": TOKEN})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(len(await self.rows()), 1)

    async def test_bad_timezone_stored_as_utc(self):
        await self.register(tz="Mars/Base")
        [row] = await self.rows()
        self.assertEqual(row.timezone, "UTC")

    async def test_rejects_non_expo_token(self):
        await self.register(token="abc", status=422)
        await self.register(token="ExponentPushToken[" + "x" * 300 + "]", status=422)

    async def test_delete_removes_own_token(self):
        await self.register()
        r = await self.app.request("DELETE", "/devices/push-token", json={"token": TOKEN})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(await self.rows(), [])
        r = await self.app.request("DELETE", "/devices/push-token", json={"token": TOKEN})
        self.assertEqual(r.status_code, 204)

    async def test_needs_sign_in(self):
        import httpx
        from app.main import app

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://api.test") as anon:
            r = await anon.post("/devices/push-token", json={"token": TOKEN, "timezone": "UTC"})
        self.assertEqual(r.status_code, 401)

    async def test_latest_device_tz_uses_most_recent(self):
        from app.push_devices import latest_device_tz

        self.assertIsNone(await latest_device_tz(self.app.user.id))
        await self.register(token="ExponentPushToken[one]", tz="Asia/Tokyo")
        await self.db.pushdevice.update_many(where={"token": "ExponentPushToken[one]"},
                                             data={"lastSeenAt": datetime.now(timezone.utc) - timedelta(hours=5)})
        await self.register(token="ExponentPushToken[two]", tz="America/New_York")
        self.assertEqual(await latest_device_tz(self.app.user.id), "America/New_York")

    async def test_account_deletion_cascades(self):
        await self.register()
        await self.db.user.delete(where={"id": self.app.user.id})
        self.assertEqual(await self.rows(), [])


class RepeatGuard(PushDevices):
    """2026-11-18 incident: build 15 re-registered its token in a tight loop. The same person, token,
    time zone and version are written at most once every 10 minutes; repeats answer at once."""

    async def test_an_exact_repeat_writes_nothing(self):
        await self.register(appVersion="1.2.0")
        old = datetime.now(timezone.utc) - timedelta(days=3)
        await self.db.pushdevice.update_many(where={"token": TOKEN}, data={"lastSeenAt": old})
        for _ in range(20):
            await self.register(appVersion="1.2.0")
        [row] = await self.rows()
        self.assertLess(row.lastSeenAt, datetime.now(timezone.utc) - timedelta(days=2))  # still the old date: no write

    async def test_a_new_time_zone_or_another_account_still_writes_at_once(self):
        await self.register()
        await self.register(tz="Europe/Paris")
        self.assertEqual((await self.rows())[0].timezone, "Europe/Paris")
        other = await self.mobile()
        await self.register(client=other, tz="Europe/Paris")
        self.assertEqual((await self.rows())[0].userId, other.user.id)

    async def test_after_ten_minutes_it_writes_again(self):
        from app import push_devices

        await self.register()
        old = datetime.now(timezone.utc) - timedelta(days=3)
        await self.db.pushdevice.update_many(where={"token": TOKEN}, data={"lastSeenAt": old})
        for key in list(push_devices._recent):
            push_devices._recent[key] -= push_devices.REPEAT_WINDOW + 1
        await self.register()
        self.assertGreater((await self.rows())[0].lastSeenAt, old)
