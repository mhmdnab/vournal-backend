"""Side-by-side checks of the Python /api against the NestJS reference.

Each ParityCase signs in one user on each backend, sends both the same request
and compares the answers once ids and "now" timestamps are masked. Both run at
the same moment, so date-dependent answers ("today", "overdue") stay comparable.

Run only through the guarded runner, with the reference started first:
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py os-reference
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py os-parity
"""

import os
import time
import re
import secrets
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

CSRF = {"X-Requested-With": "life-os"}
PARITY_PASSWORD = "parity-passphrase-1"
PYTHON_BASE = "https://life.test"

_CUID = re.compile(r"^c[a-z0-9]{20,}$")
_CUID_INSIDE = re.compile(r"(?<![a-z0-9])c[a-z0-9]{20,}(?![a-z0-9])")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
_NOW_WINDOW = timedelta(minutes=10)


def _now_label(value: str) -> str | None:
    """"<now>" for a timestamp from the last few minutes, "<now+Nd>" for one a whole
    number of days from now (a series anchored at "now", e.g. income received today:
    the two backends' clocks are milliseconds apart, so its next date is too)."""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = moment - datetime.now(timezone.utc)
    days = round(delta / timedelta(days=1))
    if abs(days) > 400 or abs(delta - timedelta(days=days)) > _NOW_WINDOW:
        return None
    return "<now>" if days == 0 else f"<now{days:+d}d>"


def _is_now(value: str) -> bool:
    return _now_label(value) == "<now>"


def normalize(body: Any) -> Any:
    """Mask what legitimately differs between the two backends: each distinct id
    becomes <id:N> (numbered by first appearance, so references still line up),
    each timestamp from the last few minutes becomes <now>, and a 400's
    validation messages (worded by class-validator vs Pydantic) become one marker."""
    ids: dict[str, str] = {}

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if node.get("statusCode") == 400 and isinstance(node.get("message"), list):
                node = {**node, "message": "<validation>"}
            return {key: walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(value) for value in node]
        if isinstance(node, str):
            if _CUID.match(node):
                return ids.setdefault(node, f"<id:{len(ids) + 1}>")
            if _ISO.match(node) and (label := _now_label(node)):
                return label
            # ids inside longer text, e.g. a link "/tasks?id=<id>"
            return _CUID_INSIDE.sub(lambda m: ids.setdefault(m[0], f"<id:{len(ids) + 1}>"), node)
        return node

    return walk(body)


def _without(node: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(node, dict):
        return {k: _without(v, keys) for k, v in node.items() if k not in keys}
    if isinstance(node, list):
        return [_without(v, keys) for v in node]
    return node


_REFERENCE_DB = "vournal_os_reference"


async def copy_reference_market_rates() -> None:
    """Give the local database the reference's live market rates (both local), so
    rate answers are computed from the same numbers on both sides."""
    import subprocess
    from urllib.parse import urlparse

    local = urlparse(os.environ["DATABASE_URL"])
    if local.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError("Refusing to copy market rates into a non-local database.")
    target = ["psql", "-h", local.hostname, "-p", str(local.port or 5432), local.path.lstrip("/"), "-q"]
    source = ["psql", "-h", local.hostname, "-p", str(local.port or 5432), _REFERENCE_DB, "-q"]
    rows = subprocess.run(
        [*source, "-c", r'\copy "MarketRate" TO STDOUT'], capture_output=True, text=True, check=True
    ).stdout
    subprocess.run(
        [*target, "-c", 'DELETE FROM "MarketRate"', "-c", r'\copy "MarketRate" FROM STDIN'],
        input=rows,
        capture_output=True,
        text=True,
        check=True,
    )
    from app.os import market_rates

    market_rates.reset_for_tests()
    # no live refresh on the Python side either (the reference runs offline): a refresh
    # attempt counts as just made, so stale stored rates stay what both sides answer with
    market_rates._last_attempt = time.monotonic()


def reference_sql(sql: str) -> str:
    """Run SQL on the reference's own local database (rows the API can't make, such
    as notifications, are put in place on both sides this way)."""
    import subprocess
    from urllib.parse import urlparse

    local = urlparse(os.environ["DATABASE_URL"])
    if local.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError("Refusing to touch a non-local database.")
    command = ["psql", "-h", local.hostname, "-p", str(local.port or 5432), _REFERENCE_DB, "-v", "ON_ERROR_STOP=1", "-Atq", "-c", sql]
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()


async def python_client(user, password: str = PARITY_PASSWORD) -> httpx.AsyncClient:
    """An in-process client on the Python app, signed in as `user` through the
    web's own sign-in route (cookies, like the browser)."""
    from app.main import app

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=PYTHON_BASE, headers=CSRF
    )
    response = await client.post("/api/auth/login", json={"email": user.email, "password": password})
    if response.status_code != 200:
        await client.aclose()
        raise AssertionError(f"Python sign-in failed: {response.status_code} {response.text}")
    return client


@unittest.skipUnless(
    os.environ.get("NEST_REFERENCE_URL"), "NEST_REFERENCE_URL not set — use isolated_backend.py os-parity"
)
class ParityCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app import security
        from app.db import db
        from app.rate_limit import limiter

        from vournal_os.support import fake_bucket

        self.db = db
        self.objects = fake_bucket(self)  # Python's attachments; NestJS keeps its own in Postgres
        limiter.reset()
        await db.connect()  # each test has its own event loop
        # Cleanups, not asyncTearDown: they still run when setup fails halfway.
        self.addAsyncCleanup(db.disconnect)
        self.email = f"parity-{uuid.uuid4().hex}@example.test"

        # NestJS throttles sign-ups per address (5 a minute) and trusts one proxy
        # hop, so each test gets its own made-up local address on the reference.
        address = f"10.{secrets.randbelow(256)}.{secrets.randbelow(256)}.{secrets.randbelow(254) + 1}"
        self.nest = httpx.AsyncClient(
            base_url=os.environ["NEST_REFERENCE_URL"],
            headers={**CSRF, "X-Forwarded-For": address},
            # Node closes an idle connection after 5 s, httpx's own default: drop ours
            # first, or a request can go out on a socket Node is closing (ReadError)
            limits=httpx.Limits(keepalive_expiry=1.0),
        )
        self.addAsyncCleanup(self.nest.aclose)
        registered = await self.nest.post(
            "/auth/register",
            json={
                "email": self.email,
                "password": PARITY_PASSWORD,
                "name": "Parity",
                "timezone": "UTC",
                "defaultCurrency": "USD",
                "language": "en",
            },
        )
        if registered.status_code not in (200, 201):
            raise AssertionError(f"NestJS register failed: {registered.status_code} {registered.text}")

        self.addAsyncCleanup(db.user.delete_many, where={"email": self.email})
        self.user = await db.user.create(
            data={
                "email": self.email,
                "passwordHash": security.hash_password(PARITY_PASSWORD),
                "plan": "plus",
            }
        )
        self.nest_user_id = (await self.nest.get("/auth/me")).json()["user"]["id"]
        self.py = await python_client(self.user)
        self.addAsyncCleanup(self.py.aclose)
        named = await self.py.patch("/api/users/me", json={"name": "Parity"})
        if named.status_code != 200:
            raise AssertionError(f"Python name update failed: {named.status_code} {named.text}")

    async def other_user(self, name: str) -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
        """A second Plus user, signed in on both sides (households, shared lists)."""
        from app import security

        email = f"parity-{uuid.uuid4().hex}@example.test"
        address = f"10.{secrets.randbelow(256)}.{secrets.randbelow(256)}.{secrets.randbelow(254) + 1}"
        nest = httpx.AsyncClient(
            base_url=os.environ["NEST_REFERENCE_URL"], headers={**CSRF, "X-Forwarded-For": address},
            limits=httpx.Limits(keepalive_expiry=1.0),
        )
        self.addAsyncCleanup(nest.aclose)
        registered = await nest.post("/auth/register", json={
            "email": email, "password": PARITY_PASSWORD, "name": name, "timezone": "UTC", "defaultCurrency": "USD", "language": "en",
        })
        if registered.status_code not in (200, 201):
            raise AssertionError(f"NestJS register failed: {registered.status_code} {registered.text}")
        self.addAsyncCleanup(self.db.user.delete_many, where={"email": email})
        user = await self.db.user.create(
            data={"email": email, "passwordHash": security.hash_password(PARITY_PASSWORD), "plan": "plus"}
        )
        py = await python_client(user)
        self.addAsyncCleanup(py.aclose)
        await py.patch("/api/users/me", json={"name": name})
        return nest, py

    async def both(self, method: str, path: str, json: Any = None) -> tuple[httpx.Response, httpx.Response]:
        """The same request to NestJS (`path` under its /api base) and to Python."""
        kwargs = {} if json is None else {"json": json}
        nest = await self.nest.request(method, path, **kwargs)
        py = await self.py.request(method, f"/api{path}", **kwargs)
        return nest, py

    def assertParity(self, nest: httpx.Response, py: httpx.Response, *, ignore_keys: tuple[str, ...] = ()):
        self.assertEqual(nest.status_code, py.status_code, f"NestJS {nest.text}\nPython {py.text}")
        expected = normalize(_without(nest.json(), ignore_keys)) if nest.content else None
        actual = normalize(_without(py.json(), ignore_keys)) if py.content else None
        self.assertEqual(expected, actual)
