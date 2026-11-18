"""Apply database migrations through the worker's DNS-safe connection.

    python scripts/migrate.py            # upgrade to the latest version
    python scripts/migrate.py --sql      # print the SQL only, change nothing

Use it instead of `alembic upgrade head` on a network whose DNS cannot resolve the database host
(see app/core/dns_fallback.py). Alembic's own command still works anywhere else.
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app.db.sync_engine import make_sync_engine  # noqa: E402

cfg = Config(str(BACKEND / "alembic.ini"))
cfg.set_main_option("script_location", str(BACKEND / "migrations"))

if "--sql" in sys.argv:
    command.upgrade(cfg, "head", sql=True)
else:
    engine = make_sync_engine()
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    print("Database is at the latest version.")
