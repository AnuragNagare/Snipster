"""YouTube sign-in refresh: refresh before expiry, retry once on 401, never loop on a dead sign-in."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.core.config import settings
from app.core.security import decrypt_token, encrypt_token
from app.db import models as m
from app.services import youtube as yt
from app.services.youtube import YouTubeAuthError, youtube_service

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "client-secret")


def make_user(db, expiry, refresh="refresh-1", access="old-access"):
    user = m.User(
        yt_access_token=encrypt_token(access),
        yt_refresh_token=encrypt_token(refresh) if refresh else None,
        yt_token_expiry=expiry,
    )
    db.add(user)
    db.commit()
    return user


class FakeHttp:
    """Stands in for httpx.post: answers the token endpoint and the upload endpoint from scripts."""

    def __init__(self, token_replies, upload_replies):
        self.token_replies = list(token_replies)
        self.upload_replies = list(upload_replies)
        self.token_calls = []
        self.upload_headers = []

    def post(self, url, **kwargs):
        if url == yt.YOUTUBE_TOKEN_URL:
            self.token_calls.append(kwargs["data"])
            return self.token_replies.pop(0)
        self.upload_headers.append(kwargs["headers"]["Authorization"])
        self.upload_metadata = json.loads(kwargs["files"]["metadata"][1])
        return self.upload_replies.pop(0)


def reply(status, body):
    return SimpleNamespace(status_code=status, json=lambda: body, text=json.dumps(body))


TOKEN_OK = reply(200, {"access_token": "new-access", "expires_in": 3600})
UPLOAD_OK = reply(200, {"id": "abc123"})


@pytest.fixture
def clip():
    return SimpleNamespace(storage_key="clips/x/clip.mp4", caption="Caption", hashtags="#a #b")


@pytest.fixture
def http(monkeypatch):
    def install(token_replies=(), upload_replies=(UPLOAD_OK,)):
        fake = FakeHttp(token_replies, upload_replies)
        monkeypatch.setattr(yt.httpx, "post", fake.post)
        return fake

    monkeypatch.setattr(
        yt.storage_service, "download_to_file",
        lambda key, path: open(path, "wb").write(b"video"),
    )
    return install


def test_an_expired_token_is_refreshed_before_uploading(sync_db, clip, http):
    fake = http([TOKEN_OK])
    user = make_user(sync_db, NOW - timedelta(minutes=5))
    result = youtube_service.upload(clip, user)
    assert result["post_id"] == "abc123"
    assert fake.upload_headers == ["Bearer new-access"]
    assert fake.token_calls[0]["grant_type"] == "refresh_token"
    assert fake.token_calls[0]["refresh_token"] == "refresh-1"
    # saved and committed: a fresh session sees the new token and expiry
    sync_db.expire_all()
    assert decrypt_token(user.yt_access_token) == "new-access"
    assert user.yt_token_expiry.replace(tzinfo=timezone.utc) > NOW + timedelta(minutes=30)


def test_a_token_about_to_expire_is_refreshed_too(sync_db, clip, http):
    fake = http([TOKEN_OK])
    user = make_user(sync_db, NOW + timedelta(seconds=30))
    youtube_service.upload(clip, user)
    assert len(fake.token_calls) == 1


def test_a_fresh_token_is_used_as_is(sync_db, clip, http):
    fake = http()
    user = make_user(sync_db, NOW + timedelta(hours=1))
    youtube_service.upload(clip, user)
    assert fake.token_calls == [] and fake.upload_headers == ["Bearer old-access"]


def test_unknown_expiry_tries_the_token_then_refreshes_on_401(sync_db, clip, http):
    fake = http([TOKEN_OK], [reply(401, {}), UPLOAD_OK])
    user = make_user(sync_db, None)
    youtube_service.upload(clip, user)
    assert fake.upload_headers == ["Bearer old-access", "Bearer new-access"]
    assert len(fake.token_calls) == 1


def test_a_second_401_is_a_sign_in_error_not_a_loop(sync_db, clip, http):
    fake = http([TOKEN_OK], [reply(401, {}), reply(401, {})])
    user = make_user(sync_db, None)
    with pytest.raises(YouTubeAuthError):
        youtube_service.upload(clip, user)
    assert len(fake.upload_headers) == 2


def test_a_revoked_sign_in_says_reconnect(sync_db, clip, http):
    http([reply(400, {"error": "invalid_grant"})])
    user = make_user(sync_db, NOW - timedelta(minutes=5))
    with pytest.raises(YouTubeAuthError, match="Reconnect"):
        youtube_service.upload(clip, user)


def test_a_missing_refresh_token_is_a_sign_in_error(sync_db, clip, http):
    http()
    user = make_user(sync_db, NOW - timedelta(minutes=5), refresh=None)
    with pytest.raises(YouTubeAuthError, match="refresh token"):
        youtube_service.upload(clip, user)


def test_missing_google_credentials_on_the_worker_is_explained(sync_db, clip, http, monkeypatch):
    http()
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "")
    user = make_user(sync_db, NOW - timedelta(minutes=5))
    with pytest.raises(YouTubeAuthError, match="GOOGLE_CLIENT_ID"):
        youtube_service.upload(clip, user)


def test_other_refresh_failures_are_ordinary_errors_that_may_be_retried(sync_db, clip, http):
    http([reply(500, {"error": "backend_error"})])
    user = make_user(sync_db, NOW - timedelta(minutes=5))
    with pytest.raises(RuntimeError):
        youtube_service.upload(clip, user)


def test_an_unconnected_account_is_a_sign_in_error(clip, http):
    http()
    with pytest.raises(YouTubeAuthError):
        youtube_service.upload(clip, SimpleNamespace(yt_access_token=None))


def test_privacy_goes_into_the_upload(sync_db, clip, http):
    fake = http()
    user = make_user(sync_db, NOW + timedelta(hours=1))
    youtube_service.upload(clip, user, privacy="unlisted")
    assert fake.upload_metadata["status"]["privacyStatus"] == "unlisted"


def test_default_privacy_is_still_public(sync_db, clip, http):
    fake = http()
    user = make_user(sync_db, NOW + timedelta(hours=1))
    youtube_service.upload(clip, user)
    assert fake.upload_metadata["status"]["privacyStatus"] == "public"


def test_an_invalid_privacy_value_is_refused(sync_db, clip, http):
    http()
    user = make_user(sync_db, NOW + timedelta(hours=1))
    with pytest.raises(ValueError):
        youtube_service.upload(clip, user, privacy="everyone")


# ─── The publish task ────────────────────────────────────────────────────────

def make_job(db, status=m.PublishStatus.QUEUED):
    video = m.SourceVideo(ownership_confirmed=True, status=m.VideoStatus.READY)
    db.add(video)
    db.flush()
    clip = m.Clip(source_video_id=video.id, start_time=0, end_time=20, storage_key="clips/x/clip.mp4",
                  approval_status=m.ApprovalStatus.APPROVED, render_status=m.RenderStatus.DONE)
    db.add(clip)
    db.flush()
    job = m.PublishJob(clip_id=clip.id, platform=m.Platform.YOUTUBE, status=status, retry_count=0)
    db.add(job)
    db.commit()
    return job


def run_publish(monkeypatch, session_factory, job_id, upload):
    from app.workers import tasks

    monkeypatch.setattr(tasks, "_get_sync_session", session_factory)
    monkeypatch.setattr(youtube_service, "upload", upload)
    return tasks.publish_clip_task.apply(args=[str(job_id)])


def test_a_sign_in_error_fails_the_job_without_retrying(sync_db, session_factory, monkeypatch):
    make_user(sync_db, None)
    job = make_job(sync_db)
    calls = []

    def upload(clip, user, privacy, credit=None, publish_at=None):
        calls.append(privacy)
        raise YouTubeAuthError("Reconnect YouTube in Settings.")

    result = run_publish(monkeypatch, session_factory, job.id, upload)
    assert result.successful()  # finished, not scheduled for a retry
    assert calls == ["public"]  # no autopilot settings row yet
    check = session_factory()
    saved = check.get(m.PublishJob, job.id)
    assert saved.status == m.PublishStatus.FAILED
    assert json.loads(saved.error_info)["auth"] is True


def test_the_task_uses_the_autopilot_privacy_setting(sync_db, session_factory, monkeypatch):
    make_user(sync_db, None)
    sync_db.add(m.AutopilotSettings(id=1, privacy="unlisted"))
    job = make_job(sync_db)
    seen = []

    def upload(clip, user, privacy, credit=None, publish_at=None):
        seen.append(privacy)
        return {"post_id": "v1", "post_url": "https://www.youtube.com/shorts/v1"}

    run_publish(monkeypatch, session_factory, job.id, upload)
    assert seen == ["unlisted"]
    assert session_factory().get(m.PublishJob, job.id).status == m.PublishStatus.PUBLISHED


def test_the_callback_records_the_token_expiry():
    """Source check: the OAuth callback must save expires_in, or refresh never knows the expiry."""
    import inspect
    from app.api.routes import auth

    src = inspect.getsource(auth.google_callback)
    assert "yt_token_expiry" in src and "expires_in" in src
