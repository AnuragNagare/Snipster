# Snipster: language-aware captions and editable subtitles: Plan

Status: Phases 1-5 DONE. Still unverified: the UI in a browser, the migrations on Neon, real Hindi speech through Whisper and Gemini. Progress goes in `changes-captions.md`,
background in `context-captions.md`. The Hindi-support work is in `plan.md` / `changes.md` /
`context.md` and is finished in code; this plan builds on it.

## What is already done (from the Hindi plan, phases 1-6)
- Whisper detects the language; it is saved in `source_videos.language`.
- Gemini writes the caption and hashtags in that language (Hindi -> Devanagari).
- Caption and hashtags are editable (`PATCH /clips/{id}`, `ClipEditor.tsx`).
- Burned-in subtitles use the Whisper transcript and a Devanagari font for Hindi.
- The language shows as a badge on each clip.

## What is still missing (the scope of this plan)
1. Detection can be wrong (Hinglish, short or noisy audio) and the user cannot correct it.
2. No way to regenerate a caption after the language is corrected.
3. Burned-in subtitles cannot be edited. A wrong Whisper word stays in the video.

Interpretation of "captions below the post": both the post text (caption + hashtags, which become
the YouTube description) and the subtitles burned into the video. Item 3 is the real extra work.

## Phases: 5

### Phase 1: Language override and caption regeneration (backend) [DONE]
- `PATCH /videos/{id}/language` accepts `en` or `hi` and saves it on `SourceVideo.language`.
- `POST /clips/{id}/regenerate-caption` queues `generate_caption_task` for that clip (the task
  already reads `video.language`). Commit before queueing, like the other routes.
- Files: `api/routes/videos.py`, `api/routes/clips.py`, `db/schemas.py`.

### Phase 2: Editable subtitles: storage and renderer (backend) [DONE]
- New nullable column `clips.subtitles` (Text, JSON list of `{start, end, text}`) and a migration
  after `8c1f2a9d4e7b`.
- `GET /clips/{id}/subtitles`: the saved lines, or lines built from the transcript if none are saved.
- `PATCH /clips/{id}/subtitles`: saves edited lines (validate times inside the clip, text length).
- `renderer.render()` writes the SRT from the saved lines when present, else from the transcript.
- `rerender` keeps working; re-rendering uses the edited subtitles.
- Files: `db/models.py`, `db/schemas.py`, `api/routes/clips.py`, `services/renderer.py`,
  `workers/tasks.py`, `migrations/versions/`.

### Phase 3: Frontend, subtitle editor [DONE]
- A "Subtitles" tab in `ClipEditor.tsx`: one row per line (time + text), edit text, then
  "Save & Re-render".
- Devanagari must display and save correctly.
- Files: `components/ClipEditor.tsx`, `api/index.ts`, `types/index.ts`.

### Phase 4: Frontend, language override and regenerate [DONE]
- On the clip card or editor: a Hindi / English selector, and a "Regenerate caption" button.
- Changing the language asks whether to regenerate captions for all clips of the video.
- Files: `components/ClipCard.tsx`, `components/ClipEditor.tsx`, `pages/Review.tsx`, `api/index.ts`.

### Phase 5: Tests and verification [DONE]
- Unit tests: language override, regenerate queues the task, subtitle save and validation, renderer
  uses saved lines, migration chain.
- Real check: render a clip with edited Hindi subtitles through ffmpeg (done earlier, see changes-captions.md).
- Still needs a real Hindi video through Whisper and Gemini (not available on this PC so far).

## Decisions and risks
- Separate YouTube title field: NOT in this plan. Today the title is `caption[:100]`.
- A subtitle edit changes the stored lines only; it never rewrites the Whisper transcript.
- Editing the language after publish has no effect on already-published videos.
- The migration touches Neon; the user runs `alembic upgrade head`.
