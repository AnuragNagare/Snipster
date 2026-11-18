"""A7: autopilot settings, run-now and the Activity feed, against a real (in-memory SQLite) database."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_session
from app.core.config import settings
from app.db import models as m
from app.db.session import Base
from app.main import app

NOW = datetime.now(timezone.utc)


@pytest.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def client(maker):
    async def session():
        async with maker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_session] = session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def seed_job(maker, *, status=m.PublishStatus.PUBLISHED, autopilot=True, post_id="abc",
                   error=None, caption="Hello", title="My talk", when=None):
    async with maker() as s:
        video = m.SourceVideo(ownership_confirmed=True, status=m.VideoStatus.READY, language="hi",
                              original_filename="file.mp4")
        s.add(video)
        await s.flush()
        clip = m.Clip(source_video_id=video.id, start_time=0, end_time=30, caption=caption)
        s.add(clip)
        await s.flush()
        if title:
            s.add(m.AutopilotSource(youtube_video_id=uuid.uuid4().hex[:11], title=title, status="done",
                                    source_video_id=video.id, picked_at=NOW))
        job = m.PublishJob(
            clip_id=clip.id, platform=m.Platform.YOUTUBE, status=status, autopilot=autopilot, retry_count=0,
            platform_post_id=post_id, platform_post_url=f"https://www.youtube.com/shorts/{post_id}" if post_id else None,
            published_at=when if status == m.PublishStatus.PUBLISHED else None,
            scheduled_at=when if status == m.PublishStatus.SCHEDULED else None,
            created_at=when or NOW, error_info=error,
        )
        s.add(job)
        await s.commit()
        return job.id


# ─── Settings ────────────────────────────────────────────────────────────────

async def test_first_read_creates_the_safe_defaults(client):
    r = await client.get("/api/autopilot/settings")
    d = r.json()
    assert r.status_code == 200
    assert (d["enabled"], d["dry_run"], d["auto_approve"], d["privacy"]) == (False, True, False, "private")
    assert (d["posts_per_day"], d["clips_per_video"], d["run_times"]) == (3, 2, "9,18")
    assert d["seconds_since_tick"] is None and d["posted_today"] == 0 and d["youtube_connected"] is False


async def test_settings_can_be_changed_and_are_kept(client):
    r = await client.patch("/api/autopilot/settings", json={
        "enabled": True, "dry_run": False, "posts_per_day": 2, "run_times": "18, 7", "timezone": "Asia/Kolkata",
        "privacy": "unlisted"})
    d = r.json()
    assert r.status_code == 200 and d["enabled"] is True and d["dry_run"] is False
    assert d["run_times"] == "7,18" and d["timezone"] == "Asia/Kolkata" and d["privacy"] == "unlisted"
    again = (await client.get("/api/autopilot/settings")).json()
    assert again["enabled"] is True and again["posts_per_day"] == 2


async def test_run_times_are_cleaned_up(client):
    d = (await client.patch("/api/autopilot/settings", json={"run_times": " 18 ,9, 9 "})).json()
    assert d["run_times"] == "9,18"


async def test_only_the_fields_sent_are_changed(client):
    await client.patch("/api/autopilot/settings", json={"posts_per_day": 5})
    d = (await client.patch("/api/autopilot/settings", json={"enabled": True})).json()
    assert d["posts_per_day"] == 5 and d["enabled"] is True


@pytest.mark.parametrize("body", [
    {"posts_per_day": 0}, {"posts_per_day": 11}, {"clips_per_video": 0}, {"clips_per_video": 6},
    {"privacy": "everyone"}, {"run_times": ""}, {"run_times": "25"}, {"run_times": "9,abc"},
    {"timezone": "Mars/Base"}, {"enabled": "maybe"},
])
async def test_invalid_settings_are_refused_and_change_nothing(client, body):
    r = await client.patch("/api/autopilot/settings", json=body)
    assert r.status_code == 422
    d = (await client.get("/api/autopilot/settings")).json()
    assert (d["posts_per_day"], d["clips_per_video"], d["privacy"], d["run_times"]) == (3, 2, "private", "9,18")


async def test_settings_report_the_scheduler_heartbeat_and_counts(client, maker):
    async with maker() as s:
        s.add(m.AutopilotSettings(id=1, last_tick_at=NOW - timedelta(minutes=7)))
        s.add(m.User(yt_access_token="x"))
        await s.commit()
    await seed_job(maker, status=m.PublishStatus.PUBLISHED, when=NOW)
    await seed_job(maker, status=m.PublishStatus.SCHEDULED, when=NOW + timedelta(hours=2))
    d = (await client.get("/api/autopilot/settings")).json()
    assert 400 <= d["seconds_since_tick"] <= 440 and d["tick_minutes"] == settings.AUTOPILOT_TICK_MINUTES
    assert d["posted_today"] == 1 and d["scheduled_count"] == 1 and d["youtube_connected"] is True


# ─── Run now ─────────────────────────────────────────────────────────────────

async def test_run_now_needs_autopilot_on(client, monkeypatch):
    calls = []
    monkeypatch.setattr("app.api.routes.autopilot.autopilot_tick_task", SimpleNamespace(delay=lambda **k: calls.append(k)))
    r = await client.post("/api/autopilot/run-now")
    assert r.status_code == 400 and calls == []


async def test_run_now_queues_a_forced_tick_after_saving(client, monkeypatch):
    order = []
    real_commit = AsyncSession.commit

    async def spy_commit(self):
        order.append("commit")
        await real_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", spy_commit)
    monkeypatch.setattr("app.api.routes.autopilot.autopilot_tick_task",
                        SimpleNamespace(delay=lambda **k: order.append(("enqueue", k))))
    await client.patch("/api/autopilot/settings", json={"enabled": True})
    order.clear()
    r = await client.post("/api/autopilot/run-now")
    assert r.status_code == 200
    # the worker reads the settings row, so it must be saved before the task is queued
    assert order[:2] == ["commit", ("enqueue", {"force": True})]


async def test_run_now_reports_a_dead_queue(client, monkeypatch):
    def delay(**kwargs):
        raise ConnectionError("redis")

    monkeypatch.setattr("app.api.routes.autopilot.autopilot_tick_task", SimpleNamespace(delay=delay))
    await client.patch("/api/autopilot/settings", json={"enabled": True})
    r = await client.post("/api/autopilot/run-now")
    assert r.status_code == 503


# ─── Activity ────────────────────────────────────────────────────────────────

async def test_activity_lists_posts_newest_first_with_their_details(client, maker):
    await seed_job(maker, when=NOW - timedelta(hours=3), post_id="old", caption="Old one")
    await seed_job(maker, when=NOW - timedelta(hours=1), post_id="new", caption="New one", title="Talk 2")
    d = (await client.get("/api/activity")).json()
    assert [j["caption"] for j in d["jobs"]] == ["New one", "Old one"]
    first = d["jobs"][0]
    assert first["post_url"].endswith("/new") and first["source_title"] == "Talk 2"
    assert first["language"] == "hi" and first["autopilot"] is True and first["dry_run"] is False
    assert len(d["sources"]) == 2 and d["sources"][0]["status"] == "done"


async def test_a_dry_run_is_labelled_and_has_no_link(client, maker):
    await seed_job(maker, post_id="dry-run")
    # seed_job gives every post_id a link; a real dry run has none
    async with maker() as s:
        job = (await s.execute(__import__("sqlalchemy").select(m.PublishJob))).scalars().one()
        job.platform_post_url = None
        await s.commit()
    j = (await client.get("/api/activity")).json()["jobs"][0]
    assert j["dry_run"] is True and j["post_url"] is None


async def test_failures_show_the_reason_and_whether_to_reconnect(client, maker):
    await seed_job(maker, status=m.PublishStatus.FAILED, post_id=None, when=NOW,
                   error=json.dumps({"error": "Reconnect YouTube in Settings.", "auth": True}))
    await seed_job(maker, status=m.PublishStatus.FAILED, post_id=None, when=NOW - timedelta(hours=1),
                   error=json.dumps({"error": "quota exceeded", "retry": 3}))
    await seed_job(maker, status=m.PublishStatus.FAILED, post_id=None, when=NOW - timedelta(hours=2), error="plain text")
    jobs = (await client.get("/api/activity")).json()["jobs"]
    assert jobs[0]["needs_reconnect"] is True and "Reconnect" in jobs[0]["error"]
    assert jobs[1]["needs_reconnect"] is False and jobs[1]["error"] == "quota exceeded"
    assert jobs[2]["error"] == "plain text"


async def test_scheduled_posts_appear_with_their_time(client, maker):
    when = NOW + timedelta(hours=5)
    await seed_job(maker, status=m.PublishStatus.SCHEDULED, post_id=None, when=when)
    j = (await client.get("/api/activity")).json()["jobs"][0]
    assert j["status"] == "scheduled" and j["scheduled_at"] is not None and j["post_url"] is None


async def test_manual_posts_show_the_file_name_and_are_not_marked_autopilot(client, maker):
    await seed_job(maker, autopilot=False, title=None, when=NOW)
    j = (await client.get("/api/activity")).json()["jobs"][0]
    assert j["autopilot"] is False and j["source_title"] == "file.mp4"


async def test_the_limit_is_respected_and_validated(client, maker):
    for i in range(3):
        await seed_job(maker, when=NOW - timedelta(hours=i), post_id=f"p{i}")
    assert len((await client.get("/api/activity?limit=2")).json()["jobs"]) == 2
    assert (await client.get("/api/activity?limit=0")).status_code == 422
    assert (await client.get("/api/activity?limit=500")).status_code == 422


async def test_an_empty_activity_feed_is_fine(client):
    assert (await client.get("/api/activity")).json() == {"jobs": [], "sources": []}


# ─── Login ───────────────────────────────────────────────────────────────────

async def test_every_autopilot_route_needs_the_login(client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "secret")
    monkeypatch.setattr(settings, "SECRET_KEY", "x" * 40)
    for method, path in [("GET", "/api/autopilot/settings"), ("PATCH", "/api/autopilot/settings"),
                         ("POST", "/api/autopilot/run-now"), ("GET", "/api/activity")]:
        r = await client.request(method, path, json={} if method == "PATCH" else None)
        assert r.status_code == 401, path


# ─── Source mode, language and post times ────────────────────────────────────

async def test_new_settings_start_with_own_videos_english_and_no_fixed_times(client):
    d = (await client.get("/api/autopilot/settings")).json()
    assert (d["source_mode"], d["source_language"], d["post_times"]) == ("own", "en", "")


async def test_source_mode_language_and_post_times_can_be_set(client):
    d = (await client.patch("/api/autopilot/settings", json={
        "source_mode": "creative_commons", "source_language": "hi", "post_times": " 19:45 , 12:15 ",
        "timezone": "Asia/Kolkata"})).json()
    assert d["source_mode"] == "creative_commons" and d["source_language"] == "hi"
    assert d["post_times"] == "12:15,19:45"                  # cleaned and sorted
    again = (await client.get("/api/autopilot/settings")).json()
    assert again["post_times"] == "12:15,19:45"


async def test_duplicate_post_times_are_merged(client):
    d = (await client.patch("/api/autopilot/settings", json={"post_times": "19:45,19:45"})).json()
    assert d["post_times"] == "19:45"


async def test_post_times_can_be_cleared(client):
    await client.patch("/api/autopilot/settings", json={"post_times": "19:45"})
    d = (await client.patch("/api/autopilot/settings", json={"post_times": ""})).json()
    assert d["post_times"] == ""


@pytest.mark.parametrize("body", [
    {"source_mode": "anyone"}, {"source_mode": "trending"}, {"source_language": "fr"},
    {"post_times": "25:00"}, {"post_times": "19:45,abc"}, {"post_times": "7pm"},
])
async def test_invalid_source_and_time_settings_are_refused(client, body):
    r = await client.patch("/api/autopilot/settings", json=body)
    assert r.status_code == 422
    d = (await client.get("/api/autopilot/settings")).json()
    assert (d["source_mode"], d["source_language"], d["post_times"]) == ("own", "en", "")


async def test_activity_shows_who_a_creative_commons_video_came_from(client, maker):
    async with maker() as s:
        s.add(m.AutopilotSource(youtube_video_id="ccvid", title="Big talk", status="processing",
                                channel_title="Great Creator", license="creativeCommon", views=120000, likes=4500,
                                picked_at=NOW))
        await s.commit()
    src = (await client.get("/api/activity")).json()["sources"][0]
    assert src["channel_title"] == "Great Creator" and src["license"] == "creativeCommon"
    assert (src["views"], src["likes"]) == (120000, 4500)


# ─── YouTube scheduling, auto retry, cloud heartbeat ─────────────────────────

async def test_the_new_settings_start_off_and_on(client):
    d = (await client.get("/api/autopilot/settings")).json()
    assert d["schedule_on_youtube"] is False and d["auto_retry"] is True and d["seconds_since_cloud_run"] is None


async def test_scheduling_and_retry_can_be_switched(client):
    d = (await client.patch("/api/autopilot/settings", json={"schedule_on_youtube": True, "auto_retry": False})).json()
    assert d["schedule_on_youtube"] is True and d["auto_retry"] is False
    again = (await client.get("/api/autopilot/settings")).json()
    assert again["schedule_on_youtube"] is True and again["auto_retry"] is False


async def test_the_settings_report_when_the_cloud_publisher_last_ran(client, maker):
    async with maker() as s:
        s.add(m.AutopilotSettings(id=1, last_cloud_run_at=NOW - timedelta(minutes=12)))
        await s.commit()
    d = (await client.get("/api/autopilot/settings")).json()
    assert 700 <= d["seconds_since_cloud_run"] <= 760


async def test_an_upload_with_a_publish_time_still_ahead_says_when_it_goes_public(client, maker):
    when = NOW + timedelta(hours=2)
    await seed_job(maker, status=m.PublishStatus.PUBLISHED, post_id="yt1", when=NOW)
    async with maker() as s:
        job = (await s.execute(__import__("sqlalchemy").select(m.PublishJob))).scalars().one()
        job.scheduled_at, job.retry_count = when, 2
        await s.commit()
    j = (await client.get("/api/activity")).json()["jobs"][0]
    assert j["goes_public_at"] is not None and j["retry_count"] == 2


async def test_no_goes_public_for_rehearsals_past_times_or_unposted(client, maker):
    await seed_job(maker, status=m.PublishStatus.PUBLISHED, post_id="dry-run", when=NOW)           # rehearsal
    await seed_job(maker, status=m.PublishStatus.PUBLISHED, post_id="old", when=NOW - timedelta(hours=1))  # time passed
    await seed_job(maker, status=m.PublishStatus.SCHEDULED, post_id=None, when=NOW + timedelta(hours=1))  # not uploaded
    assert all(j["goes_public_at"] is None for j in (await client.get("/api/activity")).json()["jobs"])
