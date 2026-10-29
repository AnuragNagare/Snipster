"""Pipeline orchestration — starts the processing chain for a submitted video."""

from app.workers.tasks import transcribe_video_task
from app.core.logging import logger


def start_processing_pipeline(video_id: str, from_url: bool = False) -> None:
    """
    Kick off the full processing pipeline for a video.

    If from_url=True, the pipeline will first download the video via yt-dlp
    before transcribing.
    """
    if from_url:
        # Import here to avoid circular imports
        from app.workers.tasks import download_video_task
        download_video_task.delay(video_id)
    else:
        transcribe_video_task.delay(video_id)

    logger.info(f"Pipeline started for video {video_id} (from_url={from_url})")
