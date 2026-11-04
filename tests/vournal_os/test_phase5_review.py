"""Fixes from the phase 5 review: JavaScript's two-digit years (Date.UTC reads 0–99 as
1900–1999), null versus omitted fields, multer's multipart answers, the 413 shape, and
the real R2 helpers (through botocore's Stubber, no network)."""

import time
import unittest
from datetime import datetime, timezone

from vournal_os.support import OsTestCase

PNG = b"\x89PNG\r\n\x1a\n" + b"\x03" * 32


class TwoDigitYears(unittest.TestCase):
    def test_date_utc_reads_years_0_to_99_as_1900s(self):
        from app.os.common import dates, jsparse

        self.assertEqual(dates.parse_date_only("0001-01-01"), datetime(1901, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(dates.parse_date_only("0099-03-02"), datetime(1999, 3, 2, tzinfo=timezone.utc))
        self.assertEqual(dates.parse_date_only("0100-01-01"), datetime(100, 1, 1, tzinfo=timezone.utc))
        # new Date("0050-01-01") parses a string: no two-digit rule there
        self.assertEqual(jsparse.js_date("0050-01-01"), datetime(50, 1, 1, tzinfo=timezone.utc))


class Review(OsTestCase):
    async def test_an_ancient_check_in_stays_cheap(self):
        client = await self.signed_in()
        habit = (await client.post("/api/habits", json={"name": "Old"})).json()
        made = await client.post(f"/api/habits/{habit['id']}/check", json={"date": "0001-01-01"})
        self.assertEqual(made.status_code, 200, made.text)
        log = await self.db.habitlog.find_first(where={"habitId": habit["id"]})
        self.assertEqual(log.date.year, 1901)  # as NestJS's Date.UTC stored it
        started = time.monotonic()
        await client.get("/api/habits")
        self.assertLess(time.monotonic() - started, 1.0)

    async def test_null_is_not_omitted(self):
        client = await self.signed_in()
        habit = (await client.post("/api/habits", json={"name": "Water"})).json()
        await client.post(f"/api/habits/{habit['id']}/check", json={})
        cleared = (await client.post(f"/api/habits/{habit['id']}/check", json={"count": None})).json()
        self.assertEqual(cleared["todayCount"], 0)  # null <= 0: the day's log goes
        goal = (await client.post("/api/goals", json={"name": "Run"})).json()
        ms = (await client.post(f"/api/goals/{goal['id']}/milestones", json={"title": "5k"})).json()
        await client.post(f"/api/goals/{goal['id']}/milestones/{ms['id']}/toggle", json={})
        reopened = (await client.patch(f"/api/goals/{goal['id']}/milestones/{ms['id']}", json={"completed": None})).json()
        self.assertFalse(reopened["completed"])
        note = (await client.post("/api/notes", json={"title": "x", "isArchived": True})).json()
        await client.patch(f"/api/notes/{note['id']}", json={"isArchived": None})
        last = await self.db.activitylog.find_first(where={"userId": client.user.id, "entityId": note["id"]}, order={"createdAt": "desc"})
        self.assertEqual(last.action, "note.unarchived")

    async def test_multipart_answers_like_multer(self):
        client = await self.signed_in()
        task = (await client.post("/api/tasks", json={"title": "t"})).json()["id"]
        target = {"entityType": "TASK", "entityId": task}
        wrong = await client.post("/api/attachments", data=target, files={"photo": ("a.png", PNG, "image/png")})
        self.assertEqual(wrong.json()["message"], "Unexpected file field - photo")
        two = await client.post("/api/attachments", data=target, files=[("file", ("a.png", PNG, "image/png")), ("file", ("b.png", PNG, "image/png"))])
        self.assertEqual((two.status_code, two.json()["message"]), (400, "Too many files"))
        empty = await client.post("/api/attachments", data=target, files={"file": ("", b"", "application/octet-stream")})
        self.assertEqual(empty.json()["message"], "File is required")
        broken = await client.post("/api/attachments", content=b"--x\r\n", headers={"content-type": "multipart/form-data"})
        self.assertEqual((broken.status_code, broken.json()["message"]), (400, "Multipart: Boundary not found"))

    async def test_the_size_cap_answers_in_nest_shape_under_api(self):
        client = await self.signed_in()
        response = await client.post("/api/attachments", content=b"x", headers={"content-type": "application/json", "content-length": str(31 * 1024 * 1024)})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"statusCode": 413, "message": "File too large", "error": "Payload Too Large"})


class RealStorage(unittest.IsolatedAsyncioTestCase):
    async def test_object_helpers_against_a_stubbed_r2(self):
        from unittest.mock import patch

        import boto3
        from botocore.response import StreamingBody
        from botocore.stub import Stubber
        import io

        from app import storage

        client = boto3.client("s3", region_name="auto", aws_access_key_id="x", aws_secret_access_key="y", endpoint_url="https://r2.invalid")
        stub = Stubber(client)
        stub.add_response("put_object", {}, {"Bucket": "b", "Key": "os/attachments/u/1", "Body": b"hi", "ContentType": "image/png", "CacheControl": "private, no-store"})
        stub.add_response("get_object", {"Body": StreamingBody(io.BytesIO(b"hi"), 2)}, {"Bucket": "b", "Key": "os/attachments/u/1"})
        stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404, expected_params={"Bucket": "b", "Key": "os/attachments/u/2"})
        stub.add_response("delete_objects", {"Deleted": [{"Key": "os/attachments/u/1"}]}, {"Bucket": "b", "Delete": {"Objects": [{"Key": "os/attachments/u/1"}], "Quiet": True}})
        with stub, patch.object(storage, "_client_and_bucket", return_value=(client, "b")):
            await storage.put_object("os/attachments/u/1", b"hi", "image/png")
            self.assertEqual(await storage.get_object("os/attachments/u/1"), b"hi")
            self.assertIsNone(await storage.get_object("os/attachments/u/2"))
            await storage.delete_objects(["os/attachments/u/1"])
        stub.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
