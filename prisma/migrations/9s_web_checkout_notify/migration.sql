-- Web checkout opening soon (owner 2026-11-23): a free account can ask to be emailed when web
-- checkout opens. Null = not asked. Additive only.
-- AlterTable
ALTER TABLE "UserSettings" ADD COLUMN "webCheckoutNotifyAt" TIMESTAMP(3);
