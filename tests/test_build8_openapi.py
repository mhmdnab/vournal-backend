"""Build 8 (and every older build) keeps the API it was built against.

The vournal OS port adds routes under /api only. This pins every other route's
request and response schema to the snapshot taken before the port began, so a
change to a root route fails here instead of on a phone.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
Rewrite the snapshot (only when a root route is meant to change) with
UPDATE_BUILD8_SNAPSHOT=1.
"""

import json
import os
import pathlib
import unittest

SNAPSHOT = pathlib.Path(__file__).parent / "fixtures" / "build8_openapi.json"


def _inline(node, components: dict, seen: frozenset = frozenset()):
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", 1)[-1]
            if name in seen:  # a recursive schema: keep the name, not an infinite tree
                return {"$ref": name}
            return _inline(components.get(name, {}), components, seen | {name})
        return {key: _inline(value, components, seen) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline(value, components, seen) for value in node]
    return node


def root_contract(openapi: dict) -> dict:
    """Every path outside /api, with schema references written out in full, so a
    change to a shared model shows up on each route that uses it."""
    components = openapi.get("components", {}).get("schemas", {})
    return {
        path: _inline(operations, components)
        for path, operations in openapi.get("paths", {}).items()
        if path != "/api" and not path.startswith("/api/")
    }


class Build8Routes(unittest.TestCase):
    def test_root_routes_unchanged(self):
        from app.main import app

        contract = root_contract(app.openapi())
        if os.environ.get("UPDATE_BUILD8_SNAPSHOT") == "1":
            SNAPSHOT.parent.mkdir(exist_ok=True)
            SNAPSHOT.write_text(json.dumps(contract, indent=1, sort_keys=True) + "\n")
            self.fail("snapshot written")
        self.assertEqual(contract, json.loads(SNAPSHOT.read_text()))


if __name__ == "__main__":
    unittest.main()
