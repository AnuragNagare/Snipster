"""YouTube's own scheduling, automatic retry of failed posts, cloud-publisher alerts, and clean public logs."""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import cloud_publisher as cp
from app.core.config import settings
from app.db import models as m
from app.services import alerts
from app.services import autopilot as ap
from app.services.youtube import YouTubeAuthError, youtube_service
from app.workers import tasks

UTC = timezone.utc
SLOT = datetime(2026, 11, 20, 14, 15, tzinfo=UTC)          # 19:45 in India
NOW = SLOT - timedelta(hours=2)                              # two hours before the slot


class Notes:
    def __init__(self):
        self.messages = []

    def __call__(self, text, key=None, cooldown=0):
        self.messages.append(text)
        return True


@pytest.fixture
def notes():
    return Notes()


def row_with(db, **kw):
    row = ap.get_settings_row(db)
    for k, v in {"enabled": True, "dry_run": False, "privacy": "public", "schedule_on_youtube": True, **kw}.items():
        setattr(row, k, v)
    db.commit()
    return row


def make_job(db, *, when=SLOT, status=m.PublishStatus.SCHEDULED, autopilot=True, retry_count=0, error=None, caption="Secret caption"):
    video = m.SourceVideo(ownership_confirmed=True, status=m.VideoStatus.READY)
    db.add(video)
    db.flush()
    clip = m.Clip(source_video_id=video.id, start_time=0, end_time=30, storage_key="k", caption=caption)
    db.add(clip)
    db.flush()
    job = m.PublishJob(clip_id=clip.id, platform=m.Platform.YOUTUBE, status=status, scheduled_at=when, autopilot=autopilot,
                       retry_count=retry_count, error_info=error, created_at=when - timedelta(hours=6))
    db.add(job)
    db.commit()
    return job


class Publish:
    def __init__(self):
        self.calls = []

    def delay(self, arg):
        self.calls.append(arg)


# ═══ Exact time: the early-upload window ════════════════════════════════════

def test_the_window_is_three_hours_only_for_public_live_scheduled_posts(sync_db):
    assert ap.dispatch_lead(row_with(sync_db)) == timedelta(hours=3)
    for off in (dict(schedule_on_youtube=False), dict(privacy="private"), dict(privacy="unlisted"), dict(dry_run=True)):
        assert ap.dispatch_lead(row_with(sync_db, **{"schedule_on_youtube": True, "privacy": "public", "dry_run": False, **off})) == timedelta(0), off


def test_a_post_due_within_the_window_is_claimed_early_and_a_later_one_is_not(sync_db):
    soon = make_job(sync_db, when=NOW + timedelta(hours=2, minutes=59))
    later = make_job(sync_db, when=NOW + timedelta(hours=3, minutes=1))
    publish = Publish()
    ap.dispatch_due_publish_jobs(sync_db, NOW, publish, lead=timedelta(hours=3))
    assert publish.calls == [str(soon.id)]
    sync_db.expire_all()
    assert later.status == m.PublishStatus.SCHEDULED


def test_without_a_lead_nothing_is_claimed_early(sync_db):
    make_job(sync_db, when=NOW + timedelta(minutes=30))
    publish = Publish()
    ap.dispatch_due_publish_jobs(sync_db, NOW, publish)
    assert publish.calls == []


def test_the_tick_uses_the_window_from_the_settings(sync_db):
    row_with(sync_db)
    soon = make_job(sync_db, when=NOW + timedelta(hours=2))
    publish = Publish()
    ap.run_tick(sync_db, publish=publish, now=NOW, start=lambda d, u: None, notify=lambda *a, **k: True)
    assert publish.calls == [str(soon.id)]


# ═══ Exact time: the upload itself ══════════════════════════════════════════

@pytest.fixture
def uploads(sync_db, session_factory, monkeypatch):
    sync_db.add(m.User(yt_access_token="x"))
    sync_db.commit()
    monkeypatch.setattr(tasks, "_get_sync_session", session_factory)
    monkeypatch.setattr(alerts, "send", lambda *a, **k: True)
    seen = []

    def fake(clip, user, privacy, credit=None, publish_at=None):
        seen.append({"privacy": privacy, "publish_at": publish_at})
        return {"post_id": "yt1", "post_url": "https://www.youtube.com/shorts/yt1"}

    monkeypatch.setattr(youtube_service, "upload", fake)
    return seen


def publish(session_factory, job):
    return tasks.publish_clip_task.apply(args=[str(job.id)])


def freeze_now(monkeypatch, moment):
    """The publish task reads the clock through datetime.now(timezone.utc)."""
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(tasks, "datetime", Frozen)


def test_an_early_post_is_uploaded_with_the_exact_publish_time(sync_db, session_factory, uploads, monkeypatch):
    row_with(sync_db)
    job = make_job(sync_db)
    freeze_now(monkeypatch, NOW)
    publish(session_factory, job)
    assert uploads == [{"privacy": "public", "publish_at": SLOT}]
    saved = session_factory().get(m.PublishJob, job.id)
    assert saved.status == m.PublishStatus.PUBLISHED and saved.scheduled_at.replace(tzinfo=UTC) == SLOT


def test_a_post_whose_time_has_passed_goes_out_now_without_a_publish_time(sync_db, session_factory, uploads, monkeypatch):
    row_with(sync_db)
    job = make_job(sync_db, when=NOW - timedelta(minutes=1))
    freeze_now(monkeypatch, NOW)
    publish(session_factory, job)
    assert uploads[0]["publish_at"] is None


def test_a_time_less_than_five_minutes_away_goes_out_now(sync_db, session_factory, uploads, monkeypatch):
    row_with(sync_db)
    job = make_job(sync_db, when=NOW + timedelta(minutes=4))
    freeze_now(monkeypatch, NOW)
    publish(session_factory, job)
    assert uploads[0]["publish_at"] is None


@pytest.mark.parametrize("off", [dict(schedule_on_youtube=False), dict(privacy="private"), dict(privacy="unlisted")])
def test_no_publish_time_unless_scheduling_is_on_and_visibility_is_public(sync_db, session_factory, uploads, monkeypatch, off):
    row_with(sync_db, **off)
    job = make_job(sync_db)
    freeze_now(monkeypatch, NOW)
    publish(session_factory, job)
    assert uploads[0]["publish_at"] is None


def test_the_youtube_request_uploads_private_with_publish_at(monkeypatch):
    from app.services import youtube as yt
    from cryptography.fernet import Fernet
    from app.core.security import encrypt_token

    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(yt.storage_service, "download_to_file", lambda key, path: open(path, "wb").write(b"v"))
    sent = {}

    def post(url, **kw):
        sent["meta"] = json.loads(kw["files"]["metadata"][1])
        return SimpleNamespace(status_code=200, json=lambda: {"id": "p"}, text="")

    monkeypatch.setattr(yt.httpx, "post", post)
    user = SimpleNamespace(yt_access_token=encrypt_token("t"), yt_refresh_token=None, yt_token_expiry=None)
    clip = SimpleNamespace(storage_key="k", caption="Cap", hashtags="#a")

    yt.youtube_service.upload(clip, user, privacy="public", publish_at=SLOT)
    assert sent["meta"]["status"] == {"privacyStatus": "private", "publishAt": "2026-11-20T14:15:00.000Z", "selfDeclaredMadeForKids": False}

    ist = datetime(2026, 11, 20, 19, 45, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    yt.youtube_service.upload(clip, user, privacy="public", publish_at=ist)
    assert sent["meta"]["status"]["publishAt"] == "2026-11-20T14:15:00.000Z"        # always written in UTC

    yt.youtube_service.upload(clip, user, privacy="public")
    assert sent["meta"]["status"] == {"privacyStatus": "public", "selfDeclaredMadeForKids": False}


# ═══ Automatic retry ════════════════════════════════════════════════════════

def failed_job(db, *, failed_ago, retry_count=0, when=None, **info):
    info = {"error": "network", "failed_at": (NOW - failed_ago).isoformat(), **info}
    return make_job(db, when=when or NOW - timedelta(hours=1), status=m.PublishStatus.FAILED, retry_count=retry_count, error=json.dumps(info))


def reload(session_factory, job):
    return session_factory().get(m.PublishJob, job.id)


@pytest.mark.parametrize("tries,wait_minutes", [(0, 15), (1, 60), (2, 240), (3, 720), (4, 720)])
def test_the_wait_before_each_automatic_retry(sync_db, session_factory, notes, tries, wait_minutes):
    job = failed_job(sync_db, failed_ago=timedelta(minutes=wait_minutes - 1), retry_count=tries)
    assert ap.retry_failed_jobs(sync_db, NOW, notes) == 0                     # one minute too early
    sync_db.expire_all()
    assert reload(session_factory, job).status == m.PublishStatus.FAILED
    job.error_info = json.dumps({"error": "network", "failed_at": (NOW - timedelta(minutes=wait_minutes)).isoformat()})
    sync_db.commit()
    assert ap.retry_failed_jobs(sync_db, NOW, notes) == 1
    saved = reload(session_factory, job)
    assert saved.status == m.PublishStatus.SCHEDULED and saved.retry_count == tries + 1


def test_a_retried_post_keeps_its_original_time(sync_db, session_factory, notes):
    original = NOW - timedelta(hours=1)
    job = failed_job(sync_db, failed_ago=timedelta(hours=2), when=original)
    ap.retry_failed_jobs(sync_db, NOW, notes)
    assert reload(session_factory, job).scheduled_at.replace(tzinfo=UTC) == original


@pytest.mark.parametrize("info", [dict(auth=True), dict(uploaded="abc"), dict(permanent=True), dict(gave_up=True)])
def test_failures_a_retry_cannot_fix_are_never_retried(sync_db, session_factory, notes, info):
    job = failed_job(sync_db, failed_ago=timedelta(days=0, hours=3), **info)
    assert ap.retry_failed_jobs(sync_db, NOW, notes) == 0
    assert reload(session_factory, job).status == m.PublishStatus.FAILED


def test_a_failure_with_no_recorded_time_or_details_is_left_alone(sync_db, session_factory, notes):
    a = make_job(sync_db, when=NOW - timedelta(hours=1), status=m.PublishStatus.FAILED, error=None)
    b = make_job(sync_db, when=NOW - timedelta(hours=1), status=m.PublishStatus.FAILED, error="plain text")
    c = make_job(sync_db, when=NOW - timedelta(hours=1), status=m.PublishStatus.FAILED, error=json.dumps({"error": "x"}))
    assert ap.retry_failed_jobs(sync_db, NOW, notes) == 0
    assert all(reload(session_factory, j).status == m.PublishStatus.FAILED for j in (a, b, c))


def test_a_post_older_than_a_day_is_given_up_with_one_alert(sync_db, session_factory, notes):
    job = failed_job(sync_db, failed_ago=timedelta(hours=5), when=NOW - timedelta(hours=25), error="boom")
    assert ap.retry_failed_jobs(sync_db, NOW, notes) == 0
    assert len(notes.messages) == 1 and "given up" in notes.messages[0] and "boom" in notes.messages[0]
    assert json.loads(reload(session_factory, job).error_info)["gave_up"] is True
    ap.retry_failed_jobs(sync_db, NOW + timedelta(hours=1), notes)             # asked again: no second alert
    assert len(notes.messages) == 1


def test_after_five_tries_it_is_given_up(sync_db, session_factory, notes):
    job = failed_job(sync_db, failed_ago=timedelta(hours=20), retry_count=5)
    assert ap.retry_failed_jobs(sync_db, NOW, notes) == 0
    assert reload(session_factory, job).status == m.PublishStatus.FAILED and len(notes.messages) == 1


def test_two_publishers_cannot_requeue_the_same_post_twice(sync_db, session_factory, notes):
    job = failed_job(sync_db, failed_ago=timedelta(hours=2))
    real = sync_db.scalars

    def scalars_then_rival_requeues(*a, **k):
        found = real(*a, **k).all()
        rival = session_factory()
        j = rival.get(m.PublishJob, job.id)
        j.status, j.retry_count = m.PublishStatus.SCHEDULED, 1
        rival.commit()
        return type("R", (), {"all": lambda self: found})()

    sync_db.scalars = scalars_then_rival_requeues
    assert ap.retry_failed_jobs(sync_db, NOW, notes) == 0
    sync_db.expire_all()
    assert reload(session_factory, job).retry_count == 1                           # not 2


def test_the_tick_retries_then_dispatches_in_one_go(sync_db, notes):
    row_with(sync_db, schedule_on_youtube=False)
    job = failed_job(sync_db, failed_ago=timedelta(hours=2))
    publish = Publish()
    summary = ap.run_tick(sync_db, publish=publish, now=NOW, start=lambda d, u: None, notify=notes)
    assert summary["retried"] == 1 and publish.calls == [str(job.id)]


def test_automatic_retry_can_be_switched_off(sync_db, session_factory, notes):
    row_with(sync_db, auto_retry=False, schedule_on_youtube=False)
    job = failed_job(sync_db, failed_ago=timedelta(hours=2))
    ap.run_tick(sync_db, publish=Publish(), now=NOW, start=lambda d, u: None, notify=notes)
    assert reload(session_factory, job).status == m.PublishStatus.FAILED


# ═══ How a failure is recorded, and when the user is alerted ═════════════════

@pytest.fixture
def failing(sync_db, session_factory, monkeypatch):
    sync_db.add(m.User(yt_access_token="x"))
    sync_db.commit()
    sent = []
    monkeypatch.setattr(tasks, "_get_sync_session", session_factory)
    monkeypatch.setattr(alerts, "send", lambda text, **k: sent.append(text) or True)
    return sent


def break_upload(monkeypatch, error):
    monkeypatch.setattr(youtube_service, "upload", lambda **k: (_ for _ in ()).throw(error))


def run_final_try(job):
    return tasks.publish_clip_task.apply(args=[str(job.id)], retries=tasks.publish_clip_task.max_retries)


def test_a_failure_stores_when_it_happened(sync_db, session_factory, failing, monkeypatch):
    row_with(sync_db, dry_run=False, schedule_on_youtube=False)
    job = make_job(sync_db, when=NOW - timedelta(minutes=1), status=m.PublishStatus.QUEUED)
    break_upload(monkeypatch, RuntimeError("network down"))
    run_final_try(job)
    info = json.loads(reload(session_factory, job).error_info)
    assert info["error"] == "network down" and "failed_at" in info and not info.get("permanent")
    datetime.fromisoformat(info["failed_at"])


def test_no_alert_at_the_last_try_when_an_automatic_retry_follows(sync_db, session_factory, failing, monkeypatch):
    row_with(sync_db, dry_run=False, schedule_on_youtube=False, auto_retry=True)
    job = make_job(sync_db, when=NOW - timedelta(minutes=1), status=m.PublishStatus.QUEUED)
    break_upload(monkeypatch, RuntimeError("network down"))
    run_final_try(job)
    assert failing == []


def test_alert_at_the_last_try_when_automatic_retry_is_off(sync_db, session_factory, failing, monkeypatch):
    row_with(sync_db, dry_run=False, schedule_on_youtube=False, auto_retry=False)
    job = make_job(sync_db, when=NOW - timedelta(minutes=1), status=m.PublishStatus.QUEUED)
    break_upload(monkeypatch, RuntimeError("network down"))
    run_final_try(job)
    assert len(failing) == 1 and "network down" in failing[0]


def test_a_sign_in_problem_alerts_at_once_and_is_marked_unretryable(sync_db, session_factory, failing, monkeypatch):
    row_with(sync_db, dry_run=False, schedule_on_youtube=False)
    job = make_job(sync_db, when=NOW - timedelta(minutes=1), status=m.PublishStatus.QUEUED)
    break_upload(monkeypatch, YouTubeAuthError("Reconnect YouTube in Settings."))
    tasks.publish_clip_task.apply(args=[str(job.id)])
    info = json.loads(reload(session_factory, job).error_info)
    assert info["auth"] is True and len(failing) == 1
    assert not ap.failure_is_retryable(info)


def test_a_wrong_setting_is_marked_permanent_and_alerts(sync_db, session_factory, failing, monkeypatch):
    row_with(sync_db, dry_run=False, schedule_on_youtube=False, auto_retry=True)
    job = make_job(sync_db, when=NOW - timedelta(minutes=1), status=m.PublishStatus.QUEUED)
    break_upload(monkeypatch, ValueError("Invalid privacy setting"))
    run_final_try(job)
    assert json.loads(reload(session_factory, job).error_info)["permanent"] is True and len(failing) == 1


def test_the_cloud_publisher_does_not_repeat_a_failing_upload_at_once(sync_db, session_factory, failing, monkeypatch):
    """Celery's own quick retries are skipped: the automatic retry waits 15 minutes instead."""
    row_with(sync_db, dry_run=False, schedule_on_youtube=False)
    make_job(sync_db, when=datetime.now(UTC) - timedelta(minutes=1))
    calls = []
    monkeypatch.setattr(youtube_service, "upload", lambda **k: calls.append(1) or (_ for _ in ()).throw(RuntimeError("down")))
    cp.run(datetime.now(UTC))
    assert len(calls) == 1


# ═══ The cloud publisher: heartbeat, pings, retries ═════════════════════════

@pytest.fixture
def cloud(sync_db, session_factory, monkeypatch):
    sync_db.add(m.User(yt_access_token="x"))
    sync_db.commit()
    monkeypatch.setattr(tasks, "_get_sync_session", session_factory)
    monkeypatch.setattr(alerts, "send", lambda *a, **k: True)
    pings = []
    monkeypatch.setattr(alerts.httpx, "get", lambda url, timeout: pings.append(url) or SimpleNamespace(status_code=200))
    monkeypatch.setattr(settings, "HEALTHCHECK_CLOUD_URL", "https://hc-ping.com/cloud-uuid")
    monkeypatch.setattr(youtube_service, "upload", lambda **k: {"post_id": "p", "post_url": "u"})
    return pings


def test_each_run_records_when_it_ran(sync_db, session_factory, cloud):
    row_with(sync_db, schedule_on_youtube=False)
    assert ap.get_settings_row(sync_db).last_cloud_run_at is None
    cp.run(NOW)
    sync_db.expire_all()
    assert session_factory().get(m.AutopilotSettings, 1).last_cloud_run_at.replace(tzinfo=UTC) == NOW


def test_a_healthy_run_pings_the_cloud_check_and_a_failed_one_pings_fail(sync_db, cloud, monkeypatch, capsys):
    row_with(sync_db, schedule_on_youtube=False)
    assert cp.main() == 0 and cloud == ["https://hc-ping.com/cloud-uuid"]

    make_job(sync_db, when=datetime.now(UTC) - timedelta(minutes=5))
    monkeypatch.setattr(youtube_service, "upload", lambda **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert cp.main() == 1
    assert cloud[-1] == "https://hc-ping.com/cloud-uuid/fail"


def test_no_ping_when_the_cloud_check_is_not_set(monkeypatch):
    monkeypatch.setattr(settings, "HEALTHCHECK_CLOUD_URL", "")
    monkeypatch.setattr(alerts.httpx, "get", lambda *a, **k: pytest.fail("must not be called"))
    assert alerts.ping_cloud_healthcheck() is False and alerts.ping_cloud_healthcheck(fail=True) is False


def test_the_laptop_check_and_the_cloud_check_are_separate(monkeypatch):
    seen = []
    monkeypatch.setattr(alerts.httpx, "get", lambda url, timeout: seen.append(url) or SimpleNamespace(status_code=200))
    monkeypatch.setattr(settings, "HEALTHCHECK_URL", "https://hc-ping.com/laptop")
    monkeypatch.setattr(settings, "HEALTHCHECK_CLOUD_URL", "https://hc-ping.com/cloud/")
    alerts.ping_healthcheck()
    alerts.ping_cloud_healthcheck(fail=True)
    assert seen == ["https://hc-ping.com/laptop", "https://hc-ping.com/cloud/fail"]


def test_the_cloud_run_puts_failed_posts_back_and_sends_them_in_the_same_run(sync_db, session_factory, cloud):
    row_with(sync_db, schedule_on_youtube=False)
    now = datetime.now(UTC)
    job = failed_job(sync_db, failed_ago=timedelta(minutes=20), when=now - timedelta(hours=1))
    job.error_info = json.dumps({"error": "network", "failed_at": (now - timedelta(minutes=20)).isoformat()})
    sync_db.commit()
    summary = cp.run(now)
    assert summary["requeued"] == 1 and summary["claimed"] == 1
    assert reload(session_factory, job).status == m.PublishStatus.PUBLISHED


def test_the_laptop_alerts_when_the_cloud_publisher_has_been_silent(sync_db, notes):
    row = row_with(sync_db, schedule_on_youtube=False)
    row.last_cloud_run_at = NOW - timedelta(hours=3, minutes=1)
    sync_db.commit()
    ap.run_tick(sync_db, publish=Publish(), now=NOW, start=lambda d, u: None, notify=notes)
    assert any("cloud publisher has not run" in t for t in notes.messages)


def test_no_alert_when_the_cloud_ran_recently_or_never(sync_db, notes):
    row = row_with(sync_db, schedule_on_youtube=False)
    ap.run_tick(sync_db, publish=Publish(), now=NOW, start=lambda d, u: None, notify=notes)       # never ran
    row.last_cloud_run_at = NOW - timedelta(minutes=40)
    sync_db.commit()
    ap.run_tick(sync_db, publish=Publish(), now=NOW, start=lambda d, u: None, notify=notes)
    assert not any("cloud publisher" in t for t in notes.messages)


# ═══ Logs of the cloud job are public: no captions ══════════════════════════

def test_publish_logs_never_contain_the_caption(sync_db, session_factory, uploads, monkeypatch, caplog):
    row_with(sync_db, dry_run=True)
    job = make_job(sync_db, when=NOW - timedelta(minutes=1), caption="Top secret unposted caption")
    with caplog.at_level(logging.DEBUG):
        publish(session_factory, job)
    assert "DRY RUN" in caplog.text and "Top secret" not in caplog.text

    caplog.clear()
    row_with(sync_db, dry_run=False, schedule_on_youtube=False)
    job2 = make_job(sync_db, when=NOW - timedelta(minutes=1), caption="Another secret caption")
    with caplog.at_level(logging.DEBUG):
        publish(session_factory, job2)
        break_upload(monkeypatch, RuntimeError("boom"))
        job3 = make_job(sync_db, when=NOW - timedelta(minutes=1), caption="Third secret caption")
        run_final_try(job3)
    assert "secret caption" not in caplog.text.lower()


def test_alerts_to_the_private_chat_may_still_name_the_caption(sync_db, session_factory, failing, monkeypatch):
    row_with(sync_db, dry_run=False, schedule_on_youtube=False, auto_retry=False)
    job = make_job(sync_db, when=NOW - timedelta(minutes=1), status=m.PublishStatus.QUEUED, caption="Visible in my chat")
    break_upload(monkeypatch, RuntimeError("down"))
    run_final_try(job)
    assert "Visible in my chat" in failing[0]


# ═══ Workflows and migration ════════════════════════════════════════════════

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"


def test_the_keepalive_workflow_commits_monthly_and_can_be_run_by_hand():
    import yaml

    data = yaml.safe_load((WORKFLOWS / "keepalive.yml").read_text(encoding="utf-8"))
    trig = data.get("on") or data.get(True)
    assert trig["schedule"][0]["cron"] == "17 3 1 * *" and "workflow_dispatch" in trig
    assert data["permissions"] == {"contents": "write"}
    assert data["jobs"]["keepalive"]["if"] == "github.repository == 'AnuragNagare/Snipster'"
    script = data["jobs"]["keepalive"]["steps"][-1]["run"]
    assert ".github/keepalive.txt" in script and "git push" in script


def test_the_publisher_workflow_receives_the_cloud_check_url_as_a_secret():
    import yaml

    env = yaml.safe_load((WORKFLOWS / "publisher.yml").read_text(encoding="utf-8"))["jobs"]["publish"]["steps"][-1]["env"]
    assert env["HEALTHCHECK_CLOUD_URL"] == "${{ secrets.HEALTHCHECK_CLOUD_URL }}"


def test_migration_adds_the_three_columns():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == ["f3b7d92a1c58"]
    assert script.get_revision("f3b7d92a1c58").down_revision == "e5c2a8d41f96"


def test_a_post_that_was_retried_and_failed_again_meanwhile_is_not_requeued_on_the_old_count(sync_db, session_factory, notes):
    """Still FAILED, but with a newer try count and a fresh failure time: our stale view must not requeue it."""
    job = failed_job(sync_db, failed_ago=timedelta(hours=2))
    real = sync_db.scalars

    def scalars_then_it_fails_again(*a, **k):
        found = real(*a, **k).all()
        rival = session_factory()
        j = rival.get(m.PublishJob, job.id)
        j.retry_count = 1                                           # retried once and failed again, still FAILED
        rival.commit()
        return type("R", (), {"all": lambda self: found})()

    sync_db.scalars = scalars_then_it_fails_again
    assert ap.retry_failed_jobs(sync_db, NOW, notes) == 0
    sync_db.expire_all()
    saved = reload(session_factory, job)
    assert saved.status == m.PublishStatus.FAILED and saved.retry_count == 1


def test_the_retry_log_line_counts_the_try_correctly(sync_db, notes, caplog):
    failed_job(sync_db, failed_ago=timedelta(hours=5), retry_count=2)      # after 2 tries the wait is 4 hours
    with caplog.at_level(logging.INFO):
        ap.retry_failed_jobs(sync_db, NOW, notes)
    assert "(try 3)" in caplog.text and "(try 4)" not in caplog.text
