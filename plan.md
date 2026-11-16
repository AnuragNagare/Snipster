# Snipster: Hindi support + language-aware captions: Plan

Status: Phases 1-6 DONE in code. Still unverified: a real Hindi video end to end (Whisper accuracy, Gemini Hindi caption), and the migration on Neon. Track progress in `changes.md`; background in `context.md`.

(The previous `plan.md` was the finished pipeline-speedup plan. It is still in git history at commit `cd37b23`.)

## Goal

1. Videos in Hindi transcribe correctly.
2. The app detects the spoken language (English or Hindi) and writes the post caption, hashtags
   and burned-in subtitles in that language. The caption stays editable by the user.

Out of scope for now: auto-finding trending videos and unattended posting (see "Deferred track").

## Phases: 6 (Hindi work, done) + 8 (automation track A1-A8, planned, below)

### Phase 1: Multilingual transcription [DONE]
- Change the default `WHISPER_MODEL` from `base.en` to a multilingual model. Start with `medium`
  with `int8_float16` on the RTX 3050 6 GB. `large-v3` is a later option if VRAM allows.
- Pass `language=None` so faster-whisper auto-detects. Add an optional `WHISPER_LANGUAGE` override.
- `transcribe()` keeps returning the word list (the renderer and LLM read that shape). The detected
  language is returned separately, so nothing downstream breaks.
- Files: `core/config.py`, `services/transcription.py`, `.env.example`.

### Phase 2: Store the language [DONE]
- New nullable column `source_videos.language` (String(8)).
- Alembic migration (second revision after `54b0d5b7980b`).
- `transcribe_video_task` saves it. Expose it on `VideoStatusResponse` and `ClipResponse`.
- Files: `db/models.py`, `db/schemas.py`, `workers/tasks.py`, `migrations/versions/`.

### Phase 3: Language-aware Gemini prompts [DONE]
- `select_clips` and `generate_caption` get the language and instruct Gemini to write caption and
  hashtags in it (Devanagari for `hi`). Keep the "respond ONLY with JSON" contract.
- Fix the transcript sampling that caps clip selection at about the first 12 minutes (every 3rd
  word, 600 lines). Optional; NOT done.
- Files: `services/llm.py`, `workers/tasks.py`.

### Phase 4: Devanagari-safe burned subtitles [DONE]
- Set a Devanagari-capable font in `_SUBTITLE_STYLE` (`FontName=Nirmala UI` on Windows) when the
  language is not English. Verify libass shapes conjuncts correctly.
- Keep the 4-words-per-line grouping. Check that Hindi lines are not too wide at FontSize 18.
- Files: `services/renderer.py` (the language is passed in from `render_clip_task`).

### Phase 5: Frontend [DONE]
- Show the detected language (badge on Review).
- Caption/hashtag editing already works through `ClipEditor`. Confirm Devanagari displays and saves.
- Burned-in subtitles are NOT editable by hand. That needs a separate feature and is not in this plan.
- Files: `types/index.ts`, `pages/Review.tsx`, `components/ClipCard.tsx`.

### Phase 6: Tests and real-world verification [PARTLY DONE: see changes.md]
- Unit tests: language passes through; the migration applies; the prompt includes the language.
- Real check: one Hindi clip and one English clip through the whole pipeline on the PC worker.
  Look at the transcript, the subtitles in the rendered file, and the caption.
- Record the measured Hindi transcription time on the GPU.

## Automation track: A1-A8 DONE in code. Not yet run against real YouTube, Redis, Neon or a browser

Goal: on a schedule, the app picks a video from an allowed source, clips and captions it, approves
the clips that pass checks, posts them to YouTube, tells you when something breaks, and shows all of
it on an Activity page.

Decisions already made
- Source: the user's OWN channel uploads only. Trending videos from other creators are out: copying
  them is infringement and "reused content" under YouTube policy. Creative Commons is not planned.
- Hosting: Vercel (UI + API) + Neon + R2 + hosted Redis, as now. The worker stays on the PC for now.
  A GPU-cloud worker is a later, separate plan.
- Defaults I will use unless told otherwise (all become settings, changeable on the dashboard):
  1 video per run, 2 clips posted per video, at most 3 posts per day, first uploads PRIVATE,
  dry-run ON until the user switches it off, alerts via Telegram.

User actions (cannot be done in code; the automation is not fully live without them)
- U1. Move the Google OAuth consent screen from Testing to Production (else the sign-in dies after 7 days).
- U2. Pass the YouTube API compliance audit (else every upload is forced private).
- U3. Request a quota increase if more than about 6 uploads a day are wanted (an upload costs 1,600 of 10,000 units).
- U4. Create a YouTube Data API key (for reading the channel's uploads) and a Telegram bot token + chat id.
- U5. Add `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` to the worker's `backend/.env` (today only the API has them).

### Phase A1: YouTube token refresh [DONE]
- `services/youtube.py::_get_valid_token`: if the stored expiry is near, call Google's token endpoint
  with the refresh token, save the new access token and expiry (encrypted), return it.
- `routes/auth.py::google_callback` never saves `expires_in` today, so `yt_token_expiry` is always empty;
  save it.
- A revoked or missing refresh token fails the job with a clear message and raises an alert (A6).
- Files: `services/youtube.py`, `routes/auth.py`. Tests mock Google's endpoint.

### Phase A2: Data model and settings [DONE]
- New table `autopilot_settings` (single row): `enabled`, `dry_run`, `auto_approve`, `posts_per_day`,
  `clips_per_video`, `run_times` (list of hours), `privacy` (private / unlisted / public), `last_tick_at`.
- New table `autopilot_sources`: `youtube_video_id` (unique), `title`, `duration`, `status`
  (picked / processing / done / skipped / failed), `source_video_id` (FK), `picked_at`, `reason`.
- `PublishJob` gets nothing new; `youtube.py` reads `privacy` from settings instead of the hardcoded `public`.
- Alembic migration after `b4e6d1a73c20`. The user applies it.
- Files: `db/models.py`, `db/schemas.py`, `migrations/versions/`, `services/youtube.py`.

### Phase A3: Source picker [DONE]
- New `services/autopilot.py`: read the channel's uploads (uploads playlist = `UU` + channel id minus
  `UC`; `playlistItems.list` + `videos.list` with the API key; about 1 quota unit per call, no new OAuth scope).
- Skip: ids already in `autopilot_sources`, videos under a minimum length, videos that are themselves
  Shorts, private or live videos.
- Pick the oldest unused first by default. Insert a row, create the `SourceVideo`
  (`ownership_confirmed=True`, since it is the user's own channel), call `start_processing_pipeline(from_url=True)`.
- Files: `services/autopilot.py`, `config.py` (`YOUTUBE_API_KEY`).

### Phase A4: Scheduler and dispatcher [DONE]
- Celery beat on the PC as its own process (`celery -A app.workers.celery_app beat`), one task
  `autopilot_tick_task` every few minutes. It is idempotent, so a missed or doubled tick is harmless.
- Each tick: (1) stop if disabled; (2) pick a source if a run time has passed and the daily cap allows;
  (3) the dispatcher: queue `SCHEDULED` publish jobs whose `scheduled_at` has passed, flipping them to
  `QUEUED` with one conditional UPDATE so two ticks cannot dispatch the same job; (4) write `last_tick_at`.
- Vercel Cron is NOT the scheduler: Hobby allows one run per day.
- Files: `workers/celery_app.py` (beat schedule), `workers/tasks.py`, `services/autopilot.py`, `DEPLOY.md`.

### Phase A5: Auto-approve and publish gate [DONE]
- When every clip of an autopilot video is rendered and captioned, the tick approves the best
  `clips_per_video` and rejects the rest.
- Checks before approving: render `done`, non-empty caption, duration 15-60 s, `storage_key` present, not
  the same time window as an already-approved clip. A failed check skips the clip and records why.
- Approved clips get `PublishJob`s spaced through the day (`scheduled_at`) within `posts_per_day`.
- `dry_run`: everything runs except the upload; the job is marked with a "dry run" note and no YouTube call is made.
- Files: `services/autopilot.py`, `routes/publish.py` (shared job creation), `workers/tasks.py`.

### Phase A6: Alerts and dead-man's switch [DONE]
- `services/alerts.py`: send a Telegram message (HTTP call; email as a possible later addition).
- Alert on: final publish failure, token refresh failure, pipeline `FAILED`, daily cap reached,
  tick errors.
- The PC cannot report that it is itself off. The tick pings an external dead-man's-switch URL
  (for example healthchecks.io); it alerts you when pings stop. Needs one env var, `HEALTHCHECK_URL`.
- Files: `services/alerts.py`, `workers/tasks.py`, `config.py`.

### Phase A7: Activity API and dashboard [DONE]
- API: `GET /api/activity` (recent publish jobs with video title, clip caption, status, YouTube link,
  error), `GET` and `PATCH /api/autopilot/settings`, `POST /api/autopilot/run-now`, and a kill switch
  (`enabled=false`). The existing `POST /clips/publish-jobs/{id}/retry` is reused for failed posts.
- UI: new "Activity" page and nav link: posted list, failures with Retry, upcoming queue, and the
  settings (on/off, dry run, posts per day, times, privacy). Shows worker health from `last_tick_at`.
- Files: `routes/autopilot.py` (new), `main.py`, `frontend/src/pages/Activity.tsx`, `App.tsx`, `api/index.ts`, `types/index.ts`.

### Phase A8: Tests, dry run, deploy notes [DONE]
- Unit tests: picker skips used and short videos; daily cap; dispatcher cannot double-dispatch; token
  refresh (mocked); approve gate; dry run makes no upload call; alerts fire on failure.
- Real check, in this order: dry run for a day, then one live post as PRIVATE, then switch privacy.
- `DEPLOY.md`: how to run beat, the new env vars, and the dead-man's-switch setup.

### Automation risks
- Duplicate uploads: if YouTube accepts an upload but the job then fails to save, a retry posts it twice.
  Needs a check on the job's `platform_post_id` before retrying.
- yt-dlp can be blocked or rate-limited even from a home IP when downloads are frequent.
- When the PC is off, nothing runs and only the dead-man's switch tells you.
- (Reduced in A5) Double upload: a job that already has a YouTube id is never uploaded again, and an upload that cannot be saved is not retried. A crash of the whole worker between YouTube accepting the file and the save can still double-post.
- Clips are chosen by Gemini with no human review once auto-approve is on; the gate in A5 reduces but
  does not remove bad posts. Keep privacy at private/unlisted until the output has been checked.
- Gemini only reads about the first 12 minutes of a long transcript (known gap, not part of this track).

## Known risks
- Hinglish output may be Devanagari or Roman unpredictably. This cannot be fully fixed.
- `medium` on the 6 GB GPU is slower than `base.en`. The measured number goes in `changes.md`.
- Hindi word timestamps are less accurate, so subtitles may drift slightly.
- The migration touches the production Neon database. It must be run by hand (`alembic upgrade head`)
  after review. Vercel does not run migrations.

## Launch checklist (what is left after A1-A8)

Status key: [ ] not done, [x] done, [blocked] I tried and was not allowed, you must do it.

Code and docs
- [x] Hindi support, caption language, editable subtitles, autopilot A1-A8 written and tested (296 backend tests, frontend type-checks).
- [x] Committed and pushed to GitHub (main = 9e7cb9c).
- [x] Deployed to Vercel: https://snipster-two.vercel.app (2026-11-16). Verified: health, login, database, Redis.

Your database and PC (run by you, not by me)
- [x] (applied 2026-11-16) Apply the migrations to Neon: `cd backend`, activate the venv, `alembic upgrade head`. They only add things (`source_videos.language`, `clips.subtitles`, two autopilot tables, `publish_jobs.autopilot`); nothing is dropped or rewritten. Until this is done the deployed API fails on the new columns.
- [blocked] Worker `backend/.env`: add `YOUTUBE_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `HEALTHCHECK_URL` (see `.env.example`). I did not read your `.env`, so I do not know which are already set.
- [ ] Start the worker and, in a second terminal, `celery -A app.workers.celery_app beat --loglevel=info`.

Google (your account)
- [ ] U1 Move the OAuth consent screen from Testing to Production.
- [ ] U2 Pass the YouTube API compliance audit (until then every upload is forced private).
- [ ] U3 Request a quota increase if more than about 6 uploads a day are wanted.
- [ ] U4 Create the YouTube Data API key, the Telegram bot, and the healthchecks.io check.

Rollout (DEPLOY.md, in order)
- [ ] Autopilot ON + Dry run ON + auto-approve ON, press Run now, watch rehearsals on the Activity page.
- [ ] Dry run OFF, visibility Private, let one real post go out and open it on YouTube.
- [ ] Only then Unlisted or Public.

Not verified yet
- [ ] A real Hindi video through Whisper `medium` and Gemini.
- [ ] The UI in a browser (language selector, subtitle editor, Activity page).
- [ ] Real YouTube, Telegram, healthchecks.io and Redis behaviour.

Not built
- Gemini only reads about the first 12 minutes of a long transcript; a separate YouTube title field; a GPU-cloud worker so it runs with the PC off; any quality check on auto-chosen clips beyond length, overlap, render and caption.
