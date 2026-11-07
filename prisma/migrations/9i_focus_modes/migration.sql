-- Focus modes (like iOS Focus): named profiles of sections, widgets, projects and tasks; one can be on.
-- Additive only: two nullable columns.
ALTER TABLE "UserSettings" ADD COLUMN "focusModes" JSONB, ADD COLUMN "activeFocusModeId" TEXT;
