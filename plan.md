# Snipster Pipeline Speedup — Plan (#2: compute + I/O optimisations)

**Scope:** make the existing pipeline faster. Does **not** touch the Celery queue-routing
stall (`task_routes` vs. missing `-Q`) — that is tracked separately as #1.

**Verified on this machine before writing this plan:**

| Check | Result |
|---|---|
| GPU | NVIDIA RTX 3050, 6 GB, driver 610.62 |
| `ctranslate2.get_cuda_device_count()` | `1` |
| `WhisperModel("base.en", device="cuda", compute_type="float16")` | loads OK — **but see below** |
| ffmpeg | 9.0.1 gyan full — `h264_nvenc`, `nvdec`, `cuvid`, `libass` all present |

**Correction, found during implementation.** That third row was a bad check: constructing
the model on CUDA succeeds even when the GPU cannot actually run it — the failure only
appears on the first *encode*. It does fail here:

```
RuntimeError: Library cublas64_12.dll is not found or cannot be loaded
```

This machine has the CUDA **11.8** toolkit (`cublas64_11.dll`); CTranslate2 4.8.1 wants
CUDA **12**. It already bundles `cudnn64_9.dll`, so cuBLAS was the only gap. Fixed by
adding `nvidia-cublas-cu12` to `requirements.txt` plus a `_register_cuda_dlls()` helper
that preloads the DLL — `os.add_dll_directory` alone is not enough, because that only
affects `LoadLibraryEx` with the user-dirs flag and CTranslate2's native code calls plain
`LoadLibrary`. A `_smoke_test()` now forces one real encode at load time, so a broken GPU
falls back to CPU immediately instead of dying mid-transcription.

**This was almost certainly the actual bug.** `faster_whisper.WhisperModel` defaults to
`device="auto"`, which selects CUDA whenever a GPU is visible — so the original code was
picking the GPU and then raising on the first encode, every time.

---

## The four changes

### 1. Download the source **once**, not 6×

**Today:** `tasks.py:120` downloads the full source from R2 to transcribe, then
`tasks.py:217` downloads *the entire source again inside every `render_clip_task`*.
A 5-clip job pulls the full file **6 times**. For URL jobs the file was also just
downloaded from YouTube and thrown away (`tasks.py:52`, `TemporaryDirectory`).

**Change:** new `backend/app/services/media_cache.py`

```python
get_local_source(video_id, storage_key) -> str   # returns a local path
put_local_source(video_id, src_path) -> str      # seed cache from an existing file
release(video_id) -> None                        # drop the cached copy
```

- Cache root: `settings.MEDIA_CACHE_DIR` (default `<tempdir>/snipster-cache/{video_id}/source.mp4`).
- Download to `source.mp4.part` then `os.replace()` — atomic, so a second task never
  reads a half-written file.
- Lock file per video so two concurrent renders don't both download.
- `download_video_task` calls `put_local_source()` with the file yt-dlp just produced,
  **before** the `TemporaryDirectory` unwinds → URL jobs then do zero R2 downloads.
- `release(video_id)` once every clip of that video has reached `DONE`/`FAILED`,
  plus an age sweep (>6 h) on cache access.

**Effect:** 6 full downloads → 1 (upload jobs) or 0 (URL jobs). On a home connection
with a 1 GB source this is usually the single largest win.

---

### 2. Whisper on the GPU

**Today** — `services/transcription.py:15,28`:

```python
WhisperModel("base.en", device="cpu", compute_type="int8")
model.transcribe(audio_path, word_timestamps=True)
```

CPU int8, greedy-less (default `beam_size=5`), no VAD, and decoding a full H.264 mp4
through PyAV frame by frame.

**Change:**

- Device auto-detect: `ctranslate2.get_cuda_device_count() > 0` → `device="cuda"`,
  `compute_type="float16"`; otherwise fall back to the current `cpu`/`int8`.
- Extract audio first with ffmpeg — `-vn -ac 1 -ar 16000 -c:a pcm_s16le` — and hand
  Whisper a small WAV instead of the mp4. Removes video decode from the hot path.
- `beam_size=1` (greedy), `vad_filter=True` (skips silence entirely),
  `condition_on_previous_text=False` (prevents the repetition loops that stall long runs).
- Keep `word_timestamps=True` — the SRT builder in `renderer.py:_write_srt` needs it.

All knobs land in config so this is reversible without a code edit:
`WHISPER_MODEL`, `WHISPER_DEVICE=auto`, `WHISPER_COMPUTE_TYPE=auto`, `WHISPER_BEAM_SIZE=1`,
`WHISPER_VAD=true`.

**Effect — measured**, 3m37s of speech, `base.en`, word timestamps on:

| Configuration | Time |
|---|---|
| Before: CPU, float32, beam 5, no VAD | 24.58 s |
| After, GPU unavailable: CPU, int8, beam 1, VAD | 14.96 s (1.6×) |
| After: CUDA, float16, beam 1, VAD | **3.14 s (7.8×)** |

Word counts stayed within 1% of each other (624 / 629 / 630), so the speedup is not
coming from dropped audio. 6 GB VRAM is far more than `base.en` needs, so `small.en`
becomes affordable later if you want better accuracy.

---

### 3. NVENC instead of libx264

**Today** — `renderer.py:53-55` and `:75-77`, duplicated across two near-identical methods:

```
-c:v libx264 -preset fast -crf 23
```

**Change:**

- Probe `h264_nvenc` once per process, cache the result.
- Encode with `-c:v h264_nvenc -preset p4 -tune hq -rc vbr -cq 23 -b:v 0 -pix_fmt yuv420p`.
- Add `-hwaccel cuda` for decode. Deliberately **not** `-hwaccel_output_format cuda`:
  `subtitles`/`libass` is a CPU filter, so frames must come back to system memory —
  letting ffmpeg do that copy implicitly keeps the filter chain unchanged and correct.
- Fold `_render_no_captions` / `_render_with_captions` into one `_build_cmd()`; the only
  difference between them is one `subtitles=` entry in `-vf`.
- Fall back to the current libx264 command if the probe fails. `FFMPEG_ENCODER=auto|h264_nvenc|libx264`.

Unchanged: `-ss` stays *before* `-i` (already fast input seek), the scale+crop chain,
AAC 128k, and `+faststart`.

**Effect — measured, and well below my estimate.** I predicted 3–8×; a 20 s clip
actually renders in 2.79 s on NVENC vs 4.75 s on libx264 (**1.7×**), and 2.22 s vs 3.39 s
(**1.5×**) with the surrounding I/O excluded. Breaking the 20 s render down: decode 0.35 s,
encode with no filters 1.86 s, + crop 1.95 s, + subtitles 2.22 s — encoding dominates and
the filters are nearly free, so there is little left for hardware offload to remove at this
clip length. NVENC also produced a noticeably larger file at matched quality settings
(16.3 MB vs 6.4 MB), which costs upload time to R2. Worth keeping, but it is a minor win,
not a major one.

---

### 4. Stop downloading 4K to render 1080×1920

**Today** — `tasks.py:55`:

```python
"format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
```

On a 4K source that fetches ~4–8× more bytes than the 1080×1920 output can use, then
pays to decode them on every clip.

**Change:**

```python
"format": (
    "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
    "best[height<=1080][ext=mp4]/best[height<=1080]/best"
),
"concurrent_fragment_downloads": 8,
"noprogress": True,
```

Height cap via `settings.MAX_SOURCE_HEIGHT` (default 1080).

**Effect:** proportional cut in download bytes and in per-clip decode cost.
`concurrent_fragment_downloads` alone is a large win on YouTube's fragmented formats.

---

## Files touched

| File | Change |
|---|---|
| `backend/app/services/media_cache.py` | **new** — local source cache |
| `backend/app/services/transcription.py` | CUDA autodetect, WAV extraction, VAD, greedy |
| `backend/app/services/renderer.py` | NVENC + unified `_build_cmd()` |
| `backend/app/workers/tasks.py` | use cache, seed cache, yt-dlp format cap, stage timings |
| `backend/app/core/config.py` | new settings, all defaulting to current behaviour or `auto` |
| `backend/.env.example` | document the new vars |

Untouched: frontend, DB models, migrations, API routes, Celery routing.

---

## Verification

1. `[STAGE] <name> took Ns` log line added around download / transcribe / select / render /
   caption, so the before-after is measurable rather than felt.
2. Re-run one previously-processed YouTube URL end to end; compare stage timings.
3. Confirm the rendered mp4 still plays, is 1080×1920, and has burned-in captions.
4. `pytest` — `tests/test_api/test_health.py` must stay green.

**Status:** 1, 3 and 4 done — stage timings are in, ffprobe confirms 1080x1920 / h264 /
20.000000 s / aac from both encoders and an extracted frame shows captions burned in at the
correct clip-relative time, and `pytest` reports `2 passed`. Whisper CPU-vs-GPU is measured
above. Step 2 (a full end-to-end re-run of a real YouTube URL) is yours to do — it needs a
running worker, which is blocked on #1.

## Rollback

The repo is **not** under git, so there is no `git checkout` escape hatch. Every change is
therefore gated behind a setting that restores today's behaviour without editing code:

```
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
WHISPER_VAD=false
FFMPEG_ENCODER=libx264
MEDIA_CACHE_DIR=            # empty disables the cache, restores per-clip download
MAX_SOURCE_HEIGHT=0         # 0 disables the cap
```

## Expected result

Revised down from the first draft, on measurements rather than estimates. The honest
ranking of what this buys:

1. **Whisper actually running at all** (the cuBLAS fix) — on this machine that is the
   difference between working and raising `RuntimeError`, not a percentage.
2. **The source download cache** — a 5-clip job went from 6 full R2 downloads to 1. This
   scales with source size and clip count and is the largest *structural* win.
3. **Whisper on the GPU** — 7.8× on the transcription stage.
4. **The 1080p cap** — proportional to how much bigger than 1080p the source was.
5. **NVENC** — 1.5–1.7× per render, the smallest of the five.

What this does *not* fix: the YouTube download itself, and the 5 serial Gemini calls.
Those become the floor. I am deliberately not restating a wall-clock target for a full
job, because I have not measured one end to end — the stage timings added in Verification
will give you the real number on your next run.

## Known limits

- Consumer Ampere caps concurrent NVENC sessions (3 on older drivers). Sequential renders
  are unaffected; it matters only if renders are parallelised later.
- The 5 Gemini caption calls remain serial (~10–30 s total). Batching them into one
  request is a further win, deliberately left out of this pass.
- None of this addresses #1 (queue routing). If `transcribe_video_task` is on an
  unconsumed queue, it will still never run — it will simply never run *faster*.
