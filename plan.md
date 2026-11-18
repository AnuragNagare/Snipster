
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
