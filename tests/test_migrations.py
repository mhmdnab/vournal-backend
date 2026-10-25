"""Migration checks against a scratch local database.

Run only through the guarded runner (frontend-mobile-app), which creates and
drops the scratch database and never reads this repo's .env:
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py migrate-test
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "prisma" / "migrations"
BUILD5 = MIGRATIONS / "10_build5_tasks_and_auth" / "migration.sql"
DB = os.environ.get("MIGRATE_TEST_DB", "")
PRISMA = str(ROOT / ".venv" / "bin" / "prisma")


def _number(path: pathlib.Path) -> int:
    return int(path.name.split("_", 1)[0])


def _psql(sql: str) -> str:
    out = subprocess.run(
        ["psql", DB, "-v", "ON_ERROR_STOP=1", "-Atc", sql],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _prisma(*args: str, upto: int | None = None) -> subprocess.CompletedProcess:
    """Run the Prisma CLI from a temp dir holding only the schema and the chosen
    migrations, so it can't pick up a .env."""
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(ROOT / "prisma" / "schema.prisma", tmp)
        target = pathlib.Path(tmp) / "migrations"
        target.mkdir()
        shutil.copy(MIGRATIONS / "migration_lock.toml", target)
        for folder in MIGRATIONS.iterdir():
            if folder.is_dir() and (upto is None or _number(folder) <= upto):
                shutil.copytree(folder, target / folder.name)
        env = {
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", ""),
            "DATABASE_URL": DB,
            "DIRECT_URL": DB,
            "PYTHON_DOTENV_DISABLED": "1",
        }
        return subprocess.run(
            [PRISMA, *args],
            cwd=tmp,
            env=env,
            capture_output=True,
            text=True,
        )


def _deploy(upto: int | None = None) -> None:
    result = _prisma("migrate", "deploy", "--schema", "schema.prisma", upto=upto)
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)


@unittest.skipUnless(DB, "MIGRATE_TEST_DB not set — use isolated_backend.py migrate-test")
class Migrations(unittest.TestCase):
    def setUp(self):
        _psql("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")

    def test_migration_is_additive(self):
        sql = BUILD5.read_text().upper()
        for banned in ("DROP TABLE", "DROP COLUMN", "DROP TYPE", "RENAME", "ALTER COLUMN"):
            self.assertNotIn(banned, sql)

    def test_migration_on_existing_rows(self):
        _deploy(upto=9)
        _psql(
            """INSERT INTO "User" (id, email) VALUES ('u1', 'build4@example.test');
               INSERT INTO "Entry" (id, "userId", "rawTranscript") VALUES ('e1', 'u1', 'x');
               INSERT INTO "Todo" (id, "entryId", "userId", text) VALUES ('t1', 'e1', 'u1', 'Call');"""
        )
        _deploy()
        self.assertEqual(
            _psql(
                """SELECT urgency, kind, "dismissedQuestions", "recurrenceInterval"
                   FROM "Todo" WHERE id = 't1'"""
            ),
            "0|task|{}|1",
        )
        self.assertEqual(
            _psql(
                """SELECT "twoFactorEnabled", "recoveryCodeHashes",
                          "emailVerifiedAt" IS NULL
                   FROM "User" WHERE id = 'u1'"""
            ),
            "f|{}|t",
        )

    def test_schema_matches_after_all_migrations(self):
        _deploy()
        result = _prisma(
            "migrate",
            "diff",
            "--from-url",
            DB,
            "--to-schema-datamodel",
            "schema.prisma",
            "--exit-code",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
