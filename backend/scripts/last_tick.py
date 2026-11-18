"""Prints how long ago the autopilot scheduler last ticked, read-only. Used by status-snipster.ps1."""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.db.sync_engine import make_sync_engine  # noqa: E402

try:
    engine = make_sync_engine()
    with engine.connect() as conn:
        last = conn.execute(text("select last_tick_at from autopilot_settings where id = 1")).scalar()
except Exception as exc:  # never print the connection string
    print(f"  last scheduler tick: could not read the database ({type(exc).__name__})")
    sys.exit(0)

if last is None:
    print("  last scheduler tick: never")
else:
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    print("  last scheduler tick: %d seconds ago" % (datetime.now(timezone.utc) - last).total_seconds())
