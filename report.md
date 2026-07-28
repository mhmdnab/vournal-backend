# Vournal — Build Report

**Date:** 2026-07-20 (mobile Step 4 + auth/delete Step 5 added 2026-07-22;
Step 8 — time extraction + to-do list — added 2026-07-23; Step 9 — timezone +
calendar — and Step 10 — STT/pipeline — added 2026-07-24; Step 11 — production
hardening + security audit — added 2026-07-25)
**Product name:** **Vournal** (Voice + Journal). Earlier builds used the working
name "Murmur"; see `../PRODUCT.md` for the product overview and direction.
**Scope:** Backend (schema, extraction, entries API, insights, **auth + delete**,
**time extraction + a to-do list endpoint**, **per-user timezone**, **usage
limits + rate limiting**, **password reset**), audio ingestion + speech-to-text
now on **Supabase Storage (private + signed URLs)**, a **Supabase Postgres** prod
DB with real migrations, the web frontend (full loop + **to-do list + calendar
views**), **and the mobile app (Expo/React Native) — at web parity, incl. to-do
list + calendar.** Still out of scope: social login, a real job queue/worker,
long-audio chunking, syncing to a real Apple/Google calendar, and the actual
**deploy to Render/Vercel/TestFlight** (Phase 6 — pending, prep done).
**Outcome:** Complete and verified end-to-end on **web and iOS**. Both clients
cover the whole loop under real accounts — capture, timeline, entry detail
(todo toggles + playback + delete), insights, a **to-do list grouped by when**,
and a **calendar** (all three of PRODUCT.md's payoffs are live). Spoken times are
composed against the **user's timezone**. STT default is **Speechmatics Melia**
(native code-switching); audio extraction is a single **merged repair+extraction**
call. As of Step 11 the app is **production-hardened** — private audio storage
behind signed URLs, Supabase Postgres with migrations, per-user cost caps + rate
limiting, in-app account deletion, password reset, and a verified security-audit
pass — and is ready for the deploy step.

This report covers the build steps below (the step numbers are the developer's;
there is no separate Step 7 section):
- **Step 1 — Backend** (text in → extraction → storage → insights). Complete.
- **Step 2 — Audio + Speech-to-Text** (audio in → transcript → the same pipeline). Complete.
- **Step 3 — Web frontend** (Next.js). All 4 phases complete; see §6.
- **Post-Step-3 — Lebanese dialect accuracy**; see §6b.
- **Step 4 — Mobile app** (Expo/React Native). Shell, nav, shared modules, journal; see §6c.
- **Step 5 — Auth + Delete.** All 4 phases complete (backend, web UI, mobile wiring); see §6d.
- **Step 6 — Finish the mobile app** (capture, detail + delete, insights → web parity); see §6e.
- **Step 8 — Time extraction + to-do list view.** All 3 phases complete (backend, web, mobile); see §6f.
- **Step 9 — Per-user timezone + calendar view** (web month grid, mobile week strip; nav → bottom tab bar); see §6g.
- **Step 10 — STT benchmark + pipeline cost optimization** (Melia default, merged repair+extraction); see §6h.
- **Step 11 — Production hardening + security audit** (Supabase Storage/DB, quotas + rate limiting, account deletion, password reset; audit remediation). Phases 1–5 done; Phase 6 (deploy) pending; see §6i.
- **Step 12 — Perceived speed** (client caching via TanStack Query, audio signed-URL prefetch, skeletons). All 4 phases done; measured ~66% cut in total waiting across a typical session; see §6j and PERF_RESULTS.md.

> This file started as the backend report and now covers the whole project. It
> would sit better at the `murmur/` (repo) root than inside
> `voice-journal-backend/`.

---

## 1. What this is

A self-structuring voice journal. A user speaks (or types) freely; the app transcribes and auto-extracts **todos, mood, topics, and people**, then surfaces **trends over time**. The recorder isn't the product — the extraction pipeline and the insights are.

Two ways in:
- **Text** — `POST /entries` with a transcript (processed synchronously).
- **Audio** — `POST /entries/audio` with a recording; transcribed via speech-to-text, then run through the same extraction pipeline in the background. The upload returns immediately and the client polls for the result.

## 2. Stack

- **Python 3.12**, **FastAPI**, async throughout
- **Prisma** (`prisma-client-py`, asyncio interface) + **PostgreSQL**
- **Anthropic API** via the official `anthropic` SDK (`AsyncAnthropic`) for extraction
- **Speech-to-text** via **Groq** (default, `whisper-large-v3-turbo`) or OpenAI Whisper, behind a provider seam
- Secrets come from environment variables only; `.env` is gitignored, `.env.example` has no real values.

Key package versions installed: `prisma 0.15.0`, `anthropic 0.117.0`, `fastapi 0.139.2`, `uvicorn 0.51.0`, `pydantic 2.13.4`, `openai 2.46.0`, `mutagen 1.48.1`, `python-multipart 0.0.32`.

## 3. Project layout

The repo root is `murmur/`, holding two projects:

```
murmur/
├── voice-journal-backend/   # FastAPI service (Steps 1–2)
└── frontend-web-app/        # Next.js web client (Step 3) — see §6
```

Backend (`voice-journal-backend/`):

```
prisma/schema.prisma   # data model (designed for trend aggregation)
app/__init__.py
app/main.py            # FastAPI app + all routes + shared/background processing
app/db.py              # Prisma client lifecycle + demo-user helper
app/extraction.py      # extraction prompt + Claude call + defensive parsing
app/transcription.py   # speech-to-text seam (Groq default / OpenAI)
app/repair.py          # context-aware transcript repair (audio path only)
app/storage.py         # audio storage seam (local disk for dev)
app/schemas.py         # Pydantic request/response models
test_extraction.py     # standalone extraction test (no DB/server)
test_transcription.py  # standalone transcription test over samples/ (no DB/server)
uploads/audio/         # saved recordings (gitignored)
samples/               # local audio for the transcription test (gitignored)
requirements.txt
.env.example
.gitignore
README.md
report.md              # this file
```

---

## 4. Step 1 — Backend, phase by phase

### Phase 1 — Scaffold, database, data model

- Wrote `prisma/schema.prisma` exactly as specified: `User`, `Entry`, `Todo`, `Topic`, `EntryTopic`, `Person`, `EntryPerson`, plus the `EntrySource` and `ProcessingStatus` enums. Topics and people are normalized **per user** (`@@unique([userId, name])`) so they can be counted across entries — this is what powers insights.
- `app/db.py`: one shared `Prisma()` client, `connect()`/`disconnect()` wired into the FastAPI lifespan, and an idempotent `get_demo_user_id()` that get-or-creates a fixed demo user (`demo@voicejournal.local`) so the API is testable without auth.
- `app/main.py`: `GET /health` → `{"ok": true}`.
- Ran `prisma generate` and `prisma db push`.

**Verified:** server starts clean (`Application startup complete`), `GET /health` → `{"ok":true}` (200), all 7 tables + 2 enums created in Postgres, and `get_demo_user_id()` returns a stable id across calls (single `User` row).

### Phase 2 — Extraction engine (the brain)

- `app/extraction.py`: `async extract(transcript, today=None)`.
  - `AsyncAnthropic`; model from `EXTRACTION_MODEL`, default `claude-haiku-4-5-20251001` (fast + cheap for high-volume extraction; bump to `claude-sonnet-5` for quality); `max_tokens=1024`.
  - The product's system prompt is included **verbatim** (understands English, Arabic, and Lebanese Arabizi; returns strict JSON with `summary`, `mood{score,label,emotions}`, `todos[{text,dueDate}]`, `topics[]`, `people[]`).
  - The user message includes today's date (for resolving relative due dates) and the transcript, in the exact specified format.
  - **Defensive parsing:** strips code fences → `json.loads` → falls back to the outermost `{...}` span. Then normalizes: clamps `mood.score` to 1–5 (default 3), coerces lists to clean string lists, drops empty todos, caps emotions at 4 and topics at 6, de-dupes case-insensitively. A malformed model response can never crash the caller; a blank transcript short-circuits without an API call.
- `test_extraction.py`: standalone (no DB/server), runs four samples — an English ramble with two todos + a "tomorrow" date, a low-mood entry, a **Lebanese Arabizi** entry, and a mixed English+Arabizi entry — and pretty-prints the JSON.

**Verified (live against the Anthropic API):**

- English ramble → 2 todos, `"tomorrow"` resolved to `2026-07-21`, mood 4, topics `work/health/housing`, person `Sarah`; correctly did **not** turn the past event ("shipped the flow") into a todo.
- Low-mood → mood 1, emotions `tired/frustrated/overwhelmed/irritable`, zero invented todos.
- Arabizi → understood fully ("ta3ban", "ma nemet mnee7", "5allaset el taqrir"), todos rewritten as clean English imperatives, person `Karim`.
- Mixed → `"next month"` resolved to `2026-08-20`, person `mama`.
- Defensive layer separately proven offline: fenced JSON stripped, non-integer score defaulted to 3, over-cap emotion list capped/lowercased, empty todo dropped, prose-wrapped JSON recovered, pure garbage returned the safe empty shape, out-of-range score clamped.

**Observation (not changed):** for Arabizi/mixed entries the model wrote English summaries even though the prompt asks for same-language summaries. Left as-is since the prompt is verbatim; easy to tune later if native-language summaries are wanted.

### Phase 3 — Entries API (extraction → storage)

- `app/schemas.py`: `CreateEntryRequest`, `UpdateTodoRequest`, and response models `EntryOut` / `Mood` / `TodoOut`.
- `app/main.py` routes:
  - `POST /entries` — body `{ transcript, userId?, source?, audioUrl?, durationSec? }`; `userId` falls back to the demo user. Flow: create the entry as `PROCESSING` → run extraction → update with summary/mood/emotions and `DONE` (on failure: `FAILED` + stored error, entry still returned). Then persist todos (parsing `dueDate` strings to datetimes, tolerating bad values), upsert topics by `userId_name` and link via `EntryTopic`, upsert people and link via `EntryPerson`. Returns the fully assembled entry (mood object, emotions, topics as names, people as names, todos).
  - `GET /entries?userId=&limit=` — recent entries, newest first, with relations.
  - `GET /entries/{id}` — one entry, 404 if missing.
  - `PATCH /todos/{id}` — body `{ done: bool }`, sets the todo's done state.

> In Step 2 this extract→persist logic was refactored into a shared `process_transcript()` helper (see §5) with no behavior change to the text path.

**Verified with curl:** `POST` returned a `DONE` entry with two todos (`"tomorrow"` → `2026-07-21T00:00:00Z`), mood object, topics, and person. `GET /entries` returned entries newest-first. `PATCH /todos/{id}` flipped a todo and a follow-up `GET` reflected it. Missing entry/todo → 404. In the DB, topics were **upserted, not duplicated** — after two entries, `work` and `health` each had one `Topic` row linked from both entries.

### Phase 4 — Insights (the retention hook)

- `app/schemas.py`: `InsightsOut` with `MoodTrendPoint` and `NameCount`.
- `app/main.py`: `GET /insights?userId=&days=14` — fetches the user's entries in the window (`createdAt >= now - days`) with relations and aggregates in Python:
  - `moodTrend` — average `moodScore` per UTC day over scored entries: `{date, avgScore, count}`.
  - `topTopics` / `topPeople` — occurrence counts across the window, sorted by count desc (name asc on ties).
  - `openTodos` — a DB count of the user's `done = false` todos (all-time, not windowed).
  - Plus `entryCount` and echoed `days`.

**Verified (4 entries):** `entryCount 4`; `topTopics` led by `health` (3) and `work` (3); `topPeople` Karim/Sarah/mama; `openTodos 5` (correctly reflecting the one todo marked done). The window is genuinely applied: `days=14 → 4 entries`, `days=0 → 0 entries`.

---

## 5. Step 2 — Audio + Speech-to-Text

Adds the missing core loop — **audio in → transcript → the existing extraction pipeline** — with **no schema change** (reused `Entry.audioUrl`, `Entry.durationSec`, `Entry.source=VOICE`, and the `ProcessingStatus` enum). The text path is unchanged.

### Phase 1 — Transcription engine + quality gate (risk retirement)

- `app/transcription.py`: `async transcribe(audio_path, prompt=None) -> {"text", "language", "durationSec"}` behind a thin provider seam.
  - **Default provider: Groq `whisper-large-v3-turbo`**, chosen after a cost/quality comparison — the same Whisper family as OpenAI but a stronger multilingual model, and ~9× cheaper (~$0.04/hr vs $0.36/hr). OpenAI Whisper is the alternate provider. Both run through the same `openai` SDK because Groq is OpenAI-compatible (base-URL override), so providers swap here, not in callers.
  - Model/provider from env (`STT_PROVIDER` / `STT_MODEL`). Language is **auto-detected** (not forced), so English / Arabic / code-switched all work.
  - Uses `verbose_json` for language + duration on Whisper models; falls back to a local `mutagen` probe for duration otherwise.
  - Validates format (m4a primary; mp3/wav/webm/mp4/mpga/mpeg/flac/ogg) and rejects files over 25 MB (chunking is a later step).
- `test_transcription.py`: standalone (no DB/server) — transcribes everything in `samples/` and prints language, duration, and transcript.

**Quality gate (real recordings, via Groq — this was the make-or-break test):**
- **English** → flawless.
- **Lebanese Arabic** → strong. A clean "dentist" clip transcribed correctly and went end-to-end to the right todo. One hard dialectal verb (`جدّد` "renew" → `جدة`) was mistranscribed on a terse 6-word clip and propagated to a wrong "go to Jeddah" todo — a word-level edge case (a second Lebanese clip was clean), and both `whisper-large-v3-turbo` and `whisper-large-v3` failed it identically, so model choice doesn't fix it.
- **Code-switching (English + Arabic in one sentence)** → confirmed limitation: Whisper **normalizes mixed speech toward one language** ("بكرا i wanna go to the طبيب الأسنان" → MSA Arabic, English dropped). A `prompt` primer can recover verbatim mixing, but it's a blunt lever that pulls some *pure-Arabic* utterances into broken English — so it's **opt-in** (`STT_PROMPT`), off by default. Crucially, the **meaning survives** normalization, so the extractor still produces the correct todo.

**Decision at the time:** ship with **no primer by default** (safest: clean English + Arabic; code-switch degrades to correct meaning), with `RECOMMENDED_CODE_SWITCH_PROMPT` available opt-in via `STT_PROMPT`.

> **⚠️ SUPERSEDED — see §6b.** Real use showed the `جدّد` → `جدة` class of error was
> common enough to matter. A *Lebanese* primer (dialect orthography + a bilingual
> example, rather than the English-only wording used here) fixes it without the
> pure-Arabic drift that made the original primer risky, so a primer is now **on
> by default**. The conclusion above that "model choice doesn't fix it" held up
> and was confirmed by measurement — the fix came from the primer and a repair
> pass, not a bigger transcriber.

### Phase 2 — Audio ingestion endpoint + async processing

- **Refactor (no behavior change):** the extraction→persist→`DONE`/`FAILED` logic moved out of the text endpoint into a shared **`process_transcript(entry_id, transcript)`**. The text endpoint now creates the entry and calls it (still synchronous, identical output).
- **`app/storage.py`** — audio storage seam. `save_audio()` writes `uploads/audio/{entry_id}.{ext}` and returns the ref stored on `Entry.audioUrl`; `resolve_audio_path()` reads it back. Cloud (S3/Supabase) swap happens here only.
- **`POST /entries/audio`** (multipart: `file`, optional `userId`, `durationSec`) — validates format/size, creates a `PENDING` entry (`source=VOICE`), saves the file named by entry id, schedules a **BackgroundTask**, and returns the PENDING entry **immediately**.
- **`process_audio_entry(entry_id)`** (background) — `PENDING → PROCESSING → transcribe() → save rawTranscript (+ fill durationSec) → process_transcript() → DONE`, or `FAILED` + stored error on any failure so nothing gets stuck. A code comment marks where a real queue/worker replaces BackgroundTasks in production.

### Phase 3 — End-to-end verification & docs

- Updated `README.md` with the audio curl examples, the polling contract, the STT env vars, and speech-to-text / storage notes.
- **Verified:**
  - Text path unchanged: `POST /entries` → `DONE`, `source=TEXT`, todos with resolved dates, topics/people.
  - Audio path: `POST /entries/audio` (m4a) → `PENDING` instantly (`source=VOICE`, `audioUrl` set) → polling `GET /entries/{id}` went `PENDING → PROCESSING → DONE`, ending with the Groq transcript, the right todo, topic `health`, and `durationSec` auto-filled from transcription.
  - Full end-to-end on a real **code-switched** recording: transcript normalized to MSA in `rawTranscript` (as documented) but still produced the correct todo ("Go to the dentist," due tomorrow, `health`) — graceful degradation confirmed.
  - Guards: unsupported format (`.txt`) → `400` (no entry created).

**Polling contract:** upload to `/entries/audio` → get an entry with `status: "PENDING"` and an `id` → poll `GET /entries/{id}` until `status` is `DONE` (transcript + mood + todos + topics + people populated) or `FAILED` (`error` populated). Status flow: `PENDING → PROCESSING → DONE | FAILED`. No new polling endpoint.

---

## 6. Step 3 — Web frontend (Next.js)

Web before React Native on purpose: fastest way to validate the whole loop, and
the flows port cleanly to Expo afterwards. API calls, types, and view helpers are
kept in framework-agnostic modules so the RN port reuses them unchanged.

**Status: all 4 phases complete.** Capture, timeline, entry detail, and insights
all work end to end. Every phase was gated on `tsc --noEmit`, `eslint`, and
`next build` all passing.

### Stack

Next.js **16.2.10** (App Router, Turbopack) · React 19.2 · TypeScript · Tailwind
v4 · `next/font` (Source Serif 4 + Inter). API base URL from
`NEXT_PUBLIC_API_BASE_URL`, defaulting to `http://127.0.0.1:8000`. No secrets in
the frontend.

Everything under `src/lib/` is **framework-free** — no React, no DOM — and moves
to React Native unchanged. That boundary is the point of the whole file layout.

```
src/lib/types.ts        # mirrors app/schemas.py exactly
src/lib/api.ts          # typed client for every endpoint + pollEntry() — no React imports
src/lib/mood.ts         # the 1–5 mood scale as plain data
src/lib/format.ts       # elapsed time, relative dates, due dates
src/lib/entryView.ts    # headline fallbacks, meta parts, optimistic todo helper
src/lib/chart.ts        # mood-trend SVG geometry (react-native-svg takes the same paths)
src/lib/recording.ts    # MediaRecorder format negotiation (browser-only)

src/hooks/useRecorder.ts  # the only browser-specific piece (RN swaps this file)
src/hooks/useCapture.ts   # idle → uploading → processing → done
src/hooks/useEntries.ts   # timeline load + reload
src/hooks/useEntry.ts     # one entry: load, poll if unfinished, todo toggle
src/hooks/useInsights.ts  # insights by window

src/components/Capture.tsx        # the hero screen
src/components/EntryStructure.tsx # structure-first entry rendering (shared)
src/components/EntryDetail.tsx    # detail page: playback, timestamp, live todos
src/components/EntryRow.tsx       # one timeline row
src/components/MoodTrend.tsx      # hand-rolled SVG chart
src/components/MoodDot.tsx
src/components/SiteNav.tsx        # the only chrome

src/app/page.tsx            # / — capture
src/app/journal/page.tsx    # /journal — timeline
src/app/entries/[id]/page.tsx  # /entries/[id] — detail (await params)
src/app/insights/page.tsx   # /insights
src/app/{layout.tsx,globals.css}
```

### Two backend changes (the only ones)

1. **CORS** (`app/main.py`) — `CORSMiddleware` allowing `http://localhost:3000`
   and `http://127.0.0.1:3000`. The browser cannot call the API without it.
2. **`GET /entries/{id}/audio`** (`app/main.py` + `resolve_playable_audio()` in
   `app/storage.py`) — **`Entry.audioUrl` is a storage reference, not a fetchable
   URL** (in dev, a local filesystem path), and nothing served it, so `<audio>`
   playback was impossible. This route resolves the reference and streams the
   file. Serving **by entry id** rather than mounting `uploads/` keeps the storage
   seam intact: a cloud backend presigns inside `resolve_playable_audio()` and
   this route redirects. It refuses anything resolving outside `AUDIO_DIR`, and
   overrides `mimetypes` (which reports `video/webm` for these containers) so the
   response is a genuine `audio/*` type.

### Contract corrections found by reading the backend

The frontend was typed against `app/schemas.py`, not against assumptions. Four
differences from the working spec, all resolved in favour of the code:

- **`mood` is nested** — `mood: { score, label, emotions }`. There is no
  top-level `moodScore` / `moodLabel` / `emotions`.
- `EntryOut` also carries `userId`, `audioUrl`, `durationSec`, `processedAt`, and
  `error` (the last is the FAILED-state message, which the UI needs).
  `InsightsOut` also returns `userId`.
- **Audio format is validated by filename extension, not mime type**
  (`main.py`), so the client maps the recorder's mime to an accepted extension
  via `extensionForMimeType()`. `webm` (Chrome/Firefox) and `mp4` (Safari) are
  both accepted.
- `audioUrl` is not fetchable — see the backend change above.

### Phase 1 — Scaffold, design system, API client

- Design direction **"Quiet"**: paper `#FAF8F3`, ink `#2B2926`, hairline borders
  `#EAE3D6`, 14px radii, **no shadows or gradients**. Serif carries the user's
  words; sans carries UI chrome. Deliberately **no dark mode** — the paper
  palette is the design. Tokens live in `globals.css`; the mood scale is
  duplicated as data in `lib/mood.ts` for non-CSS consumers.
- `lib/api.ts` covers every endpoint plus `pollEntry()`, which retries transient
  failures, stops immediately on 404, and is abortable.

**Verified:** tokens rendered, and a browser-side `GET /entries` round-trip
through the client returned 5 real entries — done from a client component
specifically so it exercises CORS (a server-side fetch would prove nothing).

### Phase 2 — Capture flow (the hero screen)

- `useRecorder` wraps `MediaRecorder`; `stop()` resolves with the finished Blob.
  Format is negotiated at runtime via `isTypeSupported`, but the extension is
  taken from the **produced Blob's actual type**, because browsers advertise
  types they then refuse. Mic tracks are stopped on unmount so the browser's
  recording indicator never sticks.
- `useCapture` owns `idle → uploading → processing → done`. Both the audio and
  text paths converge on the same shape. A `FAILED` entry is treated as an error
  *state*, not an exception — the backend did its job and reported failure.
- `EntryStructure` renders **structure first**: mood, summary, todos, topics,
  people — with `rawTranscript` last, collapsed, and annotated to explain that
  mixed-language wording may drift while the extracted structure stays right.
- Text fallback (`POST /entries`) for mic-free use; ⌘/Ctrl+Enter submits.

**Verified in a real browser**, by driving Chrome with a real speech file piped
into the fake microphone — genuine `MediaRecorder` capture, multipart upload and
polling, not a simulation:

- **Audio path**, status progression observed rather than assumed:
  `POST /entries/audio → PENDING`, then `GET /entries/{id}` → `PROCESSING` →
  `PROCESSING` → `DONE`. Rendered mood `neutral`, summary "Tomorrow I have a
  meeting at 11am.", todo "Attend meeting at 11am · Jul 21", topic `work`.
- **Text path:** `POST /entries → DONE` synchronously; 2 todos, topics
  `work · travel`, person `Rana`.
- **Mic denial:** a blocked `getUserMedia` shows a recovery message and still
  offers "Type instead" — no dead end.
- **Audio playback** through the new route: `duration 7.02s`, playhead
  advancing, `paused: false`.
- `tsc --noEmit` clean, `eslint` clean, `next build` succeeds.

### Notes worth carrying forward

- `create-next-app` generated an `AGENTS.md` warning that this Next version has
  breaking changes. Relevant for the unbuilt phases: **`params` is a Promise**
  (`await props.params`), `next typegen` generates a `PageProps<'/route'>`
  helper, and **`next lint` is removed** — run `eslint` directly.
- Next 16 ships React Compiler lint rules. A `setState`-in-effect used to detect
  recording support was replaced with `useSyncExternalStore`, which also removes
  a real SSR/hydration mismatch (the server cannot probe `MediaRecorder`).

### Phase 3a — Navigation + timeline

- **`SiteNav`** — the only chrome in the app, deliberately underweight: small sans
  labels, muted until active, one hairline underneath, no fill or shadow. Capture
  is the product, so on `/` it should be easy to forget. Journal stays lit on
  `/entries/*` so entry detail doesn't orphan the nav.
- **`/journal`** — entries newest-first as hairline-separated rows (not cards; a
  journal should read as a continuous list). Each row: mood dot, serif summary,
  and a meta line of relative date · topics · open-todo count.
- **Row logic lives in `src/lib/entryView.ts`**, not the component, so it ports to
  RN. The headline degrades: summary → a status stand-in while `PENDING`/
  `PROCESSING` → a quiet line for `FAILED` → the transcript's opening. A row is
  never blank. Processing entries show a hollow ring rather than a mood dot —
  asserting a mood before extraction finishes would be a lie.
- **`dir="auto"` on all user text** (headline, summary, transcript, todos, topic
  names). Arabic entries were bidi-correct but forced into the page's LTR flow;
  they now align to their own language. Worth doing given how many entries are
  Arabic or mixed.

**Verified:** navigation by clicking (not URL) between capture / journal /
insights and back via the wordmark; 20 rows newest-first, checked against the
API's own `createdAt` ordering rather than trusting the backend's `order` clause.

**Bug found here, latent since Phase 1.** The timeline rendered *"Could not reach
the API. Is the backend running?"* against a healthy backend. `request()` in
`lib/api.ts` wrapped **every** fetch rejection in `NetworkError` — including
`AbortError`. A deliberate abort was therefore disguised as "the backend is
down", and callers check `instanceof DOMException` to stay silent, which they
could never see. React's dev double-effect aborts the first request, and that
fake error raced the successful second one; Phase 1's probe page had the same
race and simply won the coin flip. `AbortError` is now rethrown untouched. This
would have hit real users on any fast navigation away from a loading screen.

### Phase 3b — Entry detail, live todos, playback

- **`/entries/[id]`** — a server component that does `await params` (Next 16
  removed synchronous access) and hands the id to a client component.
- **`useEntry`** loads the entry and, if it isn't terminal, polls until it
  settles — so an entry opened straight after recording shows the same quiet
  "Thinking…" beat as capture instead of a half-empty page.
- **Todo toggle**: optimistic flip → `PATCH /todos/{id}` → on failure the flip is
  reverted *and* the user is told, because a checkbox that silently springs back
  is worse than an error. Both directions use the same pure helper
  (`withTodoDone`), so they can't drift.
- **Playback** via `GET /entries/{id}/audio` — never `entry.audioUrl`, which is a
  server-side storage reference.
- 404 renders as *"That entry doesn't exist any more."*, not a crash.

**Verified:** opened from the timeline by clicking; toggle went
`aria-pressed false → true` with `PATCH → 200`, survived a full page reload, and
the API confirmed `done=true`. Audio: `duration 11.52s`, playhead advancing
`0.05 → 0.31 → 0.58 → 0.84`. The first playback check reported `paused: false`
but `currentTime: 0` — consistent with playback silently failing — so it was
re-tested waiting on real `timeupdate` ticks rather than being waved through. The
test restored the todo afterwards, leaving no trace in the data.

### Phase 4 — Insights

`/insights` with a 7 / 14 / 30-day window, mood trend, top topics, top people,
and the open-todo count.

- **No chart library.** The entire requirement is one muted line with no axes,
  gridlines, legend, or tooltips — every charting library ships those *on* and
  the integration is spent switching them off. Hand-rolled SVG (~60 lines) with
  the geometry in `src/lib/chart.ts` as pure functions, which also means it ports
  to RN: `react-native-svg` consumes the identical path strings. A Recharts
  dependency would not port. **This deviates from the original brief's "a chart
  lib (Recharts is fine)" — swap it if that call is wrong.**
- **Points are spaced by date, not array index.** The API only returns days that
  *have* entries, so index spacing would silently close the gaps and make a
  sporadic fortnight look like a daily habit. A Jul 13 point lands at x=283 where
  index spacing would have wrongly put it at 300.
- **The only reference mark is a dashed hairline at neutral (score 3)** — above it
  is a better-than-even day, below it isn't. That's the one horizontal rule that
  means anything here.
- **Sparse states read as sentences, not broken charts.** A single point stranded
  on a two-week axis looked like a rendering failure, so one mood day now renders
  *"One day with an entry so far — Jul 20, feeling good."* Entries-but-no-moods
  and nothing-at-all have their own calm copy.
- Counts are text, not bars: at these volumes the ordering already carries the
  information and bars would be decoration.

**Verified:** 13 chart-geometry assertions (score→y at both extremes and neutral,
window edges, date-vs-index spacing, out-of-range clamping, `days=1` not dividing
by zero, single point drawing no path, empty trend). All three sparse states were
driven by stubbing the API with crafted payloads. Window switching re-queries the
API for real (`?days=7|14|30`).

---

## 6b. Lebanese dialect accuracy (post-Step-3 work)

Real use surfaced that Arabic entries "miss sometimes". Two fixes went in, both
driven by measurement on `samples/` rather than intuition. The harness is
`test_dialect_grid.py` (model x primer grid, transcripts printed side by side —
no auto-scoring, because only the speaker can judge dialect accuracy).

**The finding that reframed it.** "مطلوب انزل جدّد باسبوري" (I need to renew my
passport) transcribed as `جدة` — Jeddah, the city — producing the todo *"Go to
Jeddah with passport"*. Critically, `whisper-large-v3` got it **just as wrong**
as `whisper-large-v3-turbo`. The two words are near-identical acoustically; what
separates them is knowing which reading makes sense. That's a language-modelling
gap, not an audio-quality one, so **a bigger transcriber does not fix it** —
which also means the earlier "upgrade the model" advice was wrong.

**Fix 1 — Lebanese primer, now the default** (`RECOMMENDED_LEBANESE_PROMPT`).
Whisper's `prompt` biases orthography as well as vocabulary, so seeding real
dialect spelling (`بدي`, `هلق`, `جدّد`) anchors decoding in the dialect, while an
appended bilingual example separately licenses code-switching. Measured: fixes
`جدّد`, preserves verbatim mixing ("بكرا I wanna go to the طبيب الأسنان"), leaves
English untouched. The older English-only primer only did the second half, which
is exactly why it used to drag Arabic entries toward English.
*Caution:* Whisper reads only the last ~224 tokens of the prompt and Arabic
tokenizes expensively — this primer is near that ceiling, so anything prepended
will silently push the dialect vocabulary out. Re-run the grid after edits.

**Fix 2 — transcript repair pass** (`app/repair.py`, audio path only). The primer
only helps with words we thought to seed; repair catches the rest. Claude reads
the transcript and corrects words that make no sense in context. **Model choice
is load-bearing: `claude-haiku-4-5` failed this case, `claude-sonnet-5` fixed
it**, so `REPAIR_MODEL` defaults to Sonnet and is deliberately independent of
`EXTRACTION_MODEL`.

Two invariants:
- **`rawTranscript` is persisted exactly as heard.** Only extraction reads the
  repaired copy. Given the decision to keep audio because "audio is the truth"
  (§7.6), a journal that silently rewrites the user's words would contradict the
  product — so the repair improves the *structure* without touching the *record*.
- **Failure is silent and safe.** Missing key, API error, empty response, or
  output whose length falls outside 0.5x-1.6x of the input (i.e. the model
  explained itself instead of transcribing) all fall back to the original.
  Verified across 9 guard cases; repair can never break ingestion.

Text entries skip repair — typed words are already what the user meant.

**Measured end to end**, primer disabled so repair had real work:
`جدة` -> `جدد`, todo *"Go to Jeddah with passport"* -> *"Renew passport"*.
It also incidentally fixed an extraction bug: `حكيم السنان` (Lebanese for "the
dentist") was being extracted as a **person** named "Hakim As-Nan", polluting the
top-people insight. With repaired context the extractor reads it as a dentist and
records no person.

**Still open:** an audio-native STT model (`gpt-4o-transcribe`, Gemini) would
likely fix this at the source without a primer, and drops into the existing
provider seam with no code change — but needs an `OPENAI_API_KEY` (currently
unset) and costs roughly 9x Groq. Untested.

## 6c. Step 4 — Mobile app (Expo / React Native)

Ports the validated web loop to a phone, against the **same backend**, in
`frontend-mobile-app/`. This section covers **Step 4 — the shell** (scaffold,
navigation, shared-module wiring, live journal). Auth wiring came in Step 5
(§6d), and capture / detail / insights in Step 6 (§6e); the app is now at web
parity.

### Stack

Expo SDK **57** (managed) · expo-router · React Native 0.86 · React 19 ·
TypeScript. `expo-audio` (recording/playback, Phase 2) and `react-native-svg`
(insights chart, Phase 4) are installed; fonts via `@expo-google-fonts`
(Source Serif 4 + Inter). Runs in **Expo Go** — every module used is bundled
there, so no custom native build is needed.

### Sharing strategy — single source, zero drift

The mobile app imports the web app's framework-agnostic modules **directly** as
`@shared/*` (→ `../frontend-web-app/src/lib/*`), via Metro `watchFolders` + a
matching `tsconfig` path. No copies, no third package — one source of truth for
`api`, `types`, `mood`, `format`, `entryView`, `chart`. Only the platform pieces
are rewritten in the mobile app (recorder, components, nav, tokens).

Three small **portability edits, made in the shared source** (both apps keep
working — the web app still typechecks):
- `@shared/api` reads `EXPO_PUBLIC_*` OR `NEXT_PUBLIC_*` for the base URL.
- `createAudioEntry` accepts RN's `{ uri, name, type }` descriptor as well as a
  web `Blob` (RN's FormData can't take a Blob for file parts).
- A DOMException polyfill (`src/polyfills.ts`) is loaded first, since Hermes may
  lack the `DOMException` the shared client constructs on aborted requests.

Two Metro-config details were required and are non-obvious:
- `resolver.resolveRequest` maps `@shared/x` to the absolute web-lib path.
- `resolver.nodeModulesPaths` pins **this** app's `node_modules` as a resolution
  root — without it, a bare import inside an out-of-root shared file (notably the
  `expo/virtual/env` shim babel injects to inline `EXPO_PUBLIC_*`) fails to
  resolve. This was the one real bundling bug and it's fixed.

### Phase 1 — Scaffold, shared wiring, tokens, nav, reachability

- Direction A "Quiet" tokens as RN values (`src/theme/`), serif/sans `Text`
  components, and a `bidiStyle` helper — RN has no `dir="auto"`, so Arabic/mixed
  text gets `writingDirection`/`textAlign` per node.
- `TopBar` (the web's SiteNav twin) + expo-router routes mirroring web: `index`
  (capture), `journal`, `entry/[id]`, `insights`. `entry/[id]` uses
  `useLocalSearchParams()` (Next 16's `await params` does NOT apply in RN).
- Capture, entry, and insights are **placeholders**; the journal is a real
  `GET /entries` list built from the shared `entryView` helpers.

**Verified on the iOS simulator (iPhone 17, iOS 26.5):** the app launches, the
capture screen renders in Source Serif 4 on paper, navigation works (routed to
`/journal`, active nav state flips), and the journal renders **28 real entries
fetched over the network** — mood dots (shared scale), relative dates, topics,
todo counts, and **Arabic entries right-aligned** via `bidiStyle`. `tsc` is clean
across the mobile app *and* the shared files.

> Capture, entry detail + delete, and insights were completed in **Step 6** —
> see §6e. This section (§6c) covers only the Step 4 shell.

### Environment reality (see §7 for detail)

This build fought a very unstable hotspot network (the Mac's IP changed **3×**
mid-session) and macOS permissions. Consequences baked into the setup:
- **API base is `127.0.0.1:8000`**, not a LAN IP — the iOS simulator shares the
  Mac's loopback, so it's immune to the IP flipping. A **physical device** needs
  the Mac's current LAN IP in `.env` + `uvicorn --host 0.0.0.0`.
- Expo Go wouldn't install via the CLI (download kept timing out); it was fetched
  **resumably** and installed into the simulator by hand.
- **Simulator taps can't be synthesized** — macOS blocks osascript from posting
  click events (reading UI works). Phase 1 navigation was driven by **deep link**
  (`exp://…/--/journal`), which exercises the real router. Phase 2's record
  button and Phase 3's todo toggle will need a real tap path (e.g. `cliclick`, or
  hands-on) — worth solving before Phase 2.

## 6d. Step 5 — Auth + Delete

Adds real accounts and delete. The data model already scoped everything to a
`userId`, so this is **enforcement, not restructuring**. **All four phases are
complete and verified** — backend enforcement (curl), web login/delete UI (real
browser), and mobile auth wiring (iOS simulator). The whole product loop now runs
under real accounts on web and iOS.

### Phase 1 — Backend auth

**Token model — deliberately two kinds:**
- **Access token:** a short-lived stateless **JWT** (HS256, `sub`=user id,
  `type`=access, 30-min TTL). Verified with no DB hit.
- **Refresh token:** a long-lived **opaque random** string (60-day TTL). Only its
  **SHA-256 hash** is stored (`RefreshToken` model), so it's revocable (logout)
  and rotated on every refresh. Not a JWT — statelessness is the wrong property
  for something you must be able to revoke.

**Endpoints:** `POST /auth/register` (validate email + min-8 password, bcrypt
hash, auto-login), `/auth/login` (generic 401 on any bad-cred case — never
reveals which field), `/auth/refresh` (verify hash → **rotate**: revoke old,
issue new pair), `/auth/logout` (revoke presented refresh token, idempotent),
`GET /auth/me`.

**Enforcement:** a `get_current_user` dependency reads `Authorization: Bearer`,
verifies the JWT, loads the user (401 otherwise). Applied to **every** data
endpoint. The **demo-user fallback and client-supplied `userId` are gone** — the
owner is always `current_user.id`. `GET /entries/{id}` and `PATCH /todos/{id}`
confirm ownership and return **404 (not 403)** for non-owners, so a user can't
probe whether someone else's id exists.

**Library choices (flagged, one deviates):**
- **bcrypt directly, not passlib.** passlib is unmaintained (2020) and warns
  against bcrypt 4.1+. A SHA-256 pre-hash lifts bcrypt's 72-byte truncation so
  long passphrases hash in full. (`bcrypt 4.3.0`.)
- **PyJWT** for access tokens (`2.13.0`). It warns if `JWT_SECRET` < 32 bytes, so
  the secret is 64 hex chars, in `.env` (never committed), documented in
  `.env.example`.
- Config (`JWT_SECRET`, access/refresh TTLs) is read **lazily** in `security.py`,
  because `app.main` calls `load_dotenv()` after its imports — an import-time read
  would see an empty secret.

**Schema / migration:**
- `User` gains `passwordHash String?` (**nullable** — so `db push` accepted the
  existing demo row, and future OAuth-only accounts fit) + `updatedAt`. New
  `RefreshToken` model (`tokenHash @unique`, `expiresAt`, `revoked`).
- **The demo account was converted into a real login, not deleted** — its ~29
  test entries are preserved, and it's signable-in as
  `demo@voicejournal.local` / `vournal-demo-2026` (via
  `scripts/set_demo_password.py`). This is the spec's "attach existing data to a
  real account" option.
- Fixing this needed `prisma generate`, which surfaced the **stale venv
  shebangs** — so those were **permanently repaired** (the long-standing
  housekeeping item); `.venv/bin/uvicorn`, `prisma`, etc. now work directly.

**Verified (curl):** no token → 401; register → tokens; bearer list shows only
own entries; cross-user read → 404; wrong-password and unknown-email → identical
401; refresh rotates and the old token then 401s; logout revokes; validation
422/409; audio-entry endpoint 401s without a token; no passwords/tokens in the
log.

**Intentional gap (documented in code):** `GET /entries/{id}/audio` is **not**
bearer-gated — `<audio>` elements can't send an `Authorization` header. Entry ids
are high-entropy cuids that only leak to the owner, so it's not trivially
exploitable, but it is a gap. The proper fix is a short-lived **signed URL** — a
later hardening step.

### Phase 2 — Delete (backend)

- **`DELETE /entries/{id}`** — ownership→404, remove the recording via the
  `storage.py` seam, then delete the entry. The DB cascade takes its todos +
  `EntryTopic`/`EntryPerson` links. **Shared `Topic`/`Person` rows are left** —
  user-level, possibly used by other entries; orphans are harmless.
- **`DELETE /entries/{id}/audio`** — strips just the recording, nulls `audioUrl`,
  **keeps the written entry** (a real privacy affordance for a voice product);
  returns the updated entry.
- **`DELETE /todos/{id}`** — ownership→404, delete.
- `storage.delete_audio()` reuses the playback containment check, so it can only
  remove a file *inside* `AUDIO_DIR`, and tolerates an already-missing file.

**Verified (curl):** text-entry delete → row + cascaded todos gone; audio-entry
delete → **file removed from disk** + row gone; audio-only delete → entry kept,
`audioUrl` null, file gone; todo delete keeps siblings; every non-owner delete →
404.

### Phase 3 — Shared client auth + Web login/register + delete UI

The API change lands **once**, in the shared client, and both apps inherit it.

- **`@shared` client:** an injected **`TokenStore`** interface (each app supplies
  its own storage — the client stays framework-agnostic); a **bearer header on
  every request**; and on a **401 → refresh once → retry** the original, else
  clear tokens and fire `onAuthRequired`. Refresh is **single-flight** — this
  matters, because refresh *rotates* (revokes the old token), so concurrent 401s
  each refreshing would revoke each other and log the user out; they await one
  shared refresh instead. Plus `register`/`login`/`refresh`/`logout`/`me`, the
  three `delete*` calls, and 204 handling.
- **Web:** a localStorage `TokenStore` (XSS-readable — the hardening is httpOnly
  cookies, a later step; bearer-in-storage keeps web + RN symmetric); an
  **`AuthProvider`** that resolves the session once and **gates rendering** so
  protected pages don't fire API calls before the client is configured or the
  user is known; login/register screens (Direction A); logout in the nav; and
  **delete UI** — a calm inline confirm for the whole entry, a hover `×` per
  todo, and "remove recording, keep the entry".

**Verified in a real browser (12 checks):** unauth → /login; register → capture;
**isolation** (new user's journal empty, not the demo's entries); create text +
audio; audio-only delete (recording section gone, **file removed from disk**,
entry kept); todo delete; entry delete (gone from journal); sign out → sign in
(own data persists); wrong password rejected. `tsc` / `eslint` / `next build`
clean.

### Phase 4 — Mobile auth wiring

- **`secureTokenStore`** — an **expo-secure-store** adapter (iOS Keychain:
  encrypted, app-sandboxed) injected into `@shared`. Native gets the stronger
  storage the web can't.
- **`AuthProvider`** (RN twin) resolves the session once; the **root layout holds
  the splash until auth resolves**, then gates: anon → `/login`, authed → app, so
  the app opens straight to the right screen. Login/Register screens + `AuthForm`
  (RN, Direction A). The journal is unchanged — it just works now that the shared
  client attaches the token.

**Verified on the iOS simulator, driven end-to-end:** fresh launch (no token) →
**gated to the login screen**; typed `demo@voicejournal.local` + password →
signed in → nav flips to authed; the **journal loaded the authenticated user's 29
entries** (bearer token attached by the shared client), Arabic right-aligned.
`tsc` clean across mobile + shared.

> **Tap blocker (Step 4) solved:** `cliclick` posts CG events where osascript's
> `click at` was denied, and connecting the simulator's **hardware keyboard**
> (I/O menu) let `cliclick t:` type into iOS fields — real UI driving, so the
> login was actually performed, not shortcut. Sign-out on the simulator wasn't
> re-demonstrated (small nav target, tap didn't land post-navigation); it's the
> same shared `logout()` already verified on web, and gated-when-signed-out is
> proven by the launch behavior.

### Notes / follow-ups from Step 5

- **Demo account converted to a real login** (see §7): `demo@voicejournal.local` /
  `vournal-demo-2026` — a dev default; change or delete before real use.
- The mobile tsconfig's broad `include` was pulling the **web-only**
  `tokenStore.ts` into the RN typecheck; removed it — `@shared` imports are still
  checked transitively.
- **Session persistence** falls out for free: the Keychain token means relaunch
  opens straight to the journal.

## 6e. Step 6 — Finish the mobile app (capture, detail, insights)

Ports the three remaining web surfaces to Expo, reusing the shared logic. **The
mobile app is now at web parity** — the whole loop runs on iOS under auth.
Verified on the iOS simulator (the developer drove the simulator; the agent built
and typechecked). `tsc` clean across the mobile app + the shared files.

Only three things are genuinely platform-specific and were built here: the
recorder (expo-audio), the chart renderer (react-native-svg), and the audio
player. Everything else — the state machines, geometry, view logic — is the same
`@shared/*` the web uses.

### Phase 1 — Capture (the hero)

- **`useRecorder`** — expo-audio v57. The mic-permission flow is **rebuilt**, not
  ported: `requestRecordingPermissionsAsync` + `canAskAgain` distinguishes "not
  asked yet" from "blocked in Settings". Records `.m4a` (the backend's primary
  format). **`useCapture`** and **`EntryStructure`** mirror the web; states are
  identical (idle → recording+timer → uploading → processing → done), with the
  text fallback and denial recovery.

- **The audio-upload bug — the notable finding of this step.** Uploads failed
  with a misleading "Could not reach the API". The real cause, traced through
  Expo's source: the **global `fetch` in SDK 54+ is Expo's "winter" fetch**, and
  its multipart encoder (`expo/src/winter/fetch/convertFormData.ts`) **rejects
  RN's `{ uri }` file reference** — *and* you can't build an RN `Blob` from bytes
  (`"Creating blobs from 'ArrayBuffer'… not supported"`). The one shape its
  encoder accepts (besides a real Blob) is a part with a **`bytes()` method**. So
  the recorder now reads the file into a `Uint8Array` (via `expo-file-system`'s
  `File.arrayBuffer()`) and hands the shared client a `{ bytes, name, type }`
  part. Two shared-client changes fell out, both backward-compatible (web stays
  clean): `AudioFilePart` gained the `{ bytes }` shape, and abort detection is now
  **name-based** (`isAbortError`) so an aborted request isn't mislabeled a network
  error on RN. `expo-file-system` is currently a transitive dep — promote it to
  direct before a production build.

### Phase 2 — Entry detail + delete

- **`useEntry`** ported from web (load, poll-if-processing, todo toggle, and all
  three deletes). **`EntryDetail`** mirrors web: structure-first render, timestamp
  (+ duration for voice), FAILED state, calm 404, and the delete UI — delete a
  todo, "remove recording, keep the entry", and delete the whole entry behind an
  inline confirm that routes back to the journal.
- **`AudioPlayback`** — expo-audio's `useAudioPlayer` / `useAudioPlayerStatus`,
  loading `GET /entries/{id}/audio` (a plain URL — that route isn't token-gated,
  so no auth header needed). Play/pause + progress. Rendered only when the entry
  has audio, so the player hooks stay unconditional.

### Phase 3 — Insights

- **`MoodTrend`** draws with **`react-native-svg` from the identical
  `@shared/chart` path strings** — the exact reason the geometry was split into a
  pure module in the web build. Date-spaced points, one dashed neutral reference
  line, mood-coloured dots; the same sparse-state sentences as web (0 and 1-point
  cases don't render a broken chart). The screen has the 7/14/30-day switch, top
  topics/people (bidi-aware), and the open-todo count.

## 6f. Step 8 — Time extraction + to-do list view

Two capabilities, built together because they're the same idea from two ends:
teach extraction to hear **when** a task is meant to happen, then surface every
task the journal has collected in one place, grouped by that time. **All three
phases are complete** — backend (verified against the running server), web
(`tsc`/`eslint`/served), mobile (`tsc` clean; the developer drove the simulator).

**The one design decision that shapes everything.** A todo can carry a **day**
(`dueDate`, already there) and now optionally a **spoken clock time**. Only tasks
where a time was *actually said* get one — and those are the ones that will become
**calendar events** in the next step. The discriminator is deliberate and load-
bearing: *never invent a time.* "call the dentist tomorrow" stays a plain dated
todo; "doctor at 2pm tomorrow" becomes a timed one. Vague markers ("later",
"soon", "this week", "tonight") set a day at most, never a clock time. This keeps
the eventual calendar honest — only real appointments land on it.

### Phase 1 — Time extraction + `GET /todos` (backend)

- **Schema:** `Todo` gains **`startAt DateTime?`** (nullable; null = untimed)
  alongside the existing day-level `dueDate`. Backward-compatible — every existing
  todo simply has `startAt = null`. Added `@@index([userId, startAt])` for the
  list's time ordering. Applied with `prisma db push` (no migration needed for a
  nullable add); **existing entries are not re-extracted** — the field is
  forward-only.
- **Extraction (`app/extraction.py`, prompt edited verbatim-style):** each todo
  went from `{text, dueDate}` to `{text, dueDate, time}`, where `time` is
  `"HH:MM"` 24-hour **only when a clock time is spoken**. The rules teach am/pm
  common sense ("at 8" for an errand → `20:00`), relative times computed from the
  current instant ("in an hour", "in 30 minutes"), and **dialect / Arabizi**
  times ("bukra el sa3a 3" = tomorrow at 3:00, "ba3d sa3a" = in an hour, "el
  lela" = tonight). To resolve relative *times* (not just dates) the engine now
  takes a full **`now: datetime`** and passes `CURRENT DATE AND TIME:
  2026-07-20 14:30 (Monday, UTC)` — the weekday included so "next Monday"
  resolves. `_clean_time()` validates the model's `HH:MM` (range-checked, zero-
  padded) so a bad value can never form a bad `startAt`.
- **Composition:** the backend keeps `dueDate` as the day and composes
  **`startAt = dueDate + time`** (via `_compose_start_at`) only when both are
  present; times are attached as **UTC** for now (per-user timezone is a known
  deferred limitation, consistent with the UTC-bucketed mood trend). Both fields
  are stored and returned everywhere a todo appears.
- **`GET /todos`** (authenticated, user-scoped): every todo across all the user's
  entries, **soonest-first** — items sorted by `startAt or dueDate`, undated ones
  last (then by capture time), with a **`?done=`** filter. Each item carries
  `{ id, text, done, dueDate, startAt, entryId }`; the `entryId` is what lets the
  list link back to the source entry. Sorting is done in Python
  (`_todo_sort_key`), matching how insights already aggregate.

**Verified (live extraction gate, fixed `now = 2026-07-20 14:30 Monday UTC`):**

| Spoken | `dueDate` | `time` | |
| --- | --- | --- | --- |
| "call the dentist tomorrow" | `2026-07-21` | `null` | day only — no invented time |
| "doctor at 2pm tomorrow" | `2026-07-21` | `14:00` | timed |
| "meeting Sarah next Monday at 10" | `2026-07-27` | `10:00` | correct next-Monday |
| Arabizi "bukra el sa3a 3 … maw3ad" | `2026-07-21` | `15:00` | dialect time understood |
| "buy groceries" | `null` | `null` | no date or time |

Two behaviours worth noting from the same run: "call mom **tonight**" set the day
but **left time null** (didn't fabricate a clock time), while "trash out **at
8**" → `20:00` (evening common sense); and the Arabizi "**ba3d sa3a**" (in an
hour) was computed from 14:30 → `15:30`. **`GET /todos`** was then verified end to
end against the running server: a new entry's todos came back with `startAt`
composed correctly (`Doctor → 2026-07-23T14:00Z`, `Meeting Sarah →
2026-07-27T10:00Z`, day-only and undated todos `startAt: null`), soonest-first,
and the `?done=` filter partitioned cleanly (39 total = 35 open + 4 done). The
extraction gate (`test_extraction.py`) was rewritten to pass a `now` datetime and
add the timed + dialect cases.

### Phase 2 — To-do list view (web)

The list is a **view onto the journal**, not a place data lives — every row links
back to the entry that produced it, and the only mutation is the checkbox.

- **Shared layer (`@shared`, reused unchanged by mobile):** `startAt` added to the
  `Todo` type + a new `TodoListItem` (adds `entryId`); `getTodos(done?)` in the
  client; `formatTime()` in `format.ts`; and a **framework-free `lib/todos.ts`** —
  `groupTodos()` partitions into **Overdue / Today / Upcoming / Someday** plus a
  `done` pile, and `todoWhenLabel()` renders the trailing time/day. Bucketing is
  **by day, not clock time**: a task set for earlier today still reads as *Today*,
  never *Overdue* — overdue means an earlier calendar day. Splitting this into a
  pure module is what lets mobile reuse the exact same rules.
- **Web (`/todos`):** the four sections, **done collapsed at the bottom**
  (`Show done (N)`), each row a checkbox → text → time/day → a link to its source
  entry. The **optimistic toggle** mirrors the entry-detail pattern (flip now,
  persist, revert + tell the user on failure), owned by a `useTodos` hook. "To-do"
  added to the nav between Journal and Insights.

Gated on `tsc --noEmit` + `eslint` clean; the dev server serves `/todos` (200).

### Phase 3 — To-do list view (mobile)

The point of the shared split, proven again: mobile ships the **same**
`groupTodos`/`todoWhenLabel`/`getTodos` from `@shared` and only rewrites the
platform surface.

- **`useTodos`** (RN twin) — same load + optimistic toggle over `@shared/api`.
- **`todos.tsx`** — a `ScrollView` of the four sections, done collapsed at the
  bottom, each row a checkbox (`Pressable`, toggles) beside a `Pressable` that
  routes to `/entry/[id]`, with the shared `bidiStyle` on Arabic todo text.
  "To-do" added to the `TopBar`.

`tsc` clean across the mobile app + the shared files. (The mobile app has no
eslint configured, so `tsc` is the gate, as in prior mobile steps.)

## 6g. Step 9 — Per-user timezone + calendar view

The last of PRODUCT.md's three payoffs (journal, to-dos, **calendar**), with a
timezone fix first because a calendar grid makes UTC-vs-local obviously wrong.

- **Timezone (Phase 1).** `User.timezone` (IANA, nullable → UTC). Sent
  automatically by both clients (`Intl.DateTimeFormat().resolvedOptions().timeZone`)
  on register, kept fresh via a new `PATCH /auth/me` on session resolve. Extraction
  now composes `startAt` by interpreting the spoken clock time **in the user's
  timezone** and storing the correct UTC instant ("2pm tomorrow" for Asia/Beirut →
  `11:00Z`, verified), and the prompt's "current time" is the user's local now.
  Insights mood-trend buckets by the user's local day too. Forward-only:
  pre-existing timed todos keep their old UTC composition.
- **Calendar (Phases 2–3).** A framework-free `@shared/lib/calendar.ts` (the same
  treatment `todos.ts` got) does all the grouping — `monthGrid`, `weekDays`,
  `dayView` — so both clients reuse it. **Web** is a month grid over a selected-day
  panel (dots per day, an "anytime" band for dated-but-untimed items, a "Recorded"
  section, checkbox toggle, links to source entries). **Mobile** adapts to a day
  view with a week strip rather than cramming the grid onto a phone. Reuses
  `GET /todos` (client-side slicing, no new API).
- **Nav redesign (mobile).** The top nav overflowed at four sections, so it moved
  to a **bottom tab bar** (Home · Journal · To-Do · Calendar · Profile) with the
  wordmark centered up top; **Insights nested under Journal** via a quiet
  Journal/Insights switch (the Journal tab stays lit for both); a new **Profile**
  screen holds account info + sign-out. The To-Do screen also became filter-pill
  based (Overdue/Today/Upcoming/Someday/Done) to cut scrolling.

`tsc`/`eslint`/`next build` clean on web; `tsc` clean on mobile. Calendar grouping
was unit-checked under `TZ=Asia/Beirut` (someday excluded, correct local-day
placement across the UTC boundary, month-boundary padding).

## 6h. Step 10 — STT benchmark + pipeline cost optimization

Measured a candidate STT (**Speechmatics Melia**) against the shipped one (Groq
Whisper) on the real `samples/` clips, then decided whether the Claude repair pass
still earned its keep — and implemented the winner. **The developer judged
transcript quality from full side-by-side output; the agent measured and built.**
Every phase gated on a STOP.

### The hypothesis, and what the measurements actually showed

Melia claims native single-pass code-switching, which — if it also handled
Lebanese dialect — could make the repair pass (the biggest per-entry cost)
unnecessary. Tested on four clips (`benchmark_stt.py`, both providers, full
transcripts + latency + cost):
- **Code-switching: Melia wins decisively.** "بكرا I wanna go to the طبيب الأسنان"
  came back verbatim from Melia with **no primer** — the thing Whisper can only
  fake with the blunt Lebanese primer (which sometimes drags pure-Arabic toward
  English). This retired the primer.
- **Dialect: Melia does NOT fix it.** "جدّد" (renew) still came back as "جدة"
  (Jeddah) — the exact error the repair pass exists for. So Melia removed the need
  for the *primer*, not the *repair*.

### Does repair still earn its keep? (Phase 2, `benchmark_repair.py`)

On Melia's transcripts, extraction was run raw vs raw→repair→extract. On 3 of 4
clips repair changed nothing meaningful. On the passport clip it was decisive:
without repair the todo was **"Go to Jeddah with passport"**; with repair,
**"Renew passport."** A wrong calendar entry vs a right one — so repair stays.

### Merged repair + extraction (Phase 3, `test_merged_extraction.py`)

Rather than keep repair as a separate full-transcript-rewriting call (its output
tokens are the cost driver), it was folded **into** extraction: one Sonnet call
that silently corrects the mistranscription and outputs only the JSON. The gate
was **restraint** — the discriminating extraction rules must survive a generative
repair mode. Result across the full `test_extraction` suite + the Melia dialect
transcripts: **0 fabrications** (never invented a todo, a clock time, or a person),
all discriminating cases preserved ("tonight" → day, no clock time; "buy groceries"
→ no date; low-mood → no todos), and the جدة → "renew passport" recovery works in
the single call. If anything it's *more* restrained (doesn't over-infer dates).
- **The thinking-token trap.** Sonnet 5 emits an extended-thinking block by
  default, billed as output ($10/M) — that alone doubled the call and made merged
  *more* expensive than the two-call path. **Disabling thinking**
  (`thinking: {"type": "disabled"}`) fixed the cost and, measured, the dialect fix
  survives without it. So merged runs thinking-off.

### What shipped (Phase 4)

- **Default STT → Speechmatics Melia** (`melia-1`, batch API behind the seam;
  Groq/OpenAI still reachable via `STT_PROVIDER`). Also bumped the (now
  benchmark-only) repair model to the current **Sonnet 5** string.
- **Audio entries → merged extraction** (`app/extraction_merged.py`): the separate
  `repair_transcript` call was removed from the audio path; `process_transcript`
  gained a `use_merged` flag (True for audio, False for text — typed words need no
  repair). **`rawTranscript` semantics are unchanged** — still stored exactly as
  heard; the correction happens only inside extraction.
- **Verified end to end (backend):** uploaded the جدة clip through the new
  pipeline — `rawTranscript` came back `…انزل جدة باسبوري` (as heard), while the
  extracted todo was **"تجديد الباسبور" (renew passport)** with the right date. The
  text path still runs plain Haiku extraction, unchanged.

### Per-entry cost (measured, ~2-min entry)

| | STT | LLM | total |
| --- | --- | --- | --- |
| Old (Groq + repair→extract) | ~$0.0013 | ~$0.0100 | **~$0.0113** |
| New (Melia + merged, thinking off) | ~$0.0043 (≈$0 in Melia's free tier) | ~$0.0078 | **~$0.0121** / **~$0.0078 free-tier** |

The **merge cut the LLM cost ~22%** (Sonnet-merged vs Sonnet-repair + Haiku-extract,
STT held constant). But Melia STT is ~3× Groq, so the net is *cheaper within
Melia's free monthly batch hours* and *roughly neutral beyond them*. The honest
headline is **quality + simplicity** (native code-switching, primer retired, one
LLM call instead of two, dialect fix preserved) more than a raw cost cut. Caveat:
merged runs all audio extraction on Sonnet 5, so it's more exposed to the
post-2026-08-31 end of Sonnet's introductory pricing than the old split (which kept
extraction on Haiku).

### Post-Step-10 fix — gender-neutral, first-person output

Real use surfaced a bug the moment Arabic entries rendered: the extraction wrote
*about* the speaker in the **third person**, which forced it to guess a grammatical
gender — and it defaulted to **feminine** ("عندها ميتينغ" / "بدها تجدد جواز سفرها"),
so a male user's journal read as if written by a woman. Third-person summaries are
the trap: Arabic can't refer to a person in the third person without gendering the
verb.

Fix (prompt-only, no schema, no UI — the user chose this over storing a gender
field): the extraction `SYSTEM_PROMPT` now writes **summaries in the first person**
— the journaler's own voice — and **todos as neutral verbal nouns/imperatives**.
First-person present-tense Arabic is gender-neutral ("عندي", "بدي", "لازم أجدد",
"جواز سفري"), so the guess disappears entirely. Verified on the reported cases:
"عندها ميتينغ" → "عندي ميتينغ"; "بدها تجدد جواز سفرها" → summary "بدي جدد جواز سفري"
+ todo "تجديد جواز السفر"; and the Melia-raw جدة clip through the merged path →
"لازم أجدد جواز سفري" + "تجديد جواز السفر" (dialect fix and gender-neutrality holding
together in one call). Because both paths share the prompt, text and audio are both
fixed. It also reads better in English ("I have a meeting tomorrow" vs "The speaker
has a meeting"). **Forward-only:** entries extracted before the fix keep their old
wording (no re-extraction). Fully-gendered output (use "بده" for a known-male user)
remains available later behind an optional `User.gender` field if wanted.

## 6i. Step 11 — Production hardening + security audit

Takes Vournal from "works on my machine" to "safe for real users." Six phases;
**1–5 are done and verified, Phase 6 (the actual deploy) is prep-complete but not
yet executed.** Each phase ran to a STOP with the developer.

### Phase 1 — Audio → Supabase Storage + signed URLs

Closed the one known hole: `GET /entries/{id}/audio` was **not bearer-gated**
(an `<audio>`/native player can't send a header), so anyone with an entry id could
fetch a recording. `app/storage.py` was rewritten against a **private** Supabase
bucket over the Storage REST API (async httpx + the `service_role` key, server-only
— the sync `supabase` client would block the loop): `save_audio` → object key,
`create_signed_url` (≈5-min TTL), `resolve_audio_path` (downloads to a temp file
for STT, then deletes it), `delete_audio`. The audio route is now **authenticated +
ownership-checked (404 for non-owners)** and returns `{"url": <signed>}`; the client
fetches bytes **directly from storage** (no byte-proxying / double egress). A
migration script moved the 32 existing local recordings into the bucket. Verified
end-to-end: 401 without a token, 404 for a non-owner, signed URL fetch = the exact
bytes, URL **expires** after its TTL, delete removes the object.

### Phase 2 — Production database (Supabase Postgres + migrations)

Moved off local Docker to **Supabase Postgres**, and from `db push` to a real
**migration history**. The schema datasource gained `directUrl` (pooled 6543 for
the app, direct/session 5432 for migrations — the direct `db.<ref>` host is
IPv6-only, so the **Session pooler** string is used on IPv4). `prisma-client-py`'s
engine (5.17) supports `directUrl`; the IDE's Prisma-7 error is a false positive.
The initial state was **baselined** (`migrate diff --from-empty` → `migrate resolve
--applied 0_init`), then rows were migrated with `pg_dump` (local PG16) → restore
(Supabase PG17): **0 errors, every table's row count matched exactly.** The project
was **recreated in Frankfurt** after the first attempt landed in Singapore (caught
by a region probe before any data moved — the cheapest moment). App verified fully
against Supabase incl. audio-key resolution; the pooled connection works with
`?pgbouncer=true`.

### Phase 3 — Usage limits + rate limiting (cost protection)

Every audio entry costs real money (STT + a Sonnet call), so cost is bounded on
several axes (`app/quota.py`, `app/rate_limit.py`), all **fail-closed** with
**structured** errors the clients render calmly:
- **Per-user monthly entry cap** (beta 60; a `user_monthly_limit` tier seam + a
  `FAIR_USE=300` constant wired for a future paid tier), checked **before** any
  provider call → `429 MONTHLY_LIMIT_REACHED`. Rolls at the calendar month on
  `User.usagePeriodStart/Entries`.
- **Max recording length** (600 s) → `413 RECORDING_TOO_LONG`, rejected pre-STT.
- **Global daily kill switch** (`GLOBAL_DAILY_AUDIO_CAP`) → `503 SERVICE_BUSY`,
  logged loudly — the 3am backstop.
- **Per-entry usage recorded** (`Entry.sttSeconds`, `llmInTokens`, `llmOutTokens`)
  — unit economics as data, not estimates.
- **Rate limiting** (slowapi, per-IP in-memory: 120/min global, login 5/15min per
  IP **and** per email, register 3/hour). All five verified live (usage recorded;
  over-length 413; monthly 429; kill switch 503 + log; login throttled at ~5).

### Phase 4 — In-app account deletion (App Store requirement)

`DELETE /auth/me`, password-confirmed. **Order matters:** delete every audio object
from the bucket FIRST (the DB cascade can't reach external storage), THEN the user
— cascading entries, todos, topics, people, refresh + reset tokens. No soft-delete.
Web `/account` page + mobile Profile section, two-step + password. Verified: wrong
password → 403 (nothing removed); confirmed delete → rows gone **and** the bucket
holds no orphaned audio.

### Phase 5 — Password reset (Resend)

`POST /auth/forgot-password` (**always 200**, no enumeration; per-email + per-IP
rate limited; stores a **hashed, single-use, 30-min** token) and `POST
/auth/reset-password` (verify → set password → mark token used → **revoke ALL
refresh tokens**). `PasswordResetToken` model; Resend email seam (fails soft). Web
"forgot password?" → request + reset pages; mobile links out to the web reset page.
Verified: valid token resets; new password works, old fails; all sessions revoked;
used and expired tokens both 400; and a real Resend send (`200` + id).

### Security & performance audit (VOURNAL_SECURITY_PERF_AUDIT.md)

Ran the audit — verified each seeded finding against the *current* code, not the
seed. **Most of the seeded 🔴/🟠 were already closed** by Phases 1–5 (audio gating,
rate limiting, quotas, JWT `algorithms=["HS256"]` + secret-length pinned, no diary
content in logs, private bucket + signed URLs, deletion). Remediation landed the
rest that mattered for a beta, all verified live:
- **Refresh-token reuse detection** — replaying a revoked token now revokes the
  whole family (a stolen 60-day token is no longer a 60-day takeover).
- **Boot-time reaper** — orphaned `PENDING/PROCESSING` entries (a deploy kills
  in-process `BackgroundTasks`) are marked `FAILED` on startup so users can retry.
- **Security headers** (API middleware + web `next.config`), **`/docs`/`/openapi`
  gated off** by default, **magic-byte upload validation** (don't trust the
  client-declared filename), a **30 MB request cap**, and **polling backoff**
  (1.5 s → 5 s). CORS is env-driven; `expo-file-system` promoted to a direct dep;
  `.gitignore` verified safe for a GitHub push (no repo exists yet → no secrets in
  any history).

**Deliberately deferred to post-launch** (documented, non-blocking for a beta):
web tokens in `localStorage` → httpOnly cookies (mitigated by reuse detection + the
30-min access token); gzip/`Cache-Control`/ETag + a cheap status-only poll response;
Prisma pool tuning; CI dependency scanning; user-namespaced audio keys. A real
**Redis job queue** to replace `BackgroundTasks` is the right durability fix at
scale — the boot reaper is the beta-grade stand-in.

### Phase 6 — Deploy (pending)

Prep done (CORS env-ified, docs gated, headers, `.gitignore`, `expo-file-system`,
an `ios` mic usage string already present, a `remove_demo_account.py` cutover
script). The deploy itself — Render (backend, Frankfurt, always-on) → Vercel (web +
privacy/terms) → EAS/TestFlight — runs through the developer's cloud accounts and
is the remaining work.

## 6j. Step 12 — Perceived speed (data caching, audio latency, skeletons)

The problem wasn't navigation — it was **data arrival**. Every screen refetched from
scratch across a Lebanon → Frankfurt round trip with no client cache, and audio took
two sequential hops to play. A Phase 0 measurement made the case concretely: **every
backend fetch has a ~2 s floor** (a single entry takes as long as fifty — it's the
round trip, not the data; `/health`, which skips the DB, is 12 ms). This step makes
the app *feel* fast without touching the server — the right fix, since it's a
client-side problem. Redis / server upgrades were explicitly ruled out as the wrong
tool. Four phases, each to a STOP; measurement bookended it (Phase 0 baseline, Phase 4
after) so the win is proven, not asserted.

### The shared engine

The old per-app hooks (fetch-in-`useEffect` + `useState`) were replaced by **one
TanStack Query layer in `@shared`** (`queries.ts`), imported by web (`@/lib/queries`)
and mobile (`@shared/queries`) — only the `QueryClientProvider` wrapping differs. It
keeps the *exact* return shape the old hooks had (`{ state, … }` with a
loading/ready/error union), so **no screen changed** and the perf instrumentation kept
working. Each app's `src/hooks/*` is now a one-line re-export.

### Phase 1 — Caching (the big win)

`staleTime: 30 s`, and the state maps to `ready` whenever data exists — so a **revisit
renders from cache immediately with zero network**, and a background refetch never
blanks the list. Mutations (todo toggle/delete) are **optimistic with rollback**,
patching every cache the item lives in (entry ↔ todos list ↔ calendar) and invalidating
the right keys. The entry query is **prefetched on row hover (web) / press-in (mobile)**
so detail opens warm. Result: revisiting Journal went from ~2 s to instant.

### Phase 2 — Audio latency (prefetch + CDN)

The signed URL is now a cached, prefetchable query (`useAudioUrl`), warmed on row press
alongside the entry — so **hop 1 (the ~2 s signed-URL round trip) runs before the tap**,
off the critical path. Both players consume it and re-fetch once on expiry. The CDN
angle was researched against Supabase's docs and **applied honestly**: `save_audio` now
sets the object's `cache_control` via the multipart form field (the raw header is
ignored — verified), but a *private* bucket served by *per-play* signed URLs can't
benefit from edge caching (each token is a distinct cache key → always an origin miss),
and Smart CDN is Pro-only. So prefetch, not the CDN, is the audio win; the metadata is
set for correctness.

### Phase 3 — Skeletons

Quiet, hairline-toned skeletons (Direction A) replace the "Loading…" text on every data
screen, both platforms, pulsing on the **same 1.6 s beat as the capture "Thinking…"**
state. Each mirrors the shape it stands in for, so nothing reflows when data lands.
They only ever show on a true cold load — a cached revisit shows real content instantly.

### Phase 4 — Measured result

Same script as Phase 0, 3-run basis. Cold first-loads are **unchanged (~2 s)** — caching
can't remove a fetch that must happen (that floor is backend/latency work, out of scope).
But every **revisit, cross-screen cache hit, and prefetched open drops to ≈0**, and audio
open→first-sound fell from ~2.6 s to ~0.5 s. Across a typical session the total time
spent waiting dropped **~66%** (~11.4 s → ~3.8 s). Full table + honest caveats (I'm the
worst-case Lebanon→Frankfurt user; the caching wins are structural and help everyone,
the unchanged cold-load hurts far users least) in **PERF_RESULTS.md**; baseline in
**PERF_BASELINE.md**.

### A Metro gotcha worth remembering

The shared `queries.ts`/`perf.ts` live in the web app's tree but import framework
singletons (`react`, `@tanstack/react-query`). Metro's default walk-up would resolve
those to the *sibling web app's* copies → two Reacts / two query clients at runtime.
Fixed with a targeted `resolveRequest` redirect in `metro.config.js` (pin those to the
mobile app's copy), verified by bundling every route chunk and confirming a single copy
with zero web-app leakage. `disableHierarchicalLookup` was the wrong lever — it broke the
`@shared` absolute-path resolution.

## 6k. Step 13 — Retire the web client → marketing landing

The Next.js web *journaling client* had done its job as the parity reference through
Step 12, but the product ships mobile-first to TestFlight and a second full client is
maintenance the beta doesn't need. So the web app was **archived to a git branch**
(recoverable, not deleted) and its origin now serves a **one-page marketing landing**
(`frontend-web-app` rebuilt as a static Next.js site). Branding settled as lowercase
**"vournal"** across the app and the landing. The finalized **Privacy Policy** and
**Terms of Service** — naming the four data processors: Speechmatics **Melia** (STT),
**Anthropic** (extraction), **Supabase** (DB + audio storage), **Resend** (email) —
now ship in three places that must agree for App Store review: the landing footer, a
mobile **`/legal`** route, and the mobile **Profile** view. The provided icon became the
web favicon and the app icon (background removed). **No backend changes**; pushed to its
own repo (`vournal-landing`).

## 6l. Step 14 — Subscription entitlements (foundation + final pricing)

The machinery to know what a user is entitled to **right now**, with no payment provider
yet (Paddle is Step 14b). Two design commitments carry the whole thing: a **single source
of truth**, and **fail-safe** access.

- **`app/entitlements.py` is the only place that decides access.** Nothing reads
  `User.plan` directly — everything goes through **`effective_plan(user)`**, which returns
  the granted tier *only while the entitlement is live* (`planExpiresAt` null or in the
  future) and otherwise **falls back to `free`**. That fail-safe is the point: whatever
  grants a plan later can never leave access stuck-granted — it lapses to `free` the moment
  the entitlement expires. `is_paid()` and the `PLANS` / `PAID_PLANS` tuples live here too.
- **Schema.** A `Plan` enum (`free` / `daily` / `reflection`, lowercase to match the API
  and client gating) plus `plan`, `planExpiresAt`, and `paddleCustomerId` /
  `paddleSubscriptionId` on `User` — the Paddle ids added now so Step 14b needs no second
  migration.
- **`GET /auth/me` returns the *effective* plan** (and `planExpiresAt`, nulled when it
  resolves to free), so clients gate on the live entitlement, never a stale stored field.
  Mobile carries a `Plan` type + `AuthUser.plan`.
- **Plan-aware quotas.** `app/quota.py`'s monthly-entry and max-recording caps key off
  `effective_plan` — superseding Step 11's flat beta caps (§6i Phase 3).
- **Dev-only `scripts/set_plan.py`** grants a test account any plan/expiry directly against
  the DB — never an API endpoint, never reachable in prod — the manual stand-in until the
  Paddle webhook writes these same fields.

**14a-fix — final pricing.** Once pricing landed, the middle tier was renamed
**`unlimited` → `daily`** (`$3/mo · $30/yr`; `reflection` is `$10/mo · $96/yr`) via a
hand-written migration — `ALTER TYPE "Plan" RENAME VALUE`, which preserves every row and
runs in a transaction (no enum drop/recreate, no data migration). The flat free/paid caps
became **per-plan** lookups:

| plan | monthly entries | max recording |
| --- | --- | --- |
| free | 15 | 90 s |
| daily | 150 | 120 s |
| reflection | 150 | 300 s |

Unknown or expired plans fall to the tightest (free) caps. Verified live: `/auth/me`
returns `daily`; the length guard `413`s per plan (free >90 s, daily >120 s, reflection
>300 s) **before any provider call**; the monthly cap `429`s (free at 15, daily at 150)
**before any DB write**; and an expired `daily` grant resolves to `free`. The premium **AI
features that `reflection` gates** come in a later step — today the tiers differ only by
these caps.

## 7. Environment decisions worth knowing

These are setup facts that aren't obvious from the code:

1. **Dev database runs in Docker, not the machine's native Postgres.** The machine had PostgreSQL 18 on `:5432`, but its superuser password was unknown. Since a local dev DB was wanted, a throwaway Postgres container was used instead — no credentials needed from anyone:
   - Container `voice-journal-db` (image `postgres:16`), host port **5434** → container 5432.
   - Creds: user `vj`, password `vjpass`, database `voice_journal`.
   - `.env` → `DATABASE_URL="postgresql://vj:vjpass@localhost:5434/voice_journal"`.
   - Start if stopped: `docker start voice-journal-db`.
   - **For production:** swap `DATABASE_URL` for the real connection string and re-run `prisma db push` (or migrations).
   - **Port history — it was 5433 until Step 3.** Another project's container (`marketera-pg`) is also configured for 5433; when it started it took the port, `voice-journal-db` died with exit 128, and the API then reached a *different* Postgres — surfacing as the very misleading `Authentication failed ... credentials for 'vj' are not valid` rather than a connection error. Docker's proxy keeps listening on the port even when the container is down, which is what turns a missing database into an auth error. Moved to **5434** so the two projects stop colliding. The data was preserved because it lives in a named Docker volume (`f4ce7d64…`), not the container layer — the container was recreated against the same volume and all entries survived.
   - Diagnosing this class of problem: `docker ps -a` (is it actually up?) and `lsof -nP -iTCP:<port> -sTCP:LISTEN` (who really holds the port?).
2. **Python 3.12 venv.** The system `python3` is 3.9 (too old for 3.11+); a `.venv` was created with Homebrew's `python@3.12`.
3. **The venv's console scripts are broken — use `python -m`.** The venv was created while the project lived at `~/Desktop/voice-journal-backend`, then the project moved into `~/Desktop/murmur/`. Venv console scripts hardcode the interpreter path at creation time, so `uvicorn`, `prisma`, `fastapi` etc. all fail with `bad interpreter: /Users/mhmdnab/Desktop/voice-journal-backend/.venv/bin/python3.12`. The installed packages are fine — only the shebangs are stale. Work around it with module invocation, which ignores the shebang:

   ```bash
   .venv/bin/python -m uvicorn app.main:app --reload
   ```

   Permanent fix (not yet applied): `python3 -m venv --clear .venv && .venv/bin/pip install -r requirements.txt`.
4. **Prisma CLI needs the venv on PATH.** Running the bare `prisma` CLI fails with `prisma-client-py: command not found` because the generator runs in a subshell. Use `PATH="$(pwd)/.venv/bin:$PATH" .venv/bin/python -m prisma <cmd>` (this is why `generate`/`db push` are run that way).
5. **Environment variables (all in `.env`, gitignored):**

   | Variable | Required | Default / Notes |
   | --- | --- | --- |
   | `DATABASE_URL` | yes | Postgres connection string. |
   | `ANTHROPIC_API_KEY` | yes | Extraction. |
   | `EXTRACTION_MODEL` | no | `claude-haiku-4-5-20251001`. |
   | `STT_PROVIDER` | no | `groq` (default) or `openai`. |
   | `STT_MODEL` | no | groq → `whisper-large-v3-turbo`, openai → `whisper-1`. |
   | `GROQ_API_KEY` | yes\* | Speech-to-text; \*required for the default provider (free tier at console.groq.com/keys). |
   | `OPENAI_API_KEY` | yes\* | \*Required only if `STT_PROVIDER=openai`. |
   | `STT_PROMPT` | no | Opt-in code-switch primer; empty by default. |

6. **Audio storage** is local disk under `uploads/audio/` (gitignored). Cloud storage is a change in `app/storage.py` only. Recordings are **kept, not discarded after transcription** — the audio is the ground truth, it lets every past entry be re-transcribed when the STT model improves, and it costs well under a megabyte per minute. Consequences to handle later: users need a way to delete a recording, and this belongs in cloud storage before real users.

## 8. How to run it

### Backend

```bash
cd /Users/mhmdnab/Desktop/murmur/voice-journal-backend

# 1. Database (if the container is stopped)
docker start voice-journal-db

# 2. App — note `python -m`; the venv's `uvicorn` script has a stale shebang (§7.3)
.venv/bin/python -m uvicorn app.main:app --reload

# 3. Smoke test
curl http://127.0.0.1:8000/health         # -> {"ok":true}

# Text entry
curl -s -X POST http://127.0.0.1:8000/entries -H "Content-Type: application/json" \
  -d '{"transcript":"I need to call the dentist tomorrow and email Sarah about the lease."}' | python3 -m json.tool

# Audio entry (async): upload -> poll
curl -s -X POST http://127.0.0.1:8000/entries/audio -F "file=@recording.m4a" | python3 -m json.tool
curl -s http://127.0.0.1:8000/entries/<ENTRY_ID>   # poll until status is DONE / FAILED

# List / fetch / toggle a todo
curl -s "http://127.0.0.1:8000/entries?limit=5" | python3 -m json.tool
curl -s http://127.0.0.1:8000/entries/<ENTRY_ID>
curl -s -X PATCH http://127.0.0.1:8000/todos/<TODO_ID> -H "Content-Type: application/json" -d '{"done":true}'

# Insights
curl -s "http://127.0.0.1:8000/insights?days=14" | python3 -m json.tool

# Standalone gates (no DB/server)
python test_extraction.py       # requires ANTHROPIC_API_KEY
python test_transcription.py    # requires GROQ_API_KEY + audio files in samples/
```

One-time setup on a fresh checkout: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`, copy `.env.example` → `.env` and fill in values, then `PATH="$(pwd)/.venv/bin:$PATH" python -m prisma generate && ... db push`.

### Frontend

```bash
cd /Users/mhmdnab/Desktop/murmur/frontend-web-app
npm install          # first time only
npm run dev          # -> http://localhost:3000
```

`.env.local` sets `NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000`; the client
falls back to that value if unset. The backend must be running, and its CORS
allowlist must include the frontend's origin.

## 9. API surface

All data endpoints require `Authorization: Bearer <access token>` (Step 5). The
owner is the authenticated user — there is no `userId` parameter any more.

| Method | Path              | Auth | Purpose                                                             |
| ------ | ----------------- | ---- | ------------------------------------------------------------------ |
| GET    | `/health`         | —    | Liveness check → `{"ok": true}`                                     |
| POST   | `/auth/register`  | —    | Create account (email + password), auto-login → tokens             |
| POST   | `/auth/login`     | —    | Verify creds → access + refresh tokens                             |
| POST   | `/auth/refresh`   | —    | Rotate: verify refresh token → new access + refresh pair           |
| POST   | `/auth/logout`    | —    | Revoke the presented refresh token                                 |
| GET    | `/auth/me`        | ✓    | The current user                                                   |
| POST   | `/entries`        | ✓    | Create an entry from **text**, run extraction, persist (sync)      |
| POST   | `/entries/audio`  | ✓    | **Audio** upload (multipart) → `PENDING`; transcribe+extract in bg |
| GET    | `/entries`        | ✓    | Recent entries (newest first), `limit` param                       |
| GET    | `/entries/{id}`   | ✓    | One entry; 404 if missing **or not yours**; used to poll audio     |
| DELETE | `/entries/{id}`   | ✓    | Delete entry + recording + cascade todos/links                     |
| GET    | `/entries/{id}/audio` | —\* | Stream the recording for playback (\*not gated — see §6d)        |
| DELETE | `/entries/{id}/audio` | ✓ | Remove just the recording, keep the entry                          |
| GET    | `/todos`          | ✓    | All the user's todos, soonest-first; `?done=` filter (Step 8)      |
| PATCH  | `/todos/{id}`     | ✓    | Set a todo's `done` state                                          |
| DELETE | `/todos/{id}`     | ✓    | Delete a todo                                                      |
| GET    | `/insights`       | ✓    | Mood trend, top topics/people, open-todo count over `days`         |

CORS allows `http://localhost:3000` and `http://127.0.0.1:3000` (incl. the
`Authorization` header); deployed origins go in the list in `app/main.py`.

## 10. Known limitations / notes

- **Processing model.** The text path runs extraction synchronously; the audio path runs transcription + extraction via FastAPI **BackgroundTasks** (in-process). A deploy still kills in-flight jobs, but a **boot-time reaper** (Step 11) now marks orphaned `PENDING/PROCESSING` entries `FAILED` so they don't hang — the beta-grade stand-in for a real queue/worker (Celery/RQ/Arq), which stays the proper fix at scale.
- **Audio storage — now Supabase (Step 11).** Recordings live in a **private** Supabase bucket, served only via short-lived signed URLs; `Entry.audioUrl` is an object key, never a public URL. Local-disk storage is gone.
- **No chunking.** Audio over 25 MB is rejected; long recordings will need chunking + stitching later.
- **Code-switch normalization — resolved by Melia (Step 10, §6h).** Speechmatics Melia keeps mixed English+Arabic verbatim in one pass, no primer. The old Whisper+primer path (which collapsed toward one language without the primer) is still reachable via `STT_PROVIDER=groq`.
- **Summary language.** The model still sometimes returns English summaries for Arabizi/mixed entries despite the same-language instruction (prompt left verbatim).
- **Gender in Arabic — fixed (post-Step-10, §6h).** Summaries are now first-person and todos are neutral verbal nouns, so Arabic output no longer guesses (and defaults feminine on) the speaker's gender. Fully-gendered output for a known-male/female user would need an optional `User.gender` field (not built — the neutral fix was preferred). Todo text now comes out in the entry's language (Arabic entry → Arabic todo); same meaning, cosmetic.
- **Per-user timezone (Step 9).** Spoken times compose against the user's IANA timezone → correct UTC instant; the mood trend and calendar bucket by the user's local day. **Forward-only:** todos recorded before Step 9 keep their old UTC composition, and those before Step 8 have `startAt = null` (not re-extracted).
- **Timed vs. untimed is a real product distinction (Step 8), only as good as extraction.** The pipeline only sets a time when one is spoken, which is what will keep the future calendar honest — but a missed or hallucinated time propagates. Verified correct on the gate's discriminating cases (including "tonight" → day-only, dialect times), not on a large corpus.
- **Duration precision.** `Entry.durationSec` is an integer (rounded from the transcription's float seconds).
- **Auth hardened across the stack (Steps 5 + 11).** Bearer token on every data endpoint; rate limiting; **password reset**; **in-app account deletion**; **refresh-token reuse detection** (family revocation). The demo account (`demo@voicejournal.local`) still exists and **must be removed at cutover** (`scripts/remove_demo_account.py`). Still open: web tokens live in `localStorage` (mitigated by reuse detection + 30-min access token; httpOnly cookies are the post-launch fix), social login, and email verification on register.
- **Frontend gaps.** Delete now exists (entries, todos, recordings, whole account). Remaining: the timeline loads up to 50 entries with no pagination, and it does not auto-refresh after a capture. Server-state caching landed in Step 12 (§6j) — revisits now render from cache instead of refetching every navigation. Mood trend/calendar bucket by the user's local day now (Step 9).
- **Word-level STT accuracy — still not solved, now handled inside extraction (Step 10, §6h).** Melia mishears dialect words too (جدّد → جدة); the **merged** Sonnet extraction corrects them in-context and extracts in one call. Verified end-to-end (the جدة clip → "renew passport" todo), but still on a handful of clips from one speaker — evidence, not a benchmark. Harnesses: `benchmark_stt.py`, `benchmark_repair.py`, `test_merged_extraction.py` (and the older `test_dialect_grid.py` for the Whisper+primer path).
- **Merged audio extraction runs on Sonnet 5.** One model call (down from repair+extract two), but all audio extraction is now Sonnet — more exposed to the end of Sonnet's introductory pricing (2026-08-31) than the old Haiku-extraction split. Thinking is disabled to keep output tokens (and cost) down.
- **The correction is not surfaced.** `rawTranscript` is stored exactly as heard; the dialect fix happens *inside* the merged call and is never emitted, so a bad extraction can't be traced to a repaired transcript. (The old two-call path also discarded the repaired text in production — only the debug harness ever showed it.)
- **Untested audio conditions.** Everything was verified with 7–12 second clips through Chrome's fake microphone. Untested: real accents at length, 2+ minute recordings, Safari's `audio/mp4` path, and any mobile browser.

## 11. Where this stands, and what's next

The full loop is built and validated: **speak → transcribe → repair → extract →
browse → act on todos → see trends.** Nothing in the core product is missing. What
remains is a choice about direction, not a backlog of half-finished work.

### The mobile app — done, at web parity

Web-first-then-mobile was the plan, and it held. The mobile app now covers the
whole loop on iOS under auth: capture, journal, entry detail + delete, insights,
and the **to-do list** (§6c → §6f). The single-source sharing (`@shared/*`)
carried the state machines, geometry, grouping, and view logic unchanged — only
the recorder, chart renderer, and audio player were platform-specific. Simulator
driving is solved (`cliclick` + a connected hardware keyboard, §6d).

Not yet done on mobile (matches web's own gaps): timeline pagination, and the
**calendar view** (the next product surface — time extraction landed in Step 8 to
feed it). A real device hasn't been tested (recording/playback verified on the
simulator only), and `expo-file-system` should be promoted from a transitive to a
direct dependency before a production build.

### Before anyone but you uses it

These separated a working prototype from something you'd hand to a friend.
**Steps 5 + 11 closed items 1–5, 7 and 8 below;** the deploy itself is what's
left. Roughly in dependency order:

1. **Auth — done (Steps 5 + 11).** JWT auth + ownership on the API; both clients
   authenticate through the shared client; **rate limiting, password reset, and
   refresh-token reuse detection** landed in Step 11. Remaining: email
   verification on register, social login, and web tokens → httpOnly cookies. §6d/§6i.
2. **Delete — done (Steps 5 + 11).** Entries, todos, and recordings from both
   clients, audio strippable while keeping the entry, and **full password-confirmed
   account deletion** (App Store requirement) that also purges the audio bucket.
3. **Cloud audio storage — done (Step 11).** Private Supabase bucket + short-lived
   signed URLs; `app/storage.py` is the seam. Local disk is gone.
4. **A real worker/queue** (Celery/RQ/Arq) to replace FastAPI `BackgroundTasks`,
   so a restart mid-transcription doesn't strand an entry — **partially mitigated**
   by the Step 11 boot reaper (orphans → `FAILED`); the durable queue is still the
   proper fix at scale.
5. **Production database — done (Step 11).** On Supabase Postgres (Frankfurt) with
   a real migration history (moved off `prisma db push`).
6. **Deploy (Phase 6, pending).** Render (backend) → Vercel (web + privacy/terms) →
   EAS/TestFlight, and run `remove_demo_account.py` at cutover. All prep is done
   (CORS/docs/headers env-gated, `.gitignore`, `expo-file-system` direct dep).

### Quality work, in expected order of payoff

- **Per-user vocabulary priming.** Seed the Whisper prompt with the user's own
  `Topic` and `Person` names — data already stored, and it *compounds*: the more
  someone journals, the better their names transcribe. This is the most
  interesting remaining STT idea. Watch the ~224-token prompt ceiling (§6b).
- **Evaluate an audio-native STT model** (`gpt-4o-transcribe`, Gemini). Likely
  fixes dialect errors at the source without any primer, and drops into the
  existing provider seam with no code change — needs an `OPENAI_API_KEY`
  (currently unset) and costs roughly 9× Groq. Run `test_dialect_grid.py` against
  it before committing.
- **Long-audio chunking.** Anything over 25 MB is rejected outright today, which
  caps entries at a few minutes.
- **Summary language.** Arabizi and mixed entries often get English summaries
  despite the prompt asking for same-language. The extraction prompt was left
  verbatim by design; this is the first thing to tune if native-language
  summaries are wanted.
- **Timeline pagination and refresh-after-capture.** Fine at 20 entries, not at
  500.

### Housekeeping

- **Recreate the backend venv** to fix the stale console-script shebangs (§7.3) —
  `python3 -m venv --clear .venv && .venv/bin/pip install -r requirements.txt`.
- **Move this report to the `murmur/` root.** It stopped being a backend document
  several sections ago.
- **No automated test suite.** Everything here was verified with live scripts
  (`test_extraction.py`, `test_transcription.py`, `test_dialect_grid.py`, and
  browser automation). That was the right trade for a prototype; it stops being
  the right trade the moment two people are changing the code.
