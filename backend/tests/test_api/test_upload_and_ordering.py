"""Direct-to-R2 uploads, and the rule that a job is never queued before its row is saved."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_session
from app.core.config import settings
from app.db import models as m
from app.main import app
from app.services import storage as storage_module

KEY = f"source/{uuid.uuid4()}/talk.mp4"


class FakeSession:
    """Just enough of an AsyncSession to record the order of save and queue events."""

    def __init__(self, existing=None, objects=None):
        self.events = []                 # "flush", "commit", "enqueue:<name>"
        self.existing = existing         # what a "find a video by storage key" query returns
        self.objects = objects or {}     # (Model, id) -> instance, for db.get()
        self.added = []

    async def execute(self, _stmt):
        existing = self.existing
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: existing))

    async def get(self, model, ident):
        return self.objects.get((model, ident))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.events.append("flush")
        for obj in self.added:           # what the database would fill in on INSERT
            obj.id = getattr(obj, "id", None) or uuid.uuid4()
            if isinstance(obj, m.PublishJob) and obj.retry_count is None:
                obj.retry_count = 0

    async def commit(self):
        self.events.append("commit")


@pytest.fixture
def db():
    fake = FakeSession()
    app.dependency_overrides[get_session] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def queue(db, monkeypatch):
    """Replace every enqueue with a recorder that notes whether the data was already saved."""
    saved_at_enqueue = {}

    def recorder(name):
        def enqueue(*args, **kwargs):
            db.events.append(f"enqueue:{name}")
            saved_at_enqueue[name] = "commit" in db.events
        return enqueue

    monkeypatch.setattr("app.api.routes.videos.start_processing_pipeline", recorder("pipeline"))
    monkeypatch.setattr("app.api.routes.clips.render_clip_task", SimpleNamespace(delay=recorder("render")))
    monkeypatch.setattr("app.api.routes.publish.publish_clip_task", SimpleNamespace(delay=recorder("publish")))
    return saved_at_enqueue


@pytest.fixture
def storage(monkeypatch):
    s = MagicMock()
    s.get_presigned_upload_url.return_value = "https://acct.r2.cloudflarestorage.com/signed"
    s.object_size.return_value = 5_000_000
    monkeypatch.setattr("app.api.routes.videos.storage_service", s)
    return s


def body(**over):
    return {"storage_key": KEY, "filename": "talk.mp4", "ownership_confirmed": True, **over}


# ─── Step 1: ask for an upload URL ────────────────────────────────────────────

async def test_upload_url_is_issued_for_a_confirmed_video(client, db, storage):
    async with client as c:
        r = await c.post("/api/videos/upload-url",
                         json={"filename": "My Talk.mp4", "content_type": "video/mp4", "ownership_confirmed": True})
    assert r.status_code == 200
    data = r.json()
    assert data["upload_url"].startswith("https://") and data["content_type"] == "video/mp4"
    assert data["storage_key"].startswith("source/") and data["storage_key"].endswith("/My Talk.mp4")
    storage.get_presigned_upload_url.assert_called_once_with(data["storage_key"], "video/mp4")


@pytest.mark.parametrize("payload,fragment", [
    ({"filename": "a.mp4", "content_type": "video/mp4", "ownership_confirmed": False}, "rights"),
    ({"filename": "a.exe", "content_type": "application/x-msdownload", "ownership_confirmed": True}, "Unsupported"),
    ({"filename": "a.txt", "content_type": "text/plain", "ownership_confirmed": True}, "Unsupported"),
])
async def test_upload_url_refuses_unconfirmed_or_non_video_files(client, db, storage, payload, fragment):
    async with client as c:
        r = await c.post("/api/videos/upload-url", json=payload)
    assert r.status_code == 400 and fragment in r.json()["detail"]
    storage.get_presigned_upload_url.assert_not_called()


@pytest.mark.parametrize("evil,expected_tail", [
    ("../../etc/passwd.mp4", "/passwd.mp4"),
    ("C:\\Users\\me\\clip.mp4", "/clip.mp4"),
    ("a/b/c.mp4", "/c.mp4"),
    ("weird<>|name?.mp4", "/weird___name_.mp4"),
])
async def test_filenames_cannot_escape_the_source_folder(client, db, storage, evil, expected_tail):
    async with client as c:
        r = await c.post("/api/videos/upload-url",
                         json={"filename": evil, "content_type": "video/mp4", "ownership_confirmed": True})
    key = r.json()["storage_key"]
    assert key.startswith("source/") and key.endswith(expected_tail) and ".." not in key
    assert key.count("/") == 2


def test_the_signed_url_is_real_and_binds_the_content_type(monkeypatch):
    """Signing is local, so this exercises the real boto3 path with no network."""
    for k, v in {"STORAGE_ENDPOINT": "https://acct.r2.cloudflarestorage.com", "STORAGE_BUCKET": "snipster",
                 "STORAGE_ACCESS_KEY": "AKIDEXAMPLE", "STORAGE_SECRET_KEY": "secretexample"}.items():
        monkeypatch.setattr(settings, k, v)
    url = storage_module.StorageService().get_presigned_upload_url("source/x/clip.mp4", "video/mp4", 600)
    assert url.startswith("https://acct.r2.cloudflarestorage.com/snipster/source/x/clip.mp4?")
    assert "X-Amz-Signature=" in url and "X-Amz-Expires=600" in url
    assert "content-type" in url.lower()          # a different Content-Type on the PUT would be rejected


# ─── Step 2: tell the API the file arrived ────────────────────────────────────

async def test_complete_creates_the_video_and_starts_processing_after_saving(client, db, queue, storage):
    async with client as c:
        r = await c.post("/api/videos/upload-complete", json=body())
    assert r.status_code == 201 and r.json()["status"] == "pending"
    video = db.added[0]
    assert (video.storage_key, video.original_filename, video.ownership_confirmed) == (KEY, "talk.mp4", True)
    assert db.events == ["flush", "commit", "enqueue:pipeline"]
    assert queue["pipeline"] is True


@pytest.mark.parametrize("key", [
    "clips/abc/clip.mp4",                                   # someone else's object in the bucket
    "source/not-a-uuid/talk.mp4",
    f"source/{uuid.uuid4()}/../../clips/x.mp4",
    f"source/{uuid.uuid4()}/sub/dir.mp4",
    f"source/{uuid.uuid4()}/",
    "",
])
async def test_complete_only_accepts_keys_that_upload_url_could_have_made(client, db, queue, storage, key):
    async with client as c:
        r = await c.post("/api/videos/upload-complete", json=body(storage_key=key))
    assert r.status_code == 400 and "Invalid storage key" in r.json()["detail"]
    assert db.added == [] and db.events == []
    storage.object_size.assert_not_called()


async def test_complete_requires_ownership_confirmation(client, db, queue, storage):
    async with client as c:
        r = await c.post("/api/videos/upload-complete", json=body(ownership_confirmed=False))
    assert r.status_code == 400 and "rights" in r.json()["detail"]
    assert db.events == []


async def test_complete_rejects_a_file_that_was_never_uploaded(client, db, queue, storage):
    storage.object_size.return_value = None
    async with client as c:
        r = await c.post("/api/videos/upload-complete", json=body())
    assert r.status_code == 400 and "not found in storage" in r.json()["detail"]
    assert db.events == []                                  # nothing saved, nothing queued


async def test_complete_rejects_an_empty_file(client, db, queue, storage):
    storage.object_size.return_value = 0
    async with client as c:
        r = await c.post("/api/videos/upload-complete", json=body())
    assert r.status_code == 400 and db.events == []


async def test_oversized_upload_is_refused_and_deleted(client, db, queue, storage, monkeypatch):
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 10)
    storage.object_size.return_value = 11 * 1024 * 1024
    async with client as c:
        r = await c.post("/api/videos/upload-complete", json=body())
    assert r.status_code == 400 and "larger than 10 MB" in r.json()["detail"]
    storage.delete.assert_called_once_with(KEY)             # no stray multi-GB file left in the bucket
    assert db.events == []


async def test_a_retried_complete_does_not_start_a_second_pipeline(client, db, queue, storage):
    db.existing = SimpleNamespace(id=uuid.uuid4(), status=m.VideoStatus.TRANSCRIBING)
    async with client as c:
        r = await c.post("/api/videos/upload-complete", json=body())
    assert r.status_code == 201 and r.json()["message"] == "Already submitted."
    assert db.events == [] and db.added == []


async def test_a_storage_outage_is_a_clean_500_not_a_crash(client, db, queue, storage):
    storage.object_size.side_effect = RuntimeError("R2 unreachable")
    async with client as c:
        r = await c.post("/api/videos/upload-complete", json=body())
    assert r.status_code == 500 and db.events == []


# ─── The worker must be able to find the row it is told about ─────────────────

async def test_from_url_saves_before_queueing(client, db, queue):
    async with client as c:
        r = await c.post("/api/videos/from-url",
                         json={"youtube_url": "https://youtu.be/abc", "ownership_confirmed": True})
    assert r.status_code == 201
    assert db.events == ["flush", "commit", "enqueue:pipeline"]


async def test_small_multipart_upload_saves_before_queueing(client, db, queue, storage):
    storage.upload_bytes = MagicMock()
    async with client as c:
        r = await c.post("/api/videos/upload", data={"ownership_confirmed": "true"},
                         files={"file": ("t.mp4", b"bytes", "video/mp4")})
    assert r.status_code == 201
    assert db.events == ["flush", "commit", "enqueue:pipeline"]


def _clip():
    now = datetime.now(timezone.utc)
    return m.Clip(id=uuid.uuid4(), source_video_id=uuid.uuid4(), start_time=10, end_time=40,
                  render_status=m.RenderStatus.DONE, storage_key="k", approval_status=m.ApprovalStatus.APPROVED,
                  created_at=now, updated_at=now)


async def test_rerender_saves_the_new_trim_points_before_queueing(client, db, queue, monkeypatch):
    clip = _clip()
    db.objects[(m.Clip, clip.id)] = clip
    monkeypatch.setattr("app.api.routes.clips.storage_service", MagicMock(get_presigned_url=lambda k: "u"))
    async with client as c:
        r = await c.post(f"/api/clips/{clip.id}/rerender", json={"start_time": 20, "end_time": 50})
    assert r.status_code == 200
    assert (clip.start_time, clip.end_time) == (20, 50)
    assert db.events == ["commit", "enqueue:render"]        # the render task re-reads these from the DB


async def test_publish_saves_the_job_before_queueing(client, db, queue):
    clip = _clip()
    db.objects[(m.Clip, clip.id)] = clip
    async with client as c:
        r = await c.post(f"/api/clips/{clip.id}/publish", json={"platforms": ["youtube"]})
    assert r.status_code == 200 and len(r.json()) == 1
    assert db.events == ["flush", "commit", "enqueue:publish"]


async def test_publish_with_a_schedule_queues_nothing(client, db, queue):
    clip = _clip()
    db.objects[(m.Clip, clip.id)] = clip
    async with client as c:
        r = await c.post(f"/api/clips/{clip.id}/publish",
                         json={"platforms": ["youtube"], "scheduled_at": "2030-01-01T00:00:00Z"})
    assert r.status_code == 200 and "enqueue:publish" not in db.events   # unchanged behaviour


async def test_retry_saves_the_status_before_queueing(client, db, queue):
    job = m.PublishJob(id=uuid.uuid4(), clip_id=uuid.uuid4(), platform=m.Platform.YOUTUBE,
                       status=m.PublishStatus.FAILED, retry_count=0)
    db.objects[(m.PublishJob, job.id)] = job
    async with client as c:
        r = await c.post(f"/api/clips/publish-jobs/{job.id}/retry")
    assert r.status_code == 200 and job.status == m.PublishStatus.QUEUED and job.retry_count == 1
    assert db.events == ["commit", "enqueue:publish"]
