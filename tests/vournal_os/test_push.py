"""Web push: subscriptions per browser, delivery to every one of them, expired ones (404 /
410) removed, other failures logged without stopping, and nothing sent while VAPID isn't set."""

import json
import unittest
from unittest.mock import patch

from vournal_os.support import OsTestCase

SUB = {"endpoint": "https://push.example/sub/1", "keys": {"p256dh": "BPkey", "auth": "authkey"}}


class FakeResponse:
    def __init__(self, status):
        self.status_code = status
        self.text = "gone"


class Push(OsTestCase):
    async def test_routes_without_vapid(self):
        client = await self.signed_in()
        self.assertEqual((await client.get("/api/push/public-key")).json(), {"publicKey": None, "enabled": False})
        subscribed = await client.post("/api/push/subscribe", json=SUB, headers={"User-Agent": "Firefox"})
        self.assertEqual((subscribed.status_code, subscribed.json()["ok"]), (200, True))
        listed = (await client.get("/api/push/subscriptions")).json()
        self.assertEqual(([s["userAgent"] for s in listed["items"]], listed["enabled"]), (["Firefox"], False))
        self.assertEqual((await client.post("/api/push/test")).json(), {"ok": True, "sent": 0, "enabled": False})
        # the same browser signing in as someone else moves the subscription to them
        other = await self.signed_in()
        await other.post("/api/push/subscribe", json=SUB)
        self.assertEqual((await client.get("/api/push/subscriptions")).json()["total"], 0)
        self.assertEqual((await other.post("/api/push/unsubscribe", json={"endpoint": SUB["endpoint"]})).json(), {"ok": True})
        self.assertEqual(await self.db.pushsubscription.count(where={"endpoint": SUB["endpoint"]}), 0)
        for body in ({}, {"endpoint": SUB["endpoint"]}, {**SUB, "endpoint": "http://push.example/x"}, {**SUB, "keys": {"p256dh": ""}}, {**SUB, "bogus": 1}):
            self.assertEqual((await client.post("/api/push/subscribe", json=body)).status_code, 400, body)

    async def test_delivery_and_dead_subscriptions(self):
        from pywebpush import WebPushException

        from app.os import push

        user = await self.make_user()
        for i in range(3):
            await self.db.pushsubscription.create(data={"userId": user.id, "endpoint": f"https://push.example/{i}", "p256dh": "k", "auth": "a"})
        sent = []

        def fake_send(subscription, data, **kwargs):
            if subscription["endpoint"].endswith("/1"):
                raise WebPushException("gone", response=FakeResponse(410))
            if subscription["endpoint"].endswith("/2"):
                raise WebPushException("busy", response=FakeResponse(500))
            sent.append((subscription["endpoint"], json.loads(data), kwargs["ttl"]))

        payloads = [{"title": "Rent", "body": "due", "link": "/finance", "tag": "n1", "id": "n1"}, {"title": "Gym", "body": None, "link": None, "tag": "n2"}]
        with patch.object(push, "_vapid", ("PUB", "PRIV", "mailto:ops@vournal.app")), patch.object(push, "_webpush", fake_send):
            self.assertEqual(await push.send_to_user(user.id, payloads), 2)
        self.assertEqual([s[0] for s in sent], ["https://push.example/0", "https://push.example/0"])
        self.assertEqual(sent[0][1], {"title": "Rent", "body": "due", "link": "/finance", "tag": "n1", "id": "n1", "url": "/finance"})
        self.assertNotIn("url", sent[1][1])
        self.assertEqual(sent[0][2], 86400)
        left = sorted(s.endpoint for s in await self.db.pushsubscription.find_many(where={"userId": user.id}))
        self.assertEqual(left, ["https://push.example/0", "https://push.example/2"])  # the 410 one is gone, the 500 one kept
        with patch.object(push, "_vapid", None):
            self.assertEqual(await push.send_to_user(user.id, payloads), 0)


if __name__ == "__main__":
    unittest.main()
