# Changes log

Every change made to the code goes here, newest at the bottom. Format:
`date | phase | files | what changed and why`.

## 2026-11-15
- Planning only. Created `plan.md`, `changes.md`, `context.md`. No application code changed.
- `plan.md` overwritten. The old content (pipeline speedup plan) is in git history at `cd37b23`.

## 2026-11-15: Phases 1-3
Tests: `pytest` in `backend/` -> 120 passed, 3 skipped. Not yet tried on a real Hindi video.

**Phase 1: multilingual transcription**
- `backend/app/core/config.py`: `WHISPER_MODEL` default `base.en` -> `medium`; new `WHISPER_LANGUAGE` (empty = auto-detect).
- `backend/app/services/transcription.py`: `transcribe()` now returns `(words, language)` instead of `words`. Passes `language=` to Whisper only when `WHISPER_LANGUAGE` is set.
- `backend/.env.example`: documents `WHISPER_MODEL=medium` and `WHISPER_LANGUAGE`.
- NOT changed: the user's real `backend/.env`. It may still say `WHISPER_MODEL=base.en`, which overrides the new default.

**Phase 2: store the language**
- `backend/app/db/models.py`: `SourceVideo.language` (String(8), nullable).
- `backend/migrations/versions/8c1f2a9d4e7b_add_source_video_language.py`: new revision after `54b0d5b7980b`. NOT applied to any database yet.
- `backend/app/workers/tasks.py`: `transcribe_video_task` saves `video.language`.
- `backend/app/db/schemas.py`: `language` added to `VideoStatusResponse` and `ClipResponse`.
- `backend/app/api/routes/videos.py`: status endpoint returns `language`.
- `backend/app/api/routes/clips.py`: clip responses carry the source video's language (one extra `db.get` per clip request; list endpoint reuses the video it already loaded).

**Phase 3: language-aware Gemini prompts**
- `backend/app/services/llm.py`: new `_language_rule()`; `select_clips()` and `generate_caption()` take `language`. Hindi -> caption in Devanagari; other -> that language. `reason` may stay English. Unknown language -> prompt unchanged.
- `backend/app/workers/tasks.py`: passes `video.language` to both calls.
- NOT done: the optional fix for Gemini only seeing the first ~12 minutes of a long transcript.

## 2026-11-15: Phases 4-6
Tests: `pytest` in `backend/` -> 130 passed, 3 skipped. Frontend: `npx tsc -b` clean; `oxlint` shows 2 existing `set-state-in-effect` warnings (App.tsx, Review.tsx), none from these changes.

**Phase 4: Devanagari subtitles**
- `backend/app/core/config.py`: new `SUBTITLE_FONT_DEVANAGARI` (default `Nirmala UI`).
- `backend/app/services/renderer.py`: new `subtitle_style(language)` adds `FontName=...` for hi/mr/ne/sa; `render()` and `_build_cmd()` take an optional `language`.
- `backend/app/workers/tasks.py`: `render_clip_task` passes `video.language`.
- `backend/.env.example`: documents `SUBTITLE_FONT_DEVANAGARI`.
- Checked for real: rendered a 5 s test clip with Hindi words through ffmpeg 9.0.2; the frame shows correctly shaped Devanagari (conjuncts fine), fits the 1080 px width. Test clip was a synthetic blue video, not real footage.

**Phase 5: frontend**
- `frontend/src/types/index.ts`: `language?` on `Clip` and `VideoStatusResponse`.
- `frontend/src/utils/language.ts` (new): `languageLabel()` -> "Hindi" / "English" / upper-cased code.
- `frontend/src/components/ClipCard.tsx`: language badge top-right of the preview; `lang` attribute on caption and hashtags.
- `frontend/src/components/ClipEditor.tsx`: `lang` attribute on the caption and hashtag fields.
- `frontend/src/pages/Review.tsx`: header reads "N candidate clips generated · Hindi video."
- Not checked in a browser. Only type-checked.

**Phase 6: tests and verification**
- `backend/tests/test_services/test_language.py` (new, 10 tests): language rule in prompts, caption/clip-selection prompts, Devanagari font only for Hindi, ffmpeg command, `transcribe()` returns `(words, language)` and honours `WHISPER_LANGUAGE`, migration chain has one head `8c1f2a9d4e7b`.
- Real check on the PC: Whisper `medium` loads on the RTX 3050 (`cuda`, `float16`) and transcribed a synthetic English sentence correctly with language `en`. About 8.6 s including model load (model was already cached).
- NOT verified: real Hindi speech. This PC has no Hindi voice and no Hindi video was available, so Hindi transcription quality, Hinglish behaviour, timing drift and the Gemini Hindi caption are untested.
- NOT verified: the migration against Neon (not applied).
- Hindi transcription time on the GPU: not measured, for the same reason.

## 2026-11-15: pointer
Follow-up work on language override, caption regeneration and editable subtitles is logged in `changes-captions.md`. It adds migration `b4e6d1a73c20` and changes `renderer.py`, `routes/clips.py`, `routes/videos.py` and the frontend editor.

## 2026-11-15: automation track planned
- Planning only; no application code changed. `plan.md`: the old "Deferred track" section is replaced by the automation track, phases A1-A8, with decisions, user actions U1-U5 and risks.
- Found while planning: `routes/auth.py::google_callback` never saves `expires_in`, so `users.yt_token_expiry` is always empty. A1 covers it.
- Found while planning: the worker's `.env` has no `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` (only the API uses them today). Token refresh on the worker needs them (U5).

## 2026-11-15: automation phases A1-A4
Checks: full backend suite 218 passed, 3 skipped (57 new tests: 16 token/publish, 41 autopilot). Mutation checks: removing the dispatcher's claim guard, not recording the run slot, and disabling token refresh each made tests fail (the first only after I fixed a race test that did not actually race). The migration SQL was generated offline for Postgres and read; it was NOT run against Neon.

**A1: token refresh**
- `backend/app/services/youtube.py` rewritten: `refresh_access_token()` (refresh token -> new access token, saved encrypted with its own commit, expiry stored); `_get_valid_token()` refreshes when the stored expiry is within 2 minutes; `upload()` retries once after a 401 (covers tokens saved before expiry was recorded), then raises `YouTubeAuthError`. `upload()` now takes `privacy` (private/unlisted/public, default public) and refuses other values. `YouTubeAuthError` is raised for: not connected, no refresh token, missing `GOOGLE_CLIENT_ID/SECRET` on the worker, Google `invalid_grant` (revoked / 7-day Testing limit), a second 401.
- `backend/app/api/routes/auth.py`: the OAuth callback now saves `yt_token_expiry` from `expires_in`, keeps the saved refresh token if Google sends none (it used to overwrite it with NULL), and disconnect clears the expiry.
- `backend/app/workers/tasks.py::publish_clip_task`: a `YouTubeAuthError` marks the job FAILED (`error_info` has `"auth": true`) and is NOT retried; other errors still retry as before; rollback before recording a failure; passes `privacy` from the autopilot settings row (public if the row does not exist yet).

**A2: data model and settings**
- `backend/app/db/models.py`: `AutopilotSource` (one row per picked YouTube video, unique id, status picked/processing/done/skipped/failed, link to `SourceVideo`), `AutopilotSourceStatus`, `AutopilotSettings` (single row id 1: enabled=False, dry_run=True, auto_approve=False, posts_per_day=3, clips_per_video=2, run_times="9,18", timezone="UTC", privacy="private", last_tick_at, last_run_slot).
- `backend/migrations/versions/c7a2e9f15b83_add_autopilot_tables.py`: new head after `b4e6d1a73c20`. NOT applied.
- `backend/app/core/config.py`, `.env.example`: `YOUTUBE_API_KEY`, `AUTOPILOT_TICK_MINUTES` (5), `AUTOPILOT_MIN_DURATION` (180 s, also keeps Shorts out), `AUTOPILOT_MAX_DURATION` (3600), `AUTOPILOT_MAX_PAGES` (10). `requirements-worker.txt`: `tzdata`.
- Behaviour to remember: once the settings row exists its `privacy` applies to EVERY upload, manual ones included.

**A3: source picker** (`backend/app/services/autopilot.py`)
- `fetch_uploads()` reads the connected channel's uploads playlist (`UU` + channel id) and video details with the API key; `select_candidate()` picks the oldest unused public, non-live video within the length limits; `start_next_source()` creates the `AutopilotSource` and a `SourceVideo` (`ownership_confirmed=True`, own channel), commits, then queues the pipeline with `from_url=True`. If the queue is down the rows are deleted so the same video is retried. Config problems raise `AutopilotConfigError`.

**A4: scheduler and dispatcher**
- `backend/app/services/autopilot.py`: `run_tick()` = (1) `dispatch_due_publish_jobs()` (claims each due `SCHEDULED` job with one conditional UPDATE to `QUEUED`; a failed enqueue puts it back), (2) `sync_source_statuses()` (failed pipeline, or older than 12 h and not finished, -> failed), (3) if enabled and the latest run slot has not run, pick the next video unless one is still in progress. A setup problem or transient error does not use up the slot; an empty channel does.
- `backend/app/workers/tasks.py`: `autopilot_tick_task` (never raises, `ignore_result`).
- `backend/app/workers/celery_app.py`: `beat_schedule` runs it every `AUTOPILOT_TICK_MINUTES`.
- `DEPLOY.md`: how to run `celery -A app.workers.celery_app beat`, new worker env vars. `.gitignore`: `celerybeat-schedule*`.
- Hand-scheduled posts are now sent too, even with autopilot off (this gap existed before).

**Limits to remember**
- Nothing approves or posts clips yet (A5). After a video is picked and reaches READY its source row stays "processing", which blocks picking another until A5 marks it done. So today autopilot picks one video and then waits.
- Dry run and the daily post cap are stored but not used until A5.
- The worker runs one task at a time (`-P solo`), so a tick waits behind a long transcription; scheduled posts and, later, the A6 heartbeat can be late by that long.
- Not run against real YouTube, Redis or Neon.

## 2026-11-15: automation phases A5-A8
Checks: full backend suite 296 passed, 3 skipped (78 new tests since A4). Frontend `npx tsc -b` clean; `oxlint` shows the 2 old warnings plus 1 new of the same kind in `Activity.tsx` (state set inside a load effect). Mutation checks (each made tests fail): kill switch ignored, dry run ignored, dry run also blocking manual posts, gate letting short clips through, daily limit ignored (only caught after I added a test where spacing alone could not explain the result), timezone validation removed, oldest-first order flipped. A test run also caught a real bug: `_save_published` used `PublishStatus` without importing it, which made every post look like "uploaded but not saved".
Migration SQL generated offline for Postgres and read; NOT run on Neon.

**A5: auto-approve and publish gate** (`backend/app/services/autopilot.py`, `app/workers/tasks.py`)
- `check_clip()` / `choose_clips()`: a clip must be rendered, have a file and a caption, and be 15-60 s. Of those, the ones closest to 40 s win (there is no quality score), a clip overlapping a chosen one by more than half is skipped, and only `clips_per_video` are taken. Everything else is rejected, with the reason recorded on the source row.
- `plan_post_times()`: spreads posts 24h / posts_per_day apart, never more than `posts_per_day` per calendar day in the user's timezone (counting posts already made or scheduled), and moves anything that would land before the first run hour to that morning.
- `finalize_ready_sources()`: waits until every clip is rendered and captioned (gives up waiting 2 h after the video was ready), then approves, rejects, creates `SCHEDULED` jobs with `autopilot=True`, and marks the source `done`. With auto-approve off it only marks the source done and leaves every clip for manual review. A video with no clips, or none passing the gate, is marked failed and alerted.
- Kill switch: `dispatch_due_publish_jobs(include_autopilot=False)` when autopilot is off, so autopilot's own scheduled posts are held, while hand-scheduled ones still go out.
- Dry run (`publish_clip_task`): an autopilot post with dry run on is marked PUBLISHED with `platform_post_id = "dry-run"` and no URL, and YouTube is never called. It applies to autopilot posts only; a post started by hand always uploads. Dry-run posts count toward the daily limit and use the clip up (switching dry run off later does not repost them).
- Double-post protection: a job that already has a real YouTube id is not uploaded again; the post is saved with 3 attempts; if YouTube accepted the file but saving fails, the job is marked failed with "Do NOT retry", an alert carries the YouTube link, and Celery does not retry.
- New column `publish_jobs.autopilot` (migration `d91b3c6e8a47`, new head). Run-now: `autopilot_tick_task(force=True)` ignores the run slot but still waits for an unfinished video, and does not use up the slot.

**A6: alerts and dead-man's switch**
- `backend/app/services/alerts.py` (new): `send()` posts to Telegram (never raises, never logs the bot token, per-key cooldowns), `ping_healthcheck()` hits `HEALTHCHECK_URL`.
- Alerts for: a post that failed after all retries, a sign-in problem (cooldown 1 h), an upload that could not be saved, a failed or timed-out video, a video with no usable clips, autopilot not set up (cooldown 6 h), a failed start (cooldown 1 h), an empty channel (cooldown 24 h), a crashed tick (cooldown 1 h).
- `autopilot_tick_task` pings the dead-man's switch only after a tick that ran to the end.
- Config: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `HEALTHCHECK_URL` (in `core/config.py` and `.env.example`).

**A7: Activity API and dashboard**
- `backend/app/api/routes/autopilot.py` (new), registered in `main.py` behind the login: `GET`/`PATCH /api/autopilot/settings` (validated; `enabled=false` is the kill switch; response includes scheduler heartbeat, posted today, waiting count, whether YouTube is connected), `POST /api/autopilot/run-now` (needs autopilot on; commits before queueing; 503 if the queue is down), `GET /api/activity` (posts newest first with caption, source title, language, link, error, needs-reconnect flag, dry-run flag; plus the last 20 picked videos).
- `frontend/src/pages/Activity.tsx` (new), route `/activity` and nav link in `App.tsx`, API functions in `api/index.ts`, types in `types/index.ts`: scheduler badge, toggles (autopilot, dry run, auto-approve), visibility, posts per day, clips per video, run hours, timezone, "Save schedule", "Run now", Coming up, Posted and failed (with link, error, Retry, reconnect hint), Videos picked. Refreshes every 15 s.
- Not opened in a browser; type-checked only.

**A8: tests and notes**
- New test files: `tests/test_services/test_autopilot_posting.py` (gate, scheduling, finalize, kill switch, dry run, double-post, alerts), `tests/test_api/test_autopilot_routes.py` (settings, run-now, activity, login, on async SQLite), `tests/test_services/test_autopilot_e2e.py` (one test: channel -> picked -> processed -> approved -> scheduled -> dispatched -> rehearsed -> Activity feed -> kill switch, on a shared file SQLite database; only YouTube listing, the queue and the pipeline stages are faked).
- `aiosqlite` added to `requirements-dev.txt` (installed in the venv).
- `DEPLOY.md`: alert and health-check setup, and a 5-step rollout order (dry run first, then one private post, then wider).

**Limits to remember**
- Nothing here has run against real YouTube, Telegram, healthchecks.io, Redis, Neon, or in a browser.
- The settings row is created on first use with autopilot OFF, dry run ON, auto-approve OFF. Turning on "Approve clips automatically" and switching dry run off is the point of no return for real uploads; visibility defaults to private.
- `privacy` applies to every upload, manual ones included.
- Clip quality is judged only by length, overlap, and having a render and a caption. Gemini's choice is not reviewed.
- The worker handles one task at a time: a long transcription delays ticks, posts and heartbeats. Set the health-check grace time to an hour or more.
- A crash of the whole worker process between YouTube accepting an upload and the database save can still double-post (the retry cannot know).
- Still open from before: user actions U1-U5 in `plan.md` (consent screen to Production, API audit, quota, API key and Telegram bot, Google id/secret on the worker).

## 2026-11-15: launch checklist attempt
- No application code changed. `plan.md` now ends with a launch checklist.
- Tried a read-only check of Neon (`alembic current`, table row counts) and of which worker `.env` keys are set. The permission system refused it (it reaches your production database and reads secrets). Nothing was run against Neon and `.env` was not read. The migrations are therefore still NOT applied and the worker env is unchecked.
- Not committed and not deployed: committing and deploying were not done in this step; they wait for your go-ahead.

## 2026-11-15: commit done, migration blocked
- Committed 44 files as `59ffdf7` on branch `feature/hindi-captions-autopilot` (no `.env`, caches or secrets in it; `youtube-setup-plan.md` deletion and `.serena/` left out). Not pushed.
- `alembic upgrade head` was run against the database in `backend/.env` and FAILED before touching anything: the host `ep-steep-bar-b5f980pu-pooler.c-7.us-east-2.aws.neon.tech` does not resolve in DNS (`getaddrinfo failed`), while neon.tech and google.com do. So the migrations are still NOT applied.
- Checked the connected Neon account (read-only project list). None of its 6 projects matches that host (region us-east-2 with proxy c-7): Work trail (us-east-2, proxy c-9), Fit path, Policy pal, Cafe table, Quotation app, Pursuit (all ap-southeast-1). So the Snipster database is not in this account, or it was deleted or recreated. Nothing was run on any of them: they belong to other apps.
- Not deployed to Vercel: deploying against an unreachable database would give an API that fails on every call.

## 2026-11-16: migrations applied to Neon
- The earlier DNS failure was temporary on this PC. The host `ep-steep-bar-b5f980pu-pooler.c-7.us-east-2.aws.neon.tech` resolves (checked with the system resolver, 8.8.8.8 and 1.1.1.1). It is NOT in the Neon account connected to Claude (6 projects listed, none match), so it lives in another Neon account or organization.
- Before: `alembic current` = `54b0d5b7980b`; tables alembic_version, clips, publish_jobs, source_videos, users; rows: source_videos 1, clips 4, users 1, publish_jobs 0.
- Ran `alembic upgrade head` (URL given by the user, passed as the `DATABASE_URL` environment variable for that one command, not written to any file): 54b0d5b7980b -> 8c1f2a9d4e7b -> b4e6d1a73c20 -> c7a2e9f15b83 -> d91b3c6e8a47.
- After: head is `d91b3c6e8a47`; new tables `autopilot_settings`, `autopilot_sources`; columns `source_videos.language`, `clips.subtitles`, `publish_jobs.autopilot` exist; row counts unchanged (1, 4, 1, 0).
- Not touched: `backend/.env`, Vercel settings, the other Neon projects.
- Still not done: deploy to Vercel (its environment variables, including `DATABASE_URL`, are secrets only the user should enter), worker `.env` keys, Google steps.

## 2026-11-16: .env updated, pushed to GitHub, Vercel blocked
- `backend/.env` (gitignored; backup at `backend/.env.bak-before-autopilot`): `SECRET_KEY` was the "change-me..." placeholder, which makes the API refuse to start once `ADMIN_PASSWORD` is set, so it was replaced with a random 64-char key (this invalidates old login tokens only). Added EMPTY placeholders for `YOUTUBE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `HEALTHCHECK_URL`, `AUTOPILOT_TICK_MINUTES=5`. No other value changed. `DATABASE_URL`, Redis, R2, Google and Gemini values were left as they were.
- Pushed to https://github.com/AnuragNagare/Snipster: branch `feature/hindi-captions-autopilot`, and `main` fast-forwarded `cd37b23 -> 9e7cb9c` (no history rewritten).
- Vercel: the team "Anurag's projects" (`team_sL62RI0XlNcSn1icgUIsZpOE`) has no Snipster project (only nutribot-backend, ai-portfolio, backend, backend-ncuu; none linked to this repo). Creating one through the Vercel connection in Claude failed with 403 (no permission to create projects). The Vercel CLI on this PC is installed (59.5.0) but logged out. Nothing was created or deployed on Vercel.
- A random `ADMIN_PASSWORD` for the deployed app was generated and shown to the user once in chat; it is not in any file in the repo or in `.env`.
- Still to do on Vercel (see DEPLOY.md): create the project, set the environment variables (`DATABASE_URL`, `REDIS_URL`, `STORAGE_*` x5, `ADMIN_PASSWORD`, `SECRET_KEY`, `TOKEN_ENCRYPTION_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, `FRONTEND_URL`), deploy, then add the deployed callback URL to the Google OAuth client.

## 2026-11-16: deployed to Vercel
- Vercel project `anurags-projects-030904ac/snipster` (id `prj_A4ai1fTROy10KHa5W1aRV9PJYOHE`, Framework Preset "Services", root `.`) was found by the CLI after the user logged in; the folder is linked (`.vercel/`, gitignored). The Vercel connection inside Claude could not see this project (404/403), so everything was done with the Vercel CLI.
- Environment variables set on Production, copied from `backend/.env` without printing values: `DATABASE_URL`, `REDIS_URL`, `STORAGE_ACCESS_KEY`, `STORAGE_SECRET_KEY`, `ADMIN_PASSWORD`, `SECRET_KEY`, `TOKEN_ENCRYPTION_KEY`, `GOOGLE_CLIENT_SECRET` (all "sensitive", write-only) and `STORAGE_ENDPOINT`, `STORAGE_BUCKET`, `STORAGE_PUBLIC_BASE_URL`, `GOOGLE_CLIENT_ID`; later `FRONTEND_URL=https://snipster-two.vercel.app` and `GOOGLE_REDIRECT_URI=https://snipster-two.vercel.app/api/auth/google/callback`. `ADMIN_PASSWORD` is a generated value shown to the user in chat. Not set on Vercel (worker-only): Gemini, Whisper, YouTube API key, Telegram, health check.
- Deployed twice with `vercel deploy --prod` from the local folder (the second picked up the two URLs). Production address: https://snipster-two.vercel.app (deployment `snipster-kwu3nxlvu-...`, READY).
- Verified on the live site: `/health` 200; `/api/auth/config` says login required; unauthenticated API call -> 401; wrong password -> 401; right password -> token; `/api/activity` returns empty lists (new tables readable); `/api/auth/accounts` says YouTube connected; `/api/system-info` returns the worker's last report (Redis works); the Google sign-in URL carries the new redirect URI; the home page is served. Not checked: uploading a video, a real sign-in to Google, the UI in a browser.
- `system-info` showed `whisper_model: base.en`: the worker on the PC has not been restarted since the change to `medium`.

## 2026-11-17: Dashboard tab
Checks: full backend suite 320 passed, 3 skipped (23 new). Frontend `npx tsc -b` clean; `oxlint` has 1 new warning in `Dashboard.tsx` of the same kind as the other pages (state set in a load effect). Mutation checks: counting dry-run posts as real, ignoring the cache, and showing hidden like counts as 0 each made tests fail. Not opened in a browser. Not deployed.
- `backend/app/services/youtube_stats.py` (new): `fetch_video_stats()` calls `videos.list?part=statistics` with `YOUTUBE_API_KEY`; batches of 50 (1 quota unit each); results cached 5 minutes; returns views/likes/comments, with likes/comments None when hidden; videos YouTube does not return (private, deleted) are left out; failures raise `StatsUnavailable` with a safe message (no key, 403 quota/key, other status, network).
- `backend/app/api/routes/dashboard.py` (new), registered behind the login in `main.py`: `GET /api/dashboard?limit=` lists PUBLISHED jobs with a real YouTube id (dry-run ids, failed and unfinished jobs excluded), newest first, with source title, language, autopilot flag, link and numbers; totals count only posts that have numbers; `rehearsals` counts dry-run posts left out; a stats failure still returns the posts with `stats_error`. The YouTube call runs in a thread so it does not block the API.
- `backend/app/db/schemas.py`: `DashboardPost`, `DashboardTotals`, `DashboardResponse`.
- `frontend/src/pages/Dashboard.tsx` (new), nav tab "Dashboard" (between Home and Activity), route `/dashboard`, `getDashboard()` in `api/index.ts`, types in `types/index.ts`: four totals cards, per-post views/likes/comments with link, notes for posts without numbers / dry runs / errors, refresh button, auto-refresh every 60 s.
- Config/docs: `YOUTUBE_API_KEY` is now also used by the API (set it on Vercel); `.env.example`, `config.py` comments and `DEPLOY.md` updated.
- Known: the real `YOUTUBE_API_KEY` is still empty in `backend/.env`, so even after deploy the numbers stay empty until it is filled and also set on Vercel. While uploads are Private, YouTube gives no public numbers: use Unlisted or Public to see them.
