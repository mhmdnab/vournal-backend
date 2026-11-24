-- The email 3 days before a store's free month of Plus ends (owner 2026-11-24): when the free
-- month ends, whether it will renew, and when the reminder went out. Additive only.
-- AlterTable
ALTER TABLE "User" ADD COLUMN "planIntroEndsAt" TIMESTAMP(3),
ADD COLUMN "planAutoRenew" BOOLEAN,
ADD COLUMN "planIntroReminderSentAt" TIMESTAMP(3);
