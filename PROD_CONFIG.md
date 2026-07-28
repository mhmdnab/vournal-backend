# Production configuration — the wiring behind a working launch

The code is done; production is a **config chain** across three dashboards. This
is the exact list. Nothing here is a code change — it's environment variables and
one DNS record. Do it once and the password-reset flow, the API, and the app all
work end-to-end.

The three moving parts:

- **Backend** — FastAPI on **Render** → `https://vournal-backend.onrender.com`
- **Landing** — Next.js on **Vercel** → `https://vournal.app` (marketing + the
  `/forgot-password` and `/reset-password` pages)
- **Mobile** — Expo/React Native → talks to the Render backend

---

## 1. Render (backend) — Environment Variables

Set these in the Render service → **Environment**. Values marked 🔒 are secrets
(already in your local `.env` — copy them; never commit them).

| Key | Value | Why |
| --- | --- | --- |
| `DATABASE_URL` 🔒 | Supabase **pooled** string (port 6543, `?pgbouncer=true`) | Runtime queries |
| `DIRECT_URL` 🔒 | Supabase **direct** string (port 5432) | Migrations only |
| `SUPABASE_URL` | `https://<ref>.supabase.co` | Audio storage |
| `SUPABASE_SERVICE_ROLE_KEY` 🔒 | service_role key | Backend-only storage access |
| `SUPABASE_AUDIO_BUCKET` | `vournal-audio` | Private audio bucket |
| `ANTHROPIC_API_KEY` 🔒 | your key | Extraction |
| `SPEECHMATICS_API_KEY` 🔒 | your key | Melia STT |
| `JWT_SECRET` 🔒 | 32+ byte hex | Signs access tokens |
| `RESEND_API_KEY` 🔒 | your key | Password-reset email |
| `RESEND_FROM` | `vournal <noreply@vournal.app>` | Verified sender domain |
| **`WEB_BASE_URL`** | **`https://www.vournal.app`** | Builds the reset link in the email — **https, not http**; match the origin the site actually serves from (www) |
| **`CORS_ORIGINS`** | **`https://vournal.app,https://www.vournal.app`** | Lets the reset pages call the API (else the browser blocks the preflight). **Include BOTH www and non-www** — the browser sends whichever the site redirects to. |
| `ENABLE_DOCS` | *(leave unset)* | Keeps `/docs` off in prod |

> **The two that are easy to miss:** `WEB_BASE_URL` and `CORS_ORIGINS`. Without
> `WEB_BASE_URL=https://www.vournal.app` the reset email links to `localhost`.
> Without `CORS_ORIGINS` containing the exact serving origin (`https://www.vournal.app`),
> the reset page's preflight is rejected with a 400 and no `Access-Control-Allow-Origin`
> header — the "Couldn't reach the server" you saw. Include both www and non-www.

Build & start commands (native Python, no Docker) are already documented in the
README / Render dashboard — unchanged by this.

## 2. Vercel (landing) — Environment Variables + domain

- **Environment Variable:** `NEXT_PUBLIC_API_BASE_URL = https://vournal-backend.onrender.com`
  (Production scope). Without it the reset pages try `127.0.0.1:8000` and fail.
- **Domain:** point `vournal.app` at this Vercel project (Vercel → Domains → add
  `vournal.app`, then set the DNS record it shows at your registrar). Vercel issues
  the TLS cert automatically — that's what makes `WEB_BASE_URL` https valid.
- Redeploy after setting the env var (Vercel only inlines `NEXT_PUBLIC_*` at build time).

## 3. Resend (email) — domain

- `vournal.app` is already added in Resend. Confirm its **DKIM/SPF records are
  verified** (green) so reset mail isn't spam-foldered. `RESEND_FROM` must be on
  this verified domain.

## 4. Mobile (Expo) — API base

- The app reads `EXPO_PUBLIC_API_BASE_URL`. Local `.env` already has
  `https://vournal-backend.onrender.com`.
- **For an EAS build**, a local `.env` is NOT bundled — set the value in `eas.json`
  (the `production` profile's `env` block, already scaffolded) or as an EAS secret,
  so the TestFlight build points at prod.

---

## Cutover checklist (run once, in order)

1. ☐ Render: all env vars above set, `WEB_BASE_URL` + `CORS_ORIGINS` included.
2. ☐ Vercel: `NEXT_PUBLIC_API_BASE_URL` set; `vournal.app` domain pointed + TLS green.
3. ☐ Resend: domain verified.
4. ☐ Smoke-test password reset end-to-end: request from the app → email arrives →
   link opens `https://vournal.app/reset-password?token=…` → new password works,
   old one rejected.
5. ☐ Remove the demo account: `python scripts/remove_demo_account.py` (see script).
6. ☐ (Optional) Render paid instance to kill the ~60 s cold-start on the free tier.

## Note on the free-tier cold start

Render's free instance spins down when idle; the first request after that takes
~50–60 s while it wakes. Fine for a private beta; upgrade to a paid instance (or add
an external keep-warm ping) before wider testing if that first-hit latency matters.
