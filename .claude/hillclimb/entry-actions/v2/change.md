Round 2: an unnamed person ("my cousin") gets a question instead of a new person, a change that fits exactly one item is proposed rather than asked about, and a proposal withdrawn in the same run no longer reaches the card (a real bug, fixed with a test).

Analyzer (train only): ask-en-03 created a person named "cousin"; goal-az-01 asked "What should I record?" with the right goal update as a choice; multi-en-02 withdrew a wrong Rent proposal mid-run but both reached the card because _save_actions only cancelled rows already in the database.
