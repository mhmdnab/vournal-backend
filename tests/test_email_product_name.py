"""The web dashboard is 'vournal life' in every email people get (renamed 2026-11-10)."""

import unittest
from unittest.mock import patch


class ProductName(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_and_share_emails_say_vournal_life(self):
        from app import email

        sent = []

        async def capture(to, subject, html_body, text):
            sent.append((subject, html_body, text))
            return True

        with patch.object(email, "_send", capture):
            await email.send_workspace_invite("a@example.test", "Family", "Maya", "https://life.vournal.app/x")
            await email.send_invite_accepted("a@example.test", "Omar", "Family")
            await email.send_project_share("a@example.test", "Garden", "Maya", "https://life.vournal.app/y")
        self.assertEqual(len(sent), 3)
        for subject, html_body, text in sent:
            for part in (subject, html_body, text):
                self.assertNotIn("vournal OS", part)
            self.assertIn("vournal life", text)
