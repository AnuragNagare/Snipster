"""Settings that decide whether the app can talk to Neon, hosted Redis and R2."""

import ssl

import pytest

from app.core.config import Settings
from app.workers.celery_app import redis_tls_options

NEON_POOLED = (
    "postgresql://neon_user:p%40ss%2Fw@ep-cool-sky-123456-pooler.us-east-2.aws.neon.tech/neondb"
    "?sslmode=require&channel_binding=require"
)


def s(url: str) -> Settings:
    return Settings(DATABASE_URL=url)


# ─── Database URL: whatever form the provider hands you ───────────────────────

def test_neon_url_becomes_an_asyncpg_url_without_libpq_only_parameters():
    # asyncpg raises on sslmode / channel_binding — they must not reach it.
    assert s(NEON_POOLED).async_database_url == (
        "postgresql+asyncpg://neon_user:p%40ss%2Fw@ep-cool-sky-123456-pooler.us-east-2.aws.neon.tech/neondb"
    )


def test_neon_url_keeps_its_tls_settings_for_the_sync_driver():
    url = s(NEON_POOLED).sync_database_url
    assert url.startswith("postgresql+psycopg2://neon_user:p%40ss%2Fw@ep-cool-sky-123456-pooler")
    assert "sslmode=require" in url and "channel_binding=require" in url


def test_tls_and_pgbouncer_settings_are_passed_to_asyncpg_for_the_pooled_host():
    args = s(NEON_POOLED).database_connect_args
    assert args["ssl"] == "require"
    assert args["statement_cache_size"] == 0 and args["prepared_statement_cache_size"] == 0
    assert args["prepared_statement_name_func"]() != args["prepared_statement_name_func"]()  # unique per call


def test_direct_neon_host_needs_tls_but_not_the_pgbouncer_workaround():
    args = s("postgresql://u:p@ep-cool-sky-123456.us-east-2.aws.neon.tech/neondb?sslmode=require").database_connect_args
    assert args == {"ssl": "require"}


@pytest.mark.parametrize("url", [
    "postgres://u:p@host/db",                      # Heroku/Vercel style scheme
    "postgresql://u:p@host/db",
    "postgresql+asyncpg://u:p@host/db",            # the repo's old form
    "postgresql+psycopg2://u:p@host/db",
])
def test_every_common_scheme_is_accepted(url):
    cfg = s(url)
    assert cfg.async_database_url == "postgresql+asyncpg://u:p@host/db"
    assert cfg.sync_database_url == "postgresql+psycopg2://u:p@host/db"


def test_local_database_needs_no_special_arguments():
    cfg = s("postgresql+asyncpg://snipster:snipster@localhost:5432/snipster")
    assert cfg.database_connect_args == {}
    assert cfg.async_database_url == "postgresql+asyncpg://snipster:snipster@localhost:5432/snipster"


def test_asyncpg_style_ssl_parameter_is_understood_and_translated():
    cfg = s("postgresql://u:p@host/db?ssl=require")
    assert cfg.database_connect_args == {"ssl": "require"}
    assert "ssl=" not in cfg.sync_database_url and "sslmode=require" in cfg.sync_database_url


def test_sslmode_disable_means_no_tls():
    assert s("postgresql://u:p@localhost/db?sslmode=disable").database_connect_args == {}


def test_a_non_postgres_url_is_rejected_clearly():
    with pytest.raises(ValueError, match="postgresql"):
        s("mysql://u:p@host/db").async_database_url


def test_the_pooled_flag_can_be_forced_by_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_POOLED", "1")
    assert s("postgresql://u:p@some-other-host/db").database_connect_args["statement_cache_size"] == 0


def test_is_vercel_follows_the_platform_variable(monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    assert Settings().is_vercel is False
    monkeypatch.setenv("VERCEL", "1")
    assert Settings().is_vercel is True


def test_alembic_config_survives_a_url_encoded_password():
    # ConfigParser treats a single % as interpolation; migrations/env.py must double it.
    import configparser

    parser = configparser.ConfigParser()
    parser.add_section("a")
    parser.set("a", "url", s(NEON_POOLED).async_database_url.replace("%", "%%"))
    assert "p%40ss%2Fw" in parser.get("a", "url")


# ─── Redis over TLS ───────────────────────────────────────────────────────────

def test_rediss_url_gets_certificate_verification():
    assert redis_tls_options("rediss://default:x@host.upstash.io:6379") == {"ssl_cert_reqs": ssl.CERT_REQUIRED}


def test_plain_redis_url_gets_none():
    assert redis_tls_options("redis://localhost:6379/0") is None


def test_celery_is_configured_to_fail_fast_when_redis_is_unreachable():
    from app.workers.celery_app import celery_app

    conf = celery_app.conf
    assert conf.broker_transport_options["socket_connect_timeout"] == 5
    assert conf.task_publish_retry_policy["max_retries"] == 2
    assert conf.task_acks_late is True and conf.worker_prefetch_multiplier == 1   # worker behaviour unchanged
