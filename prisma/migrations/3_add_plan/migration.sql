-- CreateEnum
CREATE TYPE "Plan" AS ENUM ('free', 'unlimited', 'reflection');

-- AlterTable
ALTER TABLE "User" ADD COLUMN     "paddleCustomerId" TEXT,
ADD COLUMN     "paddleSubscriptionId" TEXT,
ADD COLUMN     "plan" "Plan" NOT NULL DEFAULT 'free',
ADD COLUMN     "planExpiresAt" TIMESTAMP(3);

