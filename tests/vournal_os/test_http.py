"""/api answers like the NestJS backend the dashboard was built for; every other
route keeps answering like FastAPI did for the app."""

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

    async def test_api_health(self):
        from app.main import app

        async with _client(app) as client:
            response = await client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
