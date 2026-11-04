"""Onboarding's starter packs answer like NestJS did: what each template creates, in both
languages and only for enabled sections, once per account."""

import unittest

from vournal_os.parity.harness import ParityCase


class OnboardingParity(ParityCase):
    maxDiff = None

    async def created(self):
        for path in ("/tasks?pageSize=100", "/habits", "/goals", "/projects", "/shopping/items", "/notes"):
            with self.subTest(path=path):
                self.assertParity(*await self.both("GET", path))

    async def test_student_pack_once(self):
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))
        self.assertParity(*await self.both("POST", "/onboarding/starter", {"template": "student"}))
        await self.created()
        self.assertParity(*await self.both("POST", "/onboarding/starter", {"template": "family"}))  # once only
        await self.created()

    async def test_arabic_with_sections_off(self):
        self.assertParity(*await self.both("PATCH", "/settings", {"language": "ar", "enabledSections": ["tasks", "notes", "shopping"]}))
        self.assertParity(*await self.both("POST", "/onboarding/starter", {"template": "family"}))
        await self.created()

    async def test_every_template_and_validation(self):
        for template in ("simple", "employee", "freelancer"):
            other = await self.other_user(f"T {template}")
            self.assertParity(await other[0].post("/onboarding/starter", json={"template": template}),
                              await other[1].post("/api/onboarding/starter", json={"template": template}))
            for path in ("/tasks?pageSize=100", "/projects", "/goals"):
                self.assertParity(await other[0].get(path), await other[1].get(f"/api{path}"))
        for body in ({}, {"template": "pirate"}, {"template": "simple", "bogus": 1}):
            with self.subTest(body=body):
                nest, py = await self.both("POST", "/onboarding/starter", body)
                self.assertParity(nest, py)
                self.assertEqual(nest.json()["message"], py.json()["message"])


if __name__ == "__main__":
    unittest.main()
