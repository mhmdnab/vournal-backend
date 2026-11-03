"""Migration checks against a scratch local database.

Run only through the guarded runner (frontend-mobile-app), which creates and
drops the scratch database and never reads this repo's .env:
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py migrate-test
"""

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "prisma" / "migrations"
BUILD5 = MIGRATIONS / "9a_build5_tasks_and_auth" / "migration.sql"
DB = os.environ.get("MIGRATE_TEST_DB", "")
PRISMA = str(ROOT / ".venv" / "bin" / "prisma")


def migration_key(name: str) -> tuple[int, str]:
    """(9, "") for "9_collapse…", (9, "a") for "9a_build5…": the intended order."""
    match = re.match(r"(\d+)([a-z]*)_", name)
    if match is None:
        raise ValueError(f"Unexpected migration folder name: {name}")
    return int(match[1]), match[2]


def require_scratch_db(url: str) -> None:
    """The tests DROP the public schema, so refuse anything but the local scratch DB."""
    parsed = urlparse(url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path.lstrip(
        "/"
    ) != "vournal_migrate_test":
        raise RuntimeError(f"Refusing to run migration tests against {url!r}.")


def _psql(sql: str) -> str:
    out = subprocess.run(
        ["psql", DB, "-v", "ON_ERROR_STOP=1", "-Atc", sql],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _prisma(
    *args: str, upto: tuple[int, str] | None = None
) -> subprocess.CompletedProcess:
    """Run the Prisma CLI from a temp dir holding only the schema and the chosen
    migrations, so it can't pick up a .env."""
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(ROOT / "prisma" / "schema.prisma", tmp)
        target = pathlib.Path(tmp) / "migrations"
        target.mkdir()
        shutil.copy(MIGRATIONS / "migration_lock.toml", target)
        for folder in MIGRATIONS.iterdir():
            if folder.is_dir() and (upto is None or migration_key(folder.name) <= upto):
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


def _deploy(upto: tuple[int, str] | None = None) -> None:
    result = _prisma("migrate", "deploy", "--schema", "schema.prisma", upto=upto)
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)


@unittest.skipUnless(DB, "MIGRATE_TEST_DB not set — use isolated_backend.py migrate-test")
class Migrations(unittest.TestCase):
    def setUp(self):
        require_scratch_db(DB)
        _psql("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")

    def test_migration_is_additive(self):
        sql = BUILD5.read_text().upper()
        for banned in ("DROP TABLE", "DROP COLUMN", "DROP TYPE", "RENAME", "ALTER COLUMN"):
            self.assertNotIn(banned, sql)

    def test_migration_on_existing_rows(self):
        _deploy(upto=(9, ""))
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
        self.assertEqual(
            _psql(
                """SELECT "appAccountToken" IS NULL, "planSource" IS NULL,
                          "appleOriginalTransactionId" IS NULL
                   FROM "User" WHERE id = 'u1'"""
            ),
            "t|t|t",
        )
        self.assertEqual(_psql('SELECT count(*) FROM "PendingGrant"'), "0")
        self.assertEqual(
            _psql(
                """SELECT "emailConfirmRequired", "sessionGeneration",
                          "appleRefreshToken" IS NULL
                   FROM "User" WHERE id = 'u1'"""
            ),
            "f|0|t",
        )
        self.assertEqual(
            _psql("""SELECT "recurrenceWeekdays" FROM "Todo" WHERE id = 't1'"""), "{}"
        )

    def test_vournal_os_on_existing_rows(self):
        # An app account from before vournal OS keeps every row, and its people get
        # the new fields' defaults rather than a failed migration.
        _deploy(upto=(9, "e"))
        _psql(
            """INSERT INTO "User" (id, email) VALUES ('u1', 'build8@example.test');
               INSERT INTO "Person" (id, "userId", name, "normalizedName")
                 VALUES ('p1', 'u1', 'Rania', 'rania');
               INSERT INTO "Entry" (id, "userId", "rawTranscript") VALUES ('e1', 'u1', 'x');
               INSERT INTO "Todo" (id, "entryId", "userId", text) VALUES ('t1', 'e1', 'u1', 'Call');"""
        )
        _deploy()
        self.assertEqual(
            _psql(
                """SELECT (SELECT email FROM "User" WHERE id = 'u1'),
                          (SELECT name FROM "Person" WHERE id = 'p1'),
                          (SELECT "rawTranscript" FROM "Entry" WHERE id = 'e1'),
                          (SELECT text FROM "Todo" WHERE id = 't1')"""
            ),
            "build8@example.test|Rania|x|Call",
        )
        self.assertEqual(
            _psql(
                """SELECT "birthdayHasYear", "reminderDaysBefore", birthday IS NULL,
                          relationship IS NULL, "createdAt" IS NOT NULL
                   FROM "Person" WHERE id = 'p1'"""
            ),
            "t|3|t|t|t",
        )

    def test_vournal_os_cascades_from_user(self):
        # Deleting an account (DELETE /auth/me) must take its vournal OS rows too.
        _deploy()
        _psql(
            """INSERT INTO "User" (id, email) VALUES ('u1', 'gone@example.test');
               INSERT INTO "UserSettings" (id, "userId", "updatedAt") VALUES ('s1', 'u1', now());
               INSERT INTO "Category" (id, "userId", scope, name, "updatedAt")
                 VALUES ('c1', 'u1', 'TASK', 'Work', now());
               DELETE FROM "User" WHERE id = 'u1';"""
        )
        self.assertEqual(
            _psql(
                """SELECT (SELECT count(*) FROM "UserSettings" WHERE id = 's1'),
                          (SELECT count(*) FROM "Category" WHERE id = 'c1')"""
            ),
            "0|0",
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


# Tables that existed before vournal OS; its migration may only touch Person.
APP_TABLES = {
    "User",
    "ExternalIdentity",
    "AuthChallenge",
    "PasswordResetToken",
    "RefreshToken",
    "Entry",
    "Todo",
    "Topic",
    "EntryTopic",
    "Person",
    "EntryPerson",
    "Place",
    "EntryPlace",
    "PendingGrant",
}


def _folders() -> list[str]:
    return [p.name for p in MIGRATIONS.iterdir() if p.is_dir()]


class MigrationLayout(unittest.TestCase):
    """No database needed: these guard the folder names and the scratch DB."""

    def test_migration_folders_sort_in_intended_order(self):
        # Prisma applies migrations in folder-name (text) order. "10_…" would sort
        # before "1_…", so new migrations after 9 are named 9a_, 9b_, …
        names = _folders()
        self.assertEqual(sorted(names), sorted(names, key=migration_key))

    def test_every_new_migration_is_additive(self):
        for name in _folders():
            if migration_key(name) <= (9, ""):
                continue
            sql = (MIGRATIONS / name / "migration.sql").read_text().upper()
            for banned in ("DROP TABLE", "DROP COLUMN", "DROP TYPE", "RENAME", "ALTER COLUMN"):
                self.assertNotIn(banned, sql, name)

    def test_vournal_os_migration_is_additive(self):
        # vournal OS only adds tables, plus new columns and links on the app's Person.
        sql = (MIGRATIONS / "9f_vournal_os" / "migration.sql").read_text()
        upper = sql.upper()
        for banned in (
            "DROP TABLE",
            "DROP COLUMN",
            "DROP TYPE",
            "RENAME",
            "ALTER COLUMN",
            "SET NOT NULL",
        ):
            self.assertNotIn(banned, upper)
        for statement in re.findall(r'ALTER TABLE\s+"(\w+)"\s+(\w+\s+\w+)', sql, flags=re.I):
            table, action = statement
            if table == "Person":
                self.assertIn(action.upper(), {"ADD COLUMN", "ADD CONSTRAINT"}, statement)
            else:
                # Foreign keys on the new tables are added after the tables exist.
                self.assertEqual(action.upper(), "ADD CONSTRAINT", statement)
                self.assertNotIn(table, APP_TABLES, statement)

    def test_refuses_non_scratch_db(self):
        for url in (
            "postgresql://u@db.render.com:5432/vournal_migrate_test",
            "postgresql://me@127.0.0.1:5433/vournal_enhancements",
            "",
        ):
            with self.assertRaises(RuntimeError, msg=url):
                require_scratch_db(url)
        require_scratch_db("postgresql://me@127.0.0.1:5433/vournal_migrate_test")
