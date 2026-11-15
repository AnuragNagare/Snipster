# Context: language-aware captions and editable subtitles

Keep current. Update when architecture, config or decisions change. Background for the whole app is in `context.md`.

## Where things stand
- Detection, storage, Gemini language prompts, Devanagari subtitle font and the language badge are done (see `changes.md`).
- Done in code: language override endpoint, caption regeneration endpoint, editable subtitles (storage, API, renderer, editor tab).
- Also done: language selector + "Regenerate all captions" on Review, "Regenerate caption" on each clip card, and 27 backend tests for the routes and renderer path.

## Facts that matter
- `source_videos.language` holds the detected code (`en`, `hi`). Clips get it at response time (`ClipResponse.language`), not as a column.
- `generate_caption_task(clip_id)` already reads `video.language`, so regeneration only needs to queue it again.
- Burned subtitles: `clips.subtitles` (JSON `[{start,end,text}]`, clip-relative seconds) wins when set; NULL falls back to `default_subtitle_lines()` from the transcript (4 words per line); `[]` = no subtitles. Changing a clip's trim points resets it to NULL (`_set_trim` in `routes/clips.py`).
- `PATCH /clips/{id}` edits caption, hashtags and trim times only. `POST /clips/{id}/rerender` takes only start and end.
- YouTube title is `caption[:100]`; description is caption + hashtags + `#Shorts` (`services/youtube.py`).
- Alembic chain: `54b0d5b7980b` -> `8c1f2a9d4e7b` (`source_videos.language`) -> `b4e6d1a73c20` (`clips.subtitles`, head). Neither new migration is applied to Neon yet; the API fails on `clips.subtitles` until `alembic upgrade head` is run.
- New routes: `PATCH /api/videos/{id}/language`, `POST /api/clips/{id}/regenerate-caption`, `GET|PATCH /api/clips/{id}/subtitles`. Frontend: `getSubtitles`/`updateSubtitles` in `api/index.ts`, tabs in `ClipEditor.tsx`.
- Editing subtitles only stores lines; the video changes at the next `POST /clips/{id}/rerender`.
- API routes commit before queueing a Celery task, because the worker is on another machine.

## Decisions
- "Captions below the post" = post text (caption + hashtags) and burned-in subtitles; both in scope.
- Separate YouTube title field is out of scope.

## Open
- Hindi speech has still never been run through Whisper and Gemini on real audio.

## Current state
- All 5 phases done. Backend suite: 161 passed, 3 skipped. Frontend type-checks.
- Frontend helpers: `utils/regenerate.ts` (queue + poll `updated_at`), `utils/language.ts` (labels), api functions `getClip`, `regenerateCaption`, `setVideoLanguage`, `getSubtitles`, `updateSubtitles`.
- The language selector only changes the stored language (captions, subtitle font); it does not re-transcribe.
- Not verified: UI in a browser, migrations on Neon (`alembic upgrade head` applies both new ones), real Hindi speech through Whisper and Gemini.
- Not built: separate YouTube title field (title is still `caption[:100]`), the deferred automation track, the Gemini ~12-minute transcript limit.
