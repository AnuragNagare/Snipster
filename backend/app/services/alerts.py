"""Tell the user when something needs them, and prove to an outside watcher that the worker is alive.

Two separate jobs:
- `send()`: a Telegram message when autopilot hits a problem. Nobody is watching a terminal.
- `ping_healthcheck()`: a request to a dead-man's-switch URL (for example healthchecks.io) after every
  healthy tick. If the PC is off, no ping arrives and THAT service notifies the user; the PC cannot
  report that it is itself down.

Neither function ever raises: a broken alert must not break the pipeline it is reporting on.
Messages must not contain secrets (tokens, keys).
"""

import time

import httpx

from app.core.config import settings
from app.core.logging import logger

TELEGRAM_API = "https://api.telegram.org"

# key -> monotonic time of the last send, for per-key cooldowns inside this process.
_last_sent: dict[str, float] = {}


def configured() -> bool:
    return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)


def send(text: str, key: str | None = None, cooldown: int = 0) -> bool:
    """
    Send `text` to the user's Telegram chat. Returns True if it was delivered.

    With a `key` and `cooldown` (seconds), the same key is not sent again until the cooldown has
    passed: a tick that fails every 5 minutes produces one message an hour, not twelve.
    """
    if key and cooldown:
        last = _last_sent.get(key)
        if last is not None and time.monotonic() - last < cooldown:
            return False

    if not configured():
        logger.warning(f"[ALERT] (Telegram not configured) {text}")
        return False

    try:
        response = httpx.post(
            f"{TELEGRAM_API}/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": f"Snipster: {text}"[:4000]},
            timeout=10.0,
        )
    except Exception as e:  # never include the URL: it contains the bot token
        logger.warning(f"[ALERT] Telegram send failed ({type(e).__name__})")
        return False

    if response.status_code != 200:
        logger.warning(f"[ALERT] Telegram answered {response.status_code}")
        return False

    if key:
        _last_sent[key] = time.monotonic()
    return True


def ping_healthcheck() -> bool:
    """Tell the dead-man's switch the worker is alive. A no-op when HEALTHCHECK_URL is not set."""
    return _ping(settings.HEALTHCHECK_URL)


def ping_cloud_healthcheck(fail: bool = False) -> bool:
    """
    Tell the cloud publisher's dead-man's switch it ran. With `fail`, report a failed run at once
    (healthchecks.io: the same address plus "/fail"). A no-op when HEALTHCHECK_CLOUD_URL is not set.
    """
    url = settings.HEALTHCHECK_CLOUD_URL.strip()
    if url and fail:
        url = url.rstrip("/") + "/fail"
    return _ping(url)


def _ping(url: str) -> bool:
    url = (url or "").strip()
    if not url:
        return False
    try:
        return httpx.get(url, timeout=10.0).status_code < 400
    except Exception as e:
        logger.warning(f"[ALERT] Health-check ping failed ({type(e).__name__})")
        return False
