"""YouTube Data API v3 publishing service."""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.orm import object_session

from app.core.config import settings
from app.core.logging import logger
from app.core.security import decrypt_token, encrypt_token
from app.services.storage import storage_service


YOUTUBE_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Refresh this long before the stored expiry, so a token never dies mid-upload.
REFRESH_MARGIN = timedelta(minutes=2)

VALID_PRIVACY = ("private", "unlisted", "public")


class YouTubeAuthError(Exception):
    """The YouTube sign-in is missing, expired or revoked. Retrying cannot fix it: the user must reconnect."""


def _as_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class YouTubeService:
    # ─── Tokens ──────────────────────────────────────────────────────────────

    def _token_is_stale(self, user) -> bool:
        """True when the access token is known to be expired or about to be. Unknown expiry counts as fresh."""
        expiry = user.yt_token_expiry
        if expiry is None:
            return False
        return _as_aware(expiry) - REFRESH_MARGIN <= datetime.now(timezone.utc)

    def refresh_access_token(self, user) -> str:
        """
        Trade the refresh token for a new access token and save it on the user row.

        Saved with its own commit: the new token must survive even if the upload that
        follows fails and the caller rolls back.
        """
        if not user.yt_refresh_token:
            raise YouTubeAuthError("No refresh token is stored. Reconnect YouTube in Settings.")
        if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
            raise YouTubeAuthError(
                "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set where the worker runs (backend/.env)."
            )

        response = httpx.post(
            YOUTUBE_TOKEN_URL,
            data={
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "refresh_token": decrypt_token(user.yt_refresh_token),
                "grant_type": "refresh_token",
            },
            timeout=30.0,
        )
        if response.status_code != 200:
            try:
                error = response.json().get("error", "")
            except ValueError:
                error = ""
            if error == "invalid_grant":
                # Revoked, or a Testing-mode consent screen's 7-day limit.
                raise YouTubeAuthError(
                    "Google rejected the saved YouTube sign-in (expired or revoked). Reconnect YouTube in Settings."
                )
            raise RuntimeError(f"Token refresh failed: {response.status_code} {response.text[:300]}")

        data = response.json()
        access_token = data["access_token"]
        user.yt_access_token = encrypt_token(access_token)
        user.yt_token_expiry = datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in", 3600)))
        if data.get("refresh_token"):  # Google only sometimes rotates it
            user.yt_refresh_token = encrypt_token(data["refresh_token"])

        session = object_session(user)
        if session is not None:
            session.commit()
        logger.info("[YOUTUBE] Access token refreshed")
        return access_token

    def _get_valid_token(self, user) -> str:
        """The access token, refreshed first if it is known to be expired."""
        if self._token_is_stale(user):
            return self.refresh_access_token(user)
        return decrypt_token(user.yt_access_token)

    # ─── Upload ──────────────────────────────────────────────────────────────

    def _post_upload(self, access_token: str, metadata: dict, file_path: str) -> httpx.Response:
        with open(file_path, "rb") as video_file:
            return httpx.post(
                YOUTUBE_UPLOAD_URL,
                params={"uploadType": "multipart", "part": "snippet,status"},
                headers={"Authorization": f"Bearer {access_token}"},
                files={
                    "metadata": (None, json.dumps(metadata), "application/json"),
                    "video": ("clip.mp4", video_file, "video/mp4"),
                },
                timeout=300.0,
            )

    def upload(
        self, clip, user, privacy: str = "public", credit: str | None = None, publish_at: datetime | None = None
    ) -> dict[str, str]:
        """
        Upload a rendered clip to YouTube as a Short.
        Returns: {"post_id": str, "post_url": str}
        """
        if not user or not user.yt_access_token:
            raise YouTubeAuthError("YouTube account not connected.")
        if privacy not in VALID_PRIVACY:
            raise ValueError(f"Invalid privacy setting: {privacy!r}")

        access_token = self._get_valid_token(user)

        # Download clip from R2 to temp file
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            storage_service.download_to_file(clip.storage_key, tmp_path)

            title = clip.caption[:100] if clip.caption else "New Short"
            description = f"{clip.caption or ''}\n\n{clip.hashtags or ''}\n\n"
            if credit:  # a Creative Commons source must be credited in the post
                description += f"{credit}\n\n"
            description += "#Shorts"

            metadata: dict[str, Any] = {
                "snippet": {
                    "title": title,
                    "description": description,
                    "tags": ["Shorts"] + (clip.hashtags or "").split(),
                    "categoryId": "22",  # People & Blogs
                },
                "status": {
                    "privacyStatus": privacy,
                    "selfDeclaredMadeForKids": False,
                },
            }
            if publish_at is not None:
                # YouTube itself makes the video public at this moment. It must be uploaded as private for
                # that to work, and it only ever becomes public (never unlisted).
                metadata["status"]["privacyStatus"] = "private"
                metadata["status"]["publishAt"] = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

            response = self._post_upload(access_token, metadata, tmp_path)
            if response.status_code == 401:
                # The expiry was unknown (tokens saved before it was recorded) or the token was
                # revoked. Refresh once and try again; a second 401 is a real sign-in problem.
                logger.info("[YOUTUBE] 401 from upload, refreshing the token and retrying once")
                access_token = self.refresh_access_token(user)
                response = self._post_upload(access_token, metadata, tmp_path)
                if response.status_code == 401:
                    raise YouTubeAuthError("YouTube rejected the sign-in. Reconnect YouTube in Settings.")

            if response.status_code not in (200, 201):
                raise RuntimeError(f"YouTube upload failed: {response.status_code} {response.text[:500]}")

            video_id = response.json()["id"]
            post_url = f"https://www.youtube.com/shorts/{video_id}"
            logger.info(f"[YOUTUBE] Published ({privacy}{', scheduled' if publish_at else ''}): {post_url}")
            return {"post_id": video_id, "post_url": post_url}

        finally:
            os.unlink(tmp_path)


youtube_service = YouTubeService()
