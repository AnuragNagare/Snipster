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
    ) -> None:
        """
        Render a clip from a source video:
        1. Trim to [start, end]
        2. Center-crop to 9:16 (1080x1920)
        3. Burn in captions from transcript (if provided)
        """
        duration = end - start
        logger.info(f"[RENDER] Rendering {start:.1f}s–{end:.1f}s ({duration:.1f}s)")

        srt_path = None
        if transcript:
            srt_path = output_path.replace(".mp4", ".srt")
            self._write_srt(transcript, start, end, srt_path)

        try:
            encoder = _encoder()
            try:
                self._run(self._build_cmd(source_path, output_path, start, end, srt_path, encoder))
            except RuntimeError:
                if encoder != "h264_nvenc":
                    raise
                # A build can advertise NVENC on a machine whose GPU refuses the
                # session (driver mismatch, encoder sessions exhausted, ...).
                logger.warning("[RENDER] NVENC failed — retrying with libx264.")
                _disable_nvenc()
                self._run(self._build_cmd(source_path, output_path, start, end, srt_path, "libx264"))
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
    ) -> list[str]:
        vf = _CROP_9_16
        if srt_path:
            # ffmpeg's filter parser needs the Windows drive colon escaped.
            srt_escaped = srt_path.replace("\\", "/").replace(":", "\\:")
            vf += f",subtitles='{srt_escaped}':force_style='{_SUBTITLE_STYLE}'"

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
        """Write an SRT subtitle file from word-level transcript, offset to clip start."""
        # Filter words within this clip's range
        words = [w for w in transcript if clip_start <= w["start"] <= clip_end]

        # Group words into ~4-word subtitle lines
        lines = []
        chunk = []
        for w in words:
            chunk.append(w)
            if len(chunk) >= 4:
                lines.append(chunk)
                chunk = []
        if chunk:
            lines.append(chunk)

        def fmt_time(seconds: float) -> str:
            seconds -= clip_start  # offset to clip-relative time
            seconds = max(0, seconds)
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            s = int(seconds % 60)
            ms = int((seconds % 1) * 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        with open(srt_path, "w", encoding="utf-8") as f:
            for i, group in enumerate(lines, 1):
                line_start = group[0]["start"]
                line_end = group[-1]["end"]
                text = " ".join(w["text"] for w in group)
                f.write(f"{i}\n{fmt_time(line_start)} --> {fmt_time(line_end)}\n{text}\n\n")

    def _run(self, cmd: list[str]) -> None:
        logger.debug(f"[RENDER] Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"[RENDER] ffmpeg error:\n{result.stderr}")
            raise RuntimeError(f"ffmpeg failed: {result.stderr[-500:]}")


renderer_service = RendererService()
