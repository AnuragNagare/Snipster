"""Public view, like and comment counts for videos, from the YouTube Data API (videos.list).

Uses the API key, so it only sees videos that are public or unlisted. A private video is simply missing
from YouTube's answer. That is not an error: the caller shows "no numbers yet" for it.

Cost: 1 quota unit per call of up to 50 videos. Results are kept for a few minutes, so reloading the
dashboard does not spend quota every time.
"""

import time

import httpx

from app.core.config import settings

YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
CACHE_SECONDS = 300
BATCH = 50

_cache: dict[str, tuple[float, dict]] = {}


class StatsUnavailable(Exception):
    """Numbers cannot be fetched right now (no API key, quota used up, YouTube down). The message is safe to show."""


def clear_cache() -> None:
    _cache.clear()


def fetch_video_stats(video_ids: list[str], *, clock=time.monotonic) -> dict[str, dict]:
    """
    {video_id: {"views": int, "likes": int | None, "comments": int | None}} for the videos YouTube returned.

    `likes` / `comments` are None when the owner hides them or turns comments off. Videos YouTube does
    not return (private, deleted) are left out of the result.
    """
    if not settings.YOUTUBE_API_KEY:
        raise StatsUnavailable("The YouTube API key is not set on the server (YOUTUBE_API_KEY).")

    unique = list(dict.fromkeys(v for v in video_ids if v))
    now = clock()
    result: dict[str, dict] = {}
    to_fetch = []
    for vid in unique:
        hit = _cache.get(vid)
        if hit and now - hit[0] < CACHE_SECONDS:
            result[vid] = hit[1]
        else:
            to_fetch.append(vid)

    for i in range(0, len(to_fetch), BATCH):
        batch = to_fetch[i:i + BATCH]
        try:
            response = httpx.get(
                f"{YOUTUBE_API}/videos",
                params={"part": "statistics", "id": ",".join(batch), "maxResults": BATCH, "key": settings.YOUTUBE_API_KEY},
                timeout=15.0,
            )
        except httpx.HTTPError:
            raise StatsUnavailable("Could not reach YouTube.")
        if response.status_code == 403:
            raise StatsUnavailable("YouTube refused the request (quota used up, or the API key is restricted).")
        if response.status_code != 200:
            raise StatsUnavailable(f"YouTube answered with an error ({response.status_code}).")

        for item in response.json().get("items", []):
            stats = item.get("statistics", {})
            entry = {
                "views": int(stats.get("viewCount", 0)),
                "likes": int(stats["likeCount"]) if "likeCount" in stats else None,
                "comments": int(stats["commentCount"]) if "commentCount" in stats else None,
            }
            _cache[item["id"]] = (now, entry)
            result[item["id"]] = entry
    return result
