"""Creative Commons picker, fixed post times, backlog guard, and the credit carried by every CC post."""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.core.config import settings
from app.db import models as m
from app.services import autopilot as ap
from app.services.autopilot import AutopilotConfigError

UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)          # 17:30 in India


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setattr(settings, "YOUTUBE_API_KEY", "key")
    monkeypatch.setattr(settings, "AUTOPILOT_MIN_DURATION", 180)
    monkeypatch.setattr(settings, "AUTOPILOT_MAX_DURATION", 3600)
    monkeypatch.setattr(settings, "AUTOPILOT_CC_MIN_VIEWS", 5000)
    monkeypatch.setattr(settings, "AUTOPILOT_CC_DAYS", 30)
    monkeypatch.setattr(settings, "YOUTUBE_CHANNEL_ID", "")


def cc(vid, views=50_000, likes=1_000, duration=600.0, **over):
    base = {
        "id": vid, "title": f"Title {vid}", "published_at": "2026-11-10T00:00:00Z", "duration": duration,
        "live": False, "public": True, "license": "creativeCommon", "made_for_kids": False,
        "channel_id": "UCother", "channel_title": "Some Creator", "category": "22", "views": views, "likes": likes, "language": "en",
    }
    base.update(over)
    return base


# ─── Choosing a Creative Commons video ───────────────────────────────────────

def test_the_most_popular_video_wins_and_likes_count():
    pool = [cc("a", views=100_000, likes=100), cc("b", views=60_000, likes=3_000), cc("c", views=10_000, likes=10)]
    # a: 100000 + 5000 = 105000, b: 60000 + 150000 = 210000
    assert ap.select_cc_candidate(pool, set())["id"] == "b"


@pytest.mark.parametrize("bad", [
    dict(license="youtube"),                 # a normal licence: never reusable
    dict(license=""),
    dict(public=False),
    dict(live=True),
    dict(made_for_kids=True),
    dict(duration=90.0),                     # a Short
    dict(duration=7200.0),
    dict(views=4_999),                       # not popular enough
    dict(language="hi"),                     # wrong language for an English search
    dict(category="10"),                     # music: a CC label there is often wrong
    dict(category="24"),                     # entertainment
    dict(category="43"),                     # TV shows
])
def test_ineligible_videos_are_never_chosen(bad):
    assert ap.select_cc_candidate([cc("x", **bad)], set()) is None


def test_the_licence_is_rechecked_even_if_the_search_said_creative_commons():
    """The pick must trust the video's own record, not the search filter."""
    assert ap.select_cc_candidate([cc("x", license="youtube")], set()) is None


def test_used_videos_and_the_users_own_channel_are_skipped():
    pool = [cc("used", views=900_000), cc("mine", views=800_000, channel_id="UCmine"), cc("ok", views=10_000)]
    assert ap.select_cc_candidate(pool, {"used"}, own_channel_id="UCmine")["id"] == "ok"


def test_a_video_with_no_language_tag_is_allowed_and_a_matching_one_too():
    assert ap.select_cc_candidate([cc("x", language="")], set()) is not None
    assert ap.select_cc_candidate([cc("x", language="en-US")], set(), language="en") is not None
    assert ap.select_cc_candidate([cc("x", language="hi")], set(), language="hi") is not None


def test_educational_and_other_categories_are_allowed():
    for category in ("22", "27", "28", "26", ""):
        assert ap.select_cc_candidate([cc("x", category=category)], set()) is not None


def test_the_blocked_categories_can_be_changed(monkeypatch):
    monkeypatch.setattr(settings, "AUTOPILOT_CC_BLOCK_CATEGORIES", "22")
    assert ap.select_cc_candidate([cc("x", category="22")], set()) is None
    assert ap.select_cc_candidate([cc("x", category="10")], set()) is not None


def test_nothing_eligible_gives_none():
    assert ap.select_cc_candidate([], set()) is None


# ─── Searching YouTube ───────────────────────────────────────────────────────

class FakeYouTube:
    def __init__(self, search_ids, details):
        self.search_ids, self.details, self.calls = search_ids, details, []

    def get(self, url, params, timeout):
        path = url.rsplit("/", 1)[1]
        self.calls.append((path, dict(params)))
        if path == "search":
            ids = self.search_ids[params["videoDuration"]]
            body = {"items": [{"id": {"videoId": v}} for v in ids]}
        else:
            wanted = params["id"].split(",")
            body = {"items": [self.details[v] for v in wanted if v in self.details]}
        return SimpleNamespace(status_code=200, json=lambda: body, text="")


def detail(vid, views=20_000, likes=500, license="creativeCommon", duration="PT10M", **snip):
    return {
        "id": vid,
        "snippet": {"title": f"T {vid}", "channelId": "UCc", "channelTitle": "Chan", "publishedAt": "2026-11-12T00:00:00Z",
                    "liveBroadcastContent": "none", "defaultAudioLanguage": "en", "categoryId": "22", **snip},
        "contentDetails": {"duration": duration},
        "statistics": {"viewCount": str(views), "likeCount": str(likes)},
        "status": {"privacyStatus": "public", "license": license, "madeForKids": False},
    }


def test_the_search_asks_for_cc_recent_popular_videos_in_two_length_buckets(monkeypatch):
    fake = FakeYouTube({"medium": ["a"], "long": ["b"]}, {"a": detail("a"), "b": detail("b", duration="PT30M")})
    monkeypatch.setattr(ap.httpx, "get", fake.get)
    found = ap.fetch_cc_candidates("en", now=NOW)
    searches = [p for path, p in fake.calls if path == "search"]
    assert [p["videoDuration"] for p in searches] == ["medium", "long"]
    assert all(p["videoLicense"] == "creativeCommon" and p["order"] == "viewCount" and p["type"] == "video" for p in searches)
    assert searches[0]["publishedAfter"] == "2026-10-18T12:00:00Z"        # 30 days before NOW
    assert searches[0]["relevanceLanguage"] == "en" and "regionCode" not in searches[0]
    assert [v["id"] for v in found] == ["a", "b"] and found[1]["duration"] == 1800
    assert found[0]["license"] == "creativeCommon" and found[0]["channel_title"] == "Chan" and found[0]["views"] == 20_000
    assert found[0]["category"] == "22"


def test_hindi_searches_are_limited_to_india(monkeypatch):
    fake = FakeYouTube({"medium": [], "long": []}, {})
    monkeypatch.setattr(ap.httpx, "get", fake.get)
    ap.fetch_cc_candidates("hi", now=NOW)
    assert fake.calls[0][1]["regionCode"] == "IN" and fake.calls[0][1]["relevanceLanguage"] == "hi"


def test_a_video_found_by_both_searches_is_fetched_once(monkeypatch):
    fake = FakeYouTube({"medium": ["a"], "long": ["a"]}, {"a": detail("a")})
    monkeypatch.setattr(ap.httpx, "get", fake.get)
    assert len(ap.fetch_cc_candidates("en", now=NOW)) == 1


def test_a_quota_error_is_raised(monkeypatch):
    monkeypatch.setattr(ap.httpx, "get", lambda *a, **k: SimpleNamespace(status_code=403, text="quota", json=dict))
    with pytest.raises(RuntimeError, match="403"):
        ap.fetch_cc_candidates("en", now=NOW)


# ─── Starting a Creative Commons source ──────────────────────────────────────

@pytest.fixture
def pipeline(monkeypatch):
    calls = []
    monkeypatch.setattr("app.workers.pipeline.start_processing_pipeline",
                        lambda video_id, from_url=False: calls.append((video_id, from_url)))
    return calls


def set_mode(db, **kw):
    row = ap.get_settings_row(db)
    for k, v in kw.items():
        setattr(row, k, v)
    db.commit()
    return row


def test_a_cc_pick_records_the_creator_and_is_not_marked_as_owned(sync_db, pipeline):
    set_mode(sync_db, source_mode="creative_commons", source_language="en")
    user = m.User(yt_channel_id="")
    sync_db.add(user)
    sync_db.commit()
    src = ap.start_next_source(sync_db, user, fetch_cc=lambda lang: [cc("v1", channel_title="Great Creator")])
    video = sync_db.get(m.SourceVideo, src.source_video_id)
    assert (src.license, src.channel_title, src.views, src.likes) == ("creativeCommon", "Great Creator", 50_000, 1_000)
    assert video.ownership_confirmed is False and video.youtube_url.endswith("v=v1")
    assert pipeline == [(str(video.id), True)]


def test_cc_mode_needs_no_own_channel_but_own_mode_does(sync_db, pipeline):
    user = m.User(yt_channel_id="")
    sync_db.add(user)
    sync_db.commit()
    set_mode(sync_db, source_mode="creative_commons")
    assert ap.start_next_source(sync_db, user, fetch_cc=lambda lang: [cc("v1")]) is not None
    set_mode(sync_db, source_mode="own")
    with pytest.raises(AutopilotConfigError, match="channel is unknown"):
        ap.start_next_source(sync_db, user, fetch=lambda ch: [])


def test_cc_mode_passes_the_chosen_language_and_never_reads_the_own_channel(sync_db, pipeline):
    set_mode(sync_db, source_mode="creative_commons", source_language="hi")
    seen = []
    user = m.User(yt_channel_id="UCmine")
    sync_db.add(user)
    sync_db.commit()
    ap.start_next_source(sync_db, user, fetch=lambda ch: seen.append("own") or [],
                         fetch_cc=lambda lang: seen.append(lang) or [cc("v1", language="hi")])
    assert seen == ["hi"]


def test_own_mode_still_marks_the_video_as_owned(sync_db, pipeline):
    user = m.User(yt_channel_id="UCmine")
    sync_db.add(user)
    sync_db.commit()
    own = {"id": "v1", "title": "Mine", "published_at": "2025-01-01", "duration": 600.0, "live": False, "public": True}
    src = ap.start_next_source(sync_db, user, fetch=lambda ch: [own])
    assert sync_db.get(m.SourceVideo, src.source_video_id).ownership_confirmed is True
    assert src.license is None and src.channel_title is None


def test_no_cc_video_available_gives_none(sync_db, pipeline):
    set_mode(sync_db, source_mode="creative_commons")
    assert ap.start_next_source(sync_db, m.User(), fetch_cc=lambda lang: []) is None and pipeline == []


# ─── The credit ──────────────────────────────────────────────────────────────

def test_a_cc_source_gets_a_credit_with_title_creator_and_link():
    src = m.AutopilotSource(youtube_video_id="abc123", title="Cool talk", channel_title="Great Creator", license="creativeCommon")
    text = ap.credit_line(src)
    assert 'Original video: "Cool talk" by Great Creator' in text
    assert "https://www.youtube.com/watch?v=abc123" in text and "Creative Commons Attribution" in text


def test_own_videos_and_no_source_get_no_credit():
    assert ap.credit_line(None) is None
    assert ap.credit_line(m.AutopilotSource(youtube_video_id="x", title="t")) is None


def run_publish(monkeypatch, session_factory, job_id, upload):
    from app.services import alerts
    from app.services.youtube import youtube_service
    from app.workers import tasks

    monkeypatch.setattr(tasks, "_get_sync_session", session_factory)
    monkeypatch.setattr(youtube_service, "upload", upload)
    monkeypatch.setattr(alerts, "send", lambda *a, **k: True)
    return tasks.publish_clip_task.apply(args=[str(job_id)])


def make_job(db, source_kwargs):
    db.add(m.User(yt_access_token="x"))
    video = m.SourceVideo(ownership_confirmed=False, status=m.VideoStatus.READY)
    db.add(video)
    db.flush()
    clip = m.Clip(source_video_id=video.id, start_time=0, end_time=30, storage_key="k", caption="Caption")
    db.add(clip)
    db.add(m.AutopilotSource(youtube_video_id="srcvid", title="Cool talk", status="done", source_video_id=video.id, **source_kwargs))
    db.flush()
    job = m.PublishJob(clip_id=clip.id, platform=m.Platform.YOUTUBE, status=m.PublishStatus.QUEUED, autopilot=True, retry_count=0)
    db.add(job)
    db.commit()
    return job


def test_the_publish_task_passes_the_credit_for_cc_sources(sync_db, session_factory, monkeypatch):
    sync_db.add(m.AutopilotSettings(id=1, dry_run=False))
    job = make_job(sync_db, dict(license="creativeCommon", channel_title="Great Creator"))
    seen = []
    run_publish(monkeypatch, session_factory, job.id,
                lambda clip, user, privacy, credit=None, publish_at=None: seen.append(credit) or {"post_id": "p", "post_url": "u"})
    assert seen and "Great Creator" in seen[0] and "srcvid" in seen[0]


def test_the_publish_task_passes_no_credit_for_own_videos(sync_db, session_factory, monkeypatch):
    sync_db.add(m.AutopilotSettings(id=1, dry_run=False))
    job = make_job(sync_db, {})
    seen = []
    run_publish(monkeypatch, session_factory, job.id,
                lambda clip, user, privacy, credit=None, publish_at=None: seen.append(credit) or {"post_id": "p", "post_url": "u"})
    assert seen == [None]


def test_the_credit_is_written_into_the_youtube_description(monkeypatch):
    import json
    from app.core.security import encrypt_token
    from app.services import youtube as yt
    from cryptography.fernet import Fernet

    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(yt.storage_service, "download_to_file", lambda key, path: open(path, "wb").write(b"v"))
    sent = {}

    def post(url, **kw):
        sent["meta"] = json.loads(kw["files"]["metadata"][1])
        return SimpleNamespace(status_code=200, json=lambda: {"id": "p"}, text="")

    monkeypatch.setattr(yt.httpx, "post", post)
    user = SimpleNamespace(yt_access_token=encrypt_token("t"), yt_refresh_token=None, yt_token_expiry=None)
    clip = SimpleNamespace(storage_key="k", caption="Cap", hashtags="#a")
    yt.youtube_service.upload(clip, user, credit="Original video: X by Y")
    d = sent["meta"]["snippet"]["description"]
    assert "Original video: X by Y" in d and d.startswith("Cap") and d.rstrip().endswith("#Shorts")
    yt.youtube_service.upload(clip, user)
    assert "Original video" not in sent["meta"]["snippet"]["description"]


# ─── Fixed post times ────────────────────────────────────────────────────────

def test_post_times_are_parsed_sorted_and_cleaned():
    assert [f"{t:%H:%M}" for t in ap.parse_post_times("19:45, 12:15, 7:05")] == ["07:05", "12:15", "19:45"]
    assert ap.parse_post_times("25:00, 12:60, x, 19-45, ") == []
    assert ap.parse_post_times("") == [] and ap.parse_post_times(None) == []


def t(h, mi=0):
    return datetime.combine(datetime(2026, 11, 17).date(), datetime.min.time().replace(hour=h, minute=mi))


def slots(*texts):
    return ap.parse_post_times(",".join(texts))


def ist(day, h, mi=0):
    return datetime(2026, 11, day, h, mi, tzinfo=IST).astimezone(UTC)


def test_posts_go_out_at_the_fixed_times_in_the_users_timezone():
    # 17:30 IST now; evening slot is today at 19:45, then the next days
    times = ap.plan_slot_times(NOW, IST, 3, [], slots("19:45"), posts_per_day=2)
    assert times == [ist(17, 19, 45), ist(18, 19, 45), ist(19, 19, 45)]


def test_two_slots_fill_a_day_in_order():
    times = ap.plan_slot_times(NOW, IST, 3, [], slots("19:45", "12:15"), posts_per_day=3)
    assert times == [ist(17, 19, 45), ist(18, 12, 15), ist(18, 19, 45)]   # 12:15 today is already past


def test_a_slot_that_already_passed_or_is_too_close_is_skipped():
    almost = datetime(2026, 11, 17, 14, 14, tzinfo=UTC)                  # 19:44 IST: the 19:45 slot is 1 min away
    assert ap.plan_slot_times(almost, IST, 1, [], slots("19:45"), 3) == [ist(18, 19, 45)]


def test_the_daily_limit_is_respected_with_existing_posts():
    existing = [ist(17, 19, 45)]                                         # today's evening slot is taken
    times = ap.plan_slot_times(NOW, IST, 2, existing, slots("19:45", "21:00"), posts_per_day=1)
    assert times == [ist(18, 19, 45), ist(19, 19, 45)]                   # one per day, tomorrow and the day after


def test_a_taken_slot_is_not_reused_but_the_next_one_is():
    existing = [ist(17, 19, 50)]                                         # 5 minutes from the slot
    times = ap.plan_slot_times(NOW, IST, 1, existing, slots("19:45", "21:30"), posts_per_day=3)
    assert times == [ist(17, 21, 30)]


def test_new_posts_queue_behind_future_ones_across_calls():
    first = ap.plan_slot_times(NOW, IST, 2, [], slots("19:45"), 3)
    second = ap.plan_slot_times(NOW, IST, 2, first, slots("19:45"), 3)
    assert second == [ist(19, 19, 45), ist(20, 19, 45)]


def test_no_slots_gives_no_times():
    assert ap.plan_slot_times(NOW, IST, 2, [], [], 3) == []


# ─── Finalizing uses fixed times ─────────────────────────────────────────────

def ready_video(db, specs):
    video = m.SourceVideo(ownership_confirmed=False, status=m.VideoStatus.READY)
    db.add(video)
    db.flush()
    for a, b in specs:
        db.add(m.Clip(source_video_id=video.id, start_time=a, end_time=b, render_status=m.RenderStatus.DONE,
                      storage_key="k", caption="c", approval_status=m.ApprovalStatus.PENDING))
    src = m.AutopilotSource(youtube_video_id=uuid.uuid4().hex[:11], title="T", status="processing",
                            source_video_id=video.id, picked_at=NOW - timedelta(hours=1))
    db.add(src)
    db.commit()
    return src


def test_finalize_schedules_clips_at_the_users_fixed_times(sync_db):
    set_mode(sync_db, enabled=True, auto_approve=True, post_times="19:45", timezone="Asia/Kolkata", clips_per_video=2, posts_per_day=2)
    src = ready_video(sync_db, [(0, 40), (100, 140)])
    ap.finalize_ready_sources(sync_db, ap.get_settings_row(sync_db), NOW, lambda *a, **k: True)
    got = sorted(j.scheduled_at.replace(tzinfo=UTC) for j in sync_db.query(m.PublishJob))
    assert got == [ist(17, 19, 45), ist(18, 19, 45)] and src.status == "done"


def test_finalize_still_spreads_posts_when_no_times_are_set(sync_db):
    set_mode(sync_db, enabled=True, auto_approve=True, post_times="", clips_per_video=2, posts_per_day=3)
    ready_video(sync_db, [(0, 40), (100, 140)])
    ap.finalize_ready_sources(sync_db, ap.get_settings_row(sync_db), NOW, lambda *a, **k: True)
    got = sorted(j.scheduled_at.replace(tzinfo=UTC) for j in sync_db.query(m.PublishJob))
    assert got[0] == NOW + timedelta(minutes=5) and got[1] - got[0] == timedelta(hours=8)


# ─── Backlog guard ───────────────────────────────────────────────────────────

def waiting_jobs(db, n):
    for _ in range(n):
        video = m.SourceVideo(ownership_confirmed=False, status=m.VideoStatus.READY)
        db.add(video)
        db.flush()
        clip = m.Clip(source_video_id=video.id, start_time=0, end_time=30)
        db.add(clip)
        db.flush()
        db.add(m.PublishJob(clip_id=clip.id, platform=m.Platform.YOUTUBE, status=m.PublishStatus.SCHEDULED,
                            scheduled_at=NOW + timedelta(days=1), autopilot=True, retry_count=0))
    db.commit()


class Publish:
    def delay(self, arg):
        pass


def test_no_new_video_is_picked_while_enough_posts_are_waiting(sync_db):
    set_mode(sync_db, enabled=True, posts_per_day=2)
    waiting_jobs(sync_db, 4)
    picks = []
    summary = ap.run_tick(sync_db, publish=Publish(), now=NOW + timedelta(hours=3),
                          start=lambda db, u: picks.append(1), notify=lambda *a, **k: True)
    assert picks == [] and summary["note"] == "enough posts are already waiting"
    assert ap.get_settings_row(sync_db).last_run_slot is None            # the run slot is kept for later


def test_a_smaller_backlog_does_not_block_picking(sync_db):
    set_mode(sync_db, enabled=True, posts_per_day=2)
    waiting_jobs(sync_db, 3)
    picks = []
    ap.run_tick(sync_db, publish=Publish(), now=NOW + timedelta(hours=3),
                start=lambda db, u: picks.append(1) or None, notify=lambda *a, **k: True)
    assert picks == [1]


# ─── Migration ───────────────────────────────────────────────────────────────

def test_migration_adds_the_new_columns_after_the_previous_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == ["f3b7d92a1c58"]
    assert script.get_revision("e5c2a8d41f96").down_revision == "d91b3c6e8a47"


# ─── Videos without speech ───────────────────────────────────────────────────

def run_transcribe(monkeypatch, session_factory, video_id, words, language="en"):
    from app.services import media_cache
    from app.services.transcription import transcription_service
    from app.workers import tasks

    queued = []
    monkeypatch.setattr(tasks, "_get_sync_session", session_factory)
    monkeypatch.setattr(media_cache, "get_local_source", lambda vid, key: "x.mp4")
    monkeypatch.setattr(media_cache, "enabled", lambda: True)
    monkeypatch.setattr(transcription_service, "transcribe", lambda path: (words, language))
    monkeypatch.setattr(tasks.select_clips_task, "delay", lambda vid: queued.append(vid))
    result = tasks.transcribe_video_task.apply(args=[str(video_id)])
    return result, queued


def spoken(n):
    return [{"start": float(i), "end": i + 0.4, "text": "word"} for i in range(n)]


def new_video(db):
    video = m.SourceVideo(ownership_confirmed=False, status=m.VideoStatus.PENDING, storage_key="k")
    db.add(video)
    db.commit()
    return video


def test_a_video_with_almost_no_speech_is_failed_not_clipped(sync_db, session_factory, monkeypatch):
    video = new_video(sync_db)
    result, queued = run_transcribe(monkeypatch, session_factory, video.id, spoken(0))
    assert result.successful() and queued == []                   # finished: no retry, no clip selection
    saved = session_factory().get(m.SourceVideo, video.id)
    assert saved.status == m.VideoStatus.FAILED
    assert saved.error_message.startswith("Almost no speech in this video (0 words found")


def test_the_limit_is_exact(sync_db, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "MIN_TRANSCRIPT_WORDS", 60)
    short, long_ = new_video(sync_db), new_video(sync_db)
    _, q1 = run_transcribe(monkeypatch, session_factory, short.id, spoken(59))
    _, q2 = run_transcribe(monkeypatch, session_factory, long_.id, spoken(60))
    assert q1 == [] and q2 == [str(long_.id)]
    assert session_factory().get(m.SourceVideo, long_.id).status == m.VideoStatus.SELECTING_CLIPS


def test_a_video_with_speech_continues_as_before(sync_db, session_factory, monkeypatch):
    video = new_video(sync_db)
    _, queued = run_transcribe(monkeypatch, session_factory, video.id, spoken(500), language="hi")
    saved = session_factory().get(m.SourceVideo, video.id)
    assert queued == [str(video.id)] and saved.language == "hi" and saved.status == m.VideoStatus.SELECTING_CLIPS


# ─── A failed video frees the run slot ───────────────────────────────────────

def failed_pick(db, hours_ago=1):
    video = m.SourceVideo(ownership_confirmed=False, status=m.VideoStatus.FAILED, error_message="Almost no speech")
    db.add(video)
    db.flush()
    db.add(m.AutopilotSource(youtube_video_id=uuid.uuid4().hex[:11], title="Silent", status="processing",
                             source_video_id=video.id, picked_at=NOW - timedelta(hours=hours_ago)))
    db.commit()


def test_a_failed_video_lets_the_same_run_slot_pick_the_next_candidate(sync_db):
    row = set_mode(sync_db, enabled=True)
    row.last_run_slot = "2026-11-17T09"
    sync_db.commit()
    failed_pick(sync_db)
    picks = []
    summary = ap.run_tick(sync_db, publish=Publish(), now=NOW, start=lambda db, u: picks.append(1) or None,
                          notify=lambda *a, **k: True)
    assert picks == [1]                                       # the freed slot was used straight away
    assert summary["note"] == "no unused eligible video"


def test_after_three_failures_a_day_the_slot_stays_used(sync_db):
    row = set_mode(sync_db, enabled=True)
    row.last_run_slot = "2026-11-17T09"
    sync_db.commit()
    for _ in range(4):
        failed_pick(sync_db)
    picks = []
    ap.run_tick(sync_db, publish=Publish(), now=NOW, start=lambda db, u: picks.append(1) or None,
                notify=lambda *a, **k: True)
    assert picks == []
    assert ap.get_settings_row(sync_db).last_run_slot == "2026-11-17T09"


def test_clip_selection_also_refuses_an_empty_transcript(sync_db, session_factory, monkeypatch):
    """A delivered-again task must not ask the AI to invent clips."""
    import json as _json
    from app.services.llm import llm_service
    from app.workers import tasks

    video = m.SourceVideo(ownership_confirmed=False, status=m.VideoStatus.SELECTING_CLIPS, transcript=_json.dumps([]))
    sync_db.add(video)
    sync_db.commit()
    asked = []
    monkeypatch.setattr(tasks, "_get_sync_session", session_factory)
    monkeypatch.setattr(llm_service, "select_clips", lambda *a, **k: asked.append(1) or [])
    assert tasks.select_clips_task.apply(args=[str(video.id)]).successful()
    assert asked == []
    saved = session_factory().get(m.SourceVideo, video.id)
    assert saved.status == m.VideoStatus.FAILED and saved.error_message.startswith("Almost no speech")


# ─── Content filters: games, sport, live recordings, sponsored videos ────────

@pytest.mark.parametrize("category", ["20", "17"])
def test_gaming_and_sports_are_blocked_by_default(category):
    assert ap.select_cc_candidate([cc("x", category=category)], set()) is None


def test_a_recording_of_a_live_stream_is_skipped_unless_allowed(monkeypatch):
    live = cc("x", was_live=True)
    assert ap.select_cc_candidate([live], set()) is None
    monkeypatch.setattr(settings, "AUTOPILOT_CC_ALLOW_LIVE_RECORDINGS", True)
    assert ap.select_cc_candidate([live], set()) is not None


@pytest.mark.parametrize("text", [
    "Check out my new video #ad",
    "This video is sponsored by Acme",
    "Thanks to our sponsor for supporting the channel",
    "Use promo code SAVE10 at checkout",
    "paid promotion",
    "Paid Partnership with Brand",
    "Brought to you by Brand",
    "discount code TREE20",
    "affiliate link below",
    "coupon code FREE",
])
def test_sponsor_words_in_the_title_or_description_exclude_a_video(text):
    assert ap.select_cc_candidate([cc("x", description=text)], set()) is None
    assert ap.select_cc_candidate([cc("x", title=text)], set()) is None


@pytest.mark.parametrize("text", [
    "A trip through the adventure of a lifetime #adventure",
    "How to add a class to your homepage",
    "We talk about advertising history",
    "The best ideas of the year",
    "",
])
def test_ordinary_words_that_only_look_similar_are_not_mistaken_for_sponsoring(text):
    assert ap.select_cc_candidate([cc("x", description=text, title="A talk")], set()) is not None


def test_the_youtube_paid_placement_flag_excludes_a_video():
    assert ap.select_cc_candidate([cc("x", paid_placement=True)], set()) is None


def test_the_real_bad_pick_from_this_project_is_now_refused():
    """The Lowe's Madden stream: gaming category, a recorded live stream, '#ad' in the title."""
    video = cc("i36PlsiPFqo", title="LIVE with Lowe's playing Madden 27, featuring Justin Jefferson! #ad",
               category="20", was_live=True, views=16_976_029)
    assert ap.select_cc_candidate([video], set()) is None
    assert ap.looks_sponsored(video)


def test_the_real_good_picks_from_this_project_still_pass():
    talk = cc("AvikGCepgNY", title="The Last Normal Year", category="22", description="A video essay.")
    woodwork = cc("BJAe7XHBhYw", title="Master Carpenter's Wood Corner Joints", category="26", description="Woodworking.")
    assert ap.select_cc_candidate([talk, woodwork], set()) is not None


def test_the_search_reads_live_and_paid_details_and_the_description(monkeypatch):
    fake = FakeYouTube({"medium": ["a"], "long": []}, {"a": {
        **detail("a"),
        "liveStreamingDetails": {"actualStartTime": "2026-11-10T00:00:00Z"},
        "paidProductPlacementDetails": {"hasPaidProductPlacement": True},
    }})
    fake.details["a"]["snippet"]["description"] = "x" * 3000
    monkeypatch.setattr(ap.httpx, "get", fake.get)
    video = ap.fetch_cc_candidates("en", now=NOW)[0]
    asked = next(p for path, p in fake.calls if path == "videos")["part"]
    assert "liveStreamingDetails" in asked and "paidProductPlacementDetails" in asked
    assert video["was_live"] is True and video["paid_placement"] is True and len(video["description"]) == 2000


def test_a_normal_video_has_no_live_or_paid_marks(monkeypatch):
    fake = FakeYouTube({"medium": ["a"], "long": []}, {"a": detail("a")})
    monkeypatch.setattr(ap.httpx, "get", fake.get)
    video = ap.fetch_cc_candidates("en", now=NOW)[0]
    assert video["was_live"] is False and video["paid_placement"] is False


def test_the_default_blocked_categories_include_gaming_and_sports():
    blocked = set(settings.AUTOPILOT_CC_BLOCK_CATEGORIES.split(","))
    assert {"20", "17", "10", "24"} <= blocked and "22" not in blocked and "27" not in blocked


# ─── Blocked title words ─────────────────────────────────────────────────────

@pytest.mark.parametrize("title", [
    "Ramayana: MOVIE REVIEW!!! (spoiler free)",
    "My honest reaction to the finale",
    "Watching the new trailer",
    "Full movie in 10 minutes",
    "IND vs PAK U19 match highlights",
    "Elden Ring gameplay part 3",
    "Weekly box office recap",
    "Best moments compilation",
])
def test_reviews_reactions_trailers_and_highlights_are_skipped(title):
    assert ap.select_cc_candidate([cc("x", title=title)], set()) is None


@pytest.mark.parametrize("title", [
    "The Last Normal Year",
    "How I reacted to a hard year: a lesson in calm",      # 'reacted' is not 'react'
    "Why trailers lie: an essay on marketing",             # 'trailers' (plural) is not 'trailer'
    "Master Carpenter's Wood Corner Joints",
    "A review of the evidence for sleep",                  # 'review' alone is fine
])
def test_similar_looking_titles_are_not_blocked(title):
    assert ap.select_cc_candidate([cc("x", title=title)], set()) is not None


def test_the_blocked_words_can_be_changed_or_emptied(monkeypatch):
    monkeypatch.setattr(settings, "AUTOPILOT_CC_BLOCK_WORDS", "essay")
    assert ap.select_cc_candidate([cc("x", title="Why trailers lie: an essay")], set()) is None
    assert ap.select_cc_candidate([cc("x", title="Full movie review")], set()) is not None
    monkeypatch.setattr(settings, "AUTOPILOT_CC_BLOCK_WORDS", "")
    assert ap.select_cc_candidate([cc("x", title="Full movie review")], set()) is not None


def test_the_ramayana_review_picked_earlier_is_now_refused():
    video = cc("w1", title="Ramayana: MOVIE REVIEW!!! (spoiler free) | Los angeles world premiere", category="24")
    assert ap.has_blocked_word(video) and ap.select_cc_candidate([video], set()) is None


def test_news_and_politics_commentary_is_blocked():
    """A commentary channel clipping Fox News segments was picked: category 25."""
    video = cc("wyIwEtPQYSE", title="Zohran's Fox News Interview Went HORRIBLY Wrong", category="25")
    assert ap.select_cc_candidate([video], set()) is None
    assert "25" in settings.AUTOPILOT_CC_BLOCK_CATEGORIES.split(",")
