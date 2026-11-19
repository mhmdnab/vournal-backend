-- Message buttons (design 2026-11-19): a thumbs up or down on an assistant reply, with an optional
-- note on what went wrong. One rating per person per reply. Additive only.
-- CreateTable
CREATE TABLE "AssistantFeedback" (
    "id" TEXT NOT NULL,
    "userId" TEXT NOT NULL,
    "messageId" TEXT NOT NULL,
    "rating" TEXT NOT NULL,
    "comment" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "AssistantFeedback_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "AssistantFeedback_messageId_userId_key" ON "AssistantFeedback"("messageId", "userId");

-- CreateIndex
CREATE INDEX "AssistantFeedback_rating_createdAt_idx" ON "AssistantFeedback"("rating", "createdAt");

-- AddForeignKey
ALTER TABLE "AssistantFeedback" ADD CONSTRAINT "AssistantFeedback_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "AssistantFeedback" ADD CONSTRAINT "AssistantFeedback_messageId_fkey" FOREIGN KEY ("messageId") REFERENCES "AssistantMessage"("id") ON DELETE CASCADE ON UPDATE CASCADE;
