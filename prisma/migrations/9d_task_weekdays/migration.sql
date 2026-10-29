-- AlterTable
ALTER TABLE "Todo" ADD COLUMN     "recurrenceWeekdays" TEXT[] DEFAULT ARRAY[]::TEXT[];

