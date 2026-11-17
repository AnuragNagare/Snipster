"""GET /api/dashboard: real posts with their numbers, rehearsals left out, failures tolerated."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_session
from app.core.config import settings
from app.db import models as m
from app.db.session import Base
from app.main import app
from app.services.youtube_stats import StatsUnavailable

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
            yield s
            await s.commit()

    app.dependency_overrides[get_session] = session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def stats(monkeypatch):
    """Replace the YouTube call. Set `stats.data` (or `stats.error`) in the test."""
    box = type("Box", (), {"data": {}, "error": None, "asked": []})()

    def fake(ids):
        box.asked.append(list(ids))
        if box.error:
            raise box.error
        return {k: v for k, v in box.data.items() if k in ids}

    monkeypatch.setattr("app.api.routes.dashboard.fetch_video_stats", fake)
    return box


async def post(maker, video_id, *, hours_ago=1, status=m.PublishStatus.PUBLISHED, caption="A caption",
               title=None, autopilot=True, url="auto"):
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
        s.add(m.PublishJob(
            clip_id=clip.id, platform=m.Platform.YOUTUBE, status=status, autopilot=autopilot, retry_count=0,
            platform_post_id=video_id,
            platform_post_url=(f"https://www.youtube.com/shorts/{video_id}" if url == "auto" else url),
            published_at=NOW - timedelta(hours=hours_ago), created_at=NOW - timedelta(hours=hours_ago),
        ))
        await s.commit()


def entry(views, likes=0, comments=0):
    return {"views": views, "likes": likes, "comments": comments}


async def test_posts_show_their_numbers_and_totals_add_up(client, maker, stats):
    await post(maker, "vidA", hours_ago=2, caption="First", title="Talk A")
    await post(maker, "vidB", hours_ago=1, caption="Second")
    stats.data = {"vidA": entry(1000, 50, 5), "vidB": entry(234, 10, 1)}
    d = (await client.get("/api/dashboard")).json()
    assert [p["caption"] for p in d["posts"]] == ["Second", "First"]            # newest first
    first = d["posts"][1]
    assert (first["views"], first["likes"], first["comments"]) == (1000, 50, 5)
    assert first["source_title"] == "Talk A" and first["language"] == "hi" and first["has_stats"] is True
    assert first["url"] == "https://www.youtube.com/shorts/vidA"
    assert d["totals"] == {"posts": 2, "with_stats": 2, "views": 1234, "likes": 60, "comments": 6}
    assert d["stats_error"] is None


async def test_rehearsals_failed_and_unfinished_posts_are_not_listed(client, maker, stats):
    await post(maker, "vidA")
    await post(maker, "dry-run", url=None)
    await post(maker, "dry-run", url=None)
    await post(maker, None, status=m.PublishStatus.FAILED, url=None)
    await post(maker, "vidQ", status=m.PublishStatus.QUEUED)
    await post(maker, "", url=None)
    stats.data = {"vidA": entry(5)}
    d = (await client.get("/api/dashboard")).json()
    assert [p["video_id"] for p in d["posts"]] == ["vidA"] and d["rehearsals"] == 2
    assert stats.asked == [["vidA"]]                       # no quota spent on rehearsals or failures


async def test_a_post_youtube_does_not_return_is_listed_without_numbers(client, maker, stats):
    await post(maker, "vidA", hours_ago=2)
    await post(maker, "vidPrivate", hours_ago=1)
    stats.data = {"vidA": entry(40, 3, 0)}
    d = (await client.get("/api/dashboard")).json()
    hidden = next(p for p in d["posts"] if p["video_id"] == "vidPrivate")
    assert hidden["has_stats"] is False and hidden["views"] is None
    assert d["totals"] == {"posts": 2, "with_stats": 1, "views": 40, "likes": 3, "comments": 0}


async def test_hidden_like_counts_do_not_break_the_sums(client, maker, stats):
    await post(maker, "vidA")
    stats.data = {"vidA": {"views": 7, "likes": None, "comments": None}}
    d = (await client.get("/api/dashboard")).json()
    assert d["posts"][0]["likes"] is None and d["totals"]["likes"] == 0 and d["totals"]["views"] == 7


async def test_when_youtube_fails_the_posts_still_show_with_a_reason(client, maker, stats):
    await post(maker, "vidA")
    stats.error = StatsUnavailable("YouTube refused the request (quota used up, or the API key is restricted).")
    r = await client.get("/api/dashboard")
    d = r.json()
    assert r.status_code == 200 and len(d["posts"]) == 1 and d["posts"][0]["has_stats"] is False
    assert "quota" in d["stats_error"] and d["totals"]["views"] == 0


async def test_an_unexpected_failure_is_also_contained(client, maker, stats):
    await post(maker, "vidA")
    stats.error = RuntimeError("something odd")
    r = await client.get("/api/dashboard")
    assert r.status_code == 200 and r.json()["stats_error"] == "Could not load the numbers from YouTube."


async def test_no_posts_means_no_youtube_call(client, stats):
    d = (await client.get("/api/dashboard")).json()
    assert d["posts"] == [] and d["totals"]["posts"] == 0 and stats.asked == []


async def test_a_post_with_no_stored_link_gets_one_built_from_its_id(client, maker, stats):
    await post(maker, "vidA", url=None)
    stats.data = {"vidA": entry(1)}
    assert (await client.get("/api/dashboard")).json()["posts"][0]["url"] == "https://www.youtube.com/shorts/vidA"


async def test_the_limit_is_applied_and_validated(client, maker, stats):
    for i in range(3):
        await post(maker, f"v{i}", hours_ago=i + 1)
    stats.data = {f"v{i}": entry(i) for i in range(3)}
    assert len((await client.get("/api/dashboard?limit=2")).json()["posts"]) == 2
    assert (await client.get("/api/dashboard?limit=0")).status_code == 422


async def test_the_dashboard_needs_the_login(client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "secret")
    monkeypatch.setattr(settings, "SECRET_KEY", "x" * 40)
    assert (await client.get("/api/dashboard")).status_code == 401
