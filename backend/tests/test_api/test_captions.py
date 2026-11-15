"""Language override, caption regeneration and editable subtitles."""

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_session
from app.db import models as m
from app.main import app
from app.services import renderer as renderer_module
from app.services.renderer import default_subtitle_lines, renderer_service

NOW = datetime(2026, 11, 15, tzinfo=timezone.utc)
TRANSCRIPT = [{"start": 10.0 + i * 0.5, "end": 10.4 + i * 0.5, "text": f"w{i}"} for i in range(6)]


class FakeSession:
    """Just enough of an AsyncSession: db.get() from a dict, and an ordered event log."""

    def __init__(self, objects):
        self.objects = objects  # (Model, id) -> instance
        self.events = []

    async def get(self, model, ident):
        return self.objects.get((model, ident))

    async def commit(self):
        self.events.append("commit")


def make_video(language="hi"):
    return m.SourceVideo(
        id=uuid.uuid4(), language=language, transcript=json.dumps(TRANSCRIPT),
        status=m.VideoStatus.READY, ownership_confirmed=True,
    )


def make_clip(video, start=10.0, end=20.0, subtitles=None):
    return m.Clip(
        id=uuid.uuid4(), source_video_id=video.id, start_time=start, end_time=end,
        render_status=m.RenderStatus.DONE, approval_status=m.ApprovalStatus.PENDING,
        caption="old", hashtags="#old", subtitles=subtitles, created_at=NOW, updated_at=NOW,
    )


@pytest.fixture
def world():
    video = make_video()
    clip = make_clip(video)
    db = FakeSession({(m.SourceVideo, video.id): video, (m.Clip, clip.id): clip})
    app.dependency_overrides[get_session] = lambda: db
    yield SimpleNamespace(db=db, video=video, clip=clip)
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def queued(world, monkeypatch):
    """Record each enqueue, and whether a commit had already happened."""
    calls = []

    def recorder(name):
        def delay(arg):
            calls.append((name, arg, "commit" in world.db.events))
        return SimpleNamespace(delay=delay)

    monkeypatch.setattr("app.api.routes.clips.generate_caption_task", recorder("caption"))
    monkeypatch.setattr("app.api.routes.clips.render_clip_task", recorder("render"))
    return calls


# ─── Language override ───────────────────────────────────────────────────────

async def test_language_can_be_corrected(client, world):
    async with client as c:
        r = await c.patch(f"/api/videos/{world.video.id}/language", json={"language": "en"})
    assert r.status_code == 200 and r.json()["language"] == "en"
    assert world.video.language == "en"


@pytest.mark.parametrize("bad", ["fr", "", "HI", 5])
async def test_only_known_languages_are_accepted(client, world, bad):
    async with client as c:
        r = await c.patch(f"/api/videos/{world.video.id}/language", json={"language": bad})
    assert r.status_code == 422
    assert world.video.language == "hi"


async def test_language_of_unknown_video_is_404(client, world):
    async with client as c:
        r = await c.patch(f"/api/videos/{uuid.uuid4()}/language", json={"language": "en"})
    assert r.status_code == 404


async def test_clip_responses_carry_the_video_language(client, world):
    async with client as c:
        r = await c.get(f"/api/clips/{world.clip.id}")
    assert r.status_code == 200 and r.json()["language"] == "hi"


# ─── Regenerate caption ──────────────────────────────────────────────────────

async def test_regenerate_queues_the_caption_task_after_committing(client, world, queued):
    async with client as c:
        r = await c.post(f"/api/clips/{world.clip.id}/regenerate-caption")
    assert r.status_code == 200 and r.json()["clip_id"] == str(world.clip.id)
    assert queued == [("caption", str(world.clip.id), True)]


async def test_regenerate_unknown_clip_is_404_and_queues_nothing(client, world, queued):
    async with client as c:
        r = await c.post(f"/api/clips/{uuid.uuid4()}/regenerate-caption")
    assert r.status_code == 404 and queued == []


# ─── Subtitles: read ─────────────────────────────────────────────────────────

async def test_subtitles_default_to_the_transcript(client, world):
    async with client as c:
        r = await c.get(f"/api/clips/{world.clip.id}/subtitles")
    data = r.json()
    assert r.status_code == 200 and data["customized"] is False
    assert [l["text"] for l in data["lines"]] == ["w0 w1 w2 w3", "w4 w5"]
    assert data["lines"][0]["start"] == 0 and data["lines"][1]["start"] == 2.0  # relative to clip start


async def test_saved_subtitles_are_returned_as_customized(client, world):
    world.clip.subtitles = json.dumps([{"start": 1, "end": 3, "text": "नमस्ते"}], ensure_ascii=False)
    async with client as c:
        r = await c.get(f"/api/clips/{world.clip.id}/subtitles")
    assert r.json() == {"lines": [{"start": 1.0, "end": 3.0, "text": "नमस्ते"}], "customized": True}


# ─── Subtitles: save ─────────────────────────────────────────────────────────

async def test_saving_subtitles_stores_sorted_json_with_hindi_intact(client, world):
    lines = [{"start": 4, "end": 6, "text": "दूसरा"}, {"start": 0.5, "end": 2, "text": "पहला"}]
    async with client as c:
        r = await c.patch(f"/api/clips/{world.clip.id}/subtitles", json={"lines": lines})
    assert r.status_code == 200 and r.json()["customized"] is True
    stored = json.loads(world.clip.subtitles)
    assert [l["text"] for l in stored] == ["पहला", "दूसरा"]
    assert "पहला" in world.clip.subtitles  # stored as real characters, not \u escapes


async def test_an_empty_list_means_no_subtitles(client, world):
    async with client as c:
        r = await c.patch(f"/api/clips/{world.clip.id}/subtitles", json={"lines": []})
    assert r.status_code == 200 and world.clip.subtitles == "[]"


@pytest.mark.parametrize("line,status", [
    ({"start": 5, "end": 5, "text": "x"}, 400),      # ends when it starts
    ({"start": 6, "end": 2, "text": "x"}, 400),      # ends before it starts
    ({"start": 0, "end": 30, "text": "x"}, 400),     # clip is 10 s long
    ({"start": -1, "end": 2, "text": "x"}, 422),     # negative start
    ({"start": 0, "end": 2, "text": ""}, 422),       # no text
    ({"start": 0, "end": 2, "text": "x" * 201}, 422),
])
async def test_bad_subtitle_lines_are_refused(client, world, line, status):
    async with client as c:
        r = await c.patch(f"/api/clips/{world.clip.id}/subtitles", json={"lines": [line]})
    assert r.status_code == status
    assert world.clip.subtitles is None


# ─── Trim changes drop edited subtitles ──────────────────────────────────────

SAVED = json.dumps([{"start": 1, "end": 3, "text": "x"}])


async def test_changing_the_trim_discards_edited_subtitles(client, world):
    world.clip.subtitles = SAVED
    async with client as c:
        r = await c.patch(f"/api/clips/{world.clip.id}", json={"start_time": 12.0})
    assert r.status_code == 200 and world.clip.subtitles is None


async def test_caption_edits_keep_the_subtitles(client, world):
    world.clip.subtitles = SAVED
    async with client as c:
        r = await c.patch(f"/api/clips/{world.clip.id}", json={"caption": "new", "start_time": 10.0})
    assert r.status_code == 200 and world.clip.subtitles == SAVED


async def test_rerender_with_the_same_trim_keeps_subtitles(client, world, queued):
    world.clip.subtitles = SAVED
    async with client as c:
        r = await c.post(f"/api/clips/{world.clip.id}/rerender", json={"start_time": 10.0, "end_time": 20.0})
    assert r.status_code == 200 and world.clip.subtitles == SAVED
    assert queued == [("render", str(world.clip.id), True)]


async def test_rerender_with_a_new_trim_discards_subtitles(client, world, queued):
    world.clip.subtitles = SAVED
    async with client as c:
        r = await c.post(f"/api/clips/{world.clip.id}/rerender", json={"start_time": 10.0, "end_time": 25.0})
    assert r.status_code == 200 and world.clip.subtitles is None


# ─── Renderer ────────────────────────────────────────────────────────────────

@pytest.fixture
def ffmpeg_calls(monkeypatch):
    """Capture the ffmpeg command and the SRT text it would have read."""
    seen = {}

    def fake_run(self, cmd):
        vf = cmd[cmd.index("-vf") + 1]
        seen["vf"] = vf
        if "subtitles=" in vf:
            path = vf.split("subtitles='")[1].split("'")[0].replace("\\:", ":")
            with open(path, encoding="utf-8") as f:
                seen["srt"] = f.read()

    monkeypatch.setattr(renderer_module.RendererService, "_run", fake_run)
    monkeypatch.setattr(renderer_module, "_ENCODER", "libx264")
    return seen


def test_render_burns_the_users_subtitles_not_the_transcript(tmp_path, ffmpeg_calls):
    renderer_service.render(
        "in.mp4", str(tmp_path / "out.mp4"), 10, 20, TRANSCRIPT, language="hi",
        subtitles=[{"start": 0.5, "end": 2.0, "text": "संपादित"}],
    )
    assert "संपादित" in ffmpeg_calls["srt"] and "w0" not in ffmpeg_calls["srt"]
    assert "00:00:00,500 --> 00:00:02,000" in ffmpeg_calls["srt"]
    assert "FontName=" in ffmpeg_calls["vf"]


def test_render_with_an_empty_subtitle_list_has_no_subtitles(tmp_path, ffmpeg_calls):
    renderer_service.render("in.mp4", str(tmp_path / "out.mp4"), 10, 20, TRANSCRIPT, subtitles=[])
    assert "subtitles=" not in ffmpeg_calls["vf"]


def test_render_without_saved_subtitles_uses_the_transcript(tmp_path, ffmpeg_calls):
    renderer_service.render("in.mp4", str(tmp_path / "out.mp4"), 10, 20, TRANSCRIPT, subtitles=None)
    assert "w0 w1 w2 w3" in ffmpeg_calls["srt"]


def test_default_lines_are_clip_relative_and_never_negative():
    lines = default_subtitle_lines(TRANSCRIPT, 10, 14)
    assert lines[0] == {"start": 0, "end": 1.9, "text": "w0 w1 w2 w3"}
    assert all(l["start"] >= 0 for l in lines)
    assert default_subtitle_lines([], 0, 10) == []
