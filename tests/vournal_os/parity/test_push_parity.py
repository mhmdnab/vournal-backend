"""Web push routes answer like NestJS did, with push not configured on either side:
the key, subscribing (and the same browser moving to another account), listing,
unsubscribing, the test push, and validation word for word."""

import unittest

from vournal_os.parity.harness import ParityCase

SUB = {"endpoint": "https://push.example/send/abc", "keys": {"p256dh": "BPkey", "auth": "authkey"}}


class PushParity(ParityCase):
    maxDiff = None

    async def check(self, method, path, body=None):
        nest, py = await self.both(method, path, body)
        self.assertParity(nest, py)
        if nest.status_code == 400:
            self.assertEqual(nest.json()["message"], py.json()["message"])
        return nest, py

    async def test_routes(self):
        await self.check("GET", "/push/public-key")
        await self.check("GET", "/push/subscriptions")
        await self.check("POST", "/push/subscribe", SUB)
        await self.check("POST", "/push/subscribe", {**SUB, "keys": {"p256dh": "new", "auth": "new"}, "userAgent": "ignored"})
        await self.check("GET", "/push/subscriptions")
        await self.check("POST", "/push/test")
        await self.check("POST", "/push/unsubscribe", {"endpoint": SUB["endpoint"]})
        await self.check("POST", "/push/unsubscribe", {"endpoint": "https://push.example/never"})
        await self.check("GET", "/push/subscriptions")

    async def test_validation(self):
        for body in ({}, {**SUB, "endpoint": "http://push.example/x"}, {**SUB, "endpoint": "push.example"}, {**SUB, "keys": {"p256dh": "", "auth": "a"}},
                     {**SUB, "keys": {"p256dh": "k"}}, {**SUB, "keys": "k"}, {**SUB, "bogus": 1}, {**SUB, "endpoint": "https://localhost:8443/push"}):
            with self.subTest(body=body):
                await self.check("POST", "/push/subscribe", body)
        for body in ({}, {"endpoint": ""}, {"endpoint": 5}):
            with self.subTest(unsubscribe=body):
                await self.check("POST", "/push/unsubscribe", body)


if __name__ == "__main__":
    unittest.main()
