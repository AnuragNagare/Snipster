# Deploying Snipster on Vercel

**What goes where.** Vercel runs the UI and the API as one project. Your PC runs the worker
(Whisper on the GPU, ffmpeg, yt-dlp, Gemini). They meet at three hosted services: Postgres,
Redis, and R2 storage. Nothing on your PC accepts incoming connections.

There is no Docker anywhere in this setup.

`vercel.json` already describes the project — a Vite frontend and a FastAPI backend, with
`/api/*` and `/health` routed to the backend and everything else to the UI.

> **Vercel Services is a beta feature.** If it ever misbehaves, the fallback is two Vercel
> projects (root directory `frontend` and `backend`) with a `/api` rewrite from the first to
> the second.

---

## 0. Before you start

| You need | Notes |
|---|---|
| A Vercel account | Hobby is enough to start |
| Cloudflare R2 bucket | Already set up for the app |
| A hosted Redis | See step 2 |
| Google Cloud OAuth client | For YouTube sign-in (step 6) |
| ffmpeg on your PC | `winget install Gyan.FFmpeg` |

Delete the two folders `_automation_backup/` and `_backup_manual_stage/` when you no longer need
them. `.vercelignore` keeps them out of the upload either way.

## 1. Database — Neon

In the Vercel dashboard: **Storage → Create → Neon**, or `vercel integration add neon`. Connecting
it to the project adds `DATABASE_URL` for you. You also need that same URL on your PC (step 4):
copy it from the Neon console, or run `vercel env pull`.

Create the tables once, from your PC:

```bash
cd backend
.venv\Scripts\activate
alembic upgrade head
```

Run it again after any future change to `backend/migrations/`. Vercel does not run migrations.

## 2. Redis — the job queue

Create a hosted Redis from the Vercel Marketplace (Redis or Upstash) or at redis.io. You want a
TLS address that starts with `rediss://`.

**Pick a fixed-price plan.** The worker checks the queue constantly. A pay-per-command plan
(Upstash pay-as-you-go) counts every check and can cost a few dollars a month for nothing.

## 3. Environment variables on Vercel

Project → **Settings → Environment Variables**. Add these for **Production**:

| Variable | Value |
|---|---|
| `DATABASE_URL` | Added by the Neon integration |
| `REDIS_URL` | `rediss://…` from step 2 |
| `STORAGE_ENDPOINT`, `STORAGE_BUCKET`, `STORAGE_ACCESS_KEY`, `STORAGE_SECRET_KEY`, `STORAGE_PUBLIC_BASE_URL` | Your R2 values |
| `ADMIN_PASSWORD` | The password for the login screen. **Required** |
| `SECRET_KEY` | 32+ random characters: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `TOKEN_ENCRYPTION_KEY` | Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Never change it afterwards |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | From Google Cloud |
| `GOOGLE_REDIRECT_URI` | `https://<your-domain>/api/auth/google/callback` |
| `FRONTEND_URL` | `https://<your-domain>` |

Optional on Vercel: `YOUTUBE_API_KEY`. Without it the **Dashboard** tab still lists your posts but shows no view counts.

Not needed on Vercel: `GEMINI_API_KEY` and the `WHISPER_*` / `FFMPEG_*` settings. The API never
uses them.

**Without `ADMIN_PASSWORD` the deployed app locks itself.** Every API call answers 503 and the UI
says so. This is on purpose: a Vercel address is public, and without a password anyone could
upload videos and publish to your channel.

## 4. The worker on your PC

`backend/.env` on your PC needs the **same** `DATABASE_URL`, `REDIS_URL`, `STORAGE_*` and
`TOKEN_ENCRYPTION_KEY` as Vercel, plus `GEMINI_API_KEY` and the Whisper settings.

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-worker.txt
celery -A app.workers.celery_app worker --loglevel=info -P solo
```

When it starts it reports its GPU and encoder to Redis, which is what the CPU/GPU badge on the
Processing page shows. Until a worker has started at least once, that badge stays on
"Detecting compute…".

### Autopilot scheduler (optional)

Autopilot picks videos from your own channel on a schedule, and also sends out posts you scheduled
by hand. It needs a second process, the scheduler, next to the worker:

```bash
cd backend
.venv\Scripts\activate
celery -A app.workers.celery_app beat --loglevel=info
```

Also add to the worker's `backend/.env`: `YOUTUBE_API_KEY` (YouTube Data API v3 key), and
`GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` (the same values as on Vercel; the worker now refreshes
the YouTube sign-in itself). Run `alembic upgrade head` first: autopilot adds two tables.

Autopilot is OFF and in dry-run until you change it on the **Activity** page, and uploads default
to private. Scheduled posts can start a few minutes late while the worker is busy with a long
transcription, because the worker handles one task at a time.

**Alerts and "the PC is off" warnings** (worker `backend/.env`):

| Variable | What it does |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram message when a post fails, a video fails, or autopilot cannot start. Create a bot with @BotFather, message it once, then open `https://api.telegram.org/bot<token>/getUpdates` to find your chat id. |
| `HEALTHCHECK_URL` | Pinged after every healthy tick. Create a check at healthchecks.io (free), set period about 10 minutes and grace time of 1 hour or more, and point its notifications at your email or Telegram. When the PC is off the pings stop and *that service* tells you. The PC cannot report that it is off. |

**Rolling it out safely** (do these in order; each step is reversible on the Activity page):

1. `alembic upgrade head`, restart the worker, start the scheduler (`... beat`).
2. Activity page: the "Scheduler seen" badge should turn green within a few minutes.
3. Turn **Autopilot on** with **Dry run on** and **Approve clips automatically on**. Press **Run now**.
   Watch it pick a video, process it and show its clips under "Coming up", then as "rehearsal".
   Nothing is uploaded.
4. Check the clips and captions look right. Then turn **Dry run off** with visibility **Private**.
   Let one real post go out and open it on YouTube.
5. Only then move visibility to Unlisted or Public.

Switching Autopilot off stops it picking, approving and posting at once; posts it had already
scheduled are held (not lost) and go out if you switch it back on. Posts you scheduled by hand are
never held. Dry run only ever applies to autopilot's posts, never to one you start yourself.

The PC has to be on and the worker running for videos to process. Videos you submit while it is
off wait in the queue.

## 5. Cloudflare R2 — allow browser uploads

The browser uploads video files **directly to R2**, because a Vercel Function rejects request
bodies over 4.5 MB. R2 therefore has to allow it. Cloudflare dashboard → R2 → your bucket →
**Settings → CORS policy**:

```json
[
  {
    "AllowedOrigins": ["https://<your-domain>", "http://localhost:5173"],
    "AllowedMethods": ["PUT", "GET", "HEAD"],
    "AllowedHeaders": ["Content-Type"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600
  }
]
```

If an upload fails with "Could not reach storage", this is the first thing to check.

## 6. Google sign-in (YouTube)

Google Cloud → APIs & Services → Credentials → your OAuth client → **Authorized redirect URIs** →
add exactly `https://<your-domain>/api/auth/google/callback` (it must match `GOOGLE_REDIRECT_URI`
character for character). Leaving the consent screen in *Testing* mode makes the sign-in expire
after 7 days; see `youtube-setup-plan.md`.

Sign-in works on your **production domain only**. Vercel preview deployments each get a different
address, which Google will not accept.

## 7. Deploy

The folder is not a git repository, so use the CLI:

```bash
npx vercel login
npx vercel link            # create or pick the project
npx vercel --prod
```

Or push to GitHub and import the repository in Vercel — either way the project root is this
folder, and Vercel reads `vercel.json`.

## 8. Check it

1. Open the site → you get a sign-in page → enter `ADMIN_PASSWORD`.
2. **Settings → Connect** on YouTube → Google → back on the site showing your channel.
3. Home → upload a small video. The button shows `Uploading 42%`, then the Processing page.
4. Watch the worker terminal on your PC pick it up.

## If it misbehaves

| Symptom | Cause |
|---|---|
| UI says "This deployment has no password set" | `ADMIN_PASSWORD` missing in Vercel — add it and redeploy |
| API won't start; log mentions `SECRET_KEY` | It is the placeholder or under 32 characters |
| "Could not reach storage" on upload | R2 CORS policy (step 5) |
| Upload works but nothing is processed | The worker is not running, or its `REDIS_URL` differs from Vercel's |
| Processing page badge stuck on "Detecting compute…" | No worker has started since Redis was last cleared; restart the worker |
| `redirect_uri_mismatch` on Connect | Redirect URI in Google Cloud differs from `GOOGLE_REDIRECT_URI` |
| 500 on every API call right after deploy | Missing `DATABASE_URL`, or `alembic upgrade head` was never run |
| Videos only process while you are at the PC | By design — the worker lives there |

## Limits to know about

- **One shared password**, not user accounts. Anyone who has it controls the channel.
- **The queue is Redis, not Vercel.** Vercel's own queue integration runs tasks inside functions
  (5-minute limit on Hobby, no GPU), which is too small for Whisper and ffmpeg.
- **Vercel Daemons** can run a worker without a PC, but they are built from a Dockerfile, are
  CPU-only (up to 4 vCPUs) and Hobby includes about 5 CPU-hours a month. That does not fit this
  workload.
- **YouTube sign-in expires** after about an hour of use; the refresh is not implemented in this
  version of the app.
