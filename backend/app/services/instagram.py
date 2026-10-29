"""Instagram Graph API publishing service."""

import time

import httpx

from app.core.config import settings
from app.core.security import decrypt_token
from app.core.logging import logger
from app.services.storage import storage_service

GRAPH_URL = "https://graph.facebook.com/v19.0"


class InstagramService:
    def publish(self, clip, user) -> dict[str, str]:
        """
        Publish a rendered clip to Instagram as a Reel.
        Instagram requires the video to be at a PUBLIC URL.

        Flow:
        1. Get public URL for the clip in R2
        2. Create media container
        3. Poll until container status = FINISHED
        4. Publish the container

        Returns: {"post_id": str, "post_url": str}
        """
        if not user.ig_access_token:
            raise ValueError("Instagram account not connected.")

        access_token = decrypt_token(user.ig_access_token)
        ig_user_id = user.ig_user_id

        # Step 1: Get public URL (R2 bucket must have public access enabled)
        video_url = storage_service.get_public_url(clip.storage_key)

        caption = f"{clip.caption or ''}\n\n{clip.hashtags or ''}"

        # Step 2: Create media container
        logger.info(f"[INSTAGRAM] Creating media container for clip {clip.id}")
        container_resp = httpx.post(
            f"{GRAPH_URL}/{ig_user_id}/media",
            params={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "share_to_feed": "true",
                "access_token": access_token,
            },
            timeout=30.0,
        )

        if container_resp.status_code != 200:
            raise RuntimeError(
                f"Instagram container creation failed: {container_resp.status_code} {container_resp.text[:300]}"
            )

        container_id = container_resp.json()["id"]
        logger.info(f"[INSTAGRAM] Container created: {container_id}")

        # Step 3: Poll until FINISHED (can take 1–5 minutes)
        self._wait_for_container(container_id, access_token)

        # Step 4: Publish
        publish_resp = httpx.post(
            f"{GRAPH_URL}/{ig_user_id}/media_publish",
            params={
                "creation_id": container_id,
                "access_token": access_token,
            },
            timeout=30.0,
        )

        if publish_resp.status_code != 200:
            raise RuntimeError(
                f"Instagram publish failed: {publish_resp.status_code} {publish_resp.text[:300]}"
            )

        post_id = publish_resp.json()["id"]
        post_url = f"https://www.instagram.com/reel/{post_id}/"
        logger.info(f"[INSTAGRAM] Published: {post_url}")
        return {"post_id": post_id, "post_url": post_url}

    def _wait_for_container(self, container_id: str, access_token: str, max_wait: int = 300) -> None:
        """Poll the container status until FINISHED or timeout."""
        elapsed = 0
        poll_interval = 10

        while elapsed < max_wait:
            resp = httpx.get(
                f"{GRAPH_URL}/{container_id}",
                params={"fields": "status_code", "access_token": access_token},
                timeout=15.0,
            )
            status = resp.json().get("status_code", "")
            logger.info(f"[INSTAGRAM] Container {container_id} status: {status}")

            if status == "FINISHED":
                return
            elif status == "ERROR":
                raise RuntimeError(f"Instagram container processing failed: {resp.json()}")

            time.sleep(poll_interval)
            elapsed += poll_interval

        raise TimeoutError(f"Instagram container {container_id} did not finish within {max_wait}s")


instagram_service = InstagramService()
