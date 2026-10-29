"""Local disk cache for source videos.

Rendering a clip needs the whole source file, and one video fans out into
several render tasks. Without a cache every one of those tasks pulls the entire
source back out of R2 — six full downloads for a five-clip job. This keeps a
single copy on local disk for as long as the video is being processed.
"""

import os
import shutil
import tempfile
import time

from app.core.config import settings
from app.core.logging import logger

# A lock older than this belonged to a worker that died mid-download.
_LOCK_STALE_SECONDS = 900


def _cache_root() -> str | None:
    """Return the cache root, or None when caching is switched off."""
    configured = settings.MEDIA_CACHE_DIR.strip()
    if not configured:
        return None
    if configured == "auto":
        return os.path.join(tempfile.gettempdir(), "snipster-cache")
    return configured


def enabled() -> bool:
    return _cache_root() is not None


def _video_dir(video_id: str) -> str:
    return os.path.join(_cache_root(), video_id)


def _source_path(video_id: str) -> str:
    return os.path.join(_video_dir(video_id), "source.mp4")


def _acquire_lock(lock_path: str, timeout: int = 1800) -> None:
    """Block until this process owns the lock, reclaiming abandoned ones."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > _LOCK_STALE_SECONDS:
                    logger.warning(f"[CACHE] Reclaiming stale lock {lock_path}")
                    os.unlink(lock_path)
                    continue
            except FileNotFoundError:
                continue  # the holder released it while we looked
            if time.monotonic() > deadline:
                raise TimeoutError(f"Timed out waiting for cache lock {lock_path}")
            time.sleep(1)


def _release_lock(lock_path: str) -> None:
    try:
        os.unlink(lock_path)
    except FileNotFoundError:
        pass


def get_local_source(video_id: str, storage_key: str) -> str:
    """
    Return a local path to the source video, hitting storage only on a miss.

    When caching is disabled this downloads to a throwaway temp file and the
    caller owns it — matching the behaviour this cache replaced.
    """
    from app.services.storage import storage_service

    if not enabled():
        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        storage_service.download_to_file(storage_key, path)
        return path

    _sweep()
    path = _source_path(video_id)
    if os.path.exists(path):
        logger.info(f"[CACHE] Hit for {video_id}")
        return path

    os.makedirs(_video_dir(video_id), exist_ok=True)
    lock = path + ".lock"
    _acquire_lock(lock)
    try:
        if os.path.exists(path):
            logger.info(f"[CACHE] Hit for {video_id} (filled while waiting)")
            return path

        partial = path + ".part"
        logger.info(f"[CACHE] Miss for {video_id} — downloading {storage_key}")
        storage_service.download_to_file(storage_key, partial)
        # Atomic swap, so a concurrent reader never opens a half-written file.
        os.replace(partial, path)
        return path
    finally:
        _release_lock(lock)


def put_local_source(video_id: str, src_path: str) -> str | None:
    """Seed the cache with a file we already have — e.g. straight from yt-dlp."""
    if not enabled():
        return None

    os.makedirs(_video_dir(video_id), exist_ok=True)
    dest = _source_path(video_id)
    partial = dest + ".part"
    shutil.move(src_path, partial)
    os.replace(partial, dest)
    logger.info(f"[CACHE] Seeded {video_id} from {src_path}")
    return dest


def release(video_id: str) -> None:
    """Drop the cached copy once nothing needs it any more."""
    if not enabled():
        return
    shutil.rmtree(_video_dir(video_id), ignore_errors=True)
    logger.info(f"[CACHE] Released {video_id}")


def _sweep() -> None:
    """Delete entries left behind by jobs that failed before releasing."""
    root = _cache_root()
    if not root or not os.path.isdir(root):
        return

    cutoff = time.time() - settings.MEDIA_CACHE_MAX_AGE_HOURS * 3600
    for name in os.listdir(root):
        entry = os.path.join(root, name)
        try:
            if os.path.isdir(entry) and os.path.getmtime(entry) < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                logger.info(f"[CACHE] Swept stale entry {name}")
        except OSError:
            pass
