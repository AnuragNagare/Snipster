# Changes log: language-aware captions and editable subtitles

Newest at the bottom. Format: date, phase, files, what changed and why.

## 2026-11-15
- Planning only. Created `plan-captions.md`, `changes-captions.md`, `context-captions.md`.
  No application code changed. The existing `plan.md`, `changes.md`, `context.md` were left as they are.

## 2026-11-15: Phases 1-3
Checks: existing backend suite 134 passed, 3 skipped (134 = 130 + 4 earlier tests; no new tests yet, that is phase 5). Frontend `npx tsc -b` clean; `oxlint` shows only the 2 existing warnings. Real ffmpeg render with custom Hindi subtitles and with an empty list both succeeded.

**Phase 1: language override and regenerate (backend)**
- `backend/app/api/routes/videos.py`: `PATCH /api/videos/{id}/language` (`en` or `hi`) saves `SourceVideo.language`. Only affects captions and renders made afterwards.
- `backend/app/api/routes/clips.py`: `POST /api/clips/{id}/regenerate-caption` queues `generate_caption_task`.
- `backend/app/db/schemas.py`: `LanguageUpdateRequest`, `LanguageResponse`, `RegenerateCaptionResponse`.

**Phase 2: editable subtitles (backend)**
- `backend/app/db/models.py`: `Clip.subtitles` (Text, JSON list of `{start, end, text}`, clip-relative seconds). NULL = not edited; `[]` = user removed all.
- `backend/migrations/versions/b4e6d1a73c20_add_clip_subtitles.py`: new head after `8c1f2a9d4e7b`. NOT applied to any database.
- `backend/app/api/routes/clips.py`: `GET /clips/{id}/subtitles` (saved lines, or built from the transcript) and `PATCH /clips/{id}/subtitles` (validates end > start and times inside the clip; sorts by start). New `_set_trim()`: a real change to the trim points, via PATCH or rerender, sets `subtitles` back to NULL because the lines were timed against the old trim.
- `backend/app/services/renderer.py`: new `default_subtitle_lines()` (times rounded to 3 decimals); `_write_srt()` now uses it plus a new `_write_srt_lines()`; same 4-words-per-line SRT output as before. `render()` takes `subtitles`: a list is used as is (empty = no subtitles), None falls back to the transcript.
- `backend/app/workers/tasks.py`: `render_clip_task` passes the clip's saved subtitles.
- `backend/tests/test_services/test_language.py`: the migration test no longer pins the head to `8c1f2a9d4e7b`; it checks there is a single head.

**Phase 3: frontend subtitle editor**
- `frontend/src/types/index.ts`: `SubtitleLine`, `SubtitlesResponse`.
- `frontend/src/api/index.ts`: `getSubtitles()`, `updateSubtitles()`.
- `frontend/src/components/ClipEditor.tsx`: now has Details and Subtitles tabs. Subtitles tab lazy-loads the lines; each row has start, end, text, delete; "Add line"; validation message; "Save Subtitles" and "Save & Re-render". Re-render there uses the clip's saved trim, not the sliders. The Details tab warns when trim changes will discard edited subtitles.
- Not opened in a browser; type-checked only.

**Known behaviour to remember**
- Saving subtitles does not change the rendered file; the lines appear after the next re-render.
- The existing Details-tab "Save & Re-render" only sends the trim times; it does not save caption/hashtag edits (this was already so).

## 2026-11-15: Phases 4-5
Checks: full backend suite 161 passed, 3 skipped (27 new tests). Frontend `npx tsc -b` clean; `oxlint` shows only the 2 existing warnings. A mutation check (disabling the "trim change drops subtitles" line) made 2 of the new tests fail, then the line was restored.

**Phase 4: language selector and regenerate (frontend)**
- `frontend/src/api/index.ts`: `getClip()`, `regenerateCaption()`, `setVideoLanguage()`.
- `frontend/src/utils/regenerate.ts` (new): `regenerateCaptionAndWait()` queues the task, then polls `GET /clips/{id}` every 3 s for up to 90 s until `updated_at` changes. Returns false if the worker never answered.
- `frontend/src/components/ClipCard.tsx`: "Regenerate caption" button under the caption, with a spinner while waiting and a toast when done or timed out.
- `frontend/src/pages/Review.tsx`: "Spoken language" selector (English / Hindi) in the header. Changing it saves the language, updates the clips in the store, and shows "Regenerate all captions" plus a note that old captions and rendered clips keep the old language and that audio is not re-transcribed.
- Not opened in a browser; type-checked only.

**Phase 5: tests**
- `backend/tests/test_api/test_captions.py` (new, 27 tests): language override (valid, invalid values -> 422, unknown video -> 404, clip responses carry the language); regenerate (queues after commit, 404 queues nothing); subtitles read (from transcript, saved), save (sorted, Hindi stored as real characters, empty list, 6 bad-line cases); trim change drops subtitles (PATCH and rerender), caption-only edit and same-trim rerender keep them; renderer burns user subtitles, none for an empty list, transcript fallback, clip-relative default lines.

**Limits worth remembering**
- The language selector does not re-transcribe. If Whisper detected English for a Hindi video, the transcript (and the default subtitles) are already wrong; fix by setting `WHISPER_LANGUAGE=hi` in `backend/.env` and uploading again, or by editing the subtitles by hand.
- Regenerate-all runs every clip at once; the worker handles them one at a time, so the last clip can take longer than the 90 s wait.
