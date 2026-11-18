
## 2026-11-17: channel set, dry run result: nothing to pick
- User's channel: Snipster (@SnipDaily), id `UCdVtMTDv431hgv9jOVgOjcQ` (resolved with the API key, matches the user's screenshot). Written to `backend/.env` as `YOUTUBE_CHANNEL_ID`. Worker and beat restarted (old celery processes stopped, `celerybeat-schedule*` removed, new ones started with logs in the scratchpad).
- The channel has 5 public videos, all Shorts of 38-58 s (3 views total at the time: 7). Autopilot reads 3-60 minute videos, so 0 are eligible. "Run now" result in the worker log: `No unused eligible video on the channel`. Activity feed: no jobs, no sources. Settings: enabled, dry run on, auto-approve on, private; scheduler heartbeat fresh (31 s).
- So the dry run could not rehearse a post: autopilot only clips long videos from the user's own channel, and that channel has none. The loop (pick, clip, approve, schedule, rehearse) is covered by the automated end-to-end test but has not run on real data.
- Observation: the 5 Shorts on the channel were not posted by this database (it has 0 publish jobs) and their topic matches the video `youtu.be/eaHfex71yO0` ("Raj Shamani Insights"), which is not the user's channel. Rights to that source were not verified.

## 2026-11-17: Creative Commons picker, fixed post times, credit
Checks: backend 379 passed, 3 skipped; frontend `tsc` clean (oxlint: same state-in-effect warnings as other pages). Mutation checks (each made tests fail): licence not rechecked, own channel not excluded, slot daily limit ignored, CC video marked as owned, credit not passed to upload, backlog guard off. Migration `e5c2a8d41f96` generated as SQL, read, then APPLIED to Neon (additive: 5 columns on autopilot_sources, 3 on autopilot_settings; nothing dropped).
- DB/models: `autopilot_sources` + channel_id, channel_title, license, views, likes; `autopilot_settings` + source_mode ('own'), source_language ('en'), post_times ('').
- `services/autopilot.py`: `parse_post_times`, `fetch_cc_candidates`, `cc_score`, `select_cc_candidate`, `credit_line`, `plan_slot_times`, `waiting_autopilot_posts`; `start_next_source` now handles both modes (a CC video's `SourceVideo.ownership_confirmed` is False; own is True); `finalize_ready_sources` uses fixed times when set; `_existing_post_times` also counts future-scheduled posts; `run_tick` skips picking while posts_per_day x 2 posts wait.
- `services/youtube.py::upload(credit=)`, `workers/tasks.py::publish_clip_task` looks up the source and passes the credit for CC sources.
- Config: `AUTOPILOT_CC_DAYS` (30), `AUTOPILOT_CC_MIN_VIEWS` (5000), `AUTOPILOT_CC_BLOCK_CATEGORIES` (1,10,24,30,31,43,44).
- API/UI: settings accept `source_mode`, `source_language`, `post_times` (validated, cleaned); Activity feed sources carry creator, licence, views, likes; Activity page has "Where videos come from", "Video language", "Post at (times)" and shows "Creative Commons by <creator> - views - likes".
- Real check with the user's key (read-only, ~200 quota units each): English found 100 CC videos, 21 eligible; before the category filter the top pick was a K-pop show from a large label (so the filter was added), after it the pick was a carpentry video. Hindi: 100 found, only 4 eligible, all political/news recordings.
- Worker and beat restarted on the new code; the live site (old API code) still works against the migrated database.
- Not done: nothing deployed (live UI cannot set the new settings yet); no CC dry run yet; auto-approve for CC mode left to the user.

## 2026-11-17: pushed and deployed
- The user deleted README.md, DEPLOY.md, automation_plan.md, plan-captions.md, changes-captions.md, context-captions.md and youtube-setup-plan.md on purpose; that deletion was committed (`437b1cb`). History keeps them. `plan.md`, `changes.md` and `context.md` remain.
- GitHub `main` = `437b1cb` (fast-forward from `3871724`; includes the Dashboard tab, Creative Commons picker, fixed post times, channel fallback). Branch `feature/dashboard-tab` also pushed.
- Vercel: `YOUTUBE_API_KEY` was NOT among the project's variables although the user believed it was. Added it from `backend/.env` (sensitive). Deployed to production twice with the CLI; the second deployment (`snipster-6aew25092-...`, READY) has the key. Production address: https://snipster-two.vercel.app.
- Verified on the live site: health 200; home page 200; `/api/dashboard` 200 with no stats_error (the key works there); `/api/autopilot/settings` returns `source_mode`, `source_language`, `post_times`; a bad post time is refused with 422; the built JavaScript contains the Dashboard tab, "Trending Creative Commons videos" and "Post at (times)". Not opened in a browser.
- Live autopilot settings were not changed: enabled, dry run on, auto-approve on, source `own`, English, no fixed times, UTC, private.

## 2026-11-17: first real Creative Commons dry run; no-speech guard
- The user chose Creative Commons / English on the live Activity page and pressed Run now. Result: the worker picked "Even Top Engineers Are Amazed by This 65-Year-Old Master Carpenter's Wood Corner Joints!" (Top Wood Tips, 2.57M views). Download worked (24 s). Whisper `medium` on the GPU found **0 words**: a wordless woodworking video. The pipeline then asked Gemini to choose clips from an empty transcript, which would have invented clips. Stopped by hand; no clips were created.
- The user's Asia/Kolkata timezone and 19:45 post time were NOT saved: only the "Where videos come from" dropdown saves immediately; time and timezone need the "Save schedule" button. Saved for them through the API: timezone Asia/Kolkata, post_times 19:45 (source creative_commons, English, dry run ON, auto-approve ON, private unchanged).
- Fix 1: `MIN_TRANSCRIPT_WORDS` (60). `transcribe_video_task` now fails a video with fewer words (status FAILED, message "Almost no speech in this video (N words found, at least 60 needed), so there is nothing to clip.", no retry, no clip selection); `select_clips_task` repeats the check so a task delivered again can never ask the AI to invent clips.
- Fix 2: when a picked video fails, `run_tick` frees the run slot (`last_run_slot` reset) so the next candidate is tried straight away, at most 3 failed videos per local day (`MAX_RETRIES_PER_DAY`, `failures_today`).
- Cleanup: the wordless test video's source row and video were marked failed in the database (0 clips existed). Worker and beat restarted on the fixed code.
- Tests: 385 passed, 3 skipped (+11); mutation checks: guard removed, slot not freed, no daily retry cap each failed tests.
- Second Run now: picked "The Last Normal Year" (vlogbrothers), a talking video; Whisper detected English at 99.95%. Result of the rest of the run: see below / next entry.
- Not deployed: this fix is only on the local worker (the worker runs here, not on Vercel); the Vercel API and UI do not need it. Not yet committed to GitHub.

## 2026-11-17 (night): first full Creative Commons dry run succeeded end to end
- Pick: "The Last Normal Year" (vlogbrothers, 9:14 min, English 99.95%, CC). Timings on this PC (RTX 3050): download 20 s, Whisper `medium` 231 s (1,856 words), Gemini clip selection 113 s (4 clips), render about 28 s per clip (NVENC), Gemini caption 40-190 s per clip (the slowest step). About 16 minutes from pick to scheduled.
- Result: clips 130.4-188.7 s etc.; 2 approved (47.9 s and 49.2 s), 2 rejected (57-58 s, further from the 40 s target). Two posts scheduled for 19:45 IST on Nov 18 and Nov 19 (14:15Z) as autopilot jobs; source marked done ("Scheduled 2 post(s). Left out: not among best 2."). An approved clip was downloaded and checked: H.264, 1080x1920, 47.9 s, English subtitles burned in at the bottom. The vertical crop is a centre crop of a landscape video (tight on the speaker).
- Not yet seen: the actual dry-run publish (it fires at the scheduled time, about 15 hours later), the credit text in a real upload (dry run uploads nothing), a real YouTube upload.
- A third pick ("Even Master Carpenters...", Top Wood Tips) started right after, probably from another Run now press or the freed run slot; likely wordless again, which the guard will fail.
- Commit `8205cf0` (no-speech guard, slot retry) pushed to GitHub `main`.

## 2026-11-18: GitHub Actions download test
- Added `.github/workflows/download-test.yml` (manual, no secrets), pushed to `main`, ran it once (run 44343627752), then removed it. Result: all 5 client modes failed with "Sign in to confirm you're not a bot"; verdict "download BLOCKED from GitHub runners". The repo is public (unlimited Actions minutes); the token has `workflow` scope.
- No application code changed.

## 2026-11-18: auto-start plan written
- Planning only. `plan.md` got a new section "Auto-start on the laptop" with 5 phases (L1 launcher with a supervisor loop, L2 stop/status helpers, L3 Task Scheduler install and uninstall, L4 verification, L5 docs and power settings), plus limits. No application code, scripts or Windows settings were changed. Older sections of `plan.md` were kept as history.
- Waiting for the user's go-ahead for L1-L5 (L3 changes Windows startup settings).
