-- The app's review card (design 2026-11-06): an entry waits for the user's Save, and a Plus
-- recording's AI actions are tied to it. Additive only: older apps never read these.
ALTER TABLE "Entry" ADD COLUMN "reviewState" TEXT, ADD COLUMN "proposal" JSONB, ADD COLUMN "inJournal" BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE "AssistantConversation" ADD COLUMN "entryId" TEXT;
ALTER TABLE "AssistantAction" ADD COLUMN "entryId" TEXT;
ALTER TABLE "UserSettings" ADD COLUMN "aiActionsAskedAt" TIMESTAMP(3);
CREATE UNIQUE INDEX "AssistantConversation_entryId_key" ON "AssistantConversation"("entryId");
CREATE INDEX "AssistantAction_entryId_idx" ON "AssistantAction"("entryId");
ALTER TABLE "AssistantConversation" ADD CONSTRAINT "AssistantConversation_entryId_fkey" FOREIGN KEY ("entryId") REFERENCES "Entry"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "AssistantAction" ADD CONSTRAINT "AssistantAction_entryId_fkey" FOREIGN KEY ("entryId") REFERENCES "Entry"("id") ON DELETE CASCADE ON UPDATE CASCADE;
