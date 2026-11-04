"""Phase 1 review leftovers: to_json on hand-built models, a guard against FastAPI's
own response encoding, Express-style trailing slashes, and two session cases that
already worked but had no test (Plus lapsing mid-session, a reset ending web sessions)."""

import secrets
import unittest
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from vournal_os.support import OsTestCase


class Shape(BaseModel):
    name: str
    count: int = 0
    tags: list[str] = []


class Serialization(unittest.TestCase):
    def test_hand_built_models_keep_their_defaults(self):
        from app.os.common.serialize import to_json

        self.assertEqual(to_json(Shape(name="x")), {"name": "x", "count": 0, "tags": []})

    def test_database_rows_are_told_apart_by_prismas_base_class(self):
        """to_json relies on prisma's private _PrismaModel: an upgrade that renames it fails here."""
        from prisma import models
        from prisma.bases import _PrismaModel

        self.assertTrue(issubclass(models.Task, _PrismaModel))
        self.assertFalse(issubclass(Shape, _PrismaModel))

    def test_nest_routes_refuse_a_response_model(self):
        from app.os.common.http import nest_router

        router = nest_router("/x")
        with self.assertRaises(TypeError):

            @router.get("", response_model=Shape)
            async def handler():
                return {}


class Sessions(OsTestCase):
    async def test_a_trailing_slash_matches_like_express(self):
        client = await self.signed_in()
        plain, slashed = await client.get("/api/tasks"), await client.get("/api/tasks/")
        self.assertEqual((slashed.status_code, slashed.json()), (plain.status_code, plain.json()))
        made = await client.post("/api/tags/", json={"name": "slash"})
        self.assertEqual(made.status_code, 201, made.text)

    async def test_plus_lapsing_mid_session_stops_the_next_call(self):
        client = await self.signed_in()
        self.assertEqual((await client.get("/api/tasks")).status_code, 200)
        await self.db.user.update(where={"id": client.user.id}, data={"planExpiresAt": datetime.now(timezone.utc) - timedelta(minutes=1)})
        lapsed = await client.get("/api/tasks")
        self.assertEqual(lapsed.status_code, 403)
        self.assertEqual(lapsed.json()["code"], "plus_required")

    async def test_a_password_reset_ends_web_sessions(self):
        from app import security
        from app.main import app  # noqa: F401  (the root routes live on the same app)

        client = await self.signed_in()
        raw = secrets.token_urlsafe(32)
        await self.db.passwordresettoken.create(
            data={"userId": client.user.id, "tokenHash": security.hash_refresh_token(raw), "expiresAt": datetime.now(timezone.utc) + timedelta(minutes=30)}
        )
        reset = await client.post("/auth/reset-password", json={"token": raw, "newPassword": "a-brand-new-passphrase"})
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual((await client.get("/api/users/me")).status_code, 401)
        self.assertEqual((await client.post("/api/auth/refresh")).status_code, 401)


if __name__ == "__main__":
    unittest.main()
