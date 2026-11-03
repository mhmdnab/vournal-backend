"""Notifications answer like NestJS did, from identical rows on both sides."""

import unittest

from vournal_os.parity.harness import ParityCase, reference_sql

ROWS = [
    ("n1", "Pay rent", "Due today", "/tasks?id=x", "TASK", "2026-11-01 08:00:00", None),
    ("n2", "Call mum", None, None, None, "2026-11-02 08:00:00", "2026-11-02 09:00:00"),
    ("n3", "Dentist", "Tomorrow 09:00", "/calendar?id=y", "EVENT", "2026-11-03 08:00:00", None),
    ("n4", "Old", None, None, None, "2026-10-01 08:00:00", "2026-10-02 08:00:00"),
]


class NotificationsParity(ParityCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        values = []
        for key, title, body, link, kind, created, read in ROWS:
            literal = lambda v: "NULL" if v is None else "'" + v.replace("'", "''") + "'"  # noqa: E731
            values.append(
                f"('c' || md5(random()::text), '{self.nest_user_id}', {literal(title)}, {literal(body)}, {literal(link)}, "
                f'{literal(kind)}::"EntityType", \'{key}\', {literal(read)}, {literal(created)})'
            )
        reference_sql(
            'INSERT INTO "Notification" (id, "userId", title, body, link, "entityType", "dedupeKey", "readAt", "createdAt") VALUES '
            + ", ".join(values)
        )
        for key, title, body, link, kind, created, read in ROWS:
            await self.db.notification.create(
                data={
                    "userId": self.user.id, "title": title, "body": body, "link": link, "entityType": kind,
                    "dedupeKey": key, "readAt": f"{read}Z".replace(" ", "T") if read else None,
                    "createdAt": f"{created}Z".replace(" ", "T"),
                }
            )

    async def test_list_and_counts(self):
        for query in ("?noSync=true", "?noSync=true&unread=true", "?noSync=true&pageSize=2&page=2", "?noSync=maybe"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/notifications{query}"))
        self.assertParity(*await self.both("GET", "/notifications/unread-count"))

    async def test_read_unread_all(self):
        items = {
            "nest": (await self.nest.get("/notifications?noSync=true")).json()["items"],
            "py": (await self.py.get("/api/notifications?noSync=true")).json()["items"],
        }
        first = (items["nest"][0]["id"], items["py"][0]["id"])
        for suffix in ("/read", "/read", "/unread"):
            self.assertParity(
                await self.nest.post(f"/notifications/{first[0]}{suffix}"),
                await self.py.post(f"/api/notifications/{first[1]}{suffix}"),
                ignore_keys=("readAt",),
            )
        self.assertParity(*await self.both("POST", "/notifications/read-all"))
        self.assertParity(*await self.both("GET", "/notifications/unread-count"))

    async def test_clear_read_and_delete(self):
        self.assertParity(*await self.both("DELETE", "/notifications"))
        items = {
            "nest": (await self.nest.get("/notifications?noSync=true")).json()["items"],
            "py": (await self.py.get("/api/notifications?noSync=true")).json()["items"],
        }
        target = (items["nest"][0]["id"], items["py"][0]["id"])
        for _ in range(2):
            self.assertParity(
                await self.nest.delete(f"/notifications/{target[0]}"), await self.py.delete(f"/api/notifications/{target[1]}")
            )
        self.assertParity(*await self.both("GET", "/notifications?noSync=true"))

    async def test_validation_shapes(self):
        for query in ("?unread=perhaps", "?page=0", "?pageSize=201"):
            self.assertParity(*await self.both("GET", f"/notifications{query}"))


if __name__ == "__main__":
    unittest.main()
