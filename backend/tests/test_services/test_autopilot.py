"""Autopilot: schedule slots, reading the channel, picking a source, the tick and the dispatcher."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.db import models as m
from app.services import autopilot as ap
from app.services.autopilot import AutopilotConfigError

UTC = timezone.utc


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


class Recorder:
    """A stand-in Celery task: records .delay() calls, optionally failing."""

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def delay(self, arg):
        if self.fail:
            raise ConnectionError("redis is down")
        self.calls.append(arg)


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setattr(settings, "YOUTUBE_API_KEY", "key")
    monkeypatch.setattr(settings, "AUTOPILOT_MIN_DURATION", 180)
    monkeypatch.setattr(settings, "AUTOPILOT_MAX_DURATION", 3600)


@pytest.fixture
def pipeline(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.workers.pipeline.start_processing_pipeline",
        lambda video_id, from_url=False: calls.append((video_id, from_url)),
    )
    return calls


@pytest.fixture
def user(sync_db):
    u = m.User(yt_channel_id="UCabc123", yt_channel_name="Me")
    sync_db.add(u)
    sync_db.commit()
    return u


def upload(vid, published="2025-01-01T00:00:00Z", duration=600.0, live=False, public=True, title="T"):
    return {"id": vid, "title": title, "published_at": published, "duration": duration, "live": live, "public": public}


# ─── Schedule maths ──────────────────────────────────────────────────────────

def test_run_hours_ignore_junk():
    assert ap.parse_run_hours("18, 9,9, x, 25, -1, ") == [9, 18]
    assert ap.parse_run_hours("") == [] and ap.parse_run_hours(None) == []


def row(**kw):
    base = dict(run_times="9,18", timezone="UTC")
    return m.AutopilotSettings(**{**base, **kw})


def test_slot_is_the_latest_hour_that_has_started():
    assert ap.latest_due_slot(row(), at("2026-11-15T08:59:00")) is None
    assert ap.latest_due_slot(row(), at("2026-11-15T09:00:00")) == "2026-11-15T09"
    assert ap.latest_due_slot(row(), at("2026-11-15T17:59:00")) == "2026-11-15T09"
    assert ap.latest_due_slot(row(), at("2026-11-15T23:00:00")) == "2026-11-15T18"  # only the latest, no burst


def test_slots_use_the_users_timezone():
    # 04:00 UTC is 09:30 in India
    assert ap.latest_due_slot(row(timezone="Asia/Kolkata"), at("2026-11-15T04:00:00")) == "2026-11-15T09"
    assert ap.latest_due_slot(row(timezone="Asia/Kolkata"), at("2026-11-15T03:00:00")) is None


def test_an_unknown_timezone_falls_back_to_utc():
    assert ap.latest_due_slot(row(timezone="Mars/Base"), at("2026-11-15T10:00:00")) == "2026-11-15T09"


def test_iso_durations():
    assert ap.parse_iso_duration("PT1H2M3S") == 3723
    assert ap.parse_iso_duration("PT45S") == 45
    assert ap.parse_iso_duration("PT10M") == 600
    assert ap.parse_iso_duration("P1DT1H") == 90000
    assert ap.parse_iso_duration("garbage") == 0


def test_uploads_playlist_id():
    assert ap.uploads_playlist_id("UCabc123") == "UUabc123"
    with pytest.raises(AutopilotConfigError):
        ap.uploads_playlist_id("xyz")


# ─── Reading the channel ─────────────────────────────────────────────────────

def test_fetch_uploads_pages_through_the_playlist_and_reads_details(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append((url.rsplit("/", 1)[1], dict(params)))
        if url.endswith("playlistItems"):
            if params.get("pageToken") == "P2":
                return SimpleNamespace(status_code=200, json=lambda: {"items": [
                    {"contentDetails": {"videoId": "v3", "videoPublishedAt": "2025-03-01"}, "snippet": {"title": "C"}}]})
            return SimpleNamespace(status_code=200, json=lambda: {"nextPageToken": "P2", "items": [
                {"contentDetails": {"videoId": "v1", "videoPublishedAt": "2025-01-01"}, "snippet": {"title": "A"}},
                {"contentDetails": {"videoId": "v2", "videoPublishedAt": "2025-02-01"}, "snippet": {"title": "B"}}]})
        ids = params["id"].split(",")
        return SimpleNamespace(status_code=200, json=lambda: {"items": [
            {"id": i, "contentDetails": {"duration": "PT10M"}, "snippet": {"liveBroadcastContent": "none"},
             "status": {"privacyStatus": "public"}} for i in ids]})

    monkeypatch.setattr(ap.httpx, "get", fake_get)
    found = ap.fetch_uploads("UCabc123")
    assert [u["id"] for u in found] == ["v1", "v2", "v3"]
    assert all(u["duration"] == 600 and u["public"] and not u["live"] for u in found)
    assert calls[0][1]["playlistId"] == "UUabc123" and calls[0][1]["key"] == "key"
    assert calls[1][1]["pageToken"] == "P2"


def test_fetch_uploads_raises_on_api_errors(monkeypatch):
    monkeypatch.setattr(ap.httpx, "get", lambda *a, **k: SimpleNamespace(status_code=403, text="quota", json=dict))
    with pytest.raises(RuntimeError, match="403"):
        ap.fetch_uploads("UCabc123")


def test_a_channel_with_no_uploads_playlist_gives_an_empty_list(monkeypatch):
    monkeypatch.setattr(ap.httpx, "get", lambda *a, **k: SimpleNamespace(status_code=404, text="", json=dict))
    assert ap.fetch_uploads("UCabc123") == []


# ─── Choosing ────────────────────────────────────────────────────────────────

def test_the_oldest_unused_eligible_video_is_chosen():
    pool = [upload("new", "2025-06-01"), upload("old", "2024-01-01"), upload("mid", "2025-01-01")]
    assert ap.select_candidate(pool, set())["id"] == "old"
    assert ap.select_candidate(pool, {"old"})["id"] == "mid"


@pytest.mark.parametrize("bad", [
    upload("short", duration=60),          # a Short: under the minimum
    upload("edge", duration=179),
    upload("huge", duration=7200),
    upload("live", live=True),
    upload("private", public=False),
])
def test_ineligible_videos_are_never_chosen(bad):
    assert ap.select_candidate([bad], set()) is None


def test_the_length_limits_are_inclusive():
    assert ap.select_candidate([upload("a", duration=180)], set()) is not None
    assert ap.select_candidate([upload("b", duration=3600)], set()) is not None


# ─── Starting a source ───────────────────────────────────────────────────────

def test_starting_creates_the_rows_and_queues_the_pipeline(sync_db, user, pipeline):
    source = ap.start_next_source(sync_db, user, fetch=lambda ch: [upload("v1", title="My talk")])
    video = sync_db.get(m.SourceVideo, source.source_video_id)
    assert source.status == "processing" and source.youtube_video_id == "v1"
    assert video.youtube_url == "https://www.youtube.com/watch?v=v1"
    assert video.ownership_confirmed is True and video.user_id == user.id
    assert pipeline == [(str(video.id), True)]  # queued after the rows were committed, from_url=True


def test_a_video_is_never_picked_twice(sync_db, user, pipeline):
    fetch = lambda ch: [upload("v1")]
    assert ap.start_next_source(sync_db, user, fetch=fetch) is not None
    assert ap.start_next_source(sync_db, user, fetch=fetch) is None
    assert len(pipeline) == 1


def test_it_reads_the_connected_channel_only(sync_db, user, pipeline):
    seen = []
    ap.start_next_source(sync_db, user, fetch=lambda ch: seen.append(ch) or [])
    assert seen == ["UCabc123"]


def test_missing_setup_is_reported(sync_db, user, pipeline, monkeypatch):
    monkeypatch.setattr(settings, "YOUTUBE_API_KEY", "")
    with pytest.raises(AutopilotConfigError, match="YOUTUBE_API_KEY"):
        ap.start_next_source(sync_db, user, fetch=lambda ch: [])
    monkeypatch.setattr(settings, "YOUTUBE_API_KEY", "key")
    monkeypatch.setattr(settings, "YOUTUBE_CHANNEL_ID", "")
    with pytest.raises(AutopilotConfigError, match="channel is unknown"):
        ap.start_next_source(sync_db, m.User(yt_channel_id=""), fetch=lambda ch: [])
    with pytest.raises(AutopilotConfigError):
        ap.start_next_source(sync_db, None, fetch=lambda ch: [])


def test_a_dead_queue_leaves_the_video_unpicked_for_the_next_try(sync_db, user, monkeypatch):
    def boom(video_id, from_url=False):
        raise ConnectionError("redis is down")

    monkeypatch.setattr("app.workers.pipeline.start_processing_pipeline", boom)
    with pytest.raises(ConnectionError):
        ap.start_next_source(sync_db, user, fetch=lambda ch: [upload("v1")])
    assert sync_db.query(m.AutopilotSource).count() == 0 and sync_db.query(m.SourceVideo).count() == 0


# ─── The tick ────────────────────────────────────────────────────────────────

def configure(db, **kw):
    s = ap.get_settings_row(db)
    for k, v in kw.items():
        setattr(s, k, v)
    db.commit()
    return s


def starter(*uploads):
    return lambda db, u: ap.start_next_source(db, u, fetch=lambda ch: list(uploads))


def test_settings_start_safe(sync_db):
    s = ap.get_settings_row(sync_db)
    assert (s.enabled, s.dry_run, s.auto_approve, s.privacy) == (False, True, False, "private")
    assert ap.get_settings_row(sync_db) is s


def test_a_disabled_autopilot_picks_nothing(sync_db, user, pipeline):
    summary = ap.run_tick(sync_db, publish=Recorder(), now=at("2026-11-15T10:00:00"), start=starter(upload("v1")))
    assert summary["picked"] is None and pipeline == []
    assert ap.get_settings_row(sync_db).last_tick_at is not None


def test_an_enabled_autopilot_picks_once_per_slot(sync_db, user, pipeline):
    configure(sync_db, enabled=True)
    first = ap.run_tick(sync_db, publish=Recorder(), now=at("2026-11-15T10:00:00"),
                        start=starter(upload("v1", "2025-01-01"), upload("v2", "2025-02-01")))
    assert first["picked"] == "v1"
    # finish the first one so it is not "in progress", then tick again inside the same slot
    src = sync_db.query(m.AutopilotSource).one()
    src.status = "done"
    sync_db.commit()
    again = ap.run_tick(sync_db, publish=Recorder(), now=at("2026-11-15T10:05:00"),
                        start=starter(upload("v1"), upload("v2", "2025-02-01")))
    assert again["picked"] is None and again["note"] == "no run slot due"
    later = ap.run_tick(sync_db, publish=Recorder(), now=at("2026-11-15T18:00:00"),
                        start=starter(upload("v1"), upload("v2", "2025-02-01")))
    assert later["picked"] == "v2"
    assert len(pipeline) == 2


def test_nothing_is_picked_before_the_first_slot(sync_db, user, pipeline):
    configure(sync_db, enabled=True)
    summary = ap.run_tick(sync_db, publish=Recorder(), now=at("2026-11-15T08:00:00"), start=starter(upload("v1")))
    assert summary["picked"] is None and pipeline == []


def test_a_video_still_in_progress_blocks_the_next_pick(sync_db, user, pipeline):
    configure(sync_db, enabled=True)
    ap.run_tick(sync_db, publish=Recorder(), now=at("2026-11-15T10:00:00"), start=starter(upload("v1")))
    blocked = ap.run_tick(sync_db, publish=Recorder(), now=at("2026-11-15T18:00:00"),
                          start=starter(upload("v1"), upload("v2", "2025-02-01")))
    assert blocked["picked"] is None and "in progress" in blocked["note"]
    assert len(pipeline) == 1


def test_a_setup_problem_does_not_use_up_the_slot(sync_db, user, pipeline, monkeypatch):
    configure(sync_db, enabled=True)
    monkeypatch.setattr(settings, "YOUTUBE_API_KEY", "")
    bad = ap.run_tick(sync_db, publish=Recorder(), now=at("2026-11-15T10:00:00"), start=starter(upload("v1")))
    assert "not set up" in bad["note"] and ap.get_settings_row(sync_db).last_run_slot is None
    monkeypatch.setattr(settings, "YOUTUBE_API_KEY", "key")
    good = ap.run_tick(sync_db, publish=Recorder(), now=at("2026-11-15T10:05:00"), start=starter(upload("v1")))
    assert good["picked"] == "v1"


def test_a_transient_error_is_caught_and_retried_next_tick(sync_db, user, pipeline):
    configure(sync_db, enabled=True)

    def flaky(db, u):
        raise ConnectionError("youtube is down")

    summary = ap.run_tick(sync_db, publish=Recorder(), now=at("2026-11-15T10:00:00"), start=flaky)
    assert "error" in summary["note"] and ap.get_settings_row(sync_db).last_run_slot is None
    assert ap.get_settings_row(sync_db).last_tick_at is not None  # the tick itself still counted as alive


def test_an_empty_channel_uses_the_slot_and_says_so(sync_db, user, pipeline):
    configure(sync_db, enabled=True)
    summary = ap.run_tick(sync_db, publish=Recorder(), now=at("2026-11-15T10:00:00"), start=starter())
    assert summary["note"] == "no unused eligible video"
    assert ap.get_settings_row(sync_db).last_run_slot == "2026-11-15T09"


# ─── Stuck and failed pipelines ──────────────────────────────────────────────

def picked_source(db, video_status, picked_at):
    video = m.SourceVideo(ownership_confirmed=True, status=video_status, error_message="download blocked")
    db.add(video)
    db.flush()
    src = m.AutopilotSource(youtube_video_id=uuid.uuid4().hex[:11], status="processing",
                            source_video_id=video.id, picked_at=picked_at)
    db.add(src)
    db.commit()
    return src


def test_a_failed_pipeline_frees_autopilot_and_records_why(sync_db):
    src = picked_source(sync_db, m.VideoStatus.FAILED, at("2026-11-15T09:00:00"))
    ap.sync_source_statuses(sync_db, at("2026-11-15T09:30:00"))
    assert src.status == "failed" and src.reason == "download blocked"
    assert not ap.in_flight(sync_db)


def test_a_stuck_pipeline_times_out(sync_db):
    src = picked_source(sync_db, m.VideoStatus.TRANSCRIBING, at("2026-11-15T00:00:00"))
    ap.sync_source_statuses(sync_db, at("2026-11-15T13:00:00"))
    assert src.status == "failed" and "Timed out" in src.reason


def test_a_recent_or_finished_pipeline_is_left_alone(sync_db):
    recent = picked_source(sync_db, m.VideoStatus.TRANSCRIBING, at("2026-11-15T09:00:00"))
    ready = picked_source(sync_db, m.VideoStatus.READY, at("2026-11-14T00:00:00"))  # waiting for approval
    ap.sync_source_statuses(sync_db, at("2026-11-15T10:00:00"))
    assert recent.status == "processing" and ready.status == "processing"


# ─── Dispatcher ──────────────────────────────────────────────────────────────

def make_job(db, status, when):
    video = m.SourceVideo(ownership_confirmed=True, status=m.VideoStatus.READY)
    db.add(video)
    db.flush()
    clip = m.Clip(source_video_id=video.id, start_time=0, end_time=20)
    db.add(clip)
    db.flush()
    job = m.PublishJob(clip_id=clip.id, platform=m.Platform.YOUTUBE, status=status, scheduled_at=when, retry_count=0)
    db.add(job)
    db.commit()
    return job


NOW = at("2026-11-15T12:00:00")


def test_due_scheduled_posts_are_queued_and_marked(sync_db):
    due = make_job(sync_db, m.PublishStatus.SCHEDULED, NOW - timedelta(minutes=1))
    later = make_job(sync_db, m.PublishStatus.SCHEDULED, NOW + timedelta(hours=1))
    publish = Recorder()
    assert ap.dispatch_due_publish_jobs(sync_db, NOW, publish) == 1
    assert publish.calls == [str(due.id)]
    sync_db.expire_all()
    assert due.status == m.PublishStatus.QUEUED and later.status == m.PublishStatus.SCHEDULED


def test_other_states_are_never_dispatched(sync_db):
    for status in (m.PublishStatus.QUEUED, m.PublishStatus.PUBLISHING, m.PublishStatus.PUBLISHED, m.PublishStatus.FAILED):
        make_job(sync_db, status, NOW - timedelta(hours=1))
    publish = Recorder()
    assert ap.dispatch_due_publish_jobs(sync_db, NOW, publish) == 0 and publish.calls == []


def test_a_post_is_dispatched_only_once(sync_db):
    make_job(sync_db, m.PublishStatus.SCHEDULED, NOW - timedelta(minutes=1))
    publish = Recorder()
    ap.dispatch_due_publish_jobs(sync_db, NOW, publish)
    ap.dispatch_due_publish_jobs(sync_db, NOW + timedelta(minutes=5), publish)
    assert len(publish.calls) == 1


def test_a_lost_race_does_not_double_queue(sync_db, session_factory):
    """Another tick claims the job between our read and our update."""
    job = make_job(sync_db, m.PublishStatus.SCHEDULED, NOW - timedelta(minutes=1))
    real_scalars = sync_db.scalars

    def scalars_then_rival_claims(*args, **kwargs):
        result = real_scalars(*args, **kwargs)
        found = result.all()  # we have now seen the job as SCHEDULED...
        rival = session_factory()
        rival.get(m.PublishJob, job.id).status = m.PublishStatus.QUEUED  # ...and a rival tick claims it
        rival.commit()
        return SimpleNamespace(all=lambda: found)

    sync_db.scalars = scalars_then_rival_claims
    publish = Recorder()
    assert ap.dispatch_due_publish_jobs(sync_db, NOW, publish) == 0
    assert publish.calls == []


def test_a_dead_queue_puts_the_post_back_to_try_again(sync_db):
    job = make_job(sync_db, m.PublishStatus.SCHEDULED, NOW - timedelta(minutes=1))
    assert ap.dispatch_due_publish_jobs(sync_db, NOW, Recorder(fail=True)) == 0
    sync_db.expire_all()
    assert job.status == m.PublishStatus.SCHEDULED


def test_hand_scheduled_posts_go_out_even_when_autopilot_is_off(sync_db, user, pipeline):
    make_job(sync_db, m.PublishStatus.SCHEDULED, NOW - timedelta(minutes=1))
    publish = Recorder()
    summary = ap.run_tick(sync_db, publish=publish, now=NOW, start=starter())
    assert summary["dispatched"] == 1 and len(publish.calls) == 1


# ─── Celery wiring ───────────────────────────────────────────────────────────

def test_beat_runs_the_tick_task():
    from app.workers.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["autopilot-tick"]
    assert entry["task"] == "app.workers.tasks.autopilot_tick_task"
    assert entry["schedule"] == settings.AUTOPILOT_TICK_MINUTES * 60.0
    assert "app.workers.tasks.autopilot_tick_task" in celery_app.tasks


def test_the_tick_task_runs_against_the_database_and_never_raises(sync_db, session_factory, monkeypatch):
    from app.workers import tasks

    monkeypatch.setattr(tasks, "_get_sync_session", session_factory)
    assert tasks.autopilot_tick_task.apply().successful()
    assert session_factory().get(m.AutopilotSettings, 1).last_tick_at is not None

    monkeypatch.setattr(ap, "run_tick", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert tasks.autopilot_tick_task.apply().successful()  # logged, not raised


def test_migration_chain_has_one_head_after_autopilot():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == ["f3b7d92a1c58"]
    assert script.get_revision("f3b7d92a1c58").down_revision == "e5c2a8d41f96"
    assert script.get_revision("e5c2a8d41f96").down_revision == "d91b3c6e8a47"
    assert script.get_revision("d91b3c6e8a47").down_revision == "c7a2e9f15b83"
    assert script.get_revision("c7a2e9f15b83").down_revision == "b4e6d1a73c20"


# ─── Channel id fallback ─────────────────────────────────────────────────────

def test_the_channel_from_settings_is_used_when_sign_in_saved_none(sync_db, pipeline, monkeypatch):
    monkeypatch.setattr(settings, "YOUTUBE_CHANNEL_ID", " UCfromenv ")
    user = m.User(yt_channel_id="")           # what the upload-only sign-in leaves behind
    sync_db.add(user)
    sync_db.commit()
    seen = []
    ap.start_next_source(sync_db, user, fetch=lambda ch: seen.append(ch) or [upload("v1")])
    assert seen == ["UCfromenv"] and len(pipeline) == 1


def test_a_saved_channel_wins_over_the_setting(sync_db, pipeline, monkeypatch):
    monkeypatch.setattr(settings, "YOUTUBE_CHANNEL_ID", "UCfromenv")
    user = m.User(yt_channel_id="UCsaved")
    sync_db.add(user)
    sync_db.commit()
    seen = []
    ap.start_next_source(sync_db, user, fetch=lambda ch: seen.append(ch) or [])
    assert seen == ["UCsaved"]
