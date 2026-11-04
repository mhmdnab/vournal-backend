"""The app's person merge (POST /threads/people/{id}/merge, called by build 8) keeps the
vournal OS data: the person kept wins, its blanks are filled from the merged one, and
dates, gift ideas, debts, notes, reminders and notifications move across. The answer the
app reads is unchanged.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

import unittest
import uuid
from datetime import datetime, timezone

import httpx


class PersonMerge(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app import security
        from app.db import db
        from app.main import app

        self.db = db
        await db.connect()
        self.addAsyncCleanup(db.disconnect)
        self.user = await db.user.create(data={"email": f"merge-{uuid.uuid4().hex}@example.test", "plan": "plus"})
        self.addAsyncCleanup(db.user.delete_many, where={"id": self.user.id})
        token = security.create_access_token(self.user.id, self.user.sessionGeneration)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                        headers={"Authorization": f"Bearer {token}"})
        self.addAsyncCleanup(self.client.aclose)

    async def person(self, name, **fields):
        from app.entities import normalize_name

        return await self.db.person.create(data={"userId": self.user.id, "name": name, "normalizedName": normalize_name(name), **fields})

    async def mention(self, person, text):
        entry = await self.db.entry.create(data={"userId": self.user.id, "rawTranscript": text, "emotions": []})
        await self.db.entryperson.create(data={"entryId": entry.id, "personId": person.id})
        return entry

    async def test_merge_moves_os_data_and_fills_blanks(self):
        when = datetime(1990, 5, 4, tzinfo=timezone.utc)
        kept = await self.person("Bilal", relationship="Friend", phone="+961 1", reminderDaysBefore=7)
        merged = await self.person("بلال", relationship="Cousin", birthday=when, birthdayHasYear=False, email="b@example.com",
                                   notes="likes tea", avatarColor="#123456")
        both = await self.mention(kept, "with Bilal")
        await self.db.entryperson.create(data={"entryId": both.id, "personId": merged.id})
        only_merged = await self.mention(merged, "with بلال")
        date = await self.db.importantdate.create(data={"userId": self.user.id, "personId": merged.id, "title": "Graduation", "date": when})
        gift = await self.db.giftidea.create(data={"personId": merged.id, "title": "Book"})
        debt = await self.db.debt.create(data={"userId": self.user.id, "personId": merged.id, "direction": "I_OWE", "title": "Lunch",
                                               "principal": 10, "currency": "USD", "startDate": when})
        note = await self.db.note.create(data={"userId": self.user.id, "personId": merged.id, "title": "About him", "content": ""})
        reminder = await self.db.reminder.create(data={"userId": self.user.id, "title": "Call", "remindAt": when,
                                                       "entityType": "PERSON", "entityId": merged.id})
        notification = await self.db.notification.create(data={"userId": self.user.id, "title": "Birthday", "dedupeKey": "birthday:x",
                                                                "entityType": "PERSON", "entityId": merged.id})

        response = await self.client.post(f"/threads/people/{merged.id}/merge", json={"intoId": kept.id})
        self.assertEqual((response.status_code, response.json()), (200, {"id": kept.id}))

        row = await self.db.person.find_unique(where={"id": kept.id})
        self.assertEqual((row.name, row.relationship, row.phone, row.reminderDaysBefore), ("Bilal", "Friend", "+961 1", 7))  # kept wins
        self.assertEqual((row.birthday, row.birthdayHasYear, row.email, row.notes, row.avatarColor),
                         (when, False, "b@example.com", "likes tea", "#123456"))  # blanks filled, with the year rule
        self.assertIn("بلال", row.aliases)
        self.assertIsNone(await self.db.person.find_unique(where={"id": merged.id}))
        for table, id in ((self.db.importantdate, date.id), (self.db.giftidea, gift.id), (self.db.debt, debt.id), (self.db.note, note.id)):
            self.assertEqual((await table.find_unique(where={"id": id})).personId, kept.id, table)
        self.assertEqual((await self.db.reminder.find_unique(where={"id": reminder.id})).entityId, kept.id)
        self.assertEqual((await self.db.notification.find_unique(where={"id": notification.id})).entityId, kept.id)
        links = {l.entryId for l in await self.db.entryperson.find_many(where={"personId": kept.id})}
        self.assertEqual(links, {both.id, only_merged.id})

    async def test_kept_birthday_keeps_its_year_flag(self):
        kept = await self.person("Sara", birthday=datetime(1995, 12, 1, tzinfo=timezone.utc))
        merged = await self.person("Sarah", birthday=datetime(2000, 1, 2, tzinfo=timezone.utc), birthdayHasYear=False,
                                   anniversary=datetime(2015, 6, 1, tzinfo=timezone.utc))
        self.assertEqual((await self.client.post(f"/threads/people/{merged.id}/merge", json={"intoId": kept.id})).status_code, 200)
        row = await self.db.person.find_unique(where={"id": kept.id})
        self.assertEqual((row.birthday.year, row.birthdayHasYear, row.anniversary.year), (1995, True, 2015))

    async def test_topics_and_errors_unchanged(self):
        a = await self.db.topic.create(data={"userId": self.user.id, "name": "Work", "normalizedName": "work"})
        b = await self.db.topic.create(data={"userId": self.user.id, "name": "Job", "normalizedName": "job"})
        self.assertEqual((await self.client.post(f"/threads/topics/{b.id}/merge", json={"intoId": a.id})).json(), {"id": a.id})
        self.assertEqual((await self.db.topic.find_unique(where={"id": a.id})).aliases, ["Job"])
        person = await self.person("Omar")
        self.assertEqual((await self.client.post(f"/threads/people/{person.id}/merge", json={"intoId": person.id})).status_code, 400)
        self.assertEqual((await self.client.post(f"/threads/people/{person.id}/merge", json={"intoId": "nope"})).status_code, 404)


if __name__ == "__main__":
    unittest.main()
