
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
