-- Step 17, Phase 1 (expand): add the nullable normalizedName comparison key to
-- Person and Topic. Backfilled by scripts/backfill_normalized_names.py, then made
-- required + unique in migration 6 (contract). Kept nullable here so this applies
-- cleanly to populated tables before the values exist.
ALTER TABLE "Person" ADD COLUMN "normalizedName" TEXT;
ALTER TABLE "Topic" ADD COLUMN "normalizedName" TEXT;
