"""A5/A6: which clips are posted, when, dry run, the kill switch, and alerts."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.core.config import settings
from app.db import models as m
from app.services import alerts
from app.services import autopilot as ap

UTC = timezone.utc
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


class Notes:
    """Collects alert messages instead of sending them."""

    def __init__(self):
        self.messages = []

    def __call__(self, text, key=None, cooldown=0):
        self.messages.append(text)
        return True


@pytest.fixture
def notes():
    return Notes()


def clip(start=0, end=40, render=m.RenderStatus.DONE, caption="Great clip", key="clips/x.mp4",
         approval=m.ApprovalStatus.PENDING):
    return m.Clip(
        id=uuid.uuid4(), start_time=start, end_time=end, render_status=render, caption=caption,
        storage_key=key, approval_status=approval,
    )


# ─── The gate ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kwargs,fragment", [
    (dict(render=m.RenderStatus.FAILED), "render"),
    (dict(render=m.RenderStatus.RENDERING), "render"),
    (dict(key=None), "no rendered file"),
    (dict(caption=None), "no caption"),
    (dict(caption="   "), "no caption"),
    (dict(start=0, end=10), "too short"),
    (dict(start=0, end=75), "too long"),
])
def test_clips_that_fail_the_gate_are_named(kwargs, fragment):
    assert fragment in ap.check_clip(clip(**kwargs))


def test_good_clips_pass_and_the_limits_are_inclusive():
    assert ap.check_clip(clip()) is None
    assert ap.check_clip(clip(0, 15)) is None and ap.check_clip(clip(0, 60)) is None


def test_the_best_clips_are_chosen_closest_to_the_target_length():
    a, b, c = clip(0, 16), clip(100, 142), clip(200, 258)   # 16 s, 42 s, 58 s; target is 40 s
    chosen, reasons = ap.choose_clips([a, b, c], 2)
    assert chosen == [b, c] and reasons[a.id] == "not among the best 2"


def test_failing_clips_are_never_chosen_even_if_few_remain():
    good, bad = clip(0, 40), clip(100, 105)
    chosen, reasons = ap.choose_clips([bad, good], 2)
    assert chosen == [good] and "too short" in reasons[bad.id]


def test_a_clip_that_mostly_repeats_a_chosen_one_is_skipped():
    a, dup, other = clip(0, 40), clip(5, 45), clip(100, 140)
    chosen, reasons = ap.choose_clips([a, dup, other], 3)
    assert a in chosen and other in chosen and dup not in chosen
    assert reasons[dup.id] == "overlaps a clip that was chosen"


def test_touching_clips_are_not_overlaps():
    a, b = clip(0, 40), clip(40, 80)
    assert len(ap.choose_clips([a, b], 2)[0]) == 2


# ─── Post times ──────────────────────────────────────────────────────────────

def test_posts_are_spread_out_by_the_daily_rate():
    times = ap.plan_post_times(NOW, ZoneInfo("UTC"), 2, [], posts_per_day=3)
    assert times[0] == NOW + timedelta(minutes=5)
    assert times[1] - times[0] == timedelta(hours=8)


def test_the_daily_limit_pushes_posts_to_the_next_morning():
    existing = [NOW - timedelta(hours=2), NOW - timedelta(hours=1)]  # two already today, limit 3
    times = ap.plan_post_times(NOW, ZoneInfo("UTC"), 2, existing, posts_per_day=3, first_hour=9)
    assert times[0].date() == NOW.date()                      # the third slot today
    assert times[1] == datetime(2026, 11, 16, 9, 0, tzinfo=UTC)  # the fourth waits for tomorrow


def test_the_limit_counts_days_in_the_users_timezone():
    kolkata = ZoneInfo("Asia/Kolkata")
    now = datetime(2026, 11, 15, 20, 0, tzinfo=UTC)   # 01:30 on the 16th in India
    existing = [datetime(2026, 11, 15, 19, 0, tzinfo=UTC)]  # 00:30 on the 16th in India: today's post
    times = ap.plan_post_times(now, kolkata, 1, existing, posts_per_day=1, first_hour=9)
    # In UTC that post is on the 15th and the next one could go on the 16th; in India the 16th is full.
    assert times[0] == datetime(2026, 11, 17, 9, 0, tzinfo=kolkata).astimezone(UTC)


def test_new_posts_queue_behind_ones_already_scheduled():
    existing = [NOW + timedelta(hours=1)]
    times = ap.plan_post_times(NOW, ZoneInfo("UTC"), 1, existing, posts_per_day=3)
    assert times[0] == NOW + timedelta(hours=1) + timedelta(hours=8)


def test_a_limit_of_one_still_terminates():
    times = ap.plan_post_times(NOW, ZoneInfo("UTC"), 3, [], posts_per_day=1)
    assert len(times) == 3 and len({t.date() for t in times}) == 3


# ─── Finishing a video ───────────────────────────────────────────────────────

def ready_video(db, clips, picked_ago=timedelta(hours=1), video_status=m.VideoStatus.READY):
    """A picked video in state `video_status` with these (start, end, kwargs) clips; returns (source, clips)."""
    video = m.SourceVideo(ownership_confirmed=True, status=video_status)
    db.add(video)
    db.flush()
    made = []
    for spec in clips:
        c = m.Clip(source_video_id=video.id, start_time=spec[0], end_time=spec[1],
                   render_status=m.RenderStatus.DONE, storage_key="clips/x.mp4", caption="Caption",
                   approval_status=m.ApprovalStatus.PENDING, **(spec[2] if len(spec) > 2 else {}))
        db.add(c)
        made.append(c)
    src = m.AutopilotSource(youtube_video_id=uuid.uuid4().hex[:11], title="My talk", status="processing",
                            source_video_id=video.id, picked_at=NOW - picked_ago)
    db.add(src)
    db.commit()
    return src, made


def settings_row(db, **kw):
    row = ap.get_settings_row(db)
    for k, v in {"enabled": True, "auto_approve": True, **kw}.items():
        setattr(row, k, v)
    db.commit()
    return row


def test_auto_approve_picks_the_best_schedules_them_and_rejects_the_rest(sync_db, notes):
    src, (a, b, c) = ready_video(sync_db, [(0, 42), (100, 116), (200, 258)])   # 42 s, 16 s, 58 s
    row = settings_row(sync_db, clips_per_video=2)
    ap.finalize_ready_sources(sync_db, row, NOW, notes)

    assert (a.approval_status, b.approval_status, c.approval_status) == (
        m.ApprovalStatus.APPROVED, m.ApprovalStatus.REJECTED, m.ApprovalStatus.APPROVED)
    jobs = sync_db.query(m.PublishJob).order_by(m.PublishJob.scheduled_at).all()
    assert len(jobs) == 2 and all(j.status == m.PublishStatus.SCHEDULED and j.autopilot for j in jobs)
    assert {j.clip_id for j in jobs} == {a.id, c.id}
    assert src.status == "done" and "Scheduled 2" in src.reason
    assert notes.messages == []


def test_with_auto_approve_off_clips_are_left_for_review(sync_db, notes):
    src, (a, b) = ready_video(sync_db, [(0, 40), (100, 140)])
    row = settings_row(sync_db, auto_approve=False)
    ap.finalize_ready_sources(sync_db, row, NOW, notes)
    assert a.approval_status == b.approval_status == m.ApprovalStatus.PENDING
    assert sync_db.query(m.PublishJob).count() == 0
    assert src.status == "done" and "review" in src.reason


def test_it_waits_while_clips_are_still_rendering(sync_db, notes):
    src, (a, b) = ready_video(sync_db, [(0, 40), (100, 140)], picked_ago=timedelta(minutes=30))
    b.render_status = m.RenderStatus.RENDERING
    sync_db.commit()
    ap.finalize_ready_sources(sync_db, settings_row(sync_db), NOW, notes)
    assert src.status == "processing" and sync_db.query(m.PublishJob).count() == 0


def test_after_the_patience_runs_out_it_goes_ahead_with_what_passed(sync_db, notes):
    video_old = NOW - timedelta(hours=3)
    src, (a, b) = ready_video(sync_db, [(0, 40), (100, 140)])
    b.render_status = m.RenderStatus.FAILED
    sync_db.get(m.SourceVideo, a.source_video_id).updated_at = video_old
    sync_db.commit()
    ap.finalize_ready_sources(sync_db, settings_row(sync_db), NOW, notes)
    assert src.status == "done" and sync_db.query(m.PublishJob).count() == 1
    assert "render" in src.reason  # why the other clip was left out


def test_a_video_where_no_clip_passes_fails_and_alerts(sync_db, notes):
    src, _ = ready_video(sync_db, [(0, 8), (100, 105)])
    ap.finalize_ready_sources(sync_db, settings_row(sync_db), NOW, notes)
    assert src.status == "failed" and "too short" in src.reason
    assert len(notes.messages) == 1 and "My talk" in notes.messages[0]
    assert sync_db.query(m.PublishJob).count() == 0


def test_a_video_with_no_clips_fails_and_alerts(sync_db, notes):
    src, _ = ready_video(sync_db, [])
    ap.finalize_ready_sources(sync_db, settings_row(sync_db), NOW, notes)
    assert src.status == "failed" and len(notes.messages) == 1


def test_videos_still_processing_are_ignored(sync_db, notes):
    src, _ = ready_video(sync_db, [(0, 40)], video_status=m.VideoStatus.TRANSCRIBING)
    ap.finalize_ready_sources(sync_db, settings_row(sync_db), NOW, notes)
    assert src.status == "processing"


def test_finishing_twice_never_schedules_a_clip_twice(sync_db, notes):
    src, (a,) = ready_video(sync_db, [(0, 40)])
    row = settings_row(sync_db)
    ap.finalize_ready_sources(sync_db, row, NOW, notes)
    src.status = "processing"   # as if a second tick saw it again
    sync_db.commit()
    ap.finalize_ready_sources(sync_db, row, NOW, notes)
    assert sync_db.query(m.PublishJob).count() == 1


def test_the_daily_limit_counts_posts_that_already_exist(sync_db, notes):
    src, _ = ready_video(sync_db, [(0, 40), (100, 140)])
    old_video = m.SourceVideo(ownership_confirmed=True, status=m.VideoStatus.READY)
    sync_db.add(old_video)
    sync_db.flush()
    old_clip = m.Clip(source_video_id=old_video.id, start_time=0, end_time=30)
    sync_db.add(old_clip)
    sync_db.flush()
    sync_db.add(m.PublishJob(clip_id=old_clip.id, platform=m.Platform.YOUTUBE, status=m.PublishStatus.PUBLISHED,
                             published_at=NOW - timedelta(hours=1), created_at=NOW - timedelta(hours=1), retry_count=0))
    sync_db.commit()
    row = settings_row(sync_db, posts_per_day=2, clips_per_video=2)
    ap.finalize_ready_sources(sync_db, row, NOW, notes)
    times = sorted(j.scheduled_at.replace(tzinfo=UTC) for j in sync_db.query(m.PublishJob).filter(m.PublishJob.autopilot.is_(True)))
    assert times[0].date() == NOW.date() and times[1].date() > NOW.date()  # one fits today, one waits


# ─── Kill switch and dry run ─────────────────────────────────────────────────

def scheduled_job(db, autopilot, when):
    video = m.SourceVideo(ownership_confirmed=True, status=m.VideoStatus.READY)
    db.add(video)
    db.flush()
    c = m.Clip(source_video_id=video.id, start_time=0, end_time=30, storage_key="k", caption="c")
    db.add(c)
    db.flush()
    job = m.PublishJob(clip_id=c.id, platform=m.Platform.YOUTUBE, status=m.PublishStatus.SCHEDULED,
                       scheduled_at=when, autopilot=autopilot, retry_count=0)
    db.add(job)
    db.commit()
    return job


class Publish:
    def __init__(self):
        self.calls = []

    def delay(self, arg):
        self.calls.append(arg)


def test_turning_autopilot_off_stops_its_scheduled_posts_but_not_manual_ones(sync_db, notes):
    auto = scheduled_job(sync_db, True, NOW - timedelta(minutes=1))
    manual = scheduled_job(sync_db, False, NOW - timedelta(minutes=1))
    settings_row(sync_db, enabled=False)
    publish = Publish()
    ap.run_tick(sync_db, publish=publish, now=NOW, notify=notes)
    assert publish.calls == [str(manual.id)]
    sync_db.expire_all()
    assert auto.status == m.PublishStatus.SCHEDULED   # held, not lost
    settings_row(sync_db, enabled=True)
    ap.run_tick(sync_db, publish=publish, now=NOW, start=lambda db, u: None, notify=notes)
    assert str(auto.id) in publish.calls              # goes out once it is switched back on


def test_a_disabled_autopilot_does_not_approve_clips(sync_db, notes):
    src, (a,) = ready_video(sync_db, [(0, 40)])
    settings_row(sync_db, enabled=False)
    ap.run_tick(sync_db, publish=Publish(), now=NOW, notify=notes)
    assert a.approval_status == m.ApprovalStatus.PENDING and src.status == "processing"


def test_force_picks_now_without_using_up_the_slot(sync_db, notes):
    sync_db.add(m.User(yt_channel_id="UCabc"))
    sync_db.commit()
    settings_row(sync_db, auto_approve=False)
    picked = SimpleNamespace(youtube_video_id="v9")
    summary = ap.run_tick(sync_db, publish=Publish(), now=NOW - timedelta(hours=5), start=lambda db, u: picked,
                          notify=notes, force=True)
    assert summary["picked"] == "v9" and ap.get_settings_row(sync_db).last_run_slot is None


def run_publish(monkeypatch, session_factory, job_id, upload):
    from app.workers import tasks
    from app.services.youtube import youtube_service

    monkeypatch.setattr(tasks, "_get_sync_session", session_factory)
    monkeypatch.setattr(youtube_service, "upload", upload)
    monkeypatch.setattr(alerts, "send", lambda *a, **k: True)
    return tasks.publish_clip_task.apply(args=[str(job_id)])


def test_dry_run_never_calls_youtube_for_autopilot_posts(sync_db, session_factory, monkeypatch):
    sync_db.add(m.User(yt_access_token="x"))
    settings_row(sync_db, dry_run=True)
    job = scheduled_job(sync_db, True, NOW)
    uploads = []
    run_publish(monkeypatch, session_factory, job.id, lambda **k: uploads.append(k))
    saved = session_factory().get(m.PublishJob, job.id)
    assert uploads == [] and saved.status == m.PublishStatus.PUBLISHED
    assert saved.platform_post_id == "dry-run" and saved.platform_post_url is None


def test_dry_run_never_blocks_a_post_you_start_yourself(sync_db, session_factory, monkeypatch):
    sync_db.add(m.User(yt_access_token="x"))
    settings_row(sync_db, dry_run=True)
    job = scheduled_job(sync_db, False, NOW)
    uploads = []

    def upload(clip, user, privacy, credit=None, publish_at=None):
        uploads.append(privacy)
        return {"post_id": "real1", "post_url": "https://www.youtube.com/shorts/real1"}

    run_publish(monkeypatch, session_factory, job.id, upload)
    assert uploads == ["private"]
    assert session_factory().get(m.PublishJob, job.id).platform_post_id == "real1"


def test_with_dry_run_off_autopilot_posts_really_upload(sync_db, session_factory, monkeypatch):
    sync_db.add(m.User(yt_access_token="x"))
    settings_row(sync_db, dry_run=False, privacy="unlisted")
    job = scheduled_job(sync_db, True, NOW)
    seen = []

    def upload(clip, user, privacy, credit=None, publish_at=None):
        seen.append(privacy)
        return {"post_id": "v7", "post_url": "https://www.youtube.com/shorts/v7"}

    run_publish(monkeypatch, session_factory, job.id, upload)
    assert seen == ["unlisted"]


# ─── Double-post protection ──────────────────────────────────────────────────

def test_a_job_that_already_has_a_youtube_id_is_never_uploaded_again(sync_db, session_factory, monkeypatch):
    sync_db.add(m.User(yt_access_token="x"))
    job = scheduled_job(sync_db, False, NOW)
    job.platform_post_id, job.status = "abc", m.PublishStatus.QUEUED
    sync_db.commit()
    uploads = []
    run_publish(monkeypatch, session_factory, job.id, lambda **k: uploads.append(k))
    assert uploads == [] and session_factory().get(m.PublishJob, job.id).status == m.PublishStatus.PUBLISHED


def test_an_upload_that_cannot_be_saved_is_not_retried(sync_db, session_factory, monkeypatch):
    from app.workers import tasks

    sync_db.add(m.User(yt_access_token="x"))
    job = scheduled_job(sync_db, False, NOW)
    messages = []
    monkeypatch.setattr(tasks, "_save_published", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    from app.services.youtube import youtube_service

    monkeypatch.setattr(tasks, "_get_sync_session", session_factory)
    monkeypatch.setattr(youtube_service, "upload", lambda **k: {"post_id": "v1", "post_url": "u"})
    monkeypatch.setattr(alerts, "send", lambda text, **k: messages.append(text) or True)
    result = tasks.publish_clip_task.apply(args=[str(job.id)])
    assert result.successful()   # finished: no retry that would upload again
    saved = session_factory().get(m.PublishJob, job.id)
    assert saved.status == m.PublishStatus.FAILED and "Do NOT retry" in saved.error_info
    assert any("v1" in text and "on YouTube" in text for text in messages)


def test_the_final_failure_raises_an_alert_but_earlier_ones_do_not(sync_db, session_factory, monkeypatch):
    from app.workers import tasks
    from app.services.youtube import youtube_service

    sync_db.add(m.User(yt_access_token="x"))
    job = scheduled_job(sync_db, False, NOW)
    messages = []
    monkeypatch.setattr(tasks, "_get_sync_session", session_factory)
    monkeypatch.setattr(youtube_service, "upload", lambda **k: (_ for _ in ()).throw(RuntimeError("quota")))
    monkeypatch.setattr(alerts, "send", lambda text, **k: messages.append(text) or True)
    monkeypatch.setattr(tasks.publish_clip_task, "retry", lambda **k: (_ for _ in ()).throw(RuntimeError("retry")))

    tasks.publish_clip_task.apply(args=[str(job.id)], retries=0)
    assert messages == []
    tasks.publish_clip_task.apply(args=[str(job.id)], retries=tasks.publish_clip_task.max_retries)
    assert len(messages) == 1 and "failed after" in messages[0] and "quota" in messages[0]


# ─── Alerts ──────────────────────────────────────────────────────────────────

@pytest.fixture
def telegram(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(alerts, "_last_sent", {})
    sent = []

    def post(url, json, timeout):
        sent.append((url, json))
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(alerts.httpx, "post", post)
    return sent


def test_an_alert_goes_to_the_users_chat(telegram):
    assert alerts.send("a video failed") is True
    url, body = telegram[0]
    assert url.endswith("/bot123:SECRET/sendMessage") and body["chat_id"] == "42"
    assert body["text"] == "Snipster: a video failed"


def test_alerts_without_telegram_set_up_are_only_logged(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "")
    assert alerts.send("hello") is False


def test_a_cooldown_stops_repeats_of_the_same_alert(telegram):
    assert alerts.send("tick failed", key="tick", cooldown=3600) is True
    assert alerts.send("tick failed", key="tick", cooldown=3600) is False
    assert alerts.send("other problem", key="other", cooldown=3600) is True
    assert len(telegram) == 2


def test_a_failed_send_never_raises_and_never_leaks_the_token(telegram, monkeypatch, caplog):
    def boom(url, json, timeout):
        raise ConnectionError(f"cannot reach {url}")

    monkeypatch.setattr(alerts.httpx, "post", boom)
    with caplog.at_level("WARNING"):
        assert alerts.send("x") is False
    assert "SECRET" not in caplog.text


def test_a_telegram_error_reply_is_a_quiet_false(telegram, monkeypatch):
    monkeypatch.setattr(alerts.httpx, "post", lambda *a, **k: SimpleNamespace(status_code=401))
    assert alerts.send("x") is False


def test_the_deadmans_switch_is_pinged_only_when_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(alerts.httpx, "get", lambda url, timeout: calls.append(url) or SimpleNamespace(status_code=200))
    monkeypatch.setattr(settings, "HEALTHCHECK_URL", "")
    assert alerts.ping_healthcheck() is False and calls == []
    monkeypatch.setattr(settings, "HEALTHCHECK_URL", "https://hc-ping.com/abc")
    assert alerts.ping_healthcheck() is True and calls == ["https://hc-ping.com/abc"]


def test_a_dead_ping_url_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "HEALTHCHECK_URL", "https://hc-ping.com/abc")
    monkeypatch.setattr(alerts.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("x")))
    assert alerts.ping_healthcheck() is False


def test_a_failed_pipeline_alerts_once_and_names_the_video(sync_db, notes):
    video = m.SourceVideo(ownership_confirmed=True, status=m.VideoStatus.FAILED, error_message="download blocked")
    sync_db.add(video)
    sync_db.flush()
    sync_db.add(m.AutopilotSource(youtube_video_id="v1", title="My talk", status="processing",
                                  source_video_id=video.id, picked_at=NOW - timedelta(hours=1)))
    sync_db.commit()
    ap.sync_source_statuses(sync_db, NOW, notes)
    ap.sync_source_statuses(sync_db, NOW, notes)   # already failed: no second alert
    assert len(notes.messages) == 1 and "My talk" in notes.messages[0] and "download blocked" in notes.messages[0]


def test_setup_problems_alert_with_a_cooldown_key(sync_db):
    sync_db.add(m.User())
    sync_db.commit()
    settings_row(sync_db)
    seen = []
    ap.run_tick(sync_db, publish=Publish(), now=NOW,
                start=lambda db, u: (_ for _ in ()).throw(ap.AutopilotConfigError("no key")),
                notify=lambda text, key=None, cooldown=0: seen.append((key, cooldown)))
    assert seen == [("config", 6 * 3600)]


def test_the_tick_task_pings_after_a_healthy_tick_and_alerts_on_a_broken_one(session_factory, monkeypatch):
    from app.workers import tasks

    monkeypatch.setattr(tasks, "_get_sync_session", session_factory)
    pings, sent = [], []
    monkeypatch.setattr(alerts, "ping_healthcheck", lambda: pings.append(1))
    monkeypatch.setattr(alerts, "send", lambda text, **k: sent.append(text))
    assert tasks.autopilot_tick_task.apply().successful()
    assert pings == [1] and sent == []

    monkeypatch.setattr(ap, "run_tick", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert tasks.autopilot_tick_task.apply().successful()
    assert pings == [1] and len(sent) == 1 and "boom" in sent[0]   # no ping for a broken tick


def test_spacing_never_lands_in_the_small_hours():
    # 3 posts a day => 8 h apart: 22:00 + 8 h would be 06:00, which waits for 09:00 instead
    late = datetime(2026, 11, 15, 21, 55, tzinfo=UTC)
    times = ap.plan_post_times(late, ZoneInfo("UTC"), 2, [], posts_per_day=3, first_hour=9)
    assert times[0].hour == 22 and times[1] == datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


def test_a_full_day_is_never_exceeded_even_when_spacing_would_allow_another_post():
    # three posts already went out this morning (e.g. by hand); spacing alone would allow 18:00 today
    existing = [datetime(2026, 11, 15, h, 0, tzinfo=UTC) for h in (8, 9, 10)]
    now = datetime(2026, 11, 15, 11, 0, tzinfo=UTC)
    times = ap.plan_post_times(now, ZoneInfo("UTC"), 1, existing, posts_per_day=3, first_hour=9)
    assert times == [datetime(2026, 11, 16, 9, 0, tzinfo=UTC)]
