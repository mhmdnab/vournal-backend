"""Build 4 phones keep talking to the build 5 backend.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

import os
import unittest
import uuid
from unittest.mock import patch

import httpx

BUILD4_TODO_FIELDS = ("id", "text", "dueDate", "startAt", "done", "entryId")


class Build4Contract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import db

        self.db = db
        await db.connect()
        self.address = f"build4-{uuid.uuid4().hex}@example.test"

    async def asyncTearDown(self):
        await self.db.user.delete_many(where={"email": self.address})
        await self.db.disconnect()

    async def _client(self):
        from app.main import app

        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://build4"
        )

    async def _seed_todo(self, user_id: str):
        entry = await self.db.entry.create(
            data={
                "userId": user_id,
                "rawTranscript": "Call the dentist tomorrow.",
                "status": "DONE",
            }
        )
        return await self.db.todo.create(
            data={"entryId": entry.id, "userId": user_id, "text": "Call the dentist"}
        )

    async def test_build4_contract(self):
        async with await self._client() as client:
            registered = await client.post(
                "/auth/register",
                json={"email": self.address, "password": "local-passphrase"},
            )
            self.assertEqual(registered.status_code, 200, registered.text)
            body = registered.json()
            self.assertLessEqual({"accessToken", "refreshToken", "tokenType"}, set(body))
            self.assertNotIn("challengeToken", body)

            logged_in = await client.post(
                "/auth/login",
                json={"email": self.address, "password": "local-passphrase"},
            )
            self.assertEqual(logged_in.status_code, 200, logged_in.text)
            self.assertNotIn("challengeToken", logged_in.json())

            refreshed = await client.post(
                "/auth/refresh", json={"refreshToken": logged_in.json()["refreshToken"]}
            )
            self.assertEqual(refreshed.status_code, 200, refreshed.text)
            self.assertIn("accessToken", refreshed.json())

            user = await self.db.user.find_unique(where={"email": self.address})
            await self._seed_todo(user.id)
            auth = {"Authorization": f"Bearer {refreshed.json()['accessToken']}"}
            todos = await client.get("/todos", headers=auth)
            self.assertEqual(todos.status_code, 200, todos.text)
            todo = todos.json()[0]
            for key in BUILD4_TODO_FIELDS:
                self.assertIn(key, todo)
            self.assertIn("urgency", todo)
            self.assertIn("followUps", todo)
            self.assertNotIn("calendarRequested", todo)
            self.assertNotIn("remindersRequested", todo)

    async def test_details_endpoint_without_flag(self):
        from app import security

        user = await self.db.user.create(
            data={
                "email": self.address,
                "passwordHash": security.hash_password("local-passphrase"),
            }
        )
        todo = await self._seed_todo(user.id)
        auth = {"Authorization": f"Bearer {security.create_access_token(user.id)}"}
        async with await self._client() as client:
            saved = await client.patch(
                f"/todos/{todo.id}/details", json={"urgency": 2}, headers=auth
            )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["urgency"], 2)


if __name__ == "__main__":
    unittest.main()
