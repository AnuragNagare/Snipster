# Context

Keep this file current. Update it whenever the architecture, config or decisions change.

## What the app is
Snipster: long video in, YouTube Shorts out, with human review before publishing.
Browser (React/Vite) -> Vercel (UI + FastAPI) -> Neon Postgres, R2 storage, Redis queue.
Celery worker runs on the user's Windows PC (RTX 3050 6 GB): faster-whisper, ffmpeg, yt-dlp, Gemini.

## Pipeline (backend/app/workers/tasks.py)
download (URL only) -> transcribe_video_task -> select_clips_task (Gemini picks 3-5 segments)
-> per clip: render_clip_task (ffmpeg 9:16 + burned SRT) and generate_caption_task (Gemini)
-> user reviews -> publish_clip_task (YouTube Data API).

## Facts that matter for the current work
- Transcript is stored as a JSON list of words: `{"start", "end", "text"}`. The LLM service and the
  renderer both read that shape, so keep it.
- `transcription_service.transcribe()` returns `(words, language)`. `transcribe_video_task` stores the language in `source_videos.language`; the transcript JSON shape is unchanged.
- `WHISPER_MODEL` now defaults to `medium` (multilingual); `WHISPER_LANGUAGE` empty = auto-detect. A `backend/.env` that still sets `base.en` overrides this and breaks Hindi.
- Subtitle style is `_SUBTITLE_STYLE` in `services/renderer.py`; `subtitle_style(language)` adds `FontName=<SUBTITLE_FONT_DEVANAGARI>` for hi/mr/ne/sa. Font must exist on the worker PC (`Nirmala UI` on Windows).
- Prompts get the language via `_language_rule()` in `services/llm.py`. API responses and the frontend (`Clip.language`, `utils/language.ts`) carry it.
- Caption and hashtag editing: `PATCH /clips/{id}` plus `ClipEditor.tsx`. It does not change burned-in subtitles.
- Alembic revisions: `54b0d5b7980b` (initial) -> `8c1f2a9d4e7b` (adds `source_videos.language`). The second is written but NOT applied to Neon.
- Vercel does not run migrations. The user runs `alembic upgrade head` from the PC.
- Worker and API must share `DATABASE_URL`, `REDIS_URL`, `STORAGE_*`, `TOKEN_ENCRYPTION_KEY`.
- Stale `.pyc` files exist for `posts`, `autopilot` and `discovery` modules that are not in the source
  tree (left over from before the YouTube-only change). Ignore them.

## Known gaps (not part of the current phases)
- YouTube access token refresh is a `TODO` in `services/youtube.py`.
- `PublishJob.scheduled_at` is stored but nothing dispatches scheduled jobs.
- Gemini clip selection only sees roughly the first 12 minutes of a long transcript.
- README milestone table is out of date.

## Decisions so far
- Work covers Hindi support and language-aware captions (plan phases 1-6).
- Automation is phases A1-A8 in `plan.md`; all are done in code. Source = the user's own channel only; trending videos from other creators are ruled out (copyright, reused-content policy).
- Automation defaults: PC worker, Celery beat as scheduler (Vercel Hobby cron is once a day), first uploads private, dry-run on first, Telegram alerts, external dead-man's-switch for "PC is off".
- Automation blockers that are user actions: consent screen to Production, YouTube API audit, quota request, YouTube API key, Telegram bot, Google client id/secret on the worker.

## Current state
- Phases 1-6 done in code. Backend tests: 130 passed. Frontend type-checks.
- Verified for real: Hindi subtitle render through ffmpeg (shaping and width OK); Whisper `medium` runs on the GPU (cuda, float16) on English audio.
- NOT verified: real Hindi speech through Whisper and Gemini (no Hindi sample on this PC), browser UI, migration on Neon.
- Before the app works for real: run `alembic upgrade head` from `backend/`, restart the worker. `backend/.env` has no `WHISPER_MODEL`, so the new default `medium` applies; `medium` is already cached locally.
- `ClipResponse.language` is filled at response time from the source video; it is not a column on `clips`.
- A follow-up plan (language override, regenerate caption, editable subtitles) is finished; see `plan-captions.md`, `changes-captions.md`, `context-captions.md`. Alembic head is now `b4e6d1a73c20` (adds `clips.subtitles`), also not applied to Neon.
- Next: run the rollout in DEPLOY.md (dry run first) and do the user actions U1-U5. Also not done: fix Gemini seeing only the first ~12 min of long transcripts.

## Automation track: facts for later
- (Fixed in A1) `users.yt_token_expiry` is now written by the OAuth callback and by refresh; tokens saved before this have no expiry, so the first upload refreshes after a 401.
- (Fixed in A4) `SCHEDULED` jobs are now dispatched by the tick. `publish_clip_task` sets the job FAILED before calling `self.retry`, and a retry after a successful YouTube upload could post twice.
- (Fixed in A2) YouTube privacy comes from the settings row; `public` only if the row does not exist.
- (Fixed in A4) A beat schedule exists. `GET /api/system-info` only reports worker hardware at startup, not liveness.
- The UI has no list of everything posted; only `GET /clips/{id}/publish-jobs` per clip exists.
- Current state: A1-A8 all done in code (see changes.md). Never run against real services or in a browser.
- Built: token refresh (`services/youtube.py`, `YouTubeAuthError`, no retry on auth errors); `autopilot_sources` + `autopilot_settings` tables (migration `c7a2e9f15b83`, head, NOT applied to Neon); `services/autopilot.py` (picker, tick, dispatcher); `autopilot_tick_task` + beat schedule.
- Alembic chain: ... -> `b4e6d1a73c20` -> `c7a2e9f15b83` (autopilot tables) -> `d91b3c6e8a47` (`publish_jobs.autopilot`, head). None of the last three are applied to Neon.
- Run on the PC: worker (`celery ... worker -P solo`) AND beat (`celery -A app.workers.celery_app beat`). Worker `.env` needs `YOUTUBE_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.
- Settings row defaults: enabled False, dry_run True, auto_approve False, privacy private, 9 and 18 o'clock UTC, 3 posts/day, 2 clips/video. It is edited on the Activity page (`GET`/`PATCH /api/autopilot/settings`).
- `privacy` on the settings row applies to every upload once the row exists (the first tick creates it).
- Tests use an in-memory SQLite database (`tests/conftest.py`: `sync_db`, `session_factory`) for worker-side code.
- The tick shares the single-task worker, so it can be late behind a long transcription; the health-check grace time must allow for that.


## Automation: how it works now (A5-A8)
- Tick (`services/autopilot.py::run_tick`, every 5 min via beat): dispatch due posts (autopilot's own only when enabled) -> mark failed/timed-out videos and alert -> if enabled: `finalize_ready_sources` (gate, approve, schedule) then pick the next video when a run slot is due and none is in progress. `force=True` (Run now) skips the slot check.
- A source row goes picked -> processing -> done/failed. With auto-approve off a source is marked done as soon as its clips are ready, and clips wait on the Review page.
- `publish_jobs.autopilot` marks jobs autopilot made. Only they obey the kill switch and dry run. Dry-run jobs end PUBLISHED with `platform_post_id = "dry-run"`.
- Gate: rendered + file + caption + 15-60 s; best = closest to 40 s; overlap > 50% skipped. Post times: 24h/posts_per_day apart, max posts_per_day per local day, nothing before the first run hour.
- Alerts (`services/alerts.py`): Telegram + health-check ping. Worker `.env` needs `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `HEALTHCHECK_URL`, `YOUTUBE_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.
- API: `routes/autopilot.py` (`/api/autopilot/settings`, `/api/autopilot/run-now`, `/api/activity`). UI: `pages/Activity.tsx`.
- Tests: 296 backend tests; worker-side tests use in-memory SQLite (`conftest.py`), route tests use async SQLite (`aiosqlite`), `test_autopilot_e2e.py` runs the whole loop on a file database.

## Launch status (latest)
- Code complete and tested; nothing is committed, deployed, or applied to Neon.
- The assistant was not allowed to touch the production Neon database or read `backend/.env`. So: migrations `8c1f2a9d4e7b`, `b4e6d1a73c20`, `c7a2e9f15b83`, `d91b3c6e8a47` are unapplied (head is `d91b3c6e8a47`), and which worker `.env` keys are set is unknown.
- Whoever continues: do not assume Neon is migrated. Ask the user to run `alembic upgrade head` (or to say it is OK to run it) and to confirm the env keys. The full checklist is at the end of `plan.md`.
- Uncommitted noise to leave out of any commit: the deleted `youtube-setup-plan.md` (deleted before this work) and the untracked `.serena/`.

## Latest: where launch is stuck
- Branch `feature/hindi-captions-autopilot`, commit `59ffdf7`, local only.
- The database URL in `backend/.env` points to a Neon host that no longer resolves, and no project in the connected Neon account matches it. Migrations are unapplied and the app cannot reach its database from this PC until `DATABASE_URL` is fixed.
- Do NOT run migrations on the other Neon projects (Work trail, Quotation app, Policy pal, etc.): they are other apps' databases.
- Next: the user finds or recreates the Snipster database and sets `DATABASE_URL` (in `backend/.env` and in Vercel), then `alembic upgrade head` creates everything from scratch (all 4 revisions), then deploy.

## Latest (2026-11-16): database is migrated
- Neon database (host `ep-steep-bar-b5f980pu-pooler.c-7.us-east-2...`, in a Neon account NOT connected to Claude) is at head `d91b3c6e8a47`. Existing data (1 video, 4 clips, 1 user) is intact. The earlier "host does not resolve" was a temporary DNS problem; ignore the older note saying the database is unreachable or that migrations are unapplied.
- The database password was pasted in a chat message; the user should rotate it in the Neon console when convenient, then update `backend/.env` and Vercel.
- Remaining: deploy to Vercel, worker `.env` keys, start worker + beat, Google steps, rollout, real Hindi test, browser check.

## Latest (2026-11-16, later): GitHub updated, Vercel not created
- GitHub `main` = `9e7cb9c` (all the work). Branch `feature/hindi-captions-autopilot` also pushed.
- `backend/.env`: `SECRET_KEY` fixed; autopilot keys added empty (`YOUTUBE_API_KEY`, `TELEGRAM_*`, `HEALTHCHECK_URL`). `GOOGLE_REDIRECT_URI` and `FRONTEND_URL` still point to localhost: on Vercel they must be the deployed domain.
- Vercel: no Snipster project exists. The Vercel connection in Claude cannot create projects (403). Vercel CLI is logged out. Next step is the user running `vercel login` (type `! vercel login` in the prompt), after which: `vercel link`, set env vars from `backend/.env` plus the new `ADMIN_PASSWORD`, `vercel --prod`.
- Do not claim the app is deployed: it is not.

## Latest (2026-11-16): LIVE on Vercel
- Production: https://snipster-two.vercel.app (Vercel project `snipster`, team `anurags-projects-030904ac`). Deploy from this folder with `vercel deploy --prod --scope anurags-projects-030904ac` (CLI logged in as `anuragnagare77-2616`). The Claude-side Vercel connection cannot see this project; use the CLI.
- Production env vars are set (names in changes.md). `DATABASE_URL`, `TOKEN_ENCRYPTION_KEY` etc. on Vercel equal the values in `backend/.env`; they must stay identical to the worker's.
- The deployed login password is stored only in Vercel (sensitive) and was shown once in chat.
- Still open: (1) add `https://snipster-two.vercel.app/api/auth/google/callback` to the Google OAuth client's Authorized redirect URIs; (2) add this domain to the R2 bucket CORS (PUT, GET, HEAD) or browser uploads fail; (3) restart the worker (still reports `base.en`) and start `celery ... beat`; (4) fill `YOUTUBE_API_KEY`, `TELEGRAM_*`, `HEALTHCHECK_URL` in `backend/.env`; (5) consent screen to Production + API audit; (6) dry-run rollout from DEPLOY.md; (7) rotate the database password and the admin password (both appeared in chat).

## Dashboard tab (2026-11-17)
- `GET /api/dashboard` + `pages/Dashboard.tsx` + nav tab. Shows only real Snipster posts (PUBLISHED, real YouTube id); stats from the Data API with `YOUTUBE_API_KEY` (key must be set on Vercel, and is still empty locally). Private videos return no numbers; dry-run posts are not listed. 5-minute in-memory cache per serverless instance.
- Not deployed, not browser-checked. Backend tests: 320.

## Latest (2026-11-17): waiting on DNS before the dry run
- Worker `.env` now has `YOUTUBE_API_KEY` (valid). Still empty: `TELEGRAM_*`, `HEALTHCHECK_URL` (alerts and the PC-off warning will not work yet; not needed for the dry run).
- Blocker: this PC's DNS cannot resolve `ep-steep-bar-b5f980pu-pooler.c-7.us-east-2.aws.neon.tech` (8.8.8.8 can). User must switch DNS to 8.8.8.8 / 1.1.1.1. Then: start worker + beat, turn Autopilot on with Dry run on, press Run now, watch the Activity page.

## Latest (2026-11-17, evening): dry run blocked on the channel id
- Worker + beat are running on this PC (started by Claude, background). Autopilot is enabled (dry run, auto-approve, private) but cannot pick: no channel. Needs the user's own channel: give a handle (like @name), a channel URL or a `UC...` id; then set `YOUTUBE_CHANNEL_ID=` in `backend/.env`, restart the worker, press Run now.
- `users.yt_channel_id` is '' because sign-in lacks the `youtube.readonly` scope. Adding that scope to `routes/auth.py` plus reconnecting would also fix it and let the Dashboard see private videos' numbers, but widens the permission and needs the consent screen updated; not done.
- Do NOT use the channel of the video in the DB (`Raj Shamani Insights`, UCxNHw_mF6vHbnunoMtIwHWQ): not the user's.
- Code change (channel fallback + tests) is uncommitted on branch `feature/dashboard-tab`.
