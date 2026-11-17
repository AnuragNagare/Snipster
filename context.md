
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
