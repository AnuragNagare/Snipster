"""ffmpeg render service — trim, 9:16 crop, and caption burn-in."""

import os
import subprocess
from typing import Any

from app.core.config import settings
from app.core.logging import logger

# Scale until the frame covers 1080x1920, then take the centre of it.
_CROP_9_16 = (
    "scale=iw*max(1080/iw\\,1920/ih):ih*max(1080/iw\\,1920/ih),"
    "crop=1080:1920"
)

_SUBTITLE_STYLE = (
    "FontSize=18,PrimaryColour=&Hffffff,OutlineColour=&H000000,Outline=2,Alignment=2"
)

# Languages whose script the default subtitle font cannot draw (it shows empty boxes).
_DEVANAGARI_LANGUAGES = {"hi", "mr", "ne", "sa"}


def subtitle_style(language: str | None) -> str:
    """The libass style string; adds a Devanagari-capable font for Hindi and related languages."""
    if language and language.lower() in _DEVANAGARI_LANGUAGES and settings.SUBTITLE_FONT_DEVANAGARI.strip():
        return f"FontName={settings.SUBTITLE_FONT_DEVANAGARI.strip()},{_SUBTITLE_STYLE}"
    return _SUBTITLE_STYLE


def default_subtitle_lines(transcript: list[dict], clip_start: float, clip_end: float) -> list[dict]:
    """Subtitle lines for a clip built from the word-level transcript: ~4 words per line,
    times relative to the clip start."""
    words = [w for w in transcript if clip_start <= w["start"] <= clip_end]
    lines: list[dict] = []
    for i in range(0, len(words), 4):
        group = words[i:i + 4]
        lines.append({
            "start": round(max(0, group[0]["start"] - clip_start), 3),
            "end": round(max(0, group[-1]["end"] - clip_start), 3),
            "text": " ".join(w["text"] for w in group),
        })
    return lines


# Resolved once per worker process.
_ENCODER: str | None = None


def _probe_encoder() -> str:
    configured = settings.FFMPEG_ENCODER.lower()
    if configured != "auto":
        logger.info(f"[RENDER] Using configured encoder: {configured}")
        return configured

    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True
        )
        if "h264_nvenc" in result.stdout:
            logger.info("[RENDER] Using NVENC hardware encoder.")
            return "h264_nvenc"
    except Exception as e:
        logger.warning(f"[RENDER] Encoder probe failed: {e}")

    logger.info("[RENDER] Using libx264 software encoder.")
    return "libx264"


def _encoder() -> str:
    global _ENCODER
    if _ENCODER is None:
        _ENCODER = _probe_encoder()
    return _ENCODER


def _disable_nvenc() -> None:
    """Stop trying NVENC for the rest of this process."""
    global _ENCODER
    _ENCODER = "libx264"


class RendererService:
    def render(
        self,
        source_path: str,
        output_path: str,
        start: float,
        end: float,
        transcript: list[dict[str, Any]] | None = None,
        language: str | None = None,
        subtitles: list[dict[str, Any]] | None = None,
    ) -> None:
        """
        Render a clip from a source video:
        1. Trim to [start, end]
        2. Center-crop to 9:16 (1080x1920)
        3. Burn in subtitles: the user's edited `subtitles` if given (an empty list means none),
           otherwise lines built from the transcript (if provided)
        """
        duration = end - start
        logger.info(f"[RENDER] Rendering {start:.1f}s–{end:.1f}s ({duration:.1f}s)")

        srt_path = None
        if subtitles is not None:
            if subtitles:
                srt_path = output_path.replace(".mp4", ".srt")
                self._write_srt_lines(subtitles, srt_path)
        elif transcript:
            srt_path = output_path.replace(".mp4", ".srt")
            self._write_srt(transcript, start, end, srt_path)

        try:
            encoder = _encoder()
            try:
                self._run(self._build_cmd(source_path, output_path, start, end, srt_path, encoder, language))
            except RuntimeError:
                if encoder != "h264_nvenc":
                    raise
                # A build can advertise NVENC on a machine whose GPU refuses the
                # session (driver mismatch, encoder sessions exhausted, ...).
                logger.warning("[RENDER] NVENC failed — retrying with libx264.")
                _disable_nvenc()
                self._run(self._build_cmd(source_path, output_path, start, end, srt_path, "libx264", language))
        finally:
            if srt_path and os.path.exists(srt_path):
                os.unlink(srt_path)

        logger.info(f"[RENDER] Done → {output_path}")

    def _build_cmd(
        self,
        source: str,
        output: str,
        start: float,
        end: float,
        srt_path: str | None,
        encoder: str,
        language: str | None = None,
    ) -> list[str]:
        vf = _CROP_9_16
        if srt_path:
            # ffmpeg's filter parser needs the Windows drive colon escaped.
            srt_escaped = srt_path.replace("\\", "/").replace(":", "\\:")
            vf += f",subtitles='{srt_escaped}':force_style='{subtitle_style(language)}'"

        cmd = ["ffmpeg", "-y", "-loglevel", "error"]

        if encoder == "h264_nvenc":
            # Decode on the GPU too. Deliberately no -hwaccel_output_format:
            # subtitles/libass is a CPU filter, so frames have to land back in
            # system memory anyway, and letting ffmpeg do that copy implicitly
            # keeps the filter chain identical to the software path.
            cmd += ["-hwaccel", "cuda"]

        # -ss before -i is an input seek: ffmpeg jumps to the keyframe instead
        # of decoding the file from 0.
        cmd += ["-ss", str(start), "-i", source, "-t", str(end - start), "-vf", vf]

        if encoder == "h264_nvenc":
            cmd += [
                "-c:v", "h264_nvenc",
                "-preset", "p4",       # p1 fastest … p7 slowest
                "-tune", "hq",
                "-rc", "vbr",
                "-cq", "23",           # quality target, mirrors libx264 -crf 23
                "-b:v", "0",           # pure constant-quality VBR
                "-pix_fmt", "yuv420p",
            ]
        else:
            cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]

        cmd += ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", output]
        return cmd

    def _write_srt(
        self,
        transcript: list[dict],
        clip_start: float,
        clip_end: float,
        srt_path: str,
    ) -> None:
        """Write an SRT subtitle file from the word-level transcript, offset to clip start."""
        self._write_srt_lines(default_subtitle_lines(transcript, clip_start, clip_end), srt_path)

    def _write_srt_lines(self, lines: list[dict], srt_path: str) -> None:
        """Write an SRT file from {"start", "end", "text"} lines (times relative to the clip)."""

        def fmt_time(seconds: float) -> str:
            seconds = max(0, seconds)
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            s = int(seconds % 60)
            ms = int((seconds % 1) * 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        with open(srt_path, "w", encoding="utf-8") as f:
            for i, line in enumerate(lines, 1):
                start, end = fmt_time(line["start"]), fmt_time(line["end"])
                f.write(f"{i}\n{start} --> {end}\n{line['text']}\n\n")

    def _run(self, cmd: list[str]) -> None:
        logger.debug(f"[RENDER] Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"[RENDER] ffmpeg error:\n{result.stderr}")
            raise RuntimeError(f"ffmpeg failed: {result.stderr[-500:]}")


renderer_service = RendererService()
