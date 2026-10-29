"""Whisper transcription service using faster-whisper (GPU when one is available)."""

import ctypes
import os
import subprocess
import tempfile
from typing import Any

from faster_whisper import WhisperModel

from app.core.config import settings
from app.core.logging import logger


def _register_cuda_dlls() -> None:
    """
    Put the pip-installed CUDA runtime DLLs / shared objects on the search path.
    Works for both Windows (bin/*.dll) and Linux (lib/*.so).
    """
    try:
        import nvidia
    except ImportError:
        return

    if hasattr(os, "add_dll_directory"):
        bin_dirs = []
        for pkg_root in nvidia.__path__:
            if not os.path.isdir(pkg_root):
                continue
            for name in os.listdir(pkg_root):
                bin_dir = os.path.join(pkg_root, name, "bin")
                if os.path.isdir(bin_dir):
                    bin_dirs.append(bin_dir)
                    try:
                        os.add_dll_directory(bin_dir)
                    except OSError:
                        pass

        for dll_name in ("cublasLt64_12.dll", "cublas64_12.dll"):
            for bin_dir in bin_dirs:
                path = os.path.join(bin_dir, dll_name)
                if os.path.exists(path):
                    try:
                        ctypes.CDLL(path)
                    except OSError as e:
                        logger.warning(f"[WHISPER] Could not preload {dll_name}: {e}")
                    break
    else:
        # Linux: Preload .so libraries from pip packages
        for pkg_root in nvidia.__path__:
            if not os.path.isdir(pkg_root):
                continue
            for name in os.listdir(pkg_root):
                lib_dir = os.path.join(pkg_root, name, "lib")
                if os.path.isdir(lib_dir):
                    for fname in os.listdir(lib_dir):
                        if (fname.startswith("libcublas") or fname.startswith("libcudnn")) and (
                            ".so" in fname
                        ):
                            try:
                                ctypes.CDLL(os.path.join(lib_dir, fname))
                            except OSError:
                                pass


def _cuda_available() -> bool:
    try:
        _register_cuda_dlls()
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception as e:
        logger.warning(f"[WHISPER] CUDA probe failed, falling back to CPU: {e}")
        return False
        logger.warning(f"[WHISPER] CUDA probe failed, falling back to CPU: {e}")
        return False


def _resolve_device() -> tuple[str, str]:
    """Pick (device, compute_type) — explicit settings win over autodetection."""
    device = settings.WHISPER_DEVICE.lower()
    compute_type = settings.WHISPER_COMPUTE_TYPE.lower()

    if device == "auto":
        device = "cuda" if _cuda_available() else "cpu"
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"
    return device, compute_type


def _smoke_test(model: WhisperModel) -> None:
    """
    Force one real encode on the GPU.

    Constructing a WhisperModel on CUDA succeeds even when cuBLAS is missing —
    the failure only surfaces on the first encode. Without this, a broken GPU
    setup would blow up mid-transcription instead of falling back to CPU.
    """
    import numpy as np

    silence = np.zeros(16000, dtype=np.float32)  # 1 second
    segments, _ = model.transcribe(silence, beam_size=1)
    for _ in segments:
        break


class TranscriptionService:
    _model: WhisperModel | None = None

    def _get_model(self) -> WhisperModel:
        if self._model is None:
            _register_cuda_dlls()
            device, compute_type = _resolve_device()
            logger.info(
                f"[WHISPER] Loading {settings.WHISPER_MODEL} on {device} "
                f"({compute_type}) — first run may take a moment..."
            )
            try:
                model = WhisperModel(
                    settings.WHISPER_MODEL, device=device, compute_type=compute_type
                )
                if device == "cuda":
                    _smoke_test(model)
                self._model = model
            except Exception as e:
                if device == "cpu":
                    raise
                # A visible GPU is not a working one — missing cuBLAS/cuDNN,
                # bad driver, no free VRAM.
                logger.warning(f"[WHISPER] GPU unusable ({e}); falling back to CPU int8.")
                self._model = WhisperModel(
                    settings.WHISPER_MODEL, device="cpu", compute_type="int8"
                )
        return self._model

    def _extract_audio(self, media_path: str) -> str | None:
        """
        Decode to 16 kHz mono WAV up front.

        Whisper only ever looks at audio, so handing it the full H.264 mp4 makes
        it drag the video stream through the decoder for nothing.
        Returns None if extraction fails, so the caller can use the original.
        """
        fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", media_path,
            "-vn",                    # drop video
            "-ac", "1",               # mono
            "-ar", "16000",           # the rate Whisper resamples to anyway
            "-c:a", "pcm_s16le",
            wav_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.warning(
                f"[WHISPER] Audio extraction failed, using source directly: "
                f"{result.stderr[-300:]}"
            )
            os.unlink(wav_path)
            return None
        return wav_path

    def transcribe(self, audio_path: str) -> list[dict[str, Any]]:
        """
        Transcribe an audio/video file.

        Returns a list of word-level segments:
        [{"start": 0.0, "end": 1.2, "text": "Hello"}, ...]
        """
        model = self._get_model()
        logger.info(f"[WHISPER] Transcribing {audio_path}")

        wav_path = self._extract_audio(audio_path)
        target = wav_path or audio_path

        try:
            kwargs: dict[str, Any] = {
                # _write_srt in the renderer needs word-level timings.
                "word_timestamps": True,
                "beam_size": settings.WHISPER_BEAM_SIZE,
                # Long recordings otherwise drift into repetition loops.
                "condition_on_previous_text": False,
            }
            if settings.WHISPER_VAD:
                kwargs["vad_filter"] = True
                kwargs["vad_parameters"] = {"min_silence_duration_ms": 500}

            segments, info = model.transcribe(target, **kwargs)
            logger.info(
                f"[WHISPER] Detected language: {info.language} "
                f"({info.language_probability:.2%})"
            )

            # segments is a generator — it must be drained before the finally
            # block deletes the wav underneath it.
            words = []
            for segment in segments:
                if segment.words:
                    for word in segment.words:
                        words.append({
                            "start": round(word.start, 3),
                            "end": round(word.end, 3),
                            "text": word.word.strip(),
                        })

            logger.info(f"[WHISPER] Transcribed {len(words)} words")
            return words
        finally:
            if wav_path:
                os.unlink(wav_path)


transcription_service = TranscriptionService()
