-- Step 17, Phase 1 (contract): normalizedName is backfilled on every row
-- (scripts/backfill_normalized_names.py) and collisions are collapsed, so make it
-- required and move the per-user uniqueness from `name` to `normalizedName`. The
-- same surface form (case/whitespace/Arabic variant) can now never make two rows.
ALTER TABLE "Person" ALTER COLUMN "normalizedName" SET NOT NULL;
ALTER TABLE "Topic" ALTER COLUMN "normalizedName" SET NOT NULL;

DROP INDEX "Person_userId_name_key";
DROP INDEX "Topic_userId_name_key";

CREATE UNIQUE INDEX "Person_userId_normalizedName_key" ON "Person"("userId", "normalizedName");
CREATE UNIQUE INDEX "Topic_userId_normalizedName_key" ON "Topic"("userId", "normalizedName");
