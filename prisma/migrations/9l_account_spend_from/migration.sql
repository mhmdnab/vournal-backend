-- Whether the spending plan spends from this account (owner, 2026-11-09). Savings and
-- investment accounts start switched off.
ALTER TABLE "Account" ADD COLUMN "spendFrom" BOOLEAN NOT NULL DEFAULT true;
UPDATE "Account" SET "spendFrom" = false WHERE "type" IN ('SAVINGS', 'INVESTMENT');
