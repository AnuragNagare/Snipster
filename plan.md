
## Creative Commons picker + fixed post times (added 2026-11-17): DONE in code, migrated, not deployed
Goal: at the set times the app picks a trending video, makes the post and posts it, on the schedule agreed
(7:45 PM IST first, English; Hindi lunch slot 12:15 PM IST later).
Decision: NOT "any trending video". Only videos whose owners licensed them for reuse (YouTube "Creative Commons"),
credited in every post. Reusing ordinary trending videos was declined: not the user's to give permission for.
- [x] Source mode setting: `own` (default) or `creative_commons`; language `en` / `hi`.
- [x] CC picker (`services/autopilot.py`): 2 searches (4-20 min, >20 min, 100 quota units each), recent 30 days, ranked by views + 50 x likes; the licence is re-read from the video record; skips non-public, live, made-for-kids, outside 3-60 min, under 5,000 views, wrong language, the user's own channel, used videos, and categories Film/Music/Entertainment/Movies/Anime/Shows/Trailers.
- [x] Credit: every post from a CC video carries "Original video: title by creator + link + CC BY" in its description.
- [x] Fixed post times (`post_times`, in the user's timezone), daily limit, 30-minute clash rule; blank = old even spacing.
- [x] Backlog guard: no new pick while `posts_per_day x 2` posts are waiting.
- [x] Activity page: source, language, post times; picked videos show creator, views, likes.
- [x] Tests: 379 backend (60+ new), mutation-checked; migration `e5c2a8d41f96` applied to Neon.
- [ ] Deploy (UI and API on Vercel are still the old code).
- [ ] Dry run with CC mode; user decides on auto-approve.
Known limits: the CC pool is thin (English ~21 eligible/month, Hindi ~4, mostly news recordings); popularity ranking is not niche-aware (it picked carpentry and vlogs); mislabelled CC uploads exist; language per slot is global, not per slot.

## Cloud worker without the laptop (investigated 2026-11-18)
Goal: run the pipeline with the laptop off, ideally free.
Free/cheap ideas considered: GitHub Actions (public repo, unlimited minutes, 2 CPUs / 7 GB, 6 h per job), Google Cloud Run Jobs (about 50 CPU-hours a month free, card needed), small paid VPS plus a hosted Whisper API, GPU on demand (RunPod/Modal).
Test done: a manual GitHub Actions workflow tried to download 30 seconds of a Creative Commons video (`AvikGCepgNY`) with yt-dlp 2026.10.26 from a GitHub runner (Microsoft network, AS8075), in five YouTube client modes (default, tv, mweb, android_vr, web_safari). All five failed with "Sign in to confirm you're not a bot". Verdict: downloads from GitHub runners are BLOCKED without cookies or a proxy.
Consequence: every data-centre route (GitHub, Google Cloud, any VPS) will most likely hit the same block for downloading other people's videos. Google Cloud Run was not tested.
Ways around it, none free and reliable: (a) a residential proxy for yt-dlp (paid, per GB), (b) browser cookies from a YouTube account stored as a secret (fragile, expires, risks that account), (c) only process videos that are uploaded to storage directly (own videos), which needs no YouTube download and could run free on GitHub Actions, but then there is no trending/Creative Commons picking.
Open decision for the user: which of (a), (b), (c), or keep the laptop.

## Auto-start on the laptop (2026-11-18): L1-L4 DONE and running, L5 partly done (user steps left)

Goal: when the laptop is switched on and the user logs in, the worker and the scheduler start by themselves, run hidden in the background, restart if they crash, and process whatever is waiting in the queue. The user does nothing. No `.exe` is built: the worker carries Whisper and the GPU libraries, so a packaged exe would be huge and add nothing. A Windows Task Scheduler task plus a small launcher script does the job and can be removed with one command.

What this does not do: it does not make processing run while the laptop is off, asleep or offline. Posting without the laptop is a separate plan (GitHub Actions, see the previous section).

### Phases: 5

#### Phase L1: Launcher with a supervisor loop [DONE]
- New `backend/scripts/start-snipster.ps1`. It runs hidden and does this in order:
  1. Single-instance guard (a lock file or named mutex) so a second start does nothing.
  2. Waits until the network works: keeps retrying until the Neon host and the Redis host resolve (the user's DNS has failed intermittently before), with a time limit and a log line for each retry.
  3. Starts the worker (`celery -A app.workers.celery_app worker -P solo`) and the scheduler (`... beat`) from `backend/.venv`, with the working directory set to `backend/`.
  4. Watches both. If one exits, it starts it again after a short pause that grows after repeated crashes (1 s, 5 s, 30 s, then every 60 s), and writes why it exited to the log.
  5. Removes a stale `celerybeat-schedule` file before starting the scheduler (a bad one can stop it).
- Logs go to `%LOCALAPPDATA%\Snipster\logs` (`worker.log`, `beat.log`, `launcher.log`), rotated at about 5 MB, 3 files kept. Logs never contain secrets (the worker log already hides the Redis password).
- It reads `backend/.env` the way the worker does today and writes no secrets anywhere.

#### Phase L2: Stop and status helpers [DONE]
- `backend/scripts/stop-snipster.ps1`: stops the launcher and the processes it started, and only those (found through the lock file, not by killing every Python process).
- `backend/scripts/status-snipster.ps1`: shows whether the launcher, worker and scheduler are running, their start times, the last lines of each log, and the age of the last scheduler tick as the live site reports it.

#### Phase L3: Register with Windows Task Scheduler [DONE]
- `backend/scripts/install-autostart.ps1` creates the task "Snipster Worker" for the current user:
  - Trigger: at log on.
  - Runs only while the user is logged on (no password stored; this also keeps access to the GPU session that CUDA and NVENC need).
  - Allowed on battery, not stopped when switching to battery, no time limit, a second instance is ignored, restarts after failure.
  - Hidden, no console window.
- `backend/scripts/uninstall-autostart.ps1` removes the task and stops the processes.
- The installer first stops any worker or scheduler the user (or Claude) started by hand, so there are no duplicates.
- Needs the user's go-ahead before it is run, because it changes Windows startup settings.

#### Phase L4: Verify [DONE]
- Run the task by hand and confirm worker and scheduler appear and the live Activity page shows "Scheduler seen".
- Kill the worker on purpose and confirm the launcher restarts it and logs why.
- Start the launcher twice and confirm the second start does nothing.
- Start it with the network down, then bring the network back, and confirm it waits and then starts.
- Put a video in the queue while no worker runs, start the launcher, and confirm the queue is processed.
- The user does one real reboot test (Claude cannot reboot the laptop): restart, log in, wait two minutes, check the Activity page.

#### Phase L5: Docs and power settings [PARTLY DONE: lid setting and a real reboot test are the user's]
- Short how-to in these md files: install, remove, where the logs are, how to check status.
- Power settings the user should set (not changed automatically unless the user says yes): lid close = do nothing and sleep = never while plugged in, otherwise the laptop sleeps and processing stops.

### Limits and risks
- At log on, not at boot: if Windows restarts after an update and waits at the sign-in screen, nothing runs until the user signs in. Auto sign-in would fix that but lowers security; the user decides.
- Sleep or hibernate stops the work. After wake, the worker reconnects on its own (tested in L4 if possible), but jobs that were running may need to be retried.
- The laptop must be online, have the NVIDIA driver, and keep the project folder where it is (`D:\Git hub project\Snipster`). Moving the folder breaks the task.
- A failed video is retried by autopilot at most 3 times a day (existing rule).

#### Added during the work: DNS fallback for the worker's database connection [DONE]
Finding: on the user's current network the laptop's DNS cannot resolve the Neon host (`*.c-7.us-east-2.aws.neon.tech`) although Google's DNS (8.8.8.8) can. The DNS server set by hand earlier is lost whenever the network changes. Unattended start would wait forever. Fix in code, worker only (the API on Vercel is unaffected): `app/core/dns_fallback.py` (plain UDP DNS question to 8.8.8.8 then 1.1.1.1, standard library only, decision cached 5 minutes) and `app/db/sync_engine.py` (psycopg2 `hostaddr` with the original host name kept, so TLS and Neon routing still work; the address is looked up again for every new connection). Used by the worker (`_get_sync_session`) and `scripts/last_tick.py`. Redis and everything else keep the normal resolver (Redis resolved fine). The launcher waits through `scripts/check_host.py`, which accepts the fallback for the database host only.

### How to use (auto-start)
- Installed task: "Snipster Worker" (runs at log on, current user, hidden). Scripts are in `backend/scripts/`:
  - `powershell -File scripts\status-snipster.ps1`: what is running, last log lines, seconds since the last scheduler tick.
  - `powershell -File scripts\stop-snipster.ps1`: stop worker and scheduler (the task starts them again at the next log on).
  - `powershell -File scripts\install-autostart.ps1` / `uninstall-autostart.ps1`: add or remove the task.
- Logs: `%LOCALAPPDATA%\Snipster\logs` (`launcher.log`, `worker.err.log` holds the worker's output, `beat.err.log` the scheduler's; the last 3 runs are kept as `.1`, `.2`, `.3`, rotated at each start).
- Power: sleep and hibernate are already "never" when plugged in on this laptop. The lid-close action could not be read by script; the user should check Control Panel > Power Options > "Choose what closing the lid does" and set "Do nothing". A closed lid or sleep stops everything.
- Still for the user: one real reboot test (restart, sign in, wait two minutes, check the Activity page shows the scheduler).

## Cloud posting without the laptop (built 2026-11-18): DONE and proven; real uploads still off
Goal: a post goes out at its set time even when the laptop is off. The heavy work (pick, download, transcribe, render) still needs the laptop; posting does not, because it is only an upload through Google's official API.
How it works: `.github/workflows/publisher.yml` runs `python -m app.cloud_publisher` every 15 minutes on a GitHub runner (public repo, unlimited minutes). It claims each due scheduled post with the same atomic update the laptop scheduler uses (SCHEDULED to QUEUED, so whichever side claims first posts it and the other finds nothing), then runs the same publish task: dry run, kill switch, privacy setting, token refresh and the Creative Commons credit all behave exactly as on the laptop. Autopilot's own posts are held while autopilot is switched off; posts scheduled by hand always go out. It exits with code 1 when a post failed, so GitHub shows a red run and emails the owner.
Secrets: 8 repository secrets (write-only): DATABASE_URL, STORAGE_ENDPOINT, STORAGE_BUCKET, STORAGE_ACCESS_KEY, STORAGE_SECRET_KEY, TOKEN_ENCRYPTION_KEY, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET. TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are read by the workflow but not stored yet (empty locally). Not given to it: Redis, Gemini, admin password, session key, YouTube API key.
Proof: (1) a manual run with nothing due connected to the real database and said "Nothing due". (2) With the laptop's worker and scheduler stopped, a temporary due rehearsal post was published by the GitHub job and recorded as a rehearsal in the database. The temporary post was then deleted. (3) An earlier run showed the guard: the laptop's scheduler claimed a post one second before the cloud looked, so the cloud correctly did nothing.
Limits: GitHub starts scheduled runs 5-30 minutes late, so a post can go out a little after 7:45 PM IST. GitHub pauses scheduled runs after 60 days with no repository activity (re-enable in the Actions tab or push any commit). A job claimed by the laptop but never run (laptop switched off right after claiming) waits in the queue until the laptop is back: the cloud only takes SCHEDULED posts. A failed upload stays FAILED until Retry is pressed on the Activity page, and Retry goes through the laptop's queue.
Not changed: dry run is still ON, so nothing has been uploaded to YouTube.

## Exact time, alerts, auto-retry (2026-11-18): DONE in code and deployed; YouTube scheduling and the cloud check need the user's steps
Decisions: the repository stays PUBLIC (unlimited free Actions minutes, publisher keeps running every 15 minutes).

#### Phase E0: Log fix [DONE]
- Log lines never carry caption text (only job ids). Telegram alerts may still name the caption (private chat).

#### Phase E1: Exact posting time through YouTube's own scheduling [DONE]
- New setting `schedule_on_youtube` (off by default). When on, privacy is Public, and the post is not a dry run: the post is claimed up to 3 hours early and uploaded as private with YouTube's `publishAt` set to the exact slot (for example 19:45 IST). YouTube makes it public at that second, so a late GitHub run no longer matters.
- Only when privacy is Public: `publishAt` can only make a video public. With Private or Unlisted the post goes out at the slot as before. A post whose time is already near or past is uploaded immediately.
- Activity shows "uploaded, goes public at ...". Switching autopilot off does NOT cancel a video already uploaded with a publish time: it goes public unless the user changes it in YouTube Studio. Documented in the UI.
- Unknown until tested: YouTube may keep uploads from an unaudited API project locked private, which could also stop the switch to public.

#### Phase E2: Alerts and keep-alive for the cloud publisher [DONE]
- The cloud job pings a dead-man's-switch (healthchecks.io, new setting/secret `HEALTHCHECK_CLOUD_URL`) after every run, and pings `/fail` when a post failed. When pings stop (including the 60-day pause) the service messages the user.
- The cloud job records `last_cloud_run_at`; the Activity page shows how long ago it ran; the laptop scheduler alerts (Telegram) if it has been silent for more than 3 hours.
- `.github/workflows/keepalive.yml`: once a month a tiny commit keeps the repository active so GitHub does not pause scheduled workflows. Whether a bot commit counts as activity is not guaranteed by GitHub's documentation.

#### Phase E3: Automatic retry [DONE]
- Failed posts are put back (SCHEDULED) after 15 minutes, 1 hour, 4 hours, 12 hours, at most 5 automatic tries, only while the post is less than 24 hours old, then an alert says it was given up. Setting `auto_retry` (on by default).
- Never retried: sign-in problems (needs reconnect), "uploaded but not saved" (a retry would post twice), invalid settings, jobs with no recorded failure time.
- Done by both the laptop scheduler and the cloud job, with one conditional update so a post is requeued only once.
- The publish task no longer alerts at its own first failure when an automatic retry will follow.

#### Phase E4: Tests, migration, deploy, docs [DONE]
- Tests for every rule above, mutation checks, workflow file checks; migration `f3b7d92a1c58`; frontend toggles and status text; real runs of the cloud job and the keep-alive workflow; Vercel redeploy.

Status and proof (2026-11-18, night)
- E0: publish log lines no longer name captions (checked by a test and in a real cloud run). Alerts to the private Telegram chat may still name the caption. Correction of an earlier statement: the first version of the cloud job did print a 60-character caption snippet in its dry-run line; that is now removed.
- E1: built and tested with fakes, NOT tried against real YouTube. Off by default (`schedule_on_youtube`). Works only with visibility Public and dry run off. Needs the user to switch it on, and depends on whether YouTube lets an unaudited API project make a video public at all (unknown).
- E2: cloud run heartbeat (`autopilot_settings.last_cloud_run_at`, shown on the Activity page), `HEALTHCHECK_CLOUD_URL` ping and `/fail` ping, laptop alert after 3 hours of cloud silence, monthly keep-alive workflow. The keep-alive workflow was run by hand and did commit to `main` as the bot. NOT done: the user has not created the healthchecks.io check, so `HEALTHCHECK_CLOUD_URL` is not set and the 60-day alert does not exist yet. Whether a bot commit counts as "activity" for GitHub's 60-day rule is not guaranteed.
- E3: proven for real: with the laptop worker stopped, a temporary FAILED post (retryable error, failed 20 minutes before) was put back by the GitHub job, retried and recorded as a rehearsal; then deleted. The rules (waits 15 min, 1 h, 4 h, 12 h, 12 h; 5 tries; under a day; never for sign-in problems, uploaded-but-unsaved, wrong settings) are covered by tests.
- E4: migration `f3b7d92a1c58` applied to Neon (3 columns, additive). Backend 473 passed; frontend type-checks; deployed to Vercel (new toggles live). New helper `backend/scripts/migrate.py` runs migrations through the DNS fallback because `alembic upgrade head` failed on this network.

User steps still open
1. Create a free check at healthchecks.io (period 15 minutes, grace time 1 hour, notifications to email or Telegram), then store its ping URL: `gh secret set HEALTHCHECK_CLOUD_URL --repo AnuragNagare/Snipster` (or give the URL to Claude).
2. Telegram bot token and chat id (also GitHub secrets `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and `backend/.env`) so alerts reach the phone.
3. Decide when to switch on "Exact time through YouTube" (needs Public visibility and dry run off, so after the YouTube API audit).
