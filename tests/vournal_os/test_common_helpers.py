"""Ownership checks, the activity log and query-string parsing shared by every section."""

import unittest

from vournal_os.support import OsTestCase


class Ownership(OsTestCase):
    async def test_assert_owned_404_for_other_user(self):
        from app.os.common.ownership import assert_owned
        from fastapi import HTTPException

        me, other = await self.make_user(), await self.make_user()
        theirs = await self.db.category.create(data={"userId": other.id, "scope": "TASK", "name": "Theirs"})
        await assert_owned("category", None, me.id)  # nothing given: nothing to check
        await assert_owned("category", theirs.id, other.id)
        with self.assertRaises(HTTPException) as caught:
            await assert_owned("category", theirs.id, me.id)
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(caught.exception.detail["message"], "Category not found")

    async def test_assert_many_owned_dedupes(self):
        from app.os.common.ownership import assert_many_owned
        from fastapi import HTTPException

        me, other = await self.make_user(), await self.make_user()
        mine = await self.db.tag.create(data={"userId": me.id, "name": "mine"})
        theirs = await self.db.tag.create(data={"userId": other.id, "name": "theirs"})
        await assert_many_owned("tag", [mine.id, mine.id], me.id)
        await assert_many_owned("tag", [], me.id)
        with self.assertRaises(HTTPException) as caught:
            await assert_many_owned("tag", [mine.id, theirs.id], me.id)
        self.assertEqual(caught.exception.detail["message"], "Tag not found")

    async def test_parent_scoped_model(self):
        from app.os.common.ownership import assert_owned
        from fastapi import HTTPException

        me, other = await self.make_user(), await self.make_user()
        person = await self.db.person.create(data={"userId": other.id, "name": "Rania", "normalizedName": "rania"})
        gift = await self.db.giftidea.create(data={"personId": person.id, "title": "Book"})
        await assert_owned("giftidea", gift.id, other.id)
        with self.assertRaises(HTTPException) as caught:
            await assert_owned("giftidea", gift.id, me.id)
        self.assertEqual(caught.exception.detail["message"], "Gift idea not found")


class Activity(OsTestCase):
    async def test_log_activity_derives_entity_type(self):
        from app.os.common.activity import log_activity

        me = await self.make_user()
        await log_activity(me.id, "task.created", entity_id="t1", title="Call")
        await log_activity(me.id, "category.created", entity_id="c1", name="Work", scope="TASK")
        await log_activity(me.id, "tag.deleted", entity_id="g1")
        rows = {r.action: r for r in await self.db.activitylog.find_many(where={"userId": me.id})}
        self.assertEqual(rows["task.created"].entityType, "TASK")
        self.assertEqual(rows["task.created"].metadata, {"title": "Call"})
        self.assertIsNone(rows["category.created"].entityType)
        self.assertEqual(rows["category.created"].entityId, "c1")
        self.assertEqual(rows["category.created"].metadata, {"name": "Work", "scope": "TASK"})
        self.assertIsNone(rows["tag.deleted"].metadata)

    async def test_log_activity_never_raises(self):
        from app.os.common.activity import log_activity

        await log_activity("no-such-user", "task.created", entity_id="x")
        self.assertEqual(await self.db.activitylog.count(where={"userId": "no-such-user"}), 0)

    async def test_system_actions_not_logged(self):
        from app.os.common.activity import log_activity

        me = await self.make_user()
        await log_activity(me.id, "system.tick")
        self.assertEqual(await self.db.activitylog.count(where={"userId": me.id}), 0)

    async def test_big_metadata_is_truncated(self):
        from app.os.common.activity import log_activity

        me = await self.make_user()
        await log_activity(me.id, "note.updated", entity_id="n1", body="x" * 9000)
        row = await self.db.activitylog.find_first(where={"userId": me.id})
        self.assertEqual(row.metadata, {"truncated": True})


class Query(unittest.TestCase):
    def test_string_list(self):
        from app.os.common.query import string_list

        self.assertEqual(string_list("a, b,,c"), ["a", "b", "c"])
        self.assertEqual(string_list(["a,b", "c"]), ["a", "b", "c"])
        self.assertIsNone(string_list(""))
        self.assertIsNone(string_list(None))


if __name__ == "__main__":
    unittest.main()
