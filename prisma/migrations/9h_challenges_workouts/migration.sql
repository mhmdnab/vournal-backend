-- Personal challenges (habit groups over a fixed number of days) and workout plans + sessions.
-- Additive only: new tables, no change to existing ones.
-- CreateTable
CREATE TABLE "Challenge" (
    "id" TEXT NOT NULL,
    "userId" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "description" TEXT,
    "icon" TEXT,
    "color" TEXT,
    "startDate" DATE NOT NULL,
    "durationDays" INTEGER NOT NULL,
    "isArchived" BOOLEAN NOT NULL DEFAULT false,
    "sortOrder" INTEGER NOT NULL DEFAULT 0,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "Challenge_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "ChallengeHabit" (
    "id" TEXT NOT NULL,
    "challengeId" TEXT NOT NULL,
    "habitId" TEXT NOT NULL,
    "sortOrder" INTEGER NOT NULL DEFAULT 0,

    CONSTRAINT "ChallengeHabit_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "WorkoutPlan" (
    "id" TEXT NOT NULL,
    "userId" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "description" TEXT,
    "isActive" BOOLEAN NOT NULL DEFAULT true,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "WorkoutPlan_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "WorkoutPlanDay" (
    "id" TEXT NOT NULL,
    "planId" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "weekday" INTEGER,
    "sortOrder" INTEGER NOT NULL DEFAULT 0,

    CONSTRAINT "WorkoutPlanDay_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "WorkoutPlanExercise" (
    "id" TEXT NOT NULL,
    "dayId" TEXT NOT NULL,
    "exerciseId" TEXT,
    "name" TEXT NOT NULL,
    "sets" INTEGER NOT NULL DEFAULT 3,
    "reps" TEXT,
    "weightKg" DECIMAL(6,2),
    "restSec" INTEGER,
    "notes" TEXT,
    "sortOrder" INTEGER NOT NULL DEFAULT 0,

    CONSTRAINT "WorkoutPlanExercise_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "WorkoutSession" (
    "id" TEXT NOT NULL,
    "userId" TEXT NOT NULL,
    "planId" TEXT,
    "planDayId" TEXT,
    "name" TEXT NOT NULL,
    "date" DATE NOT NULL,
    "durationMin" INTEGER,
    "notes" TEXT,
    "completedAt" TIMESTAMP(3),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "WorkoutSession_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "WorkoutSet" (
    "id" TEXT NOT NULL,
    "sessionId" TEXT NOT NULL,
    "exerciseId" TEXT,
    "name" TEXT NOT NULL,
    "reps" INTEGER,
    "weightKg" DECIMAL(6,2),
    "done" BOOLEAN NOT NULL DEFAULT false,
    "sortOrder" INTEGER NOT NULL DEFAULT 0,

    CONSTRAINT "WorkoutSet_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "Challenge_userId_isArchived_idx" ON "Challenge"("userId", "isArchived");

-- CreateIndex
CREATE INDEX "ChallengeHabit_habitId_idx" ON "ChallengeHabit"("habitId");

-- CreateIndex
CREATE UNIQUE INDEX "ChallengeHabit_challengeId_habitId_key" ON "ChallengeHabit"("challengeId", "habitId");

-- CreateIndex
CREATE INDEX "WorkoutPlan_userId_idx" ON "WorkoutPlan"("userId");

-- CreateIndex
CREATE INDEX "WorkoutPlanDay_planId_idx" ON "WorkoutPlanDay"("planId");

-- CreateIndex
CREATE INDEX "WorkoutPlanExercise_dayId_idx" ON "WorkoutPlanExercise"("dayId");

-- CreateIndex
CREATE INDEX "WorkoutSession_userId_date_idx" ON "WorkoutSession"("userId", "date");

-- CreateIndex
CREATE INDEX "WorkoutSet_sessionId_idx" ON "WorkoutSet"("sessionId");

-- AddForeignKey
ALTER TABLE "Challenge" ADD CONSTRAINT "Challenge_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ChallengeHabit" ADD CONSTRAINT "ChallengeHabit_challengeId_fkey" FOREIGN KEY ("challengeId") REFERENCES "Challenge"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ChallengeHabit" ADD CONSTRAINT "ChallengeHabit_habitId_fkey" FOREIGN KEY ("habitId") REFERENCES "Habit"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "WorkoutPlan" ADD CONSTRAINT "WorkoutPlan_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "WorkoutPlanDay" ADD CONSTRAINT "WorkoutPlanDay_planId_fkey" FOREIGN KEY ("planId") REFERENCES "WorkoutPlan"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "WorkoutPlanExercise" ADD CONSTRAINT "WorkoutPlanExercise_dayId_fkey" FOREIGN KEY ("dayId") REFERENCES "WorkoutPlanDay"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "WorkoutSession" ADD CONSTRAINT "WorkoutSession_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "WorkoutSession" ADD CONSTRAINT "WorkoutSession_planId_fkey" FOREIGN KEY ("planId") REFERENCES "WorkoutPlan"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "WorkoutSession" ADD CONSTRAINT "WorkoutSession_planDayId_fkey" FOREIGN KEY ("planDayId") REFERENCES "WorkoutPlanDay"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "WorkoutSet" ADD CONSTRAINT "WorkoutSet_sessionId_fkey" FOREIGN KEY ("sessionId") REFERENCES "WorkoutSession"("id") ON DELETE CASCADE ON UPDATE CASCADE;

