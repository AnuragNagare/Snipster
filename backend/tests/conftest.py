"""Shared fixtures. `sync_db` is a real (in-memory SQLite) database for the worker-side services."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base


@pytest.fixture
def session_factory():
    """A fresh empty database with every table; sessions keep their attributes after commit/close."""
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def sync_db(session_factory):
    session = session_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def no_real_alerts(monkeypatch):
    """
    Tests must never reach a real inbox, phone or health-check service. backend/.env may hold real Gmail,
    Telegram and health-check settings, and the settings object reads it; blank them for every test.
    A test that needs one sets it again itself.
    """
    from app.core.config import settings

    for name in ("SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL_TO", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                 "HEALTHCHECK_URL", "HEALTHCHECK_CLOUD_URL"):
        monkeypatch.setattr(settings, name, "")
