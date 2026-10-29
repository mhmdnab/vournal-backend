-- AlterTable
ALTER TABLE "User" ADD COLUMN     "appAccountToken" TEXT,
ADD COLUMN     "appleOriginalTransactionId" TEXT,
ADD COLUMN     "planSource" TEXT;

-- CreateTable
CREATE TABLE "PendingGrant" (
    "id" TEXT NOT NULL,
    "email" TEXT NOT NULL,
    "source" TEXT NOT NULL,
    "externalId" TEXT NOT NULL,
    "expiresAt" TIMESTAMP(3) NOT NULL,
    "appliedAt" TIMESTAMP(3),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "PendingGrant_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "PendingGrant_externalId_key" ON "PendingGrant"("externalId");

-- CreateIndex
CREATE INDEX "PendingGrant_email_idx" ON "PendingGrant"("email");

-- CreateIndex
CREATE UNIQUE INDEX "User_appAccountToken_key" ON "User"("appAccountToken");

-- CreateIndex
CREATE UNIQUE INDEX "User_appleOriginalTransactionId_key" ON "User"("appleOriginalTransactionId");

