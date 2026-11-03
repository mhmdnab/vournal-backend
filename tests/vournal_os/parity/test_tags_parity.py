"""Tags answer like NestJS did."""

import unittest

from vournal_os.parity.harness import ParityCase


class TagsParity(ParityCase):
    async def test_tags_crud(self):
        self.assertParity(*await self.both("GET", "/tags"))
        nest, py = await self.both("POST", "/tags", {"name": "  Urgent ", "color": "#ff0000"})
        self.assertParity(nest, py)
        self.assertParity(*await self.both("POST", "/tags", {"name": "Later"}))
        self.assertParity(*await self.both("POST", "/tags", {"name": "urgent"}))
        self.assertParity(*await self.both("GET", "/tags"))
        paths = (f"/tags/{nest.json()['id']}", f"/api/tags/{py.json()['id']}")
        for method, body in (("PATCH", {"color": None}), ("PATCH", {"name": "LATER"}), ("PATCH", {"name": "Now"}), ("DELETE", None), ("DELETE", None)):
            kwargs = {} if body is None else {"json": body}
            self.assertParity(
                await self.nest.request(method, paths[0], **kwargs), await self.py.request(method, paths[1], **kwargs)
            )
        self.assertParity(*await self.both("GET", "/tags"))

    async def test_tag_validation_shapes(self):
        for body in ({}, {"name": ""}, {"name": "x" * 41}, {"name": "x", "color": "blue"}, {"name": 5}, {"name": "x", "extra": 1}):
            self.assertParity(*await self.both("POST", "/tags", body))


if __name__ == "__main__":
    unittest.main()
