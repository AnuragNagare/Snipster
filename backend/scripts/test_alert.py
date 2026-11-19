"""Send one test alert through every channel that is set up, and say which worked. Prints no secrets.

    python scripts/test_alert.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import alerts  # noqa: E402

print("Telegram set up:", alerts.telegram_configured())
print("Email set up    :", alerts.email_configured())
if not alerts.configured():
    print("Nothing is set up. Fill SMTP_USER and SMTP_PASSWORD (or TELEGRAM_*) in backend/.env.")
    sys.exit(1)

if alerts.telegram_configured():
    print("Telegram sent   :", alerts._send_telegram("This is a test alert from Snipster."))
if alerts.email_configured():
    print("Email sent      :", alerts._send_email("This is a test alert from Snipster.\nIf you can read this, alerts work."))
