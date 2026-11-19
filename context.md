
## Latest (2026-11-17, night): channel set; autopilot has nothing eligible
- `YOUTUBE_CHANNEL_ID=UCdVtMTDv431hgv9jOVgOjcQ` (@SnipDaily "Snipster") is in `backend/.env`. Worker + beat running (restarted 21:37). Autopilot ON, dry run, auto-approve, private.
- The channel only has 5 Shorts (38-58 s): zero eligible sources (min 180 s). To rehearse, the user needs a long (3-60 min) public video of their OWN on this channel, or to use the Upload File / YouTube URL tabs for a video they own.
- Dashboard lists only posts recorded in `publish_jobs` (0 rows), so it would be empty even though the channel has 5 Shorts.
- Unverified: whether the user has rights to the long video behind the existing Shorts (Raj Shamani Insights). The app's ownership checkbox was ticked for it.

## Creative Commons mode + fixed times (2026-11-17)
- Settings: `source_mode` own|creative_commons, `source_language` en|hi, `post_times` "HH:MM,..." in `timezone`. Live settings are still own / dry run / auto-approve (they were not changed). The live API (Vercel) does not know the new fields until redeployed.
- CC rules and credit: see plan.md. Never any non-CC video of another creator. CC pick has `ownership_confirmed=False` and `autopilot_sources.license='creativeCommon'`; its posts carry the credit.
- Migration head is `e5c2a8d41f96` (applied to Neon). Worker + beat running on the new code since 22:12.
- Recommended before going live in CC mode: keep Dry run ON, turn Approve automatically OFF so clips wait on the Review page, check mislabelled or news content, then decide.
- Quota: each pick costs ~200 units (2 searches) + ~1,600 per upload; default 10,000/day.

## Deployed (2026-11-17, late)
- Production https://snipster-two.vercel.app runs everything on GitHub `main` (`437b1cb`): Dashboard, CC picker, fixed times. `YOUTUBE_API_KEY` is now set on Vercel. README/DEPLOY/automation_plan and the `-captions` notes were deleted by the user on purpose; do not recreate them.
- Live settings are unchanged (own channel, dry run, auto-approve ON, English, no fixed post times, UTC, private). Nothing has been picked or posted yet.
- To start the agreed plan: in Activity set Where videos come from, Video language, Post at (e.g. 19:45), Timezone Asia/Kolkata, then Run now. Recommended first: Dry run ON, Approve automatically OFF so clips wait on the Review page.

## Latest (2026-11-17, ~22:45): CC dry run in progress
- Live settings: CC, English, post_times 19:45, Asia/Kolkata, dry run ON, auto-approve ON, private.
- A wordless video was picked first and failed the new no-speech guard (`MIN_TRANSCRIPT_WORDS=60`); popularity ranking cannot see speech, so expect some picks to fail this way (max 3 retries a day, then the slot stays used).
- Remember for the user: only the source dropdown auto-saves in the Activity card; "Save schedule" is needed for post times, timezone, posts/day, clips/video, run hours.

## State at end of 2026-11-17
- Dry run proven through scheduling on real data. Pending jobs: 2 autopilot posts at 19:45 IST on 2026-11-18 and 2026-11-19; with dry run ON they will be rehearsals (no upload).
- Pace measured: ~16 min per video end to end; Gemini is the slow part. Expect about 2-3 videos per day on this PC.
- Open: first real (private) upload not done; credit line unseen in a live post; decide auto-approve for CC mode (still ON); thin Hindi CC pool.

## 2026-11-18: cloud worker finding
- yt-dlp downloads are blocked from GitHub-hosted runners (bot check, 5 client modes tried). Expect the same from other data-centre IPs. Options and trade-offs are at the end of plan.md ("Cloud worker without the laptop"). The laptop remains the only place downloads are known to work. Nothing about the cloud worker is built.

## Auto-start on the laptop (2026-11-18): planned, not started
- Plan lives at the end of `plan.md` (phases L1-L5). Approach: PowerShell launcher with a supervisor loop plus a Task Scheduler task "Snipster Worker" at log on, no `.exe`. Scripts will go in `backend/scripts/`, logs in `%LOCALAPPDATA%\Snipster\logs`.
- Currently the worker and scheduler on the laptop are started by hand (background processes started by Claude, logs in the session scratchpad). They stop when the laptop shuts down.
- Posting without the laptop (GitHub Actions publisher) is a separate, later plan; the user approved the idea but it is not built. Needs repository secrets.
- Cloud processing is blocked by the YouTube download block on data-centre IPs (see the "Cloud worker without the laptop" section).

## Laptop auto-start is installed (2026-11-18)
- Task "Snipster Worker" (at log on) runs `backend/scripts/start-snipster.ps1`, which keeps the worker and scheduler running hidden and restarts them if they crash. Check with `powershell -File backend\scripts\status-snipster.ps1`. Logs: `%LOCALAPPDATA%\Snipster\logs`. Remove with `uninstall-autostart.ps1`. Do not start a second worker by hand: the launcher kills strays of this project at start.
- The worker reaches Neon through a DNS fallback when the network's DNS cannot resolve the database host (common on the user's hotspot/Wi-Fi): `app/core/dns_fallback.py`, `app/db/sync_engine.py`. Worker only; the Vercel API is unaffected. Do not "simplify" it back to `create_engine(settings.sync_database_url)` in the worker.
- PowerShell notes: scripts must stay ASCII (Windows PowerShell 5.1); never pipe a native command's output into `Select-Object -First 1` when its exit code matters; do not pass multi-line code through `python -c`.
- Live state: autopilot ON, dry run ON, Creative Commons, English, post time 19:45 Asia/Kolkata, 4 posts scheduled (14:15Z on Nov 18, 19, 20, 21). The Nov 18 one was overdue when the worker came back, so a rehearsal should appear on the first tick.
- Open: real reboot test by the user; lid-close setting; alerts (Telegram, health check) still unset; cloud posting without the laptop (GitHub Actions publisher) approved in principle, not built.

## Cloud publisher is live (2026-11-18)
- `.github/workflows/publisher.yml` + `backend/app/cloud_publisher.py`: every 15 minutes GitHub claims and publishes due SCHEDULED posts, so posting no longer needs the laptop. Same publish task as the laptop (dry run, kill switch, privacy, credit). Proven with the laptop worker stopped. Check runs with `gh run list --repo AnuragNagare/Snipster --workflow publisher.yml`.
- Both sides may run at once: the atomic claim (SCHEDULED to QUEUED) means only one posts a given job. Do not add a second claim path or change that update to drop the status condition.
- Repository secrets hold production credentials (8 names, see plan.md). If a key is rotated (database password, storage keys, Google secret, token encryption key), update the GitHub secret too (`gh secret set NAME --repo AnuragNagare/Snipster`), and Vercel and `backend/.env`.
- When real uploads are switched on (dry run OFF), both the laptop and GitHub will upload; uploads stay private by default and YouTube forces private until the API audit passes.
- Still open: Telegram/health-check alerts, a real reboot test, lid-close setting, first real private upload, scheduled cron run not yet observed.

## Exact time, auto-retry, cloud alerts (2026-11-18, night)
- Live on Vercel and GitHub `main`. Migration head `f3b7d92a1c58`, applied. Use `python scripts/migrate.py` (not `alembic upgrade head`) on a network that cannot resolve the database host.
- Auto-retry is ON by default (15 min, 1 h, 4 h, 12 h, 12 h; max 5; post under 24 h old). It never retries sign-in problems, "uploaded but not saved", wrong settings. The cloud and laptop requeue with one conditional UPDATE: keep both conditions (status and try count).
- "Exact time through YouTube" (`schedule_on_youtube`) is OFF and untested against real YouTube. It uploads up to 3 hours early as private with `publishAt`; switching autopilot off does not cancel an uploaded video.
- Cloud publisher writes `autopilot_settings.last_cloud_run_at`; the laptop alerts after 3 hours of silence. `HEALTHCHECK_CLOUD_URL` (GitHub secret) is empty until the user creates a healthchecks.io check.
- Public repo: logs are public, so never log captions, URLs of private things, or secrets. The test `test_publish_logs_never_contain_the_caption` guards this.
- `keepalive.yml` commits monthly, so `git pull --rebase` before pushing if the bot committed.
- Dry run is still ON; nothing has been uploaded to YouTube yet.

## Creative Commons filters (2026-11-19)
- The picker now skips gaming and sports categories, live-stream recordings, sponsored videos (paid flag and words like #ad), and titles with review/reaction/trailer/highlights style words. Settings: `AUTOPILOT_CC_BLOCK_CATEGORIES`, `AUTOPILOT_CC_ALLOW_LIVE_RECORDINGS`, `AUTOPILOT_CC_BLOCK_WORDS` in `backend/.env` (defaults in `core/config.py`). Worker-only.
- Hindi Creative Commons has 1 eligible video (news): not worth enabling. English has about 14 per month.
- A cancelled pick keeps its `autopilot_sources` row (status failed) so it is never picked again; deleting that row would make it eligible again.
- Open: two posts scheduled for Nov 20 and 21 come from a refused film review; decide to remove them. Consider turning auto-approve off for Creative Commons mode.

## Email alerts (2026-11-19)
- Alerts go to Telegram and/or Gmail SMTP (`SMTP_USER`, `SMTP_PASSWORD` app password, optional `ALERT_EMAIL_TO`), in `backend/.env` for the laptop and as GitHub secrets for the cloud job. Test with `python scripts\test_alert.py` from `backend`. Not set up yet: waiting for the user's Gmail address and app password; until then alerts only appear in the logs.
- Never write the user's email address or app password into the repository, a log line, or a commit; the workflow reads them from secrets.

## State after the final checks (2026-11-19)
- Alerts: Gmail works (real test mail sent). Cloud job pings healthchecks.io each run. Telegram not used.
- Content filters now also block category 25 (News & Politics). Two rehearsal posts from the refused Fox News commentary pick are still scheduled (dry run is ON, so harmless): ask the user before deleting them.
- No real YouTube upload has happened. The assistant cannot start one (permission system). The user can press "Publish YouTube Shorts" on an approved clip in the Review page.
- Known UI gap: Edit (trim, caption, subtitles) shows only for pending clips; with auto-approve on, no clip is pending.
- Do not type passwords into the live site; the user's browser session is already signed in.
