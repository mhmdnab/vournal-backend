"""The app's review card (design 2026-11-06): which entries the lists may show.

A new app asks for review mode; its entry then waits as a proposal ("needs_review") until the
user saves it. Older apps never ask, so their entries keep reviewState null and list as always.
An entry saved with "Save as journal entry" off keeps its to-dos but leaves the journal."""

# Prisma `where` fragments: listable entries, and those the journal shows
LISTED = {"OR": [{"reviewState": None}, {"reviewState": "saved"}]}
IN_JOURNAL = {**LISTED, "inJournal": True}
# the same for raw SQL, on an "Entry" aliased e
LISTED_SQL = '(e."reviewState" IS NULL OR e."reviewState" = \'saved\') AND e."inJournal"'
