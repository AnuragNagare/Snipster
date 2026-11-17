"""Fetching view/like/comment counts: batching, caching, hidden counts, and failures."""

from types import SimpleNamespace

import httpx
import pytest

from app.core.config import settings
from app.services import youtube_stats as ys
from app.services.youtube_stats import StatsUnavailable, fetch_video_stats


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setattr(settings, "YOUTUBE_API_KEY", "key")
    ys.clear_cache()
    yield
    ys.clear_cache()


def item(vid, views=10, likes=2, comments=1):
    stats = {"viewCount": str(views)}
    if likes is not None:
        stats["likeCount"] = str(likes)
    if comments is not None:
        stats["commentCount"] = str(comments)
    return {"id": vid, "statistics": stats}


class Fake:
    def __init__(self, catalog, status=200, error=None):
        self.catalog, self.status, self.error, self.calls = catalog, status, error, []

    def get(self, url, params, timeout):
        self.calls.append(params)
        if self.error:
            raise self.error
        wanted = params["id"].split(",")
        body = {"items": [self.catalog[v] for v in wanted if v in self.catalog]}
        return SimpleNamespace(status_code=self.status, json=lambda: body)


def install(monkeypatch, catalog, **kw):
    fake = Fake(catalog, **kw)
    monkeypatch.setattr(ys.httpx, "get", fake.get)
    return fake


def test_counts_are_returned_as_numbers(monkeypatch):
    install(monkeypatch, {"a": item("a", views=1234, likes=56, comments=7)})
    assert fetch_video_stats(["a"]) == {"a": {"views": 1234, "likes": 56, "comments": 7}}


def test_hidden_likes_and_comments_are_none_not_zero(monkeypatch):
    install(monkeypatch, {"a": item("a", views=5, likes=None, comments=None)})
    assert fetch_video_stats(["a"])["a"] == {"views": 5, "likes": None, "comments": None}


def test_private_or_deleted_videos_are_left_out(monkeypatch):
    install(monkeypatch, {"a": item("a")})
    assert list(fetch_video_stats(["a", "gone"])) == ["a"]


def test_the_request_asks_only_for_statistics_with_the_key(monkeypatch):
    fake = install(monkeypatch, {"a": item("a")})
    fetch_video_stats(["a"])
    assert fake.calls[0]["part"] == "statistics" and fake.calls[0]["key"] == "key"


def test_more_than_50_videos_are_fetched_in_batches(monkeypatch):
    ids = [f"v{i}" for i in range(120)]
    fake = install(monkeypatch, {v: item(v) for v in ids})
    assert len(fetch_video_stats(ids)) == 120
    assert [len(c["id"].split(",")) for c in fake.calls] == [50, 50, 20]


def test_duplicate_and_empty_ids_are_ignored(monkeypatch):
    fake = install(monkeypatch, {"a": item("a")})
    fetch_video_stats(["a", "a", "", "a"])
    assert fake.calls[0]["id"] == "a"


def test_results_are_cached_so_reloads_cost_no_quota(monkeypatch):
    fake = install(monkeypatch, {"a": item("a", views=1)})
    clock = iter([100.0, 100.0, 160.0, 160.0])
    fetch_video_stats(["a"], clock=lambda: next(clock))
    again = fetch_video_stats(["a"], clock=lambda: next(clock))
    assert len(fake.calls) == 1 and again["a"]["views"] == 1


def test_the_cache_expires_and_numbers_refresh(monkeypatch):
    fake = install(monkeypatch, {"a": item("a", views=1)})
    fetch_video_stats(["a"], clock=lambda: 100.0)
    fake.catalog["a"] = item("a", views=99)
    fresh = fetch_video_stats(["a"], clock=lambda: 100.0 + ys.CACHE_SECONDS + 1)
    assert len(fake.calls) == 2 and fresh["a"]["views"] == 99


def test_only_uncached_videos_are_requested(monkeypatch):
    fake = install(monkeypatch, {"a": item("a"), "b": item("b")})
    fetch_video_stats(["a"], clock=lambda: 100.0)
    fetch_video_stats(["a", "b"], clock=lambda: 101.0)
    assert fake.calls[1]["id"] == "b"


def test_a_missing_api_key_is_explained(monkeypatch):
    monkeypatch.setattr(settings, "YOUTUBE_API_KEY", "")
    with pytest.raises(StatsUnavailable, match="YOUTUBE_API_KEY"):
        fetch_video_stats(["a"])


def test_quota_or_key_problems_are_explained(monkeypatch):
    install(monkeypatch, {}, status=403)
    with pytest.raises(StatsUnavailable, match="quota"):
        fetch_video_stats(["a"])


def test_other_http_errors_are_reported(monkeypatch):
    install(monkeypatch, {}, status=500)
    with pytest.raises(StatsUnavailable, match="500"):
        fetch_video_stats(["a"])


def test_a_network_failure_is_reported_without_leaking_the_key(monkeypatch):
    install(monkeypatch, {}, error=httpx.ConnectError("boom key=key"))
    with pytest.raises(StatsUnavailable) as err:
        fetch_video_stats(["a"])
    assert "key" not in str(err.value).replace("YouTube", "")
