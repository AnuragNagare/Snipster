"""The worker's synchronous database engine, with a DNS fallback.

When this machine's resolver cannot find the database host, the address is looked up through public DNS
and the connection goes to that IP. The host name is still sent (libpq `host` + `hostaddr`), so TLS and
Neon's routing work exactly as before.
"""

import psycopg2
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app.core import dns_fallback
from app.core.config import settings
from app.core.logging import logger


def connect_kwargs() -> dict:
    """Arguments for psycopg2.connect, plus `hostaddr` when the system resolver cannot find the host."""
    url = make_url(settings.sync_database_url)
    kwargs = {
        "host": url.host,
        "port": url.port or 5432,
        "dbname": url.database,
        "user": url.username,
        "password": url.password,
        **{k: v for k, v in url.query.items()},
    }
    address = dns_fallback.fallback_address(url.host or "")
    if address:
        kwargs["hostaddr"] = address
        logger.warning(f"[DB] System DNS cannot resolve {url.host}; connecting via public DNS ({address})")
    return kwargs


def make_sync_engine():
    return create_engine(
        settings.sync_database_url,
        creator=lambda: psycopg2.connect(**connect_kwargs()),
        pool_pre_ping=True,
        pool_recycle=300,
    )
