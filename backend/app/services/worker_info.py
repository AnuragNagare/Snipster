"""What the worker can do, shared with the API through Redis.

The API runs on Vercel and the worker runs on your PC, so the API cannot look at the
GPU itself. The worker reports once at startup; the API reads it for the CPU/GPU badge.
"""

import json
import subprocess
import time

from app.core.config import settings

KEY = "snipster:worker_info"


def collect() -> dict:
    """Probe this machine. Only ever called inside the worker (imports Whisper)."""
    whisper_device = settings.WHISPER_DEVICE.lower()
    whisper_compute = settings.WHISPER_COMPUTE_TYPE.lower()

    if whisper_device == "auto" or whisper_compute == "auto":
        from app.services.transcription import _cuda_available

        cuda_ok = _cuda_available()
        if whisper_device == "auto":
            whisper_device = "cuda" if cuda_ok else "cpu"
        if whisper_compute == "auto":
            whisper_compute = "float16" if whisper_device == "cuda" else "int8"

    ffmpeg_encoder = settings.FFMPEG_ENCODER.lower()
    if ffmpeg_encoder == "auto":
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=5
            )
            ffmpeg_encoder = "h264_nvenc" if "h264_nvenc" in result.stdout else "libx264"
        except Exception:
            ffmpeg_encoder = "libx264"

    return {
        "whisper_device": whisper_device,           # "cuda" or "cpu"
        "whisper_compute_type": whisper_compute,    # "float16", "int8", ...
        "whisper_model": settings.WHISPER_MODEL,
        "ffmpeg_encoder": ffmpeg_encoder,           # "h264_nvenc" or "libx264"
        "using_gpu": whisper_device == "cuda",
        "reported_at": int(time.time()),
    }


def publish() -> None:
    import redis

    client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=5, socket_timeout=5)
    client.set(KEY, json.dumps(collect()))


async def read() -> dict | None:
    """The worker's last report, or None if it has never reported (or Redis is unreachable)."""
    import redis.asyncio as aioredis

    client = aioredis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=3, socket_timeout=3)
    try:
        raw = await client.get(KEY)
        return json.loads(raw) if raw else None
    except Exception:
        return None
    finally:
        await client.aclose()
