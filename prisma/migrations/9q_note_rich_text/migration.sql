-- Word-style notes (design 2026-11-18): a note's format ("markdown" for every existing note) and its
-- words for lists and search; pasted images marked inline so they're cleaned up with the note.
-- Additive only: existing rows keep their markdown and are converted when next edited.
ALTER TABLE "Note" ADD COLUMN "contentFormat" TEXT NOT NULL DEFAULT 'markdown',
ADD COLUMN "contentText" TEXT NOT NULL DEFAULT '';

ALTER TABLE "Attachment" ADD COLUMN "inline" BOOLEAN NOT NULL DEFAULT false;
