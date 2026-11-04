"""Attachments answer like NestJS did (NestJS kept the bytes in Postgres, Python in the
bucket): upload, list, download with its headers, delete, the limits."""

import unittest

from vournal_os.parity.harness import ParityCase

PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 200


class AttachmentsParity(ParityCase):
    async def tasks(self):
        nest, py = await self.both("POST", "/tasks", {"title": "Visa"})
        return nest.json()["id"], py.json()["id"]

    async def upload(self, ids, name="scan é (1).png", data=PNG, mime="image/png", fields=None):
        out = []
        for client, prefix, task in ((self.nest, "", ids[0]), (self.py, "/api", ids[1])):
            form = {"entityType": "TASK", "entityId": task, **(fields or {})}
            files = {"file": (name, data, mime)} if data is not None else None
            out.append(await client.post(f"{prefix}/attachments", data=form, files=files))
        return out

    async def test_round_trip(self):
        ids = await self.tasks()
        nest, py = await self.upload(ids)
        self.assertParity(nest, py)
        for query in (f"?entityType=TASK&entityId={ids[0]}", ):
            listed = (await self.nest.get(f"/attachments{query}"), await self.py.get(f"/api/attachments?entityType=TASK&entityId={ids[1]}"))
            self.assertParity(*listed)
        att = nest.json()["id"], py.json()["id"]
        for suffix in ("/download", "/download?inline=1"):
            got = await self.nest.get(f"/attachments/{att[0]}{suffix}"), await self.py.get(f"/api/attachments/{att[1]}{suffix}")
            self.assertEqual(got[0].content, got[1].content)
            for header in ("content-type", "content-disposition", "cache-control", "content-length"):
                self.assertEqual(got[0].headers.get(header), got[1].headers.get(header), header)
        text = await self.upload(ids, name="notes.csv", data=b"a,b\n1,2\n", mime="text/csv")
        self.assertParity(*text)
        got = await self.nest.get(f"/attachments/{text[0].json()['id']}/download"), await self.py.get(f"/api/attachments/{text[1].json()['id']}/download")
        self.assertEqual(got[0].headers["content-type"], got[1].headers["content-type"])
        self.assertParity(await self.nest.delete(f"/attachments/{att[0]}"), await self.py.delete(f"/api/attachments/{att[1]}"))
        self.assertParity(await self.nest.get(f"/attachments/{att[0]}/download"), await self.py.get(f"/api/attachments/{att[1]}/download"))

    async def test_limits_and_shapes(self):
        ids = await self.tasks()
        self.assertParity(*await self.upload(ids, data=b"x" * (10 * 1024 * 1024 + 1)))
        self.assertParity(*await self.upload(ids, name="x.exe", mime="application/x-msdownload"))
        self.assertParity(*await self.upload(ids, data=None))
        self.assertParity(*await self.upload(ids, fields={"entityType": "PLANET"}))
        self.assertParity(*await self.upload(ids, fields={"extra": "1"}))
        self.assertParity(*await self.upload(ids, name='a/b\\c:"d".png'))
        self.assertParity(*await self.upload(("cnosuchtaskxxxxxxxxxxxxxx", "cnosuchtaskxxxxxxxxxxxxxx")))
        for files in ({"photo": ("a.png", PNG, "image/png")}, [("file", ("a.png", PNG, "image/png")), ("file", ("b.png", PNG, "image/png"))],
                      {"file": ("", b"", "application/octet-stream")}):
            out = []
            for client, prefix, task in ((self.nest, "", ids[0]), (self.py, "/api", ids[1])):
                out.append(await client.post(f"{prefix}/attachments", data={"entityType": "TASK", "entityId": task}, files=files))
            self.assertParity(*out)
        broken = [await c.post(f"{p}/attachments", content=b"--x\r\n", headers={"content-type": "multipart/form-data"}) for c, p in ((self.nest, ""), (self.py, "/api"))]
        self.assertParity(*broken)
        for query in ("", "?entityType=TASK", "?entityType=TASK&entityId=cnosuchtaskxxxxxxxxxxxxxx", "?entityType=TASK&entityId=x&bogus=1"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/attachments{query}"))


if __name__ == "__main__":
    unittest.main()
