-- AlterTable
ALTER TABLE "User" ADD COLUMN     "appleRefreshToken" TEXT,
ADD COLUMN     "emailConfirmRequired" BOOLEAN NOT NULL DEFAULT false;

-- AlterTable
ALTER TABLE "AuthChallenge" ADD COLUMN     "pendingIssuer" TEXT,
ADD COLUMN     "pendingSubject" TEXT;
