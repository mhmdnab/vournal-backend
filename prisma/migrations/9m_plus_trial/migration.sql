-- The free months of Plus for new accounts (owner, 2026-11-09). Additive only.
ALTER TABLE "User" ADD COLUMN "trialEndsAt" TIMESTAMP(3),
ADD COLUMN "trialReminderSentAt" TIMESTAMP(3);

-- Kept after an account is deleted (no foreign key): one trial per person.
CREATE TABLE "TrialClaim" (
    "id" TEXT NOT NULL,
    "kind" TEXT NOT NULL,
    "hash" TEXT NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "TrialClaim_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX "TrialClaim_hash_key" ON "TrialClaim"("hash");
