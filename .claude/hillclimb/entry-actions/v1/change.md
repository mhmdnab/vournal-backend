Round 1 (breadth pass): close the gaps the baseline showed — birthdays/person facts/habit activities count as needing actions, the step sees the user's habits/projects/goals with ids and targets, habit amounts become counts, "alf"/"ألف" means LBP, and an ambiguous "the project" gets a question.

Baseline failures this targets (read before the split was drawn, so round 1 isn't fully blind):
- extraction's needs-actions flag false for birthdays, a fact about a person, and "read 20 pages" (6 cases) → widened the wantsActions rule (app/extraction.py).
- habit check-ins without the amount, or not logged at all (5 cases) → the user's habits with targets are listed in the entry, plus a count rule (app/entry_actions.py, entry_actions.json).
- "150 alf" answered with a currency question (2 cases) → LBP rule.
- "the project" with two active projects not questioned (1 case) → ambiguity rule.

Independent small gaps bundled on purpose (each alone sits under the noise floor).
