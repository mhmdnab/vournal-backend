-- Step 14a-fix (final pricing): rename the `unlimited` tier to `daily`.
-- RENAME VALUE preserves every existing row and runs inside a transaction — no
-- drop/recreate of the enum, no data migration needed. The new caps for each tier
-- live in app.quota; this only renames the enum member.
ALTER TYPE "Plan" RENAME VALUE 'unlimited' TO 'daily';
