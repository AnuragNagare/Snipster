"""Login, plus OAuth routes for Google (YouTube) and Meta (Instagram)."""

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, require_admin
from app.core import auth
from app.core.config import settings
from app.core.security import encrypt_token
from app.db.models import User
from app.db.schemas import ConnectedAccountsResponse
from app.core.logging import logger
import httpx

router = APIRouter(prefix="/auth", tags=["auth"])

# ─── Login ───────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str


@router.get("/config")
async def auth_config():
    """Open on purpose: lets the UI decide whether to show a login page."""
    return {
        "auth_required": auth.auth_required(),
        # Deployed without a password: every route refuses, and the UI should say so.
        "misconfigured": settings.is_vercel and not auth.auth_required(),
    }


@router.post("/session")
async def login(payload: LoginRequest):
    """Trade the password for a signed token the UI sends as `Authorization: Bearer`."""
    if not auth.auth_required():
        return {"token": "", "auth_required": False}
    if auth.locked_out():
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in 15 minutes.")
    if not auth.password_ok(payload.password):
        auth.record_failure()
        logger.warning("[AUTH] Failed login attempt")
        raise HTTPException(status_code=401, detail="Wrong password.")
    return {"token": auth.issue("session", auth.SESSION_TTL), "auth_required": True}


@router.get("/session", dependencies=[Depends(require_admin)])
async def check_session():
    """200 if the caller's token is valid (or no login is configured)."""
    return {"ok": True}


def _require_state(state: str, kind: str) -> None:
    """The callback must belong to a sign-in that started here, not a forged link."""
    if auth.verify(state, kind) is None:
        raise HTTPException(status_code=400, detail="Invalid or expired sign-in request. Start again from Settings.")


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _get_or_create_user(db: AsyncSession) -> User:
    """Single-user app: always return the first User row, creating if absent."""
    from sqlalchemy import select
    result = await db.execute(select(User).limit(1))
    user = result.scalars().first()
    if not user:
        user = User()
        db.add(user)
        await db.flush()
    return user


# ─── Google / YouTube ────────────────────────────────────────────────────────

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_CHANNEL_URL = "https://www.googleapis.com/youtube/v3/channels"


@router.post("/google/start", dependencies=[Depends(require_admin)])
async def google_start():
    """The Google consent-screen URL to send the browser to, bound to this login by `state`."""
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/userinfo.email",
        "access_type": "offline",
        "prompt": "consent",
        "state": auth.issue("oauth-google", auth.OAUTH_TTL),
    }
    return {"url": f"{GOOGLE_AUTH_URL}?{urlencode(params)}"}


@router.get("/google/callback")
async def google_callback(code: str, state: str = "", db: AsyncSession = Depends(get_session)):
    """Exchange Google auth code for access + refresh tokens."""
    _require_state(state, "oauth-google")
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
    if token_resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to exchange Google auth code.")

    tokens = token_resp.json()
    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token", "")

    # Fetch channel info
    async with httpx.AsyncClient() as client:
        channel_resp = await client.get(
            YOUTUBE_CHANNEL_URL,
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    channel_data = channel_resp.json().get("items", [{}])[0]
    channel_id = channel_data.get("id", "")
    channel_name = channel_data.get("snippet", {}).get("title", "")

    user = await _get_or_create_user(db)
    user.yt_access_token = encrypt_token(access_token)
    user.yt_refresh_token = encrypt_token(refresh_token) if refresh_token else None
    user.yt_channel_id = channel_id
    user.yt_channel_name = channel_name

    logger.info(f"YouTube connected: {channel_name} ({channel_id})")
    return RedirectResponse(url=f"{settings.FRONTEND_URL}/settings?yt=connected")


# ─── Meta / Instagram ────────────────────────────────────────────────────────

META_AUTH_URL = "https://www.facebook.com/v19.0/dialog/oauth"
META_TOKEN_URL = "https://graph.facebook.com/v19.0/oauth/access_token"
META_ME_URL = "https://graph.facebook.com/v19.0/me"


@router.post("/meta/start", dependencies=[Depends(require_admin)])
async def meta_start():
    """The Meta consent-screen URL to send the browser to, bound to this login by `state`."""
    params = {
        "client_id": settings.META_APP_ID,
        "redirect_uri": settings.META_REDIRECT_URI,
        "scope": "instagram_basic,instagram_content_publish,pages_read_engagement",
        "response_type": "code",
        "state": auth.issue("oauth-meta", auth.OAUTH_TTL),
    }
    return {"url": f"{META_AUTH_URL}?{urlencode(params)}"}


@router.get("/meta/callback")
async def meta_callback(code: str, state: str = "", db: AsyncSession = Depends(get_session)):
    """Exchange Meta auth code for long-lived access token."""
    _require_state(state, "oauth-meta")
    async with httpx.AsyncClient() as client:
        token_resp = await client.get(
            META_TOKEN_URL,
            params={
                "client_id": settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "redirect_uri": settings.META_REDIRECT_URI,
                "code": code,
            },
        )
    if token_resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to exchange Meta auth code.")

    access_token = token_resp.json()["access_token"]

    # Get Instagram user ID
    async with httpx.AsyncClient() as client:
        me_resp = await client.get(
            META_ME_URL,
            params={"fields": "id,name", "access_token": access_token},
        )
    me_data = me_resp.json()

    user = await _get_or_create_user(db)
    user.ig_access_token = encrypt_token(access_token)
    user.ig_user_id = me_data.get("id", "")
    user.ig_username = me_data.get("name", "")

    logger.info(f"Instagram connected: {user.ig_username}")
    return RedirectResponse(url=f"{settings.FRONTEND_URL}/settings?ig=connected")


# ─── Status ───────────────────────────────────────────────────────────────────

@router.get("/accounts", response_model=ConnectedAccountsResponse, dependencies=[Depends(require_admin)])
async def get_connected_accounts(db: AsyncSession = Depends(get_session)):
    """Return which platforms are currently connected."""
    user = await _get_or_create_user(db)
    return ConnectedAccountsResponse(
        youtube_connected=bool(user.yt_access_token),
        youtube_channel_name=user.yt_channel_name,
        instagram_connected=bool(user.ig_access_token),
        instagram_username=user.ig_username,
    )


@router.delete("/google/disconnect", dependencies=[Depends(require_admin)])
async def disconnect_youtube(db: AsyncSession = Depends(get_session)):
    user = await _get_or_create_user(db)
    user.yt_access_token = None
    user.yt_refresh_token = None
    user.yt_channel_id = None
    user.yt_channel_name = None
    return {"message": "YouTube disconnected."}


@router.delete("/meta/disconnect", dependencies=[Depends(require_admin)])
async def disconnect_instagram(db: AsyncSession = Depends(get_session)):
    user = await _get_or_create_user(db)
    user.ig_access_token = None
    user.ig_user_id = None
    user.ig_username = None
    return {"message": "Instagram disconnected."}
