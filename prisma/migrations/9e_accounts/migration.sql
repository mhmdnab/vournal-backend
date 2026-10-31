-- AlterTable
ALTER TABLE "User" ADD COLUMN     "appleRefreshToken" TEXT,
ADD COLUMN     "emailConfirmRequired" BOOLEAN NOT NULL DEFAULT false,
ADD COLUMN     "sessionGeneration" INTEGER NOT NULL DEFAULT 0;

-- AlterTable
ALTER TABLE "AuthChallenge" ADD COLUMN     "pendingIssuer" TEXT,
ADD COLUMN     "pendingSubject" TEXT;

-- AlterTable
ALTER TABLE "RefreshToken" ADD COLUMN     "generation" INTEGER NOT NULL DEFAULT 0;
