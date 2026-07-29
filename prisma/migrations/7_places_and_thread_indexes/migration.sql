-- Step 17, Phase 2: Places as a first-class entity (mirrors Person/Topic), plus
-- reverse indexes on the join tables so per-entity thread queries don't scan.
-- CreateTable
CREATE TABLE "Place" (
    "id" TEXT NOT NULL,
    "userId" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "normalizedName" TEXT NOT NULL,
    CONSTRAINT "Place_pkey" PRIMARY KEY ("id")
);
-- CreateTable
CREATE TABLE "EntryPlace" (
    "entryId" TEXT NOT NULL,
    "placeId" TEXT NOT NULL,
    CONSTRAINT "EntryPlace_pkey" PRIMARY KEY ("entryId","placeId")
);
-- CreateIndex
CREATE UNIQUE INDEX "Place_userId_normalizedName_key" ON "Place"("userId", "normalizedName");
-- CreateIndex
CREATE INDEX "EntryPlace_placeId_idx" ON "EntryPlace"("placeId");
-- CreateIndex
CREATE INDEX "EntryPerson_personId_idx" ON "EntryPerson"("personId");
-- CreateIndex
CREATE INDEX "EntryTopic_topicId_idx" ON "EntryTopic"("topicId");
-- AddForeignKey
ALTER TABLE "Place" ADD CONSTRAINT "Place_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE CASCADE ON UPDATE CASCADE;
-- AddForeignKey
ALTER TABLE "EntryPlace" ADD CONSTRAINT "EntryPlace_entryId_fkey" FOREIGN KEY ("entryId") REFERENCES "Entry"("id") ON DELETE CASCADE ON UPDATE CASCADE;
-- AddForeignKey
ALTER TABLE "EntryPlace" ADD CONSTRAINT "EntryPlace_placeId_fkey" FOREIGN KEY ("placeId") REFERENCES "Place"("id") ON DELETE CASCADE ON UPDATE CASCADE;
