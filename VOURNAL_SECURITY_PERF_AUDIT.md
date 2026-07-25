# vournal — Security, Rate-Limiting & Performance Audit

> **Run `SECURITY_PERF_AUDIT.md` first** (same rules, same phases, same 🛑 STOP markers). This file layers vournal's specifics on top: a pre-filled stack map, seeded findings to **verify rather than trust**, and the concerns that are unique to a voice-diary product.
>
> Run from the `murmur/` root so you can see all three packages.

**Severity legend:** 🔴 Critical · 🟠 High · 🟡 Medium · 🟢 Low · ✅ Already good

---

## 0. Stack map (pre-filled — confirm against the code, correct anything stale)

| Layer | What's there |
| --- | --- |
| Backend | `voice-journal-backend/` — FastAPI (Python), Prisma via `prisma-client-py` (async), PostgreSQL |
| Web | `frontend-web-app/` — Next.js 16 App Router, React 19, Tailwind v4 → Vercel |
| Mobile | `frontend-mobile-app/` — Expo / expo-router / TypeScript → EAS + TestFlight |
| Shared | `@shared/*` — single-source pure-TS modules imported by both frontends |
| Auth | Roll-your-own. Access = 30-min HS256 JWT. Refresh = 60-day opaque random, SHA-256 hashed in DB, rotated on use, revoked on logout. bcrypt direct (passlib dropped) |
| STT | Speechmatics Melia (`melia-1` batch) default; Groq / OpenAI behind the `STT_PROVIDER` seam |
| Extraction | Audio path = merged extraction on Sonnet 5 (`app/extraction_merged.py`, thinking disabled). Text path = Haiku |
| Async | FastAPI `BackgroundTasks`; client polls `GET /entries/{id}` until `DONE` |
| Prod target | Supabase (Postgres + **private** Storage bucket for audio), Render always-on for the API, Resend for email, Vercel for web |
| Dev DB | local Docker Postgres on **5434** (named volume) |

**Money-per-request paths (this is the core abuse surface):** `POST /entries/audio` → Speechmatics → Sonnet 5. `POST /entries` (text) → Haiku. Treat every one of these as a spend endpoint, not just a compute endpoint.

**Data sensitivity:** this app stores raw voice recordings and verbatim transcripts of someone's private diary, including health, relationships, finances, and religion spoken out loud in Lebanese Arabic and English. Treat the audio blob and `rawTranscript` as the most sensitive rows in the system. Every finding that touches them gets a severity bump.

---

## 1. Seeded findings — verify each, do not assume it's still true

These are known or suspected as of the last work session. Confirm current state with file:line evidence, then re-rank.

| # | Sev | Item | Where to look |
| --- | --- | --- | --- |
| 1 | 🔴 | `GET /entries/{id}/audio` is **not bearer-gated** — audio elements can't send headers, so it was left open and mitigated only by high-entropy cuid ids. Anyone with a URL (referrer leak, log, shared screenshot, backup) gets a private recording | audio route in the entries router |
| 2 | 🔴 | **No rate limiting anywhere.** Unauthenticated login/register, plus authenticated audio upload which spends real money per call | app bootstrap / middleware chain |
| 3 | 🔴 | Demo login `demo@voicejournal.local` with a known password is seeded as a dev default. Must not exist in production, or must be disabled by env | seed/bootstrap + auth service |
| 4 | 🟠 | **No per-user usage quota.** The pricing model is a free tier capped by entries — that cap has to be enforced server-side or the free tier *is* the attack | entries service |
| 5 | 🟠 | Audio upload limits: is there a max file size **and** a max duration? Duration drives STT cost, size doesn't | audio upload handler |
| 6 | 🟠 | `BackgroundTasks` has no queue, no bound on concurrent jobs, no retry, no dead-letter. A burst of uploads competes for one instance; a deploy mid-job loses the work silently | audio pipeline |
| 7 | 🟠 | Security headers / CSP on the web app; helmet-equivalent on the API | `next.config`, FastAPI middleware |
| 8 | 🟠 | Where does the **web** client store the access + refresh token? (mobile uses expo-secure-store ✅.) `localStorage` + a 60-day refresh token is an XSS-to-permanent-account-takeover chain | shared client + web auth provider |
| 9 | 🟡 | JWT secret minimum length enforced at boot? Anything under 32 chars for HS256 is weak | config validation |
| 10 | 🟡 | 500 responses: generic in production, or does the FastAPI handler return internal messages? Also check `/docs` + `/openapi.json` are gated in production | exception handler, app init |
| 11 | 🟡 | Are transcripts, prompts, or LLM responses written to logs? That's diary content in a log aggregator | STT + extraction modules |
| 12 | 🟡 | CORS allow-list is env-driven and does **not** reflect arbitrary origins | app init |
| 13 | 🟡 | Explicit request body-size limit for the JSON routes (separate from the audio route) | app init / proxy |
| 14 | 🟡 | `prisma-client-py` maintenance status and pinned versions — auth/crypto and ORM libs are the ones you can't afford to have abandoned | `pyproject.toml` |
| 15 | 🟢 | Dependency scanning in CI (`pip-audit`, `npm audit`, Dependabot) | CI config |

Already verified good in earlier work — re-confirm, don't re-litigate: all data endpoints require a bearer token; the demo-user fallback and client-supplied `userId` were removed; owner is always the current user; cross-user access returns 404; refresh tokens are hashed + rotated; the shared client does 401 → refresh-once → retry with **single-flight** refresh.

---

## 2. vournal-specific checks (in addition to the generic Phase 1)

### 2.1 Audio & transcript handling
- Is the Supabase Storage bucket **private**, with no public URL path, and are objects served only via **short-lived signed URLs** (minutes, not hours)? Signed URLs replace finding #1 — this is the single fix that closes it properly.
- Do signed URLs get regenerated per playback rather than persisted in the DB or in client state?
- Does `Entry.audioUrl` store a storage *reference*, never a fetchable public URL?
- Are uploaded files stored under randomized keys namespaced by user, so a key can't be walked?
- Encryption at rest for both the bucket and the database.
- Does deleting an entry actually delete the blob, not just the row? Same for `DELETE /entries/{id}/audio`.

### 2.2 Account deletion (also an App Store requirement)
- Is there an in-app, user-initiated **full account deletion** endpoint?
- Does it remove: all entries, todos, audio blobs, refresh tokens, and any orphaned shared `Topic` / `Person` rows that are now user-identifying?
- Is it idempotent, and does it revoke all sessions immediately?

### 2.3 Upload validation
- Content-type and magic-byte check on the uploaded audio, not just the declared MIME or the `.m4a` extension.
- Duration probe **before** dispatching to STT — reject long files up front rather than after paying for them.
- Whatever decodes or probes the file: is it handed untrusted bytes safely, and is it a maintained dependency?
- The mobile client sends a `{ bytes, name, type }` multipart part (Expo SDK 54+ fetch quirk) — make sure the server validates that part rather than trusting the client-declared `name`/`type`.

### 2.4 Prompt injection & LLM output trust
- The transcript goes straight into an LLM prompt. A user can speak instructions into their own journal. Confirm that:
  - Transcript content is clearly delimited from instructions in the prompt.
  - The extraction response is **schema-validated** before it touches the DB — no trusting the model to return well-formed JSON, no `eval`-style parsing.
  - Extraction output can never influence anything privileged: no tool calls, no SQL, no file paths, no URLs rendered as links without escaping.
- Blast radius is currently self-inflicted (a user's own journal), so this is 🟡 — but it becomes 🔴 the moment any extracted content is shared, exported, or shown to another user.

### 2.5 Spend controls (economic DoS)
- Per-account daily/monthly caps on audio minutes and extraction calls, enforced server-side and independent of the per-IP throttle.
- Hard ceiling per single request (max duration) **and** per account per window (max total minutes).
- Provider-side spend alerts on Anthropic, Speechmatics, and Groq.
- Note on the roadmap: **all audio extraction runs on Sonnet 5, which is exposed to the end of introductory pricing on 2026-08-31.** Cost modelling and any caps should be sized against post-change pricing, not current.

### 2.6 Auth specifics
- HS256 with the algorithm pinned on verify (reject a token declaring anything else).
- Refresh-token **reuse detection**: if a rotated-away token is presented, revoke the whole family — a stolen 60-day refresh token is otherwise a 60-day account takeover.
- Password reset via Resend: single-use, short expiry, non-enumerable response, and it must revoke existing refresh tokens.
- Registration and reset endpoints must not reveal whether an email exists.

### 2.7 Supabase specifics
- The backend connects with a privileged key — confirm it is server-only and never reaches either frontend bundle.
- If any client ever talks to Supabase directly, RLS policies are mandatory; if not, document explicitly that the API is the only path to the data.
- Check the pooler mode and port used for Prisma, and whether prepared statements need disabling for the pooled connection.

---

## 3. Performance — vournal specifics (in addition to generic Phase 3)

- **Polling loop:** the client polls `GET /entries/{id}` until `DONE`. Check the interval and whether it backs off — a tight poll from every capturing client is a self-inflicted load pattern. Consider a longer interval after the first few seconds, and make sure the poll response is cheap (status-only, not a full entry hydrate).
- **Indexes** for the three surfaces that now exist: entries by `(userId, createdAt)`; todos by `(userId, startAt)` and `(userId, done)` for the soonest-first `GET /todos`; calendar range queries on `startAt`; and the entry↔topic/person join tables.
- **Audio streaming route:** does it support HTTP range requests (seek/scrub), and does it stream rather than load the file into memory?
- **Connection pool** sized for a single always-on Render instance against the Supabase pooler — not the default.
- Compression and `ETag`/`Cache-Control` on the JSON GETs. Entry and todo data is per-user and must be `private`, never CDN-cacheable.
- **Deploy safety:** because background work is in-process, a deploy kills in-flight extractions. At minimum, mark orphaned `PROCESSING` entries as failed on boot and let the user retry; properly, move to a queue.
- Web: server rendering and code splitting for the heavier views (insights chart, calendar). Mobile: startup time to first usable record button.
- Observability: request timing, extraction success/failure rate, STT latency, **and cost per entry** — the last one is a product metric, not just an ops one.

---

## 4. Ordering suggestion for the roadmap

Signed URLs + private bucket, rate limiting, and per-user quotas are the three that make this safe to put in front of real users; they also happen to be exactly the deferred production-hardening track. Everything else can follow. Deliver the roadmap and 🛑 **STOP** before implementing.
