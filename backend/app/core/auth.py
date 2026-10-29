"""Single-user login: a password check that hands out signed, expiring tokens.

No database and no extra dependency — a token is `base64(payload).base64(HMAC)`
signed with SECRET_KEY. The same signing is used for the short-lived OAuth
`state` value, which ties a Google/Meta callback to a login that really started here.
"""

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Optional

from app.core.config import settings

SESSION_TTL = 30 * 24 * 3600   # a login lasts 30 days
OAUTH_TTL = 10 * 60            # an OAuth round-trip must finish within 10 minutes

# Failed password attempts allowed per window, across all clients. This is a
# one-person app, so a global limit is simpler than per-IP and can't be dodged
# by rotating addresses. The cost is that an attacker can lock you out for a while.
MAX_FAILURES = 10
FAILURE_WINDOW = 15 * 60

_failures: list[float] = []
_lock = threading.Lock()


def auth_required() -> bool:
    return bool(settings.ADMIN_PASSWORD)


def check_secret_key() -> None:
    """Refuse to run with a guessable signing key once a password is protecting the API."""
    if auth_required() and (len(settings.SECRET_KEY) < 32 or settings.SECRET_KEY.startswith("change-me")):
        raise RuntimeError(
            "ADMIN_PASSWORD is set but SECRET_KEY is the placeholder or too short, so login tokens "
            "could be forged. Set SECRET_KEY in backend/.env to a random value, e.g.\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str) -> str:
    return _b64(hmac.new(settings.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).digest())


def issue(kind: str, ttl: int, **claims) -> str:
    """A signed token of type `kind` that expires in `ttl` seconds."""
    payload = _b64(json.dumps({"k": kind, "exp": int(time.time()) + ttl, **claims}).encode())
    return f"{payload}.{_sign(payload)}"


def verify(token: Optional[str], kind: str) -> Optional[dict]:
    """The token's claims if it is genuine, unexpired and of the right kind; else None."""
    if not token or token.count(".") != 1:
        return None
    payload, signature = token.split(".")
    if not hmac.compare_digest(signature, _sign(payload)):
        return None
    try:
        claims = json.loads(_unb64(payload))
    except (ValueError, UnicodeDecodeError):
        return None
    if claims.get("k") != kind or claims.get("exp", 0) < time.time():
        return None
    return claims


def password_ok(candidate: str) -> bool:
    return bool(settings.ADMIN_PASSWORD) and secrets.compare_digest(
        candidate.encode(), settings.ADMIN_PASSWORD.encode()
    )


def locked_out() -> bool:
    now = time.time()
    with _lock:
        _failures[:] = [t for t in _failures if now - t < FAILURE_WINDOW]
        return len(_failures) >= MAX_FAILURES


def record_failure() -> None:
    with _lock:
        _failures.append(time.time())


def reset_failures() -> None:
    with _lock:
        _failures.clear()
