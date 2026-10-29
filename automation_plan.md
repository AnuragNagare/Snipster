# Snipster Autopilot — Plan (v2, decisions locked)

**Goal:** at every slot in `posting-schedule.txt` (IST), automatically post one YouTube Short
built from a currently trending YouTube video, **alternating English → Hindi → English…**,
with no human step. A new **Posted** tab in the app lists what was posted.

Status: plan only — nothing built. Existing `plan.md` (pipeline speed-up) is untouched.

## Decisions (from you)

| # | Decision |
|---|---|
| 1 | Source = whatever is **trending on YouTube** (not licence-filtered) |
| 2 | Runs on **your PC**. Vercel can host only the UI (see §1) |
| 3 | **YouTube only** for now. Instagram deferred |
| 4 | **Fully automatic**, no approval step |
| 5 | Slots exactly as in `posting-schedule.txt` (~13/week) |
| 6 | ~~Captions in the source language~~ **Changed 2026-10-28: captions are always English.** Hindi videos are still picked and posted as Hindi videos; Whisper translates the speech to English for the burned-in subtitles, and the post caption/hashtags are written in English |
| 7 | No topic bans; instead a **20–40 age-group appeal** score picks what's worth clipping |
| 8 | Google project is in **Testing mode** |
| 9 | Every description carries a **"Source: title — channel — url"** credit |
| 10 | Goal is **public** posts. Path: verify first upload → if private, manual flip in Studio as fallback while the audit is attempted (see §0.B) |
| 11 | **Best clip only** per source video |
| 12 | Failure alerts by **email** to anuragnagare77@gmail.com (SMTP; needs a Gmail app password in `.env`) |
| 13 | Regions: **IN + US** trending for the start |

---

## 0. Things that will bite, stated up front

**A. Testing mode makes "fully automatic" impossible.** In Testing, Google refresh tokens
expire after **7 days** → autopilot silently dies weekly. Fix: switch the consent screen to
*In production (unverified)* — you click through the "unverified app" warning once.

**B. Unaudited projects get uploads forced to private.** Until the project passes YouTube's
API compliance audit, uploads made through the API are expected to land **private**, whatever
`privacyStatus` says. So until then autopilot "posts" privately and nothing is publicly
visible. Verify on the first real upload. This is the biggest threat to the goal.
Be realistic about the audit: it asks you to describe the app's purpose, and an app whose
purpose is re-posting other people's trending videos is unlikely to be approved.

**C. Rights / channel risk (you've accepted it; recorded here).** Clipping trending videos you
don't own can bring Content ID claims, copyright strikes (3 = channel terminated) and
"reused content" demotion. Mitigations built into the plan, none of which remove the risk:
credit line, short clips (≤ 60 s), dedupe, and an **auto-pause** when a video is blocked,
claimed, removed or the channel gets a notice. Prefer sources where re-use is common
(commentary, tutorials, podcasts, interviews) over music videos/movies/sports/TV — those are
claimed fastest, so they get a hard penalty in ranking.

**D. Quota.** 10,000 units/day; each upload = 1,600 → **max 6 uploads/day**. Schedule uses ≤ 2–3.

**E. Your PC must be on and awake at every slot** (Windows sleep off, Docker/worker auto-start).

---

## 1. Where things run

```
Your PC (Windows, RTX 3050)                     Optional
  Docker: api + worker + beat + redis    ◄── Cloudflare Tunnel ◄── Vercel (static UI)
  Neon Postgres (cloud)   Cloudflare R2 (cloud files)
```

- **Vercel cannot run the pipeline** (yt-dlp, Whisper, ffmpeg, Celery, Redis). It can only
  serve the React build. Using it means exposing your PC's API through a tunnel, so the
  **default is: run UI locally on :5173 as today**; Vercel + tunnel is a later optional step.
- **Docker: keep it.** It's the packaging for api/worker/beat/redis on your PC (and for any
  later VPS). The frontend Dockerfile is dev-only and not used by Vercel.
- If you do use Vercel: `VITE_API_BASE_URL`, CORS for the Vercel domain, `vercel.json` SPA
  rewrite, and an **admin API key** — the API is currently unauthenticated and, once public,
  could post to your channel.

---

## 2. Gaps in current code that must be fixed first

| Gap | File | Why it blocks |
|---|---|---|
| G1 Token refresh is a TODO (tokens last 1 h) | `services/youtube.py` | Unattended posting |
| G2 Scheduled jobs never queued | `routes/publish.py` | Slot timing |
| G3 Whisper `base.en` is English-only | `config.py` | Hindi |
| G4 Default caption font lacks Devanagari | `renderer.py` | Hindi captions |
| G5 Gemini prompts have no language handling | `services/llm.py` | Hindi captions/hashtags |
| G6 `quotaExceeded` is retried with backoff | `tasks.py` | Wasted retries |
| G7 Clip rows get no language / source metadata | `models.py` | Alternation, credit |

---

## 3. Flow

```
beat: DISCOVER (every 4 h)
  YouTube Data API  videos.list chart=mostPopular  regionCode=IN (+US)   [1 unit/call]
  → drop: already seen, <3 min or >60 min, live, age-restricted, music/sports/movie categories
  → language guess from snippet.defaultAudioLanguage (hi*/en*)
  → score = velocity(views/hour) × engagement(likes/views) × appeal20_40 × (1 - claim_risk)
  → insert into discovered_videos (status=candidate)

beat: FILL BUFFER (every 30 min)
  for each language with buffer < BUFFER_TARGET (3):
     take best candidate of that language → SourceVideo(source='auto')
     existing chain: download → Whisper(lang) → Gemini select → render → caption
     Whisper's detected language must match; else discard candidate
     pick the single best clip → clips.autopost_ready=true, language set
     (other clips of that video are kept but not auto-posted)

beat: SLOT TICK (at each IST slot)
  language = opposite of autopilot_state.last_language (fallback to other if empty buffer)
  clip = oldest autopost_ready clip of that language
  none → log "skipped: empty buffer", no post
  else → PublishJob(youtube) → publish_clip_task → on success flip last_language
```

Why decoupled: processing takes 10+ min and can fail; the slot must still fire on time.

### Appeal for ages 20–40
YouTube does not expose viewer age, so it's a heuristic, not data. Two parts:
1. Category/topic weights in config (default high: tech, personal finance/investing, career,
   fitness/health, self-improvement, relationships, comedy, gaming, food/travel, startup/business,
   entertainment commentary; default low: kids, devotional/ritual, pure news bulletins).
2. Gemini scores the transcript sample 0–10 for "hook + relatable to 20–40" and for
   "makes sense standalone". Clips below a threshold (default 6) are not queued.
Weights/threshold are editable settings so you can tune them from results.

### Language
Per-video: metadata hint → confirmed by Whisper `info.language` (`hi`/`en`). Hindi in Roman
script ("Hinglish") is transcribed by Whisper as Hindi/Devanagari or English depending on
audio; whatever it returns is kept (decision 6) and the clip is tagged with that language.

### Slots (IST, Asia/Kolkata — stored as data, not hard-coded)
Mon–Fri 12:15 and 20:00 · Sat 10:30 and 20:00 · Sun 20:00 (+ optional 2nd evening).
Beat uses `crontab` with `timezone=Asia/Kolkata`. Post ~30 min *before* the target window per
the schedule file (e.g. fire at 12:00 and 19:45).

---

## 4. Work items

### Backend
- **Config:** `AUTOPILOT_ENABLED`, `AUTOPILOT_TZ`, `YOUTUBE_API_KEY`, `DISCOVER_REGIONS=IN,US`,
  `BUFFER_TARGET_PER_LANG=3`, `APPEAL_THRESHOLD=6`, `DAILY_POST_CAP=5`, `MAX_CONSECUTIVE_FAILURES=3`,
  `WHISPER_MODEL` → multilingual (`small` default for the 3050; `medium` optional), category weights.
- **Migration #2:**
  - `source_videos`: `source` (manual/auto), `source_yt_id` (unique), `source_channel`,
    `source_title`, `language`, `view_count_at_discovery`, `appeal_score`
  - `clips`: `language`, `autopost_ready`
  - new `discovered_videos`, `autopilot_state` (`last_language`, `paused`, `fail_streak`,
    `last_tick`, `last_skip_reason`), `slots`
- **G1:** refresh token before every upload using the stored refresh token + `yt_token_expiry`;
  persist new access token; on `invalid_grant` pause autopilot and surface "reconnect YouTube".
- **G2/G6:** slot tick replaces the dead `SCHEDULED` path (manual scheduling reuses it);
  `quotaExceeded` → stop retries, pause until next Pacific midnight.
- **G3–G5:** multilingual Whisper with language pass-through; Noto Sans Devanagari bundled and
  wired through libass `fontsdir`; language-aware Gemini prompts + the appeal/safety scorer.
- **Credit line:** `"Source: <title> — <channel> — <url>"` appended to the description of
  every auto post.
- **New services:** `services/discovery.py`, `services/autopilot.py`;
  **new tasks:** `discover_task`, `fill_buffer_task`, `slot_tick_task`; beat schedule in
  `celery_app.py`; a `beat` service in docker-compose.
- **Auto-pause triggers:** upload `403/forbidden`, copyright/claim notice, token `invalid_grant`,
  `fail_streak ≥ 3`, daily cap hit.

### API
- `GET /api/posts?limit=&language=` — published jobs joined with clip: thumbnail/preview,
  caption, language, time (IST), post URL, **privacy status**, source credit, status.
  Also returns skipped/failed slots with reasons.
- `GET /api/autopilot/status` · `POST /api/autopilot/pause|resume|run-now` · `GET/PUT /api/autopilot/settings`.
- Admin key on all `autopilot` routes.

### Frontend
- **Posted tab** (`/posted`, new nav item): newest first; cards with thumbnail, caption,
  EN/HI chip, IST time, link to the Short, privacy badge (Private / Public), source credit;
  a "skipped / failed" row type. Polls every 30 s.
- **Autopilot panel** (Settings): on/off, next slot, last language, buffer count per language,
  last error, "reconnect YouTube" prompt when the token is dead.
- `client.ts` reads `VITE_API_BASE_URL` (only needed for the Vercel path).

---

## 5. Phases

1. **Unblock:** G1 token refresh; flip Google consent screen to *In production*; one manual
   upload to confirm token survives >1 h **and to see whether the upload comes out private (B)**.
2. **Hindi:** G3–G5; push one Hindi video through the existing manual flow.
3. **Posted tab:** `/api/posts` + page (works on manual posts too).
4. **Discovery dry-run:** discovery + scoring + appeal, output only a ranked list; sanity-check by hand.
5. **Autopilot:** buffer fill, slot tick, alternation, safety rails; `run-now` for testing.
6. **PC hardening:** Docker auto-start, sleep disabled, log rotation, failure alert (email/Telegram).
7. **Optional:** Vercel UI + tunnel + admin key. Instagram later.

## 6. Acceptance checks
- Two consecutive slots produce one English then one Hindi post (state survives a restart).
- Empty buffer → slot skipped, reason visible in Posted tab, nothing broken posted.
- Token expiry mid-run → refreshed transparently; revoked → autopilot pauses with a visible reason.
- Hindi clip shows Devanagari captions correctly in the render.
- Daily cap and `quotaExceeded` stop posting for the day.
- Same source video is never processed twice.

## 7. How "public" gets resolved (decision 10)
"Public would be better" is the goal, but it is not something the code can force:
1. **Phase 1 test decides it.** If the first API upload is already public, nothing more is needed.
2. If it comes out **private**: autopilot keeps posting (privately) so the pipeline is proven;
   the Posted tab shows a Private badge and an "Open in Studio" link, and you flip to public
   by hand until the audit is approved.
3. In parallel, apply for the API compliance audit. It needs a public home page, privacy
   policy, terms, consent-screen logo and a demo video (`youtube-setup-plan.md` Phase 8).
   Approval is not guaranteed for a re-posting app; this plan does not depend on it.
4. An in-app "make public" button is a possible later addition, but `videos.update` needs a
   broader OAuth scope than `youtube.upload` (to be confirmed), which adds consent-screen work.

## 8. Scope notes from the remaining answers
- **Best clip only:** after selection, one clip per source video is marked `autopost_ready`
  (highest Gemini appeal score); the others stay in the DB for manual review.
- **Email alerts:** a small `services/notify.py` sends via SMTP on: autopilot paused, token dead,
  quota hit, 3 consecutive failures, empty buffer at a slot. Rate-limited to avoid floods.
  Needs `SMTP_USER` / `SMTP_APP_PASSWORD` in `backend/.env` (not committed; I won't read or
  store the password).
- **Regions:** `DISCOVER_REGIONS=IN,US`.

No open questions remain; the plan is ready to implement from Phase 1.
