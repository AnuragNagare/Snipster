"""Posts that are due go out from a cloud job, so the laptop does not have to be on.

Run on a schedule by .github/workflows/publisher.yml (every 15 minutes):

    python -m app.cloud_publisher

It only PUBLISHES. It never picks videos, downloads from YouTube, transcribes or renders: that needs the
laptop. Posting is just an upload through Google's official API, which works from any network.

How it stays safe next to the laptop's own scheduler:
- A due post is claimed with one conditional UPDATE (SCHEDULED -> QUEUED), the same call the laptop uses.
  Whichever side claims it first posts it; the other finds nothing to claim. No double uploads.
- The kill switch, dry run, privacy and the credit line are applied by the same publish task the laptop
  runs, so behaviour is identical.
- Exit code 1 when a post failed, so GitHub shows a red run (and emails the owner).
"""

import sys
from datetime import datetime, timezone

from app.core.logging import logger, setup_logging


class InlinePublisher:
    """Stands in for `publish_clip_task.delay`: runs the publish right here instead of queueing it."""

    def __init__(self, task):
        self.task = task
        self.job_ids: list[str] = []

    def delay(self, job_id: str) -> None:
        self.job_ids.append(job_id)
        self.task.apply(args=[job_id])   # eager: runs now, in this process; failures are stored on the job


def run(now: datetime | None = None, task=None) -> dict:
    """Publish every due scheduled post. Returns a summary with one line per job (no captions, no secrets)."""
    from app.db.models import PublishJob, PublishStatus
    from app.services import autopilot
    from app.workers import tasks

    now = now or datetime.now(timezone.utc)
    db = tasks._get_sync_session()
    try:
        row = autopilot.get_settings_row(db)
        publisher = InlinePublisher(task or tasks.publish_clip_task)
        # Posts autopilot scheduled only go out while autopilot is on (the kill switch). Posts the user
        # scheduled by hand always go out.
        claimed = autopilot.dispatch_due_publish_jobs(db, now, publisher, include_autopilot=row.enabled)

        outcomes = []
        failed = 0
        for job_id in publisher.job_ids:
            db.expire_all()
            import uuid

            job = db.get(PublishJob, uuid.UUID(job_id))
            status = job.status.value if job is not None else "missing"
            note = " (rehearsal, nothing uploaded)" if job is not None and job.platform_post_id == "dry-run" else ""
            if job is None or job.status == PublishStatus.FAILED:
                failed += 1
            outcomes.append(f"job {job_id}: {status}{note}")
        return {"claimed": claimed, "failed": failed, "jobs": outcomes}
    finally:
        db.close()


def main() -> int:
    setup_logging()
    summary = run()
    if not summary["claimed"]:
        print("Nothing due.")
    for line in summary["jobs"]:
        print(line)
    if summary["failed"]:
        print(f"{summary['failed']} post(s) failed. See the Activity page; fix the cause, then press Retry.")
        return 1
    logger.info(f"[CLOUD PUBLISHER] done, {summary['claimed']} post(s) handled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
