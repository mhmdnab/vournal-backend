-- AlterTable
ALTER TABLE "Entry" ADD COLUMN     "llmInTokens" INTEGER,
ADD COLUMN     "llmOutTokens" INTEGER,
ADD COLUMN     "sttSeconds" DOUBLE PRECISION;

-- AlterTable
ALTER TABLE "User" ADD COLUMN     "usagePeriodEntries" INTEGER NOT NULL DEFAULT 0,
ADD COLUMN     "usagePeriodStart" TIMESTAMP(3);

