"""Until web checkout opens, a free account can ask to be emailed when it does
(owner, 2026-11-23: Paddle's verification has to wait).

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check vournal_os.test_web_checkout_notify
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx

PASSWORD = "web-passphrase-1"
CSRF = {"X-Requested-With": "life-os"}
ROUTE = "/api/users/me/web-checkout-notify"


class WebCheckoutNotify(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app import security
        from app.db import db
        from app.main import app
        from app.rate_limit import limiter

        self.db = db
        self.security = security
        await db.connect()
        limiter.reset()
        self.emails: list[str] = []
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://life.test", headers=CSRF
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        if self.emails:
            await self.db.user.delete_many(where={"email": {"in": self.emails}})
        await self.db.disconnect()

    async def _user(self, **data):
        email = f"notify-{uuid.uuid4().hex}@example.test"
        self.emails.append(email)
        fields = {"email": email, "passwordHash": self.security.hash_password(PASSWORD)}
        fields.update(data)
        return await self.db.user.create(data=fields)

    async def _sign_in(self, user):
        response = await self.client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)

    async def test_free_account_starts_off(self):
        await self._sign_in(await self._user())
        me = await self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertIs(me.json()["user"]["webCheckoutNotify"], False)

    async def test_free_account_turns_it_on_and_off(self):
        user = await self._user()
        await self._sign_in(user)

        on = await self.client.put(ROUTE, json={"on": True})
        self.assertEqual(on.status_code, 200, on.text)
        self.assertIs(on.json()["webCheckoutNotify"], True)
        row = await self.db.usersettings.find_unique(where={"userId": user.id})
        self.assertIsNotNone(row.webCheckoutNotifyAt)
        self.assertIs((await self.client.get("/api/auth/me")).json()["user"]["webCheckoutNotify"], True)

        off = await self.client.put(ROUTE, json={"on": False})
        self.assertEqual(off.status_code, 200, off.text)
        self.assertIs(off.json()["webCheckoutNotify"], False)
        row = await self.db.usersettings.find_unique(where={"userId": user.id})
        self.assertIsNone(row.webCheckoutNotifyAt)

    async def test_asking_twice_keeps_the_first_time(self):
        user = await self._user()
        await self._sign_in(user)
        await self.client.put(ROUTE, json={"on": True})
        first = (await self.db.usersettings.find_unique(where={"userId": user.id})).webCheckoutNotifyAt
        await self.client.put(ROUTE, json={"on": True})
        again = (await self.db.usersettings.find_unique(where={"userId": user.id})).webCheckoutNotifyAt
        self.assertEqual(first, again)

    async def test_needs_a_session(self):
        response = await self.client.put(ROUTE, json={"on": True})
        self.assertEqual(response.status_code, 401)

    async def test_needs_a_true_or_false(self):
        await self._sign_in(await self._user())
        self.assertEqual((await self.client.put(ROUTE, json={"on": "yes"})).status_code, 400)
        self.assertEqual((await self.client.put(ROUTE, json={})).status_code, 400)


class EmailTheWaitlist(unittest.IsolatedAsyncioTestCase):
    """scripts/email_web_checkout_waitlist.py, once checkout is open: free accounts that asked."""

    async def asyncSetUp(self):
        from app import security
        from app.db import db
        from app.os.profile import ensure_os_profile

        self.db = db
        await db.connect()
        self.emails: list[str] = []
        now = datetime.now(timezone.utc)

        async def make(asked: bool, **data):
            email = f"waitlist-{uuid.uuid4().hex}@example.test"
            self.emails.append(email)
            user = await db.user.create(data={"email": email, "passwordHash": security.hash_password(PASSWORD), **data})
            await ensure_os_profile(user)
            if asked:
                await db.usersettings.update(where={"userId": user.id}, data={"webCheckoutNotifyAt": now})
            return user

        self.free_asked = await make(True)
        self.plus_asked = await make(True, plan="plus", planSource="apple", planExpiresAt=now + timedelta(days=20))
        self.lapsed_asked = await make(True, plan="plus", planSource="apple", planExpiresAt=now - timedelta(days=1))
        self.free_quiet = await make(False)

    async def asyncTearDown(self):
        await self.db.user.delete_many(where={"email": {"in": self.emails}})
        await self.db.disconnect()

    async def _mine(self, users):
        return sorted(u.email for u in users if u.email in self.emails)

    async def test_lists_free_accounts_that_asked(self):
        from app.web_checkout_waitlist import waitlist

        self.assertEqual(
            await self._mine(await waitlist()), sorted([self.free_asked.email, self.lapsed_asked.email])
        )

    async def test_dry_run_sends_nothing(self):
        from app import web_checkout_waitlist

        with mock.patch.object(web_checkout_waitlist, "send_web_checkout_open", mock.AsyncMock(return_value=True)) as send:
            await web_checkout_waitlist.email_waitlist(send=False)
        send.assert_not_called()
        row = await self.db.usersettings.find_unique(where={"userId": self.free_asked.id})
        self.assertIsNotNone(row.webCheckoutNotifyAt)

    async def test_send_emails_each_once(self):
        from app import web_checkout_waitlist

        sent: list[str] = []

        async def fake_send(to):
            sent.append(to)
            return to != self.lapsed_asked.email  # one delivery fails

        with mock.patch.object(web_checkout_waitlist, "send_web_checkout_open", fake_send):
            await web_checkout_waitlist.email_waitlist(send=True)
            mine = [e for e in sent if e in self.emails]
            self.assertEqual(sorted(mine), sorted([self.free_asked.email, self.lapsed_asked.email]))

            # delivered: off the list; failed: stays for the next run
            done = await self.db.usersettings.find_unique(where={"userId": self.free_asked.id})
            failed = await self.db.usersettings.find_unique(where={"userId": self.lapsed_asked.id})
            self.assertIsNone(done.webCheckoutNotifyAt)
            self.assertIsNotNone(failed.webCheckoutNotifyAt)

            sent.clear()
            await web_checkout_waitlist.email_waitlist(send=True)
            self.assertEqual([e for e in sent if e in self.emails], [self.lapsed_asked.email])
