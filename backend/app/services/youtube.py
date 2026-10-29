"""YouTube Data API v3 publishing service."""

import os
import tempfile
from typing import Any

import httpx

from app.core.config import settings
from app.core.security import decrypt_token
from app.core.logging import logger
from app.services.storage import storage_service


YOUTUBE_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class YouTubeService:
    def _get_valid_token(self, user) -> str:
        """Decrypt and return the access token, refreshing if needed."""
        access_token = decrypt_token(user.yt_access_token)
        # TODO: check expiry and refresh if needed
        return access_token

    def upload(self, clip, user) -> dict[str, str]:
        """
        Upload a rendered clip to YouTube as a Short.
        Returns: {"post_id": str, "post_url": str}
        """
        if not user.yt_access_token:
            raise ValueError("YouTube account not connected.")

        access_token = self._get_valid_token(user)

        # Download clip from R2 to temp file
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            storage_service.download_to_file(clip.storage_key, tmp_path)

            title = clip.caption[:100] if clip.caption else "New Short"
            description = f"{clip.caption or ''}\n\n{clip.hashtags or ''}\n\n#Shorts"

            metadata = {
                "snippet": {
                    "title": title,
                    "description": description,
                    "tags": ["Shorts"] + (clip.hashtags or "").split(),
                    "categoryId": "22",  # People & Blogs
                },
                "status": {
                    "privacyStatus": "public",
                    "selfDeclaredMadeForKids": False,
                },
            }

            with open(tmp_path, "rb") as video_file:
                import json
                response = httpx.post(
                    YOUTUBE_UPLOAD_URL,
                    params={"uploadType": "multipart", "part": "snippet,status"},
                    headers={"Authorization": f"Bearer {access_token}"},
                    files={
                        "metadata": (None, json.dumps(metadata), "application/json"),
                        "video": ("clip.mp4", video_file, "video/mp4"),
                    },
                    timeout=300.0,
                )

            if response.status_code not in (200, 201):
                raise RuntimeError(f"YouTube upload failed: {response.status_code} {response.text[:500]}")

            video_id = response.json()["id"]
            post_url = f"https://www.youtube.com/shorts/{video_id}"
            logger.info(f"[YOUTUBE] Published: {post_url}")
            return {"post_id": video_id, "post_url": post_url}

        finally:
            os.unlink(tmp_path)


youtube_service = YouTubeService()
