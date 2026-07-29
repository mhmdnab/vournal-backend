-- Step 17, Phase 4: alternate surface forms an entity also answers to. A merge
-- folds the absorbed entity's name + aliases in here so both forms survive; a
-- surface form shared by 2+ entities makes its chip read as ambiguous.
-- AlterTable
ALTER TABLE "Person" ADD COLUMN     "aliases" TEXT[] DEFAULT ARRAY[]::TEXT[];
-- AlterTable
ALTER TABLE "Place" ADD COLUMN     "aliases" TEXT[] DEFAULT ARRAY[]::TEXT[];
-- AlterTable
ALTER TABLE "Topic" ADD COLUMN     "aliases" TEXT[] DEFAULT ARRAY[]::TEXT[];
