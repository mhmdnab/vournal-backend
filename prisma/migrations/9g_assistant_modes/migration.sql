-- Dashboard AI part 1 (design 2026-11-06): the chat's mode, and what a waiting change needs.
-- Additive only: the app and build 8 never read these.
ALTER TABLE "UserSettings" ADD COLUMN "assistantMode" TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE "AssistantAction" ADD COLUMN "call" JSONB, ADD COLUMN "error" TEXT, ADD COLUMN "position" INTEGER NOT NULL DEFAULT 0;
