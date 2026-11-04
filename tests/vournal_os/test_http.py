"""/api answers like the NestJS backend the dashboard was built for; every other
route keeps answering like FastAPI did for the app."""

import math
import unittest
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, ConfigDict


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://life.test")


class ApiHttp(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_api_route_is_nest_404(self):
        from app.main import app

        async with _client(app) as client:
            response = await client.get("/api/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json(),
            {"statusCode": 404, "message": "Cannot GET /api/nope", "error": "Not Found"},
        )

    async def test_api_validation_is_nest_400(self):
        from app.os.common.http import NestJSONResponse, install_nest_errors

        router = APIRouter(prefix="/api/_test", default_response_class=NestJSONResponse)

        @router.post("/things")
        async def make(body: _Body):
            return {"title": body.title}

        app = FastAPI()
        install_nest_errors(app)
        app.include_router(router)
        async with _client(app) as client:
            response = await client.post("/api/_test/things", json={"bogus": 1})
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["statusCode"], 400)
        self.assertEqual(body["error"], "Bad Request")
        self.assertIsInstance(body["message"], list)
        self.assertTrue(body["message"])
        self.assertTrue(all(isinstance(m, str) for m in body["message"]))

    async def test_root_errors_unchanged(self):
        from app.main import app

        async with _client(app) as client:
            invalid = await client.post("/auth/login", json={})
            unauthenticated = await client.get("/auth/me")
        self.assertEqual(invalid.status_code, 422)
        self.assertIn("detail", invalid.json())
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(unauthenticated.json(), {"detail": "Not authenticated"})

    async def test_route_answers_use_nest_json(self):
        # FastAPI encodes a route's return value itself; /api routes must still
        # answer in NestJS's format, not FastAPI's ("+00:00", floats for Decimal).
        from app.os.common.http import NestJSONResponse, NestRoute, install_nest_errors

        router = APIRouter(prefix="/api/_test", default_response_class=NestJSONResponse, route_class=NestRoute)

        @router.get("/when")
        async def when():
            return {"t": datetime(2026, 11, 3, 10, 0, 0, 57000, tzinfo=timezone.utc), "n": Decimal("12.00")}

        app = FastAPI()
        install_nest_errors(app)
        app.include_router(router)
        async with _client(app) as client:
            response = await client.get("/api/_test/when")
        self.assertEqual(response.json(), {"t": "2026-11-03T10:00:00.057Z", "n": 12})

    def test_json_matches_nest(self):
        from app.os.common.serialize import to_json

        self.assertEqual(
            to_json(
                {
                    "a": Decimal("12.00"),
                    "b": Decimal("12.50"),
                    "t": datetime(2026, 11, 3, 10, 0, 0, 123456),
                    "d": datetime(2026, 11, 3, tzinfo=timezone.utc),
                }
            ),
            {
                "a": 12,
                "b": 12.5,
                "t": "2026-11-03T10:00:00.123Z",
                "d": "2026-11-03T00:00:00.000Z",
            },
        )
        self.assertIsInstance(to_json(Decimal("12.00")), int)

    def test_whole_floats_print_like_javascript_numbers(self):
        """JS has one number type: JSON.stringify(12.0) is "12", and -0 prints as 0."""
        import json

        from app.os.common.serialize import to_json

        self.assertEqual(json.dumps(to_json({"a": 12.0, "b": 12.5, "c": -0.0, "d": [700.0], "e": True})), '{"a": 12, "b": 12.5, "c": 0, "d": [700], "e": true}')
        self.assertIsInstance(to_json(1e20), int)
        self.assertIsInstance(to_json(1e21), float)  # JS switches to exponent form here
        self.assertTrue(math.isnan(to_json(math.nan)))

    async def test_api_health_checks_the_database(self):
        """NestJS's liveness check: 503 {status, db} when the database doesn't answer."""
        from app.db import db
        from app.main import app

        async with _client(app) as client:
            down = await client.get("/api/health")
            await db.connect()
            try:
                up = await client.get("/api/health")
            finally:
                await db.disconnect()
        self.assertEqual((down.status_code, down.json()), (503, {"status": "error", "db": "down"}))
        body = up.json()
        self.assertEqual((up.status_code, body["status"], body["db"]), (200, "ok", "up"))
        self.assertEqual(sorted(body), ["db", "dbLatencyMs", "status", "time", "uptimeSeconds"])
        self.assertRegex(body["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertIsInstance(body["uptimeSeconds"], int)


if __name__ == "__main__":
    unittest.main()
