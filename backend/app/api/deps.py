"""FastAPI dependencies shared across route handlers."""

from typing import AsyncGenerator

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import auth
from app.core.config import settings
from app.db.session import get_db


# Re-export get_db for use in route files
async def get_session(db: AsyncSession = Depends(get_db)) -> AsyncGenerator:
    yield db


def require_admin(authorization: str = Header(default="")) -> None:
    """
    Guard for every route that reads or changes your data or can post to your channel.
    Requires `Authorization: Bearer <token>` from POST /api/auth/session.

    With no ADMIN_PASSWORD this lets everything through on a local machine, but on
    Vercel it refuses everything: a deployed URL is public, so running it without a
    password would let anyone upload videos and publish to your channel.
    """
    if not auth.auth_required():
        if settings.is_vercel:
            raise HTTPException(
                status_code=503,
                detail="ADMIN_PASSWORD is not set. Add it (and SECRET_KEY) in the Vercel project settings, then redeploy.",
            )
        return
    token = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    if auth.verify(token, "session") is None:
        raise HTTPException(
            status_code=401, detail="Login required.", headers={"WWW-Authenticate": "Bearer"}
        )
