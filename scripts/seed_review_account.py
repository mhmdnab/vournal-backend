"""Create the App Review account: a real login with a lived-in sample journal.

App Store Review Guideline 2.1 asks a login app to "include demo account info".
This makes that account — three weeks of entries with summaries, moods, tasks,
people, topics and places — so a reviewer sees every screen with something in it.

The entries are written by hand, not run through transcription or extraction, so
seeding costs nothing, gives the same result every time, and can be dated in the
past. They are typed entries (source TEXT): there is no recording behind them,
and the app shows none.

NO PASSWORD LIVES IN THIS FILE. Pass one in REVIEW_PASSWORD, or let the script
generate one; either way it is printed once, at the end. Keep it in your password
manager and in App Store Connect — never in the repo.

Run from voice-journal-backend/:
    .venv/bin/python scripts/seed_review_account.py
    REVIEW_PASSWORD='…' .venv/bin/python scripts/seed_review_account.py
    .venv/bin/python scripts/seed_review_account.py --email someone@vournal.app
    .venv/bin/python scripts/seed_review_account.py --reset   # delete it, then recreate

It refuses to touch an account that already exists unless --reset is given, and
--reset only ever deletes the one account named by --email.
"""

import argparse
import asyncio
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Make `app` importable when run as `scripts/seed_review_account.py`.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from app import storage  # noqa: E402
from app.db import DEMO_USER_EMAIL, db  # noqa: E402
from app.entities import link_person, link_place, link_topic  # noqa: E402
from app.schemas import MIN_PASSWORD_LEN  # noqa: E402
from app.security import hash_password  # noqa: E402

DEFAULT_EMAIL = "appreview@vournal.app"

# Where the reviewer most likely is. The app re-syncs this to the device's own
# timezone on sign-in, so it only decides what hour the seeded entries show.
TIMEZONE = "America/Los_Angeles"

# No look-alike characters (0/o, 1/l/i): a reviewer types this by hand.
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def generate_password() -> str:
    groups = ["".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(4)]
    return "-".join(groups)


# ---- The journal ------------------------------------------------------------
# days_ago / at: when the entry was made, in TIMEZONE.
# todos: `due` is days from TODAY (the day of seeding), so upcoming tasks stay
#        upcoming for the review; `time` is a local "HH:MM"; `done` marks it ticked.
ENTRIES = [
    {
        "days_ago": 20, "at": "21:10",
        "transcript": "First entry. I've been meaning to keep a journal for years and never stuck with it because writing feels like homework. Talking is easier. Work was fine today, nothing special. I want to get back into running, I used to go three times a week and I miss how clear my head felt after.",
        "summary": "Starting a journal by voice and wanting to get back into running.",
        "mood": {"score": 3, "label": "calm and a little hopeful", "emotions": ["hopeful", "calm"]},
        "todos": [{"text": "Go for a run this week", "due": -17, "done": True}],
        "topics": ["journaling", "running", "work"], "people": [], "places": [],
    },
    {
        "days_ago": 18, "at": "19:40",
        "transcript": "Went for a run before work, only twenty minutes but I did it. Had lunch with Maya, she's thinking about moving closer to Mom and wanted to talk it through. I mostly listened. Daniel asked me to put together the proposal for the new client by Friday, which is tight.",
        "summary": "A short morning run, lunch with Maya about her possible move, and a proposal due Friday for Daniel.",
        "mood": {"score": 4, "label": "pleased but slightly stressed", "emotions": ["pleased", "stressed"]},
        "todos": [{"text": "Send the client proposal to Daniel", "due": -14, "done": True}],
        "topics": ["running", "family", "work"], "people": ["Maya", "Daniel"], "places": [],
    },
    {
        "days_ago": 16, "at": "23:05",
        "transcript": "Long day. I stayed at the office until nine working on the proposal and I'm not happy with it yet. Skipped the gym. I keep telling myself I work better under pressure but honestly I just sleep worse. I need to stop looking at my phone in bed.",
        "summary": "A late night at the office on the proposal, a skipped workout, and poor sleep.",
        "mood": {"score": 2, "label": "tired and frustrated", "emotions": ["tired", "frustrated"]},
        "todos": [{"text": "Stop using the phone in bed", "due": None, "done": False}],
        "topics": ["work", "sleep", "health"], "people": [], "places": ["the office"],
    },
    {
        "days_ago": 14, "at": "18:20",
        "transcript": "Sent the proposal this morning. Daniel read it within the hour and said it was the clearest one we've done, which I did not expect. I feel lighter. Went to the gym after to celebrate, which is a strange way to celebrate but it worked.",
        "summary": "The proposal went out and Daniel praised it; celebrated with a workout.",
        "mood": {"score": 5, "label": "relieved and proud", "emotions": ["relieved", "proud", "happy"]},
        "todos": [],
        "topics": ["work", "health"], "people": ["Daniel"], "places": ["the gym"],
    },
    {
        "days_ago": 12, "at": "20:30",
        "transcript": "Sunday dinner at Mom's. Maya told everyone she's decided to move in the spring. Mom cried, the good kind. Omar came by afterwards and we talked about finally taking the trip to Lisbon we've been postponing for two years. I said yes before I could think of a reason not to.",
        "summary": "Family dinner where Maya announced her move, and a decision with Omar to finally plan the Lisbon trip.",
        "mood": {"score": 5, "label": "warm and excited", "emotions": ["happy", "excited", "grateful"]},
        "todos": [{"text": "Look at flight prices to Lisbon", "due": -9, "done": True}],
        "topics": ["family", "travel"], "people": ["Maya", "Omar"], "places": ["Mom's", "Lisbon"],
    },
    {
        "days_ago": 10, "at": "22:15",
        "transcript": "Couldn't focus today. Nothing went wrong, I just felt flat. I ran in the evening and it helped a bit. I think I've been drinking too much coffee and not enough water, which sounds ridiculous to say out loud but it's probably true.",
        "summary": "A flat, unfocused day that an evening run improved slightly.",
        "mood": {"score": 3, "label": "flat but steady", "emotions": ["flat", "tired"]},
        "todos": [{"text": "Drink more water during the day", "due": None, "done": False}],
        "topics": ["health", "running"], "people": [], "places": [],
    },
    {
        "days_ago": 8, "at": "19:00",
        "transcript": "We won the client. Daniel told the whole team it was because of the proposal and I went red. He wants me to lead the kickoff, which is exciting and also terrifying because I've never run one. I called Maya on the way home and she told me to stop being modest.",
        "summary": "The team won the client and Daniel asked for the kickoff to be led; Maya was encouraging.",
        "mood": {"score": 5, "label": "excited and nervous", "emotions": ["excited", "nervous", "proud"]},
        "todos": [{"text": "Draft the kickoff agenda", "due": -5, "done": True}],
        "topics": ["work", "family"], "people": ["Daniel", "Maya"], "places": [],
    },
    {
        "days_ago": 6, "at": "21:45",
        "transcript": "Heater in the flat stopped working again. Third time this winter. I'm annoyed mostly at myself for not chasing the landlord the last two times. Also my rent went up and I haven't looked properly at what I'm spending. I should sit down with it this weekend.",
        "summary": "A broken heater, a rent increase, and a plan to review spending.",
        "mood": {"score": 2, "label": "annoyed and a bit worried", "emotions": ["annoyed", "worried"]},
        "todos": [
            {"text": "Call the landlord about the heater", "due": None, "done": False},
            {"text": "Review monthly spending", "due": 4, "done": False},
        ],
        "topics": ["home", "money"], "people": [], "places": [],
    },
    {
        "days_ago": 4, "at": "20:10",
        "transcript": "Ran five kilometres without stopping, first time since last year. Omar sent me three options for Lisbon and I like the one in the old town. We need to book flights before prices go up. Slept eight hours last night and I can tell.",
        "summary": "A first unbroken five-kilometre run in a year, Lisbon options from Omar, and a good night's sleep.",
        "mood": {"score": 4, "label": "energised and content", "emotions": ["energised", "content"]},
        "todos": [{"text": "Book flights to Lisbon", "due": 3, "done": False}],
        "topics": ["running", "travel", "sleep"], "people": ["Omar"], "places": ["Lisbon"],
    },
    {
        "days_ago": 2, "at": "18:50",
        "transcript": "Kickoff prep is going well. I have a meeting with Daniel tomorrow at ten to go through the agenda. I'm less scared of it than I was. Maya wants to have dinner on Friday at half past six to show me photos of the flat she's looking at.",
        "summary": "Kickoff preparation is on track, with an agenda review with Daniel and a dinner with Maya planned.",
        "mood": {"score": 4, "label": "confident and calm", "emotions": ["confident", "calm"]},
        "todos": [
            {"text": "Meeting with Daniel", "due": 1, "time": "10:00", "done": False},
            {"text": "Dinner with Maya", "due": 5, "time": "18:30", "done": False},
        ],
        "topics": ["work", "family"], "people": ["Daniel", "Maya"], "places": [],
    },
    {
        "days_ago": 1, "at": "22:00",
        "transcript": "Quiet evening. Went to the gym, cooked properly for once, read for an hour. My membership runs out soon and I should renew it before I find an excuse not to. Nothing dramatic today and I think that's what I wanted.",
        "summary": "A quiet, settled evening with a workout, cooking and reading.",
        "mood": {"score": 4, "label": "settled and content", "emotions": ["content", "calm"]},
        "todos": [{"text": "Renew gym membership", "due": 8, "done": False}],
        "topics": ["health", "home"], "people": [], "places": ["the gym"],
    },
]


def _local(day: datetime, hhmm: str, tz) -> datetime:
    """`day`'s date at a local clock time, as a UTC instant."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    return local.astimezone(timezone.utc)


async def _delete_account(user) -> None:
    """Same order as DELETE /auth/me: recordings first (the database cascade
    cannot reach storage), then the user row, which cascades everything else."""
    entries = await db.entry.find_many(where={"userId": user.id})
    for entry in entries:
        if entry.audioUrl:
            await storage.delete_audio(entry.audioUrl)
    await db.user.delete(where={"id": user.id})
    print(f"Deleted the existing account and its {len(entries)} entries.")


async def _seed_journal(user_id: str, tz) -> tuple[int, int]:
    today = datetime.now(tz)
    todo_count = 0
    for spec in ENTRIES:
        created = _local(today - timedelta(days=spec["days_ago"]), spec["at"], tz)
        entry = await db.entry.create(
            data={
                "userId": user_id,
                "source": "TEXT",
                "rawTranscript": spec["transcript"],
                "summary": spec["summary"],
                "moodScore": spec["mood"]["score"],
                "moodLabel": spec["mood"]["label"],
                "emotions": spec["mood"]["emotions"],
                "status": "DONE",
                "createdAt": created,
                "processedAt": created + timedelta(seconds=4),
            }
        )
        for todo in spec["todos"]:
            due_day = None if todo["due"] is None else today + timedelta(days=todo["due"])
            await db.todo.create(
                data={
                    "entryId": entry.id,
                    "userId": user_id,
                    "text": todo["text"],
                    # The DAY, stored as midnight UTC — exactly what the app's own
                    # pipeline stores for a spoken day.
                    "dueDate": (
                        None
                        if due_day is None
                        else datetime(due_day.year, due_day.month, due_day.day, tzinfo=timezone.utc)
                    ),
                    "startAt": (
                        _local(due_day, todo["time"], tz)
                        if due_day is not None and todo.get("time")
                        else None
                    ),
                    "done": todo["done"],
                    "createdAt": created,
                }
            )
            todo_count += 1
        # The app's own linkers, so names are normalised and de-duplicated the
        # same way a real entry's would be.
        for name in spec["topics"]:
            await link_topic(db, user_id, entry.id, name)
        for name in spec["people"]:
            await link_person(db, user_id, entry.id, name)
        for name in spec["places"]:
            await link_place(db, user_id, entry.id, name)
    return len(ENTRIES), todo_count


async def main() -> int:
    parser = argparse.ArgumentParser(description="Create the App Review account.")
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete the account named by --email first, then recreate it",
    )
    args = parser.parse_args()
    email = args.email.strip().lower()

    if email == DEMO_USER_EMAIL:
        print("Refusing: that is the demo account. Use scripts/remove_demo_account.py.")
        return 2

    password = os.environ.get("REVIEW_PASSWORD") or generate_password()
    if len(password) < MIN_PASSWORD_LEN:
        print(f"Refusing: the password must be at least {MIN_PASSWORD_LEN} characters.")
        return 2

    await db.connect()
    try:
        existing = await db.user.find_unique(where={"email": email})
        if existing is not None:
            if not args.reset:
                print(f"{email} already exists. Nothing was changed.")
                print("Run again with --reset to delete it and start over.")
                return 1
            await _delete_account(existing)

        tz = ZoneInfo(TIMEZONE)
        user = await db.user.create(
            data={
                "email": email,
                "passwordHash": hash_password(password),
                "timezone": TIMEZONE,
                # Dated to the first entry, so "Joined" agrees with the journal.
                "createdAt": _local(
                    datetime.now(tz) - timedelta(days=ENTRIES[0]["days_ago"]),
                    "09:00",
                    tz,
                ),
            }
        )
        entries, todos = await _seed_journal(user.id, tz)

        print()
        print("App Review account ready.")
        print(f"  email:    {email}")
        print(f"  password: {password}")
        print(f"  journal:  {entries} entries, {todos} tasks")
        print()
        print("This password is shown once and stored nowhere else.")
        return 0
    finally:
        await db.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
