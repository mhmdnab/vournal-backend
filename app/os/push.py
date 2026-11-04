"""/api/push and sending web push (port of vournalOS PushService, VAPID). The keys come
from VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY / VAPID_SUBJECT in the environment, never the
repo; without them subscribing still works and nothing is sent.

Sending goes through pywebpush, which blocks, so each delivery runs in a worker thread."""

import asyncio
import json
import logging
import os
import re

from fastapi import Depends, Request
from pywebpush import WebPushException, webpush

from app.db import db
from app.os.common import validators
from app.os.common.body import RuleBody, nested, text
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.i18n import to_lang
from app.os.notification_texts import texts
from app.os.profile import ensure_os_profile
from app.rate_limit import rate_limited

log = logging.getLogger("vournal.os")
router = nest_router("/push")

_webpush = webpush  # replaced in tests
_TTL = 60 * 60 * 24


def _read_vapid() -> tuple[str, str, str] | None:
    public, private = os.environ.get("VAPID_PUBLIC_KEY", "").strip(), os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    subject = os.environ.get("VAPID_SUBJECT", "").strip() or "mailto:admin@life-os.app"
    if not re.match(r"^(mailto:|https:)", subject):
        subject = f"mailto:{subject}"
    if not public or not private:
        log.warning("VAPID keys not configured — web push disabled")
        return None
    return public, private, subject


_vapid = _read_vapid()


def is_enabled() -> bool:
    return _vapid is not None


def _deliver(sub, payload: dict) -> None:
    public, private, subject = _vapid
    data = {**payload, **({"url": payload["link"]} if payload.get("link") else {})}
    # raw UTF-8, as JSON.stringify sent it: escaped Arabic is 3x the bytes and long ones pass the 4 KB limit
    _webpush({"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}, json.dumps(data, ensure_ascii=False),
             vapid_private_key=private, vapid_claims={"sub": subject}, ttl=_TTL, headers={"Urgency": "normal"}, timeout=10)


async def send_to_user(user_id: str, payloads: list[dict]) -> int:
    """Every payload to every subscription of the user; expired ones (404 / 410) are
    deleted. Returns the deliveries that succeeded; never raises."""
    if _vapid is None or not payloads:
        return 0
    subs = await db.pushsubscription.find_many(where={"userId": user_id})
    sent, dead = 0, set()
    for sub in subs:
        for payload in payloads:
            if sub.id in dead:
                break
            try:
                await asyncio.to_thread(_deliver, sub, payload)
                sent += 1
            except WebPushException as exc:
                status = getattr(exc.response, "status_code", None)
                if status in (404, 410):
                    dead.add(sub.id)
                else:
                    log.warning("Push failed (%s): %s", status or "network", exc)
            except Exception as exc:  # noqa: BLE001 — a network error never stops a run
                log.warning("Push failed (network): %s", exc)
    if dead:
        await db.pushsubscription.delete_many(where={"id": {"in": list(dead)}})
    return sent


# ----- routes --------------------------------------------------------------------------


def _https_url(value, field):
    """@IsUrl({ protocols: ['https'], require_protocol: true, require_tld: false }) @MaxLength(1000)."""
    problems = []
    if not isinstance(value, str) or len(value) > 1000:
        problems.append(f"{field} must be shorter than or equal to 1000 characters")
    if not (isinstance(value, str) and re.match(r"^https://[^\s/?#]+(?:[/?#]\S*)?\Z", value, re.I)):
        problems.append(f"{field} must be a URL address")
    return problems


def _not_empty(maximum: int):
    def check(value, field):
        problems = [f"{field} must be shorter than or equal to {maximum} characters"] if not isinstance(value, str) or len(value) > maximum else []
        if value in ("", None):
            problems.append(f"{field} should not be empty")
        return problems + validators.string_rules(value, field)
    return check


class SubscribeBody(RuleBody):
    RULES = (
        ("endpoint", False, False, _https_url),
        ("keys", False, False, nested((("p256dh", False, False, _not_empty(200)), ("auth", False, False, _not_empty(100))))),
        ("userAgent", True, False, text(300)),  # ignored: the User-Agent header is used
    )
    endpoint: object = None
    keys: object = None
    userAgent: object = None


class UnsubscribeBody(RuleBody):
    RULES = (("endpoint", False, False, _not_empty(1000)),)
    endpoint: object = None


@router.get("/public-key")
async def public_key(user=Depends(plus_user)):
    """The VAPID public key (null when push isn't configured on the server)."""
    return {"publicKey": _vapid[0] if _vapid else None, "enabled": is_enabled()}


@router.get("/subscriptions")
async def subscriptions(user=Depends(plus_user)):
    rows = await db.pushsubscription.find_many(where={"userId": user.id}, order={"createdAt": "desc"})
    return {"items": [{"id": r.id, "userAgent": r.userAgent, "createdAt": r.createdAt} for r in rows], "total": len(rows), "enabled": is_enabled()}


@router.post("/subscribe")
async def subscribe(body: SubscribeBody, request: Request, user=Depends(plus_user)):
    """By endpoint: one browser profile; a browser that signs in as someone else moves to them."""
    if not isinstance(body.keys, dict):  # NestJS passed it and then failed with a 500
        raise nest_error(400, ["nested property keys must be either object or array"])
    agent = request.headers.get("user-agent")
    agent = agent[:300] if agent else None
    keys = {"p256dh": body.keys["p256dh"], "auth": body.keys["auth"]}
    row = await db.pushsubscription.upsert(
        where={"endpoint": body.endpoint},
        data={"create": {"userId": user.id, "endpoint": body.endpoint, **keys, "userAgent": agent},
              "update": {"userId": user.id, **keys, "userAgent": agent}},
    )
    return {"ok": True, "id": row.id}


@router.post("/unsubscribe")
async def unsubscribe(body: UnsubscribeBody, user=Depends(plus_user)):
    await db.pushsubscription.delete_many(where={"userId": user.id, "endpoint": body.endpoint})
    return {"ok": True}


@router.post("/test")
async def test(user=Depends(plus_user)):
    """A test notification to every one of the user's browsers (5 a minute)."""
    if rate_limited("push-test", user.id, 5, 60):
        raise nest_error(429, "ThrottlerException: Too Many Requests")
    T = texts(to_lang((await ensure_os_profile(user)).language))
    sent = await send_to_user(user.id, [{"title": "Vournal", "body": T.t("pushTest"), "link": "/dashboard", "tag": "push-test"}])
    return {"ok": True, "sent": sent, "enabled": is_enabled()}
