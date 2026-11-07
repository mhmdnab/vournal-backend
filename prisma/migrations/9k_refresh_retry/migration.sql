-- A rotated refresh token remembers its successor, so a client whose rotation answer
-- was lost can retry instead of being treated as a stolen token (additive only).
ALTER TABLE "RefreshToken" ADD COLUMN "revokedAt" TIMESTAMP(3);
ALTER TABLE "RefreshToken" ADD COLUMN "replacedById" TEXT;
