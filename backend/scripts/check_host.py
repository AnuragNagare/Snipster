"""Can this machine resolve a host name? Used by start-snipster.ps1 to wait for the network.

    python check_host.py HOST [--allow-fallback]

Exit 0 and prints "system" or "fallback" when it can; exit 1 and prints "none" when it cannot.
With --allow-fallback, a name that only public DNS (8.8.8.8 / 1.1.1.1) can find counts as reachable:
the worker's database connection can use that (see app/db/sync_engine.py).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import dns_fallback  # noqa: E402

host = sys.argv[1] if len(sys.argv) > 1 else ""
if not host:
    print("none")
    sys.exit(1)
if dns_fallback.system_resolves(host):
    print("system")
    sys.exit(0)
if "--allow-fallback" in sys.argv and dns_fallback.query_public_dns(host):
    print("fallback")
    sys.exit(0)
print("none")
sys.exit(1)
