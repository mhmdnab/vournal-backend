"""The generated Prisma client must stay small enough to start on a 512 MB Render instance.

With 71 models, recursive_type_depth = 5 generates a 20 MB types.py: importing it peaks
above 1.3 GB on a first start and 513 MB cached, so Render killed the server at startup
(5 November 2026). Depth 2 (the generator's minimum) gives 7.7 MB. The types only serve
static checking: no code imports prisma.types.
"""

import pathlib
import re
import unittest

SCHEMA = pathlib.Path(__file__).resolve().parents[1] / "prisma" / "schema.prisma"


class ClientSizeTest(unittest.TestCase):
    def test_recursive_type_depth_is_the_minimum(self):
        match = re.search(r"^\s*recursive_type_depth\s*=\s*(-?\d+)", SCHEMA.read_text(), re.M)
        self.assertIsNotNone(match, "set recursive_type_depth explicitly (the default is 5)")
        self.assertEqual(int(match[1]), 2)

    def test_no_code_depends_on_the_generated_types(self):
        root = SCHEMA.parents[1]
        users = [str(p.relative_to(root)) for d in ("app", "scripts") for p in (root / d).rglob("*.py")
                 if re.search(r"prisma\.types|from prisma import types", p.read_text())]
        self.assertEqual(users, [])


if __name__ == "__main__":
    unittest.main()
