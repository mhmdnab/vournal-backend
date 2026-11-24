"""Every email uses the shared vournal template (owner approved the designs 2026-11-24):
the logo, a serif headline, at most one button, a plain-text twin with the links written out,
and "vournal" (lowercase) as the sender name."""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch


class Emails(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app import email

        self.email = email
        self.sent = []

        async def capture(to, subject, html_body, text):
            self.sent.append({"to": to, "subject": subject, "html": html_body, "text": text})
            return True

        self.patch = patch.object(email, "_send", capture)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()

    def last(self):
        return self.sent[-1]

    def assert_template(self, mail):
        self.assertIn("https://www.vournal.app/apple-icon.png", mail["html"])
        self.assertIn("hello@vournal.app", mail["html"])
        self.assertIn("<h1", mail["html"])
        for tag in ("<p", "<a ", "<table", "<strong"):
            self.assertNotIn(tag, mail["text"])  # the text twin is plain
        self.assertIn("hello@vournal.app", mail["text"])

    def test_sender_name_is_lowercase(self):
        self.assertTrue(self.email.DEFAULT_FROM.startswith("vournal <"))

    async def test_code_is_in_the_subject_and_the_body(self):
        await self.email.send_verification_code("a@example.test", "482913")
        mail = self.last()
        self.assert_template(mail)
        self.assertEqual(mail["subject"], "482 913 is your vournal code")
        self.assertIn("482 913", mail["html"])
        self.assertIn("482913", mail["text"])
        self.assertIn("10 minutes", mail["text"])

    async def test_code_with_the_password_note(self):
        await self.email.send_verification_code("a@example.test", "482913", "This removes its old password.")
        mail = self.last()
        self.assertIn("This removes its old password.", mail["html"])
        self.assertIn("This removes its old password.", mail["text"])

    async def test_password_reset(self):
        url = "https://vournal.app/reset-password?token=abc&x=1"
        await self.email.send_password_reset("a@example.test", url, 60)
        mail = self.last()
        self.assert_template(mail)
        self.assertEqual(mail["subject"], "Reset your vournal password")
        self.assertIn("token=abc&amp;x=1", mail["html"])  # escaped in HTML
        self.assertIn(url, mail["text"])
        self.assertIn("60 minutes", mail["text"])

    async def test_plus_confirm_link(self):
        await self.email.send_plus_confirm_link("a@example.test", "https://api.vournal.app/confirm?token=t")
        mail = self.last()
        self.assert_template(mail)
        self.assertEqual(mail["subject"], "Confirm your email to turn on vournal Plus")
        self.assertIn("https://api.vournal.app/confirm?token=t", mail["text"])

    async def test_web_checkout_open(self):
        await self.email.send_web_checkout_open("a@example.test")
        mail = self.last()
        self.assert_template(mail)
        self.assertEqual(mail["subject"], "You can now get vournal Plus on the web")
        self.assertIn("https://life.vournal.app", mail["text"])

    async def test_sharing_emails_escape_names(self):
        await self.email.send_workspace_invite("a@example.test", "Home <b>", "Maya & Co", "https://life.vournal.app/invite/workspace/c")
        invite = self.last()
        self.assert_template(invite)
        self.assertEqual(invite["subject"], "Maya & Co invited you to “Home <b>” on vournal")
        self.assertIn("Home &lt;b&gt;", invite["html"])
        self.assertIn("Maya &amp; Co", invite["html"])
        await self.email.send_invite_accepted("a@example.test", "Sam", "Home")
        self.assertEqual(self.last()["subject"], "Sam joined “Home”")
        await self.email.send_project_share("a@example.test", "Kitchen", "Maya", "https://life.vournal.app/invite/project/c")
        share = self.last()
        self.assertEqual(share["subject"], "Maya shared “Kitchen” with you")
        self.assertIn("don’t need vournal Plus", share["text"])

    async def test_free_month_ending_will_renew_with_apple(self):
        ends = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
        await self.email.send_free_month_ending("a@example.test", ends, 3, renews=True, billed_by="apple", manage_url="https://apps.apple.com/account/subscriptions")
        mail = self.last()
        self.assert_template(mail)
        self.assertEqual(mail["subject"], "Your free month of vournal Plus ends in 3 days")
        self.assertIn("Friday, 27 November", mail["html"])
        self.assertIn("Billed by: Apple", mail["text"])
        self.assertIn("https://apps.apple.com/account/subscriptions", mail["text"])

    async def test_free_month_ending_already_cancelled(self):
        ends = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
        await self.email.send_free_month_ending("a@example.test", ends, 3, renews=False, billed_by="apple", manage_url="https://apps.apple.com/account/subscriptions")
        mail = self.last()
        self.assertEqual(mail["subject"], "Your vournal Plus ends in 3 days")
        self.assertIn("nothing will be charged", mail["text"])
        self.assertIn("Keep vournal Plus", mail["html"])

    async def test_free_month_ending_with_paddle(self):
        ends = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
        await self.email.send_free_month_ending("a@example.test", ends, 2, renews=True, billed_by="paddle", manage_url="https://paddle.net")
        mail = self.last()
        self.assertEqual(mail["subject"], "Your free month of vournal Plus ends in 2 days")
        self.assertIn("Paddle", mail["text"])
        self.assertNotIn("Apple", mail["text"])
