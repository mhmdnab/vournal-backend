"""Expo's push API (design 2026-11-18): batches of 100 to send, 1000 for receipts, one ticket per
message in order, and a failed request never raises. No network: httpx.MockTransport."""

import json
import os
import unittest
from unittest.mock import patch

import httpx

from app import push_expo


def msg(i):
    return {"to": f"ExponentPushToken[{i}]", "title": "t", "body": "b"}


class PushExpo(unittest.IsolatedAsyncioTestCase):
    def use(self, handler):
        self.requests = []

        def record(request):
            self.requests.append(request)
            return handler(request)

        p = patch.object(push_expo, "_transport", httpx.MockTransport(record))
        p.start()
        self.addCleanup(p.stop)

    async def test_batches_of_100(self):
        def ok(request):
            batch = json.loads(request.content)
            return httpx.Response(200, json={"data": [{"status": "ok", "id": f"r-{m['to']}"} for m in batch]})

        self.use(ok)
        tickets = await push_expo.send([msg(i) for i in range(230)])
        self.assertEqual([len(json.loads(r.content)) for r in self.requests], [100, 100, 30])
        self.assertEqual(str(self.requests[0].url), push_expo.SEND_URL)
        self.assertEqual(len(tickets), 230)
        self.assertEqual(tickets[229], {"status": "ok", "id": "r-ExponentPushToken[229]"})

    async def test_device_not_registered_ticket(self):
        self.use(lambda r: httpx.Response(200, json={"data": [
            {"status": "error", "message": "gone", "details": {"error": "DeviceNotRegistered"}}]}))
        self.assertEqual(await push_expo.send([msg(1)]), [{"status": "error", "error": "DeviceNotRegistered"}])

    async def test_http_500_gives_error_tickets_and_no_raise(self):
        self.use(lambda r: httpx.Response(500, json={"errors": [{"code": "INTERNAL"}]}))
        self.assertEqual(await push_expo.send([msg(1), msg(2)]), [{"status": "error", "error": None}] * 2)

    async def test_network_error_gives_error_tickets(self):
        def boom(request):
            raise httpx.ConnectError("down")

        self.use(boom)
        self.assertEqual(await push_expo.send([msg(1)]), [{"status": "error", "error": None}])

    async def test_a_reply_that_never_comes_is_not_called_unsent(self):
        """Expo may have taken it (final review 2026-11-18): the job mustn't send it again."""
        def slow(request):
            raise httpx.ReadTimeout("slow")

        self.use(slow)
        self.assertEqual(await push_expo.send([msg(1)]), [{"status": "error", "error": "NoAnswer"}])
        self.use(lambda r: httpx.Response(200, text="not json"))
        self.assertEqual(await push_expo.send([msg(1)]), [{"status": "error", "error": "NoAnswer"}])

    async def test_empty_sends_nothing(self):
        self.use(lambda r: httpx.Response(200, json={"data": []}))
        self.assertEqual(await push_expo.send([]), [])
        self.assertEqual(self.requests, [])

    async def test_receipts_batches_of_1000_and_omits_missing(self):
        def receipts(request):
            ids = json.loads(request.content)["ids"]
            data = {i: {"status": "ok"} for i in ids if i != "r-5"}
            data["r-7"] = {"status": "error", "details": {"error": "DeviceNotRegistered"}}
            return httpx.Response(200, json={"data": data})

        self.use(receipts)
        out = await push_expo.receipts([f"r-{i}" for i in range(1500)])
        self.assertEqual([len(json.loads(r.content)["ids"]) for r in self.requests], [1000, 500])
        self.assertEqual(str(self.requests[0].url), push_expo.RECEIPTS_URL)
        self.assertNotIn("r-5", out)
        self.assertEqual(out["r-7"], {"status": "error", "error": "DeviceNotRegistered"})
        self.assertEqual(out["r-1"], {"status": "ok", "error": None})

    async def test_receipts_failure_is_none(self):
        self.use(lambda r: httpx.Response(503))
        self.assertIsNone(await push_expo.receipts(["a"]))

    async def test_receipts_none_ready_is_empty(self):
        self.use(lambda r: httpx.Response(200, json={"data": {}}))
        self.assertEqual(await push_expo.receipts(["a"]), {})

    async def test_access_token_header_only_when_set(self):
        self.use(lambda r: httpx.Response(200, json={"data": [{"status": "ok", "id": "x"}]}))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EXPO_ACCESS_TOKEN", None)
            await push_expo.send([msg(1)])
        with patch.dict(os.environ, {"EXPO_ACCESS_TOKEN": "tok"}):
            await push_expo.send([msg(1)])
        self.assertNotIn("authorization", self.requests[0].headers)
        self.assertEqual(self.requests[1].headers["authorization"], "Bearer tok")
        self.assertEqual(self.requests[1].headers["content-type"], "application/json")


if __name__ == "__main__":
    unittest.main()
