"""Sign-in, settings and the account answer like NestJS did.

Deliberate differences (spec): `plan` is new on the web's user; twoFactorEnabled
is always false and lastLoginAt always null (app accounts don't have them);
the timezone list comes from Python's zoneinfo, not ICU.
"""

import unittest

from vournal_os.parity.harness import ParityCase

_USER_EXTRAS = ("plan", "lastLoginAt")


class AuthSettingsParity(ParityCase):
    async def test_auth_me(self):
        nest, py = await self.both("GET", "/auth/me")
        self.assertParity(nest, py, ignore_keys=_USER_EXTRAS)

    async def test_settings_get(self):
        nest, py = await self.both("GET", "/settings")
        self.assertParity(nest, py)

    async def test_settings_patch(self):
        patch = {
            "defaultCurrency": "LBP",
            "timezone": "Asia/Beirut",
            "weekStartsOn": 6,
            "theme": "DARK",
            "enabledSections": ["tasks", "finance", "tasks", "health"],
            "dashboardWidgets": [{"id": "habits", "visible": False, "order": 0}],
            "notificationPrefs": {"email": True, "dailyDigestTime": None},
        }
        nest, py = await self.both("PATCH", "/settings", patch)
        self.assertParity(nest, py)
        nest, py = await self.both("GET", "/settings")
        self.assertParity(nest, py)

    async def test_settings_patch_language_ar(self):
        nest, py = await self.both("PATCH", "/settings", {"language": "ar"})
        self.assertParity(nest, py)
        nest, py = await self.both("GET", "/auth/me")
        self.assertParity(nest, py, ignore_keys=_USER_EXTRAS)

    async def test_settings_options(self):
        nest, py = await self.both("GET", "/settings/options")
        self.assertParity(nest, py, ignore_keys=("timezones",))
        self.assertIn("Asia/Beirut", py.json()["timezones"])
        self.assertEqual(py.json()["timezones"][0], "UTC")

    async def test_users_me_patch(self):
        nest, py = await self.both("PATCH", "/users/me", {"name": "  Rania  ", "avatarColor": "#aabbcc"})
        self.assertParity(nest, py, ignore_keys=_USER_EXTRAS)
        nest, py = await self.both("GET", "/users/me")
        self.assertParity(nest, py, ignore_keys=_USER_EXTRAS)

    async def test_settings_validation_shape(self):
        for body in ({"bogus": 1}, {"language": "fr"}, {"weekStartsOn": 3}, {"timezone": "Mars/Base"}):
            nest, py = await self.both("PATCH", "/settings", body)
            self.assertParity(nest, py)

    async def test_unauthenticated_shape(self):
        self.nest.cookies.clear()
        self.py.cookies.clear()
        nest, py = await self.both("GET", "/settings")
        self.assertParity(nest, py)


if __name__ == "__main__":
    unittest.main()
