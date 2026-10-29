"""Login, token handling, the Vercel fail-closed rule, and which routes are open or protected."""

import re
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport, AsyncClient

from app.core import auth
from app.core.config import settings
from app.main import app

GOOD_KEY = "k" * 48


@pytest.fixture(autouse=True)
def secured(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "correct horse")
    monkeypatch.setattr(settings, "SECRET_KEY", GOOD_KEY)
    monkeypatch.delenv("VERCEL", raising=False)
    auth.reset_failures()
    yield
    auth.reset_failures()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def login(client, password="correct horse"):
    return await client.post("/api/auth/session", json={"password": password})


async def bearer(client):
    token = (await login(client)).json()["token"]
    return {"Authorization": f"Bearer {token}"}


# ─── Tokens ───────────────────────────────────────────────────────────────────

def test_token_roundtrip_and_kind_separation():
    t = auth.issue("session", 60, extra="x")
    assert auth.verify(t, "session")["extra"] == "x"
    assert auth.verify(t, "oauth-google") is None            # a session token is not an OAuth state


def test_expired_tampered_and_garbage_tokens_are_rejected():
    t = auth.issue("session", 60)
    payload, sig = t.split(".")
    assert auth.verify(f"{payload}.{sig[:-2]}AA", "session") is None          # bad signature
    assert auth.verify(auth.issue("session", -1), "session") is None          # expired
    forged = auth._b64(b'{"k":"session","exp":9999999999}') + "." + sig
    assert auth.verify(forged, "session") is None                             # payload swapped, sig kept
    for junk in ("", "abc", "a.b.c", "....", None):
        assert auth.verify(junk, "session") is None


def test_token_signed_with_another_key_is_rejected(monkeypatch):
    t = auth.issue("session", 60)
    monkeypatch.setattr(settings, "SECRET_KEY", "z" * 48)
    assert auth.verify(t, "session") is None


def test_weak_secret_key_blocks_startup_only_when_a_password_is_set(monkeypatch):
    for weak in ("change-me", "change-me-to-a-random-secret-in-production", "short"):
        monkeypatch.setattr(settings, "SECRET_KEY", weak)
        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            auth.check_secret_key()
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "")
    auth.check_secret_key()                                   # no password, nothing to protect: fine


# ─── Login endpoint ───────────────────────────────────────────────────────────

async def test_login_success_returns_a_working_token(client):
    r = await login(client)
    assert r.status_code == 200 and r.json()["auth_required"] is True
    assert auth.verify(r.json()["token"], "session")


async def test_wrong_password_is_rejected(client):
    assert (await login(client, "nope")).status_code == 401
    assert (await login(client, "")).status_code == 401


async def test_brute_force_is_locked_out_even_for_the_right_password(client):
    for _ in range(auth.MAX_FAILURES):
        assert (await login(client, "guess")).status_code == 401
    assert (await login(client, "guess")).status_code == 429
    assert (await login(client)).status_code == 429          # lockout covers the real password too


async def test_config_is_open_and_reports_auth_state(client, monkeypatch):
    assert (await client.get("/api/auth/config")).json() == {"auth_required": True, "misconfigured": False}
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "")
    assert (await client.get("/api/auth/config")).json() == {"auth_required": False, "misconfigured": False}


async def test_login_is_a_noop_when_auth_is_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "")
    assert (await login(client, "anything")).json() == {"token": "", "auth_required": False}


# ─── Vercel: a deployed URL is public, so no password means no service ────────

async def test_on_vercel_without_a_password_every_data_route_refuses(client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "")
    monkeypatch.setenv("VERCEL", "1")
    for method, path in [("GET", "/api/auth/accounts"), ("POST", "/api/videos/from-url"),
                         ("POST", "/api/videos/upload-url"), ("GET", "/api/system-info"),
                         ("GET", "/api/clips/video/00000000-0000-0000-0000-000000000000")]:
        r = await client.request(method, path)
        assert r.status_code == 503, f"{method} {path} -> {r.status_code}"
        assert "ADMIN_PASSWORD" in r.json()["detail"]


async def test_on_vercel_the_ui_is_told_about_the_misconfiguration(client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "")
    monkeypatch.setenv("VERCEL", "1")
    assert (await client.get("/api/auth/config")).json() == {"auth_required": False, "misconfigured": True}
    assert (await client.get("/health")).status_code == 200  # health stays up so the deploy is visible


async def test_on_vercel_with_a_password_it_works_normally(client, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    assert (await client.get("/api/auth/accounts")).status_code == 401           # needs a token
    assert (await client.get("/api/auth/session", headers=await bearer(client))).status_code == 200


async def test_locally_without_a_password_routes_stay_open(client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "")
    assert (await client.get("/api/auth/session")).status_code == 200


# ─── Route protection ─────────────────────────────────────────────────────────

PROTECTED = [
    ("POST", "/api/videos/upload"),
    ("POST", "/api/videos/upload-url"),
    ("POST", "/api/videos/upload-complete"),
    ("POST", "/api/videos/from-url"),
    ("GET", "/api/videos/00000000-0000-0000-0000-000000000000/status"),
    ("GET", "/api/clips/video/00000000-0000-0000-0000-000000000000"),
    ("PATCH", "/api/clips/00000000-0000-0000-0000-000000000000/approve"),
    ("POST", "/api/clips/00000000-0000-0000-0000-000000000000/publish"),
    ("POST", "/api/clips/publish-jobs/00000000-0000-0000-0000-000000000000/retry"),
    ("GET", "/api/system-info"),
    ("GET", "/api/auth/accounts"),
    ("GET", "/api/auth/session"),
    ("DELETE", "/api/auth/google/disconnect"),
    ("POST", "/api/auth/google/start"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
async def test_every_data_route_demands_a_login(client, method, path):
    r = await client.request(method, path)
    assert r.status_code == 401, f"{method} {path} -> {r.status_code}"


@pytest.mark.parametrize("header", [
    "Bearer not-a-token", "Bearer", "Basic abc", "correct horse", f"Bearer {auth.issue('oauth-google', 60)}",
])
async def test_bad_credentials_never_pass(client, header):
    r = await client.get("/api/system-info", headers={"Authorization": header})
    assert r.status_code == 401


async def test_expired_session_is_rejected(client):
    token = auth.issue("session", -5)
    assert (await client.get("/api/auth/session", headers={"Authorization": f"Bearer {token}"})).status_code == 401


async def test_valid_token_reaches_the_route(client):
    r = await client.get("/api/auth/session", headers=await bearer(client))
    assert r.status_code == 200 and r.json() == {"ok": True}


async def test_health_and_login_stay_open(client):
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/api/auth/config")).status_code == 200


# ─── OAuth: start needs a login, callback needs the state it issued ───────────

async def test_google_start_returns_a_consent_url_with_signed_state(client, monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid")
    r = await client.post("/api/auth/google/start", headers=await bearer(client))
    url = urlparse(r.json()["url"])
    q = parse_qs(url.query)
    assert url.netloc == "accounts.google.com" and q["client_id"] == ["cid"]
    assert "youtube.upload" in q["scope"][0]
    assert auth.verify(q["state"][0], "oauth-google")


@pytest.mark.parametrize("path,wrong", [
    ("/api/auth/google/callback", ""),
    ("/api/auth/google/callback", "forged"),
    ("/api/auth/google/callback", auth.issue("oauth-other", 60)),    # a state of another kind
    ("/api/auth/google/callback", auth.issue("session", 60)),        # a login token is not a state
])
async def test_callback_without_the_right_state_is_refused_before_touching_google(client, path, wrong):
    r = await client.get(path, params={"code": "anything", "state": wrong})
    assert r.status_code == 400 and "sign-in request" in r.json()["detail"]


async def test_expired_state_is_refused(client):
    r = await client.get("/api/auth/google/callback",
                         params={"code": "c", "state": auth.issue("oauth-google", -1)})
    assert r.status_code == 400


# ─── system-info: reported by the worker, read by the API ─────────────────────

async def test_system_info_is_unavailable_until_a_worker_reports(client, monkeypatch):
    async def nothing():
        return None
    monkeypatch.setattr("app.services.worker_info.read", nothing)
    r = await client.get("/api/system-info", headers=await bearer(client))
    assert r.status_code == 503 and "worker" in r.json()["detail"].lower()


async def test_system_info_returns_what_the_worker_reported(client, monkeypatch):
    info = {"whisper_device": "cuda", "whisper_compute_type": "float16", "whisper_model": "base.en",
            "ffmpeg_encoder": "h264_nvenc", "using_gpu": True}

    async def reported():
        return info
    monkeypatch.setattr("app.services.worker_info.read", reported)
    r = await client.get("/api/system-info", headers=await bearer(client))
    assert r.status_code == 200 and r.json() == info


# ─── Safety net: no route can be added without a login ────────────────────────

# Open on purpose: the login itself, the UI's "is a login needed?" probe, health, and the
# OAuth callback (the browser arrives from Google with no token; they are
# protected by the signed `state` tested above).
OPEN = {
    ("GET", "/api/auth/config"),
    ("POST", "/api/auth/session"),
    ("GET", "/api/auth/google/callback"),
}


def _api_routes():
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api"):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                yield method, re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000000", path), path


def test_the_route_inventory_is_not_empty():
    assert len(list(_api_routes())) >= 20                     # guards against the walk silently finding nothing


@pytest.mark.parametrize("method,concrete,template", sorted(_api_routes(), key=lambda r: (r[2], r[0])))
async def test_no_api_route_answers_without_a_login(client, method, concrete, template):
    if (method, template) in OPEN:
        pytest.skip("open on purpose")
    r = await client.request(method, concrete)
    assert r.status_code == 401, f"{method} {template} is reachable without a login ({r.status_code})"


# ─── YouTube only ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", [
    ("POST", "/api/auth/meta/start"), ("GET", "/api/auth/meta/callback"), ("GET", "/api/auth/meta/login"),
    ("DELETE", "/api/auth/meta/disconnect"), ("GET", "/api/auth/google/login"),
])
async def test_instagram_and_the_old_login_routes_are_gone(client, method, path):
    r = await client.request(method, path, headers=await bearer(client))
    assert r.status_code in (404, 405), f"{method} {path} -> {r.status_code}"


async def test_accounts_report_youtube_only(client, monkeypatch):
    from types import SimpleNamespace

    from app.api.deps import get_session

    user = SimpleNamespace(yt_access_token="enc", yt_channel_name="My Channel")

    class FakeDb:
        async def execute(self, _q):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: user))

    app.dependency_overrides[get_session] = lambda: FakeDb()
    try:
        r = await client.get("/api/auth/accounts", headers=await bearer(client))
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200
    assert r.json() == {"youtube_connected": True, "youtube_channel_name": "My Channel"}


async def test_publishing_to_instagram_is_rejected(client):
    r = await client.post("/api/clips/00000000-0000-0000-0000-000000000000/publish",
                          json={"platforms": ["instagram"]}, headers=await bearer(client))
    assert r.status_code == 422


def test_the_platform_list_is_youtube_only():
    from app.db.models import Platform

    assert [p.value for p in Platform] == ["youtube"]
