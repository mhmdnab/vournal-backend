-- Collapse the Plan enum from {free, daily, reflection} to {free, plus}: one free
-- tier + one paid tier. Any daily/reflection user becomes `plus`; free stays free.
-- Postgres can't drop enum values in place, so recreate the type (transaction-safe).
ALTER TYPE "Plan" RENAME TO "Plan_old";
CREATE TYPE "Plan" AS ENUM ('free', 'plus');

ALTER TABLE "User" ALTER COLUMN "plan" DROP DEFAULT;
ALTER TABLE "User" ALTER COLUMN "plan" TYPE "Plan" USING (
  CASE WHEN "plan"::text = 'free' THEN 'free' ELSE 'plus' END
)::"Plan";
ALTER TABLE "User" ALTER COLUMN "plan" SET DEFAULT 'free';

DROP TYPE "Plan_old";
