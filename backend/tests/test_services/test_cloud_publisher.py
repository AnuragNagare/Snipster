"""The cloud publisher: due posts go out without the laptop, nothing is posted twice, the kill switch holds."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import cloud_publisher as cp
from app.db import models as m
from app.services import alerts
from app.services import autopilot as ap
from app.services.youtube import YouTubeAuthError, youtube_service
from app.workers import tasks

UTC = timezone.utc
NOW = datetime(2026, 11, 19, 14, 20, tzinfo=UTC)       # 19:50 in India: the 19:45 post is 5 minutes overdue


@pytest.fixture
def world(sync_db, session_factory, monkeypatch):
    sync_db.add(m.User(yt_access_token="x"))
    sync_db.commit()
    monkeypatch.setattr(tasks, "_get_sync_session", session_factory)
    monkeypatch.setattr(alerts, "send", lambda *a, **k: True)
    uploads = []

    def fake_upload(clip, user, privacy, credit=None):
        uploads.append({"privacy": privacy, "credit": credit})
        return {"post_id": f"yt{len(uploads)}", "post_url": f"https://www.youtube.com/shorts/yt{len(uploads)}"}

    monkeypatch.setattr(youtube_service, "upload", fake_upload)
    return type("World", (), {"db": sync_db, "factory": session_factory, "uploads": uploads})()


def settings_row(db, **kw):
    row = ap.get_settings_row(db)
    for k, v in {"enabled": True, "dry_run": False, **kw}.items():
        setattr(row, k, v)
    db.commit()


def job(db, *, autopilot=True, when=NOW - timedelta(minutes=5), status=m.PublishStatus.SCHEDULED):
    video = m.SourceVideo(ownership_confirmed=True, status=m.VideoStatus.READY)
    db.add(video)
    db.flush()
    clip = m.Clip(source_video_id=video.id, start_time=0, end_time=30, storage_key="k", caption="Caption")
    db.add(clip)
    db.flush()
    j = m.PublishJob(clip_id=clip.id, platform=m.Platform.YOUTUBE, status=status, scheduled_at=when,
                     autopilot=autopilot, retry_count=0)
    db.add(j)
    db.commit()
    return j


def status_of(world, j):
    world.db.expire_all()
    return world.factory().get(m.PublishJob, j.id)


def test_a_due_post_is_uploaded_and_recorded(world):
    settings_row(world.db)
    j = job(world.db)
    summary = cp.run(NOW)
    saved = status_of(world, j)
    assert summary["claimed"] == 1 and summary["failed"] == 0
    assert saved.status == m.PublishStatus.PUBLISHED and saved.platform_post_id == "yt1"
    assert len(world.uploads) == 1 and summary["jobs"] == [f"job {j.id}: published"]


def test_a_post_that_is_not_due_yet_is_left_alone(world):
    settings_row(world.db)
    j = job(world.db, when=NOW + timedelta(hours=1))
    assert cp.run(NOW)["claimed"] == 0
    assert status_of(world, j).status == m.PublishStatus.SCHEDULED and world.uploads == []


def test_dry_run_is_a_rehearsal_and_never_calls_youtube(world):
    settings_row(world.db, dry_run=True)
    j = job(world.db)
    summary = cp.run(NOW)
    saved = status_of(world, j)
    assert world.uploads == [] and saved.platform_post_id == "dry-run"
    assert summary["jobs"] == [f"job {j.id}: published (rehearsal, nothing uploaded)"]


def test_the_kill_switch_holds_autopilots_posts_but_not_manual_ones(world):
    settings_row(world.db, enabled=False)
    auto, manual = job(world.db, autopilot=True), job(world.db, autopilot=False)
    cp.run(NOW)
    assert status_of(world, auto).status == m.PublishStatus.SCHEDULED
    assert status_of(world, manual).status == m.PublishStatus.PUBLISHED and len(world.uploads) == 1


def test_posts_are_uploaded_with_the_settings_privacy(world):
    settings_row(world.db, privacy="unlisted")
    job(world.db)
    cp.run(NOW)
    assert world.uploads[0]["privacy"] == "unlisted"


def test_a_second_run_does_not_post_again(world):
    settings_row(world.db)
    job(world.db)
    cp.run(NOW)
    again = cp.run(NOW + timedelta(minutes=15))
    assert again["claimed"] == 0 and len(world.uploads) == 1


def test_a_post_the_laptop_already_claimed_is_not_taken(world):
    """The laptop's scheduler claimed it first (QUEUED): the cloud must not upload it too."""
    settings_row(world.db)
    j = job(world.db, status=m.PublishStatus.QUEUED)
    assert cp.run(NOW)["claimed"] == 0 and world.uploads == []
    assert status_of(world, j).status == m.PublishStatus.QUEUED


def test_two_publishers_racing_for_one_post_upload_it_once(world, session_factory, monkeypatch):
    """Another publisher claims the job between our read and our claim."""
    settings_row(world.db)
    j = job(world.db)

    def session_with_a_rival():
        session = session_factory()
        real = session.scalars

        def scalars_then_rival_claims(*a, **k):
            found = real(*a, **k).all()
            rival = session_factory()
            rival.get(m.PublishJob, j.id).status = m.PublishStatus.QUEUED
            rival.commit()
            return type("R", (), {"all": lambda self: found})()

        session.scalars = scalars_then_rival_claims
        return session

    monkeypatch.setattr(tasks, "_get_sync_session", session_with_a_rival)
    assert cp.run(NOW)["claimed"] == 0 and world.uploads == []


def test_a_failed_upload_marks_the_job_failed_and_the_run_returns_an_error(world, monkeypatch):
    settings_row(world.db)
    j = job(world.db)
    monkeypatch.setattr(youtube_service, "upload", lambda **k: (_ for _ in ()).throw(YouTubeAuthError("Reconnect YouTube in Settings.")))
    summary = cp.run(NOW)
    saved = status_of(world, j)
    assert summary["failed"] == 1 and saved.status == m.PublishStatus.FAILED
    assert json.loads(saved.error_info)["auth"] is True
    assert cp.main.__name__ == "main"


def test_main_exits_with_1_when_a_post_failed_and_0_otherwise(world, monkeypatch, capsys):
    settings_row(world.db)
    five_minutes_ago = datetime.now(UTC) - timedelta(minutes=5)     # main() uses the real clock
    job(world.db, when=five_minutes_ago)
    assert cp.main() == 0
    assert "published" in capsys.readouterr().out

    job(world.db, when=five_minutes_ago)
    monkeypatch.setattr(youtube_service, "upload", lambda **k: (_ for _ in ()).throw(YouTubeAuthError("expired")))
    assert cp.main() == 1
    out = capsys.readouterr().out
    assert "failed" in out and "Retry" in out


def test_nothing_due_exits_cleanly(world, capsys):
    settings_row(world.db)
    assert cp.main() == 0 and "Nothing due." in capsys.readouterr().out


def test_a_cc_posts_credit_reaches_the_upload(world):
    settings_row(world.db)
    j = job(world.db)
    clip = world.db.get(m.Clip, j.clip_id)
    world.db.add(m.AutopilotSource(youtube_video_id="src1", title="Cool talk", status="done", source_video_id=clip.source_video_id,
                                   license="creativeCommon", channel_title="Great Creator"))
    world.db.commit()
    cp.run(NOW)
    assert "Great Creator" in world.uploads[0]["credit"] and "src1" in world.uploads[0]["credit"]


def test_the_output_has_no_captions_and_no_secrets(world, capsys):
    settings_row(world.db)
    job(world.db, when=datetime.now(UTC) - timedelta(minutes=5))
    cp.main()
    out = capsys.readouterr().out
    assert "Caption" not in out and "postgresql" not in out and "AIza" not in out


# ─── The workflow file ───────────────────────────────────────────────────────

WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "publisher.yml"


def test_the_workflow_is_valid_and_only_publishes():
    import yaml

    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    trig = data.get("on") or data.get(True)
    assert trig["schedule"][0]["cron"] == "*/15 * * * *" and "workflow_dispatch" in trig
    assert data["concurrency"]["cancel-in-progress"] is False
    steps = data["jobs"]["publish"]["steps"]
    assert steps[-1]["run"] == "python -m app.cloud_publisher"
    assert data["permissions"] == {"contents": "read"}


def test_the_workflow_gets_only_the_secrets_publishing_needs():
    import yaml

    env = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["publish"]["steps"][-1]["env"]
    assert set(env) == {"DATABASE_URL", "STORAGE_ENDPOINT", "STORAGE_BUCKET", "STORAGE_ACCESS_KEY", "STORAGE_SECRET_KEY",
                        "TOKEN_ENCRYPTION_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
    for forbidden in ("REDIS_URL", "GEMINI_API_KEY", "ADMIN_PASSWORD", "SECRET_KEY", "YOUTUBE_API_KEY"):
        assert forbidden not in env                           # not needed to post: least privilege


def test_the_publisher_does_not_need_the_heavy_libraries():
    """Nothing it imports may pull in Whisper, yt-dlp, Gemini or ffmpeg wrappers."""
    import subprocess
    import sys

    code = ("import sys; import app.cloud_publisher as c; c.run.__doc__; "
            "from app.workers import tasks; from app.services import autopilot, youtube; "
            "bad = [n for n in ('faster_whisper','yt_dlp','google.generativeai','ffmpeg') if n in sys.modules]; "
            "print('HEAVY:' + ','.join(bad))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=Path(__file__).resolve().parents[2])
    assert out.stdout.strip().endswith("HEAVY:"), out.stdout + out.stderr
