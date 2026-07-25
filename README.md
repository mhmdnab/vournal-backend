# Voice Journal Backend

Backend for a self-structuring voice journal. A user speaks (or types) freely;
the app transcribes it and auto-extracts **todos, mood, topics, and people**,
then surfaces **trends over time**.

Two ways in:
- **Text** — `POST /entries` with a transcript (processed synchronously).
- **Audio** — `POST /entries/audio` with a recording; transcribed via
  speech-to-text, then run through the same extraction pipeline in the
  background. The upload returns immediately and the client polls for the result.

## Stack

- Python 3.11+, FastAPI, async throughout
- Prisma (`prisma-client-py`, asyncio interface) + PostgreSQL
- Anthropic API (`AsyncAnthropic`) for extraction
- Speech-to-text via **Speechmatics Melia** (default), or Groq
  `whisper-large-v3-turbo` / OpenAI Whisper, behind a provider seam
  (`app/transcription.py`)
- Audio entries use **merged extraction** (`app/extraction_merged.py`): one Sonnet
  call that fixes dialect mistranscriptions and extracts in a single pass

## Prerequisites

- Python 3.11 or newer
- A running PostgreSQL database
- An Anthropic API key (extraction)
- A Groq API key (speech-to-text; free tier at https://console.groq.com/keys) —
  or an OpenAI key if you set `STT_PROVIDER=openai`

## Setup

```bash
# 1. Create and activate a virtual environment (Python 3.11+)
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# then edit .env and set:
#   DATABASE_URL, ANTHROPIC_API_KEY, SPEECHMATICS_API_KEY
#   (optional) SPEECHMATICS_REGION, EXTRACTION_MODEL, MERGED_MODEL, STT_PROVIDER

# 4. Generate the Prisma client and apply migrations (see "Database migrations")
prisma generate
prisma migrate deploy

# 5. Run the server
uvicorn app.main:app --reload
```

## Database migrations

Production uses **Supabase Postgres** with a real migration history (`prisma/migrations/`)
— `db push` is no longer used. Two connection strings (both in `.env`):

- `DATABASE_URL` — the Supabase **pooled** URL (Supavisor transaction mode, port 6543,
  with `?pgbouncer=true`). The app uses this at runtime.
- `DIRECT_URL` — a **direct/session** URL (port 5432). Prisma uses this for migrations.
  On an IPv4-only network use the Supabase **Session pooler** string (the direct
  `db.<ref>.supabase.co` host is IPv6-only).

Workflow for a schema change:

```bash
# create + apply a migration against your dev database, and regenerate the client
PATH="$(pwd)/.venv/bin:$PATH" python -m prisma migrate dev --name <change_name>

# apply pending migrations to production (Supabase) — uses DIRECT_URL
PATH="$(pwd)/.venv/bin:$PATH" python -m prisma migrate deploy

# check state
python -m prisma migrate status
```

The initial `0_init` migration was baselined from the pre-migration schema
(`migrate diff --from-empty` → `migrate resolve --applied 0_init`).

## Configuration

All secrets come from environment variables (read from `.env` locally):

| Variable            | Required | Description                                                                 |
| ------------------- | -------- | --------------------------------------------------------------------------- |
| `DATABASE_URL`      | yes      | Postgres connection — the Supabase **pooled** URL (port 6543, `?pgbouncer=true`). |
| `DIRECT_URL`        | yes      | Direct/session Postgres URL (port 5432) — Prisma migrations use this.        |
| `ANTHROPIC_API_KEY` | yes      | Anthropic API key for extraction.                                           |
| `EXTRACTION_MODEL`  | no       | Text-entry extraction model. Defaults to `claude-haiku-4-5-20251001`.       |
| `MERGED_MODEL`      | no       | Audio-entry merged repair+extraction model. Defaults to `claude-sonnet-5`.  |
| `STT_PROVIDER`      | no       | `speechmatics` (**default** — Melia), `groq`, or `openai`.                  |
| `STT_MODEL`         | no       | Whisper-only override; ignored for speechmatics (always Melia `melia-1`).   |
| `SPEECHMATICS_API_KEY` | yes\* | STT key. \*Required when `STT_PROVIDER=speechmatics` (the default).          |
| `SPEECHMATICS_REGION`  | no    | `eu1` (default) or `us1` — the region your account/jobs live in.            |
| `GROQ_API_KEY`      | yes\*    | STT key. \*Required when `STT_PROVIDER=groq`.                                |
| `OPENAI_API_KEY`    | yes\*    | STT key. \*Required when `STT_PROVIDER=openai`.                              |
| `STT_PROMPT`        | no       | Whisper primer (Groq/OpenAI only; Melia has no primer). Defaults to `RECOMMENDED_LEBANESE_PROMPT`. |
| `REPAIR_MODEL` / `REPAIR_ENABLED` | no | Standalone repair pass (`app/repair.py`). No longer in the production audio path (merged extraction absorbed it) — only used by the benchmark harnesses. |

## API

### Health

```bash
curl http://localhost:8000/health
# -> {"ok":true}
```

### Text entry (synchronous)

```bash
curl -s -X POST http://localhost:8000/entries \
  -H "Content-Type: application/json" \
  -d '{"transcript":"I need to call the dentist tomorrow and email Sarah about the lease."}'
# -> a fully processed entry (status DONE) with mood, todos, topics, people
```

### Audio entry (async: upload → poll)

Upload a recording (`m4a` is primary; `mp3/wav/webm/mp4/mpga/mpeg/flac/ogg` also
accepted, max 25 MB). The response is a **PENDING** entry — transcription and
extraction run in the background.

```bash
# 1. Upload — returns instantly with status PENDING and an id
curl -s -X POST http://localhost:8000/entries/audio \
  -F "file=@recording.m4a"
# optional fields: -F "userId=<id>"  -F "durationSec=42"

# 2. Poll until DONE (or FAILED)
curl -s http://localhost:8000/entries/<ENTRY_ID>
```

**Polling contract:** the upload returns an entry with `status: "PENDING"` and an
`id`. Poll `GET /entries/{id}` until `status` is:
- `DONE` — `rawTranscript`, `mood`, `todos`, `topics`, `people` are populated;
- `FAILED` — `error` explains what went wrong (a failed transcription never
  leaves an entry stuck in `PROCESSING`).

Status flow: `PENDING → PROCESSING → DONE | FAILED`.

### Other routes

```bash
curl -s "http://localhost:8000/entries?limit=5"            # recent entries, newest first
curl -s http://localhost:8000/entries/<ENTRY_ID>           # one entry (404 if missing)
curl -s http://localhost:8000/entries/<ENTRY_ID>/audio     # stream the recording back
curl -s -X PATCH http://localhost:8000/todos/<TODO_ID> \
  -H "Content-Type: application/json" -d '{"done":true}'   # toggle a todo
curl -s "http://localhost:8000/insights?days=14"           # mood trend, top topics/people, open todos
```

## Speech-to-text notes (Step 10 pipeline)

- **Provider seam:** `transcribe()` in `app/transcription.py`. Default is
  **Speechmatics Melia** (`melia-1`), a batch API (submit → poll → fetch). Groq
  `whisper-large-v3-turbo` and OpenAI Whisper stay reachable via `STT_PROVIDER`;
  they run through the `openai` SDK, Melia through its own branch.
- **Why Melia:** it does **native single-pass code-switching** — mixed
  English+Lebanese in one utterance ("بكرا I wanna go to the طبيب الأسنان") comes
  back verbatim with **no primer**. Whisper can only fake this with the blunt
  Lebanese primer, which sometimes drags pure-Arabic entries toward English.
- **Language auto-detect:** Melia runs with `language: "multi"`; nothing is forced.
- **Merged repair + extraction (audio only).** Melia still mishears some dialect
  words — `جدّد` ("renew") → `جدة` (Jeddah) — so audio entries go through
  `app/extraction_merged.py`: **one Sonnet call** that silently corrects such
  mistranscriptions *and* extracts, replacing the old two-call
  repair→extract. It caught `جدة` → *"renew passport"* end-to-end in testing.
  Thinking is disabled (Sonnet's reasoning tokens bill as output and aren't needed
  for the fix). Two guarantees hold: **`rawTranscript` is stored exactly as
  heard** (the correction happens only inside extraction — the journal never
  rewrites your words), and the extraction layer **never raises** on a malformed
  response. **Text entries skip this** and use plain `EXTRACTION_MODEL` (Haiku) —
  typed words have no mistranscriptions to fix.
- **Per-entry cost (~2-min entry):** Melia STT ~$0.004 (free within Melia's
  monthly batch hours) + merged extraction ~$0.008 (Sonnet 5, thinking off) ≈
  **$0.008–0.012**. The merge itself cut the LLM cost ~22% vs the old
  repair→extract; net vs the old Groq pipeline is cheaper within Melia's free tier
  and roughly neutral beyond it, with better code-switch fidelity and one fewer
  model call. Benchmarks: `benchmark_stt.py`, `benchmark_repair.py`,
  `test_merged_extraction.py`.
- **Measuring changes:** `python test_dialect_grid.py` runs every clip in
  `samples/` through a grid of model × primer combinations and prints the
  transcripts side by side. Re-run it after touching the primer — prompt edits
  are not intuitively safe (see the ~224-token caution in `transcription.py`).
- **Limits:** 25 MB per file; longer-audio chunking is not implemented yet.

## Audio storage

Dev stores uploads on local disk under `uploads/audio/{entry_id}.{ext}` (see
`app/storage.py`, gitignored). Moving to Supabase Storage / S3 is a change in
that one module — the callers keep using `save_audio()` / `resolve_audio_path()`.

`Entry.audioUrl` is a **storage reference, not a fetchable URL** (in dev it's a
local path). Clients play recordings via `GET /entries/{id}/audio`, which
resolves the reference through `resolve_playable_audio()` and streams the file.
Serving by entry id rather than mounting `uploads/` keeps the seam intact: a
cloud backend presigns in that function and this route redirects.

## CORS

`http://localhost:3000` and `http://127.0.0.1:3000` are allowed so the web
frontend in `frontend-web-app/` can call the API from the browser. Add deployed
origins to the list in `app/main.py`.

## Usage limits & rate limiting (Step 11)

Every audio entry costs real money (STT + a Sonnet extraction call) and the
endpoint is public, so cost is bounded on several axes (`app/quota.py`,
`app/rate_limit.py`) — all **fail closed**, all returning a structured
`{"detail": {"code": ...}}` the clients render calmly:

- **Per-user monthly entries** — `BETA_MONTHLY_ENTRY_LIMIT` (60). Checked *before*
  any provider call; over-limit → `429 MONTHLY_LIMIT_REACHED` with `limit`/`used`/
  `resetAt`. Rolls at the calendar month (`User.usagePeriodStart/Entries`). The
  tier seam (`user_monthly_limit`) is wired for a future paid tier (`FAIR_USE`).
- **Max recording length** — `MAX_RECORDING_SECONDS` (600). Over-length →
  `413 RECORDING_TOO_LONG`, rejected before upload/STT.
- **Global kill switch** — `GLOBAL_DAILY_AUDIO_CAP` (500). Whole-app daily audio
  cap; exceeded → `503 SERVICE_BUSY`, logged loudly.
- **Per-entry usage recorded** — `Entry.sttSeconds`, `llmInTokens`, `llmOutTokens`
  turn unit-economics estimates into data.
- **Rate limiting** (per IP, in-memory — resets on deploy): a 120/min global
  ceiling; `login` 5/15 min (per IP **and** per email); `register` 3/hour.

## Project layout

```
prisma/schema.prisma   # data model (designed for trend aggregation)
app/main.py            # FastAPI app + routes
app/db.py              # Prisma client lifecycle + demo-user helper
app/extraction.py      # extraction prompt + Claude call + defensive parsing
app/transcription.py   # speech-to-text seam (Speechmatics Melia default / Groq / OpenAI)
app/extraction_merged.py # audio-only: merged dialect-repair + extraction (one call)
app/storage.py         # audio storage seam (local disk for dev)
app/schemas.py         # Pydantic request/response models
test_extraction.py     # standalone extraction test (no DB/server)
test_transcription.py  # standalone transcription test over samples/ (no DB/server)
uploads/audio/         # saved recordings (gitignored)
samples/               # local audio for the transcription test (gitignored)
```
