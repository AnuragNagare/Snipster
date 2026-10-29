# Snipster 🎬

> Turn one long-form YouTube video into multiple YouTube Shorts + Instagram Reels — with you in control of every publish.

## How it runs

```
Browser ──► Vercel ── UI (React) + API (FastAPI) ──► Neon Postgres
                          │   └─► Cloudflare R2  ◄── browser uploads videos straight to R2
                          ▼
                    Redis (job queue)
                          ▲
              Worker on your PC ── Whisper (GPU) · ffmpeg · yt-dlp · Gemini
```

- **Vercel** hosts the UI and the API. It cannot run the processing: there is no GPU, no
  long-running process, and a function is capped at 5 minutes.
- **Your PC** runs the worker. It only makes *outgoing* connections (database, queue, storage,
  Gemini), so nothing on your PC is exposed to the internet. If the PC is off, jobs wait in the
  queue and run when it comes back.

## Stack

| Layer | Tech |
|-------|------|
| Frontend | React + Vite + TypeScript + Tailwind |
| API | FastAPI (Vercel Function) |
| Worker | Celery (on your PC) |
| Database | Neon Postgres |
| Queue | Redis |
| Storage | Cloudflare R2 |
| Transcription | faster-whisper |
| LLM | Gemini |
| Rendering | ffmpeg |
| Publishing | YouTube Data API v3 · Instagram Graph API |

## Deploying

See **[DEPLOY.md](DEPLOY.md)** — Vercel setup, the worker on your PC, and the checks to run afterwards.

## Running locally

You need Python 3.12+, Node 20+, ffmpeg (`winget install Gyan.FFmpeg`), and a Postgres + Redis
you can reach. The simplest is the same hosted Neon and Redis you use in production.

```bash
cp backend/.env.example backend/.env     # fill it in

cd backend
python -m venv .venv
.venv\Scripts\activate                   # Windows
pip install -r requirements-worker.txt   # API + worker + local tools
alembic upgrade head                     # create the tables
```

Run three things, each in its own terminal:

```bash
# 1. API (from backend/)
uvicorn app.main:app --reload --port 8000

# 2. Worker (from backend/)
celery -A app.workers.celery_app worker --loglevel=info -P solo

# 3. UI (from frontend/)
npm install
npm run dev
```

App at http://localhost:5173 · API docs at http://localhost:8000/docs

Locally the API needs no password. Set `ADMIN_PASSWORD` (and `SECRET_KEY`) in `backend/.env` to
try the login screen.

### Tests

```bash
cd backend
pip install -r requirements-dev.txt
pytest
```

## Project structure

```
Snipster/
├── frontend/              # React + Vite SPA
├── backend/
│   ├── app/               # FastAPI app, services, Celery tasks
│   ├── migrations/        # Alembic
│   ├── requirements.txt          # what the Vercel API installs (kept small)
│   └── requirements-worker.txt   # + Whisper, yt-dlp, … for your PC
├── vercel.json            # one project, two services: frontend + backend
└── DEPLOY.md
```

## Milestones

| # | Milestone | Status |
|---|-----------|--------|
| M1 | Scaffold + Infrastructure | 🔄 In Progress |
| M2 | Upload → Transcription → Clip Selection | ⏳ Pending |
| M3 | ffmpeg Render Pipeline | ⏳ Pending |
| M4 | Review Dashboard | ⏳ Pending |
| M5 | YouTube OAuth + Publish | ⏳ Pending |
| M6 | Instagram OAuth + Publish | ⏳ Pending |
| M7 | Polish & Reliability | ⏳ Pending |
