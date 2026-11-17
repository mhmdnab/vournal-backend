-- Ask vournal (design 2026-11-17): the user's permission for the assistant to read the journal,
-- asked once and shared by the app and the website. Additive only.
ALTER TABLE "UserSettings" ADD COLUMN "assistantJournalConsentAt" TIMESTAMP(3),
ADD COLUMN "assistantJournalAskedAt" TIMESTAMP(3);
