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
