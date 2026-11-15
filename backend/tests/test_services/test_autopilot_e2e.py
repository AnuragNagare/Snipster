"""The whole autopilot loop in one test, on a real (file) SQLite database shared by worker code and the API.

Only the outside world is faked: the YouTube channel listing, the Celery queue and the pipeline stages
(download, transcribe, render), which are stood in for by marking the rows the way those tasks do.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_session
from app.db import models as m
from app.db.session import Base
from app.main import app
from app.services import alerts
from app.services import autopilot as ap
from app.workers import tasks

UTC = timezone.utc
DAY = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)


class Queue:
    def __init__(self):
        self.jobs = []

    def delay(self, arg):
        self.jobs.append(arg)


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "e2e.db"
    sync_engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(sync_engine)
    factory = sessionmaker(bind=sync_engine, expire_on_commit=False)
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async_factory = async_sessionmaker(async_engine, expire_on_commit=False)

    async def api_session():
        async with async_factory() as s:
            yield s
            await s.commit()

    app.dependency_overrides[get_session] = api_session
    monkeypatch.setattr(tasks, "_get_sync_session", factory)
    monkeypatch.setattr(alerts, "send", lambda *a, **k: True)
    monkeypatch.setattr(ap.settings, "YOUTUBE_API_KEY", "key")
    yield SimpleNamespace(factory=factory)
    app.dependency_overrides.clear()
    sync_engine.dispose()


def finish_pipeline(factory, video_id, clips):
    """What download -> transcribe -> select -> render -> caption leave behind in the database."""
    db = factory()
    video = db.get(m.SourceVideo, video_id)
    video.status, video.language = m.VideoStatus.READY, "hi"
    video.updated_at = DAY
    for start, end in clips:
        db.add(m.Clip(source_video_id=video.id, start_time=start, end_time=end, render_status=m.RenderStatus.DONE,
                      storage_key="clips/x/clip.mp4", caption="एक शानदार क्लिप", hashtags="#हिंदी"))
    db.commit()
    db.close()


async def test_a_video_goes_from_the_channel_to_a_rehearsed_post(world, monkeypatch):
    db = world.factory()
    db.add(m.User(yt_channel_id="UCabc123", yt_access_token="x"))
    row = ap.get_settings_row(db)
    row.enabled, row.auto_approve, row.dry_run = True, True, True
    row.clips_per_video, row.posts_per_day = 2, 3
    db.commit()

    channel = [
        {"id": "vidNEW", "title": "Newer talk", "published_at": "2025-06-01", "duration": 900.0, "live": False, "public": True},
        {"id": "vidOLD", "title": "Older talk", "published_at": "2024-01-01", "duration": 900.0, "live": False, "public": True},
        {"id": "vidSHORT", "title": "A Short", "published_at": "2023-01-01", "duration": 40.0, "live": False, "public": True},
    ]
    pipeline = []
    monkeypatch.setattr("app.workers.pipeline.start_processing_pipeline",
                        lambda video_id, from_url=False: pipeline.append((video_id, from_url)))
    publish = Queue()
    start = lambda d, u: ap.start_next_source(d, u, fetch=lambda ch: channel)

    # 1. 10:00, a run slot is due: the oldest eligible own video is picked (never the Short)
    first = ap.run_tick(db, publish=publish, now=DAY, start=start)
    assert first["picked"] == "vidOLD" and len(pipeline) == 1 and pipeline[0][1] is True

    # 2. the worker does its part
    source = db.query(m.AutopilotSource).one()
    finish_pipeline(world.factory, source.source_video_id, [(0, 42), (100, 140), (200, 216)])

    # 3. next tick: clips are approved, posts scheduled; nothing goes out before its time
    second = ap.run_tick(db, publish=publish, now=DAY + timedelta(minutes=2), start=start)
    db.expire_all()
    assert second["scheduled"] and publish.jobs == []
    jobs = db.query(m.PublishJob).order_by(m.PublishJob.scheduled_at).all()
    assert len(jobs) == 2 and all(j.autopilot and j.status == m.PublishStatus.SCHEDULED for j in jobs)
    assert db.query(m.AutopilotSource).one().status == "done"
    approved = db.query(m.Clip).filter(m.Clip.approval_status == m.ApprovalStatus.APPROVED).count()
    assert approved == 2

    # 4. later that day the first post is due: it is queued, and the worker "publishes" it as a rehearsal
    due = jobs[0].scheduled_at.replace(tzinfo=UTC) + timedelta(minutes=1)
    ap.run_tick(db, publish=publish, now=due, start=start)
    assert publish.jobs == [str(jobs[0].id)]
    assert tasks.publish_clip_task.apply(args=[publish.jobs[0]]).successful()

    # 5. the Activity feed tells the story, and a second video is not picked while the first is unfinished
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        feed = (await client.get("/api/activity")).json()
        status = (await client.get("/api/autopilot/settings")).json()

    rehearsed = [j for j in feed["jobs"] if j["status"] == "published"]
    waiting = [j for j in feed["jobs"] if j["status"] == "scheduled"]
    assert len(rehearsed) == 1 and rehearsed[0]["dry_run"] is True and rehearsed[0]["post_url"] is None
    assert rehearsed[0]["caption"] == "एक शानदार क्लिप" and rehearsed[0]["source_title"] == "Older talk"
    assert rehearsed[0]["language"] == "hi" and len(waiting) == 1
    assert feed["sources"][0]["status"] == "done" and feed["sources"][0]["title"] == "Older talk"
    assert status["enabled"] and status["posted_today"] == 1 and status["scheduled_count"] == 1
    assert status["seconds_since_tick"] is not None

    # 6. the kill switch holds the remaining post, and switching back on releases it
    db.expire_all()
    ap.get_settings_row(db).enabled = False
    db.commit()
    later = waiting and jobs[1].scheduled_at.replace(tzinfo=UTC) + timedelta(minutes=1)
    ap.run_tick(db, publish=publish, now=later, start=start)
    assert len(publish.jobs) == 1                      # still just the first
    ap.get_settings_row(db).enabled = True
    db.commit()
    ap.run_tick(db, publish=publish, now=later, start=start)
    assert publish.jobs[-1] == str(jobs[1].id)
    db.close()
