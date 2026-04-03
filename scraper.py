#!/usr/bin/env python3
"""
Scraper Agent
--------------
Generic scraper that polls one or more Sources (see sources/), detects newly completed events since the last check, and emails a summary when new results exist.
All URL-specific logic lives in source plugins under providers/.
"""

import sys
if sys.version_info < (3, 8):
    sys.exit("Python 3.8+ is required. Current version: " + sys.version)

import json
import logging
import os
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv

from providers import DEFAULT_PROVIDER_KEY, PROVIDERS
from providers.base import Provider

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
STATE_FILE = Path(__file__).parent / "state.json"
EMAIL_TO = [addr.strip() for addr in os.getenv("EMAIL_TO", "erancha@gmail.com").split(",")]
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # seconds between checks
DRY_RUN = False  # Set via --dry-run CLI flag; skips actual email sending


# -------------------------------------------------------------------------------------------------------------------------------
# State helpers – Persist which events have already been reported as completed between runs, using a local JSON file (state.json).
# This prevents duplicate emails: an event is only emailed about once, the first time it appears as completed.
# State is keyed per source (e.g. state["espn_nba"]["completed_ids"]) so multiple sources coexist cleanly.
# -------------------------------------------------------------------------------------------------------------------------------
def load_state() -> dict:
    """Return the full persisted state dict."""
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def provider_state(state: dict, provider: Provider) -> dict:
    """Return (or create) the sub-dict for a given provider inside the global state."""
    key = provider.state_key
    if key not in state:
        state[key] = {"completed_ids": [], "last_check": None}
    return state[key]


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def send_email(subject: str, html_body: str, plain_body: str) -> None:
    """Send an email via SMTP (TLS). Skipped when DRY_RUN is True."""
    if DRY_RUN:
        logger.info("[DRY-RUN] Email would be sent \u2013 skipping actual send.\nSubject: %s\n%s", subject, plain_body)
        return

    if not SMTP_USER or not SMTP_PASS:
        logger.warning("SMTP credentials not configured \u2013 skipping email.")
        logger.info("Set SMTP_USER and SMTP_PASS in .env to enable email.")
        return

    msg = MIMEMultipart("alternative")  # plain text + HTML; email client picks the best it can render
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(EMAIL_TO)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
    logger.info("Email sent to %s", ", ".join(EMAIL_TO))


# ---------------------------------------------------------------------------
# Agent loop – Generic: works with any Provider implementation.
# ---------------------------------------------------------------------------
def check_once(provider: Provider) -> None:
    """Run a single scrape-check-email cycle for the given provider."""
    state = load_state()
    prov_state = provider_state(state, provider)
    known_completed: set[str] = set(prov_state.get("completed_ids", []))

    data = provider.fetch()
    items = provider.parse(data)

    if not items:
        return

    day_label = provider.get_day_label(data)
    first_run = prov_state.get("last_check") is None

    # On first run, log the current state so the user sees something
    if first_run:
        logger.info("[%s] Initial fetch (%d item(s)):\n%s",
                    provider.name, len(items),
                    provider.format_text(items, provider.heading(day_label)))

    # ---- Completion evaluation ----
    # Compare the set of completed IDs from this fetch against the IDs stored in state.json.  
    # An email is sent ONLY when at least one new ID appears in the completed set that wasn't there before.
    current_completed = provider.get_completed_ids(items)
    newly_completed = current_completed - known_completed

    if not newly_completed:
        # On first run, save state even without completions so we don't repeat the initial print
        if first_run:
            prov_state["last_check"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
        return

    logger.info("[%s] %d item(s) newly completed \u2013 sending email \u2026",
                provider.name, len(newly_completed))
    if not first_run:
        logger.info("%s", provider.format_text(items, provider.heading(day_label)))

    subject = f"{provider.name} Update \u2013 {day_label}"
    html_body = (
        f"<h2>{provider.heading(day_label)}</h2>"
        + provider.items_to_html_table(items)
        + "<br><p style='color:gray;font-size:12px;'>Sent by Scraper Agent</p>"
    )
    plain_body = provider.format_text(items, provider.heading(day_label))
    send_email(subject, html_body, plain_body)

    # Update state only when new completions are found
    prov_state["completed_ids"] = list(known_completed | current_completed)
    prov_state["last_check"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


def run_loop(provider: Provider) -> None:
    """Continuously poll the given provider at CHECK_INTERVAL seconds."""
    logger.info("Scraper Agent started (provider=%s, interval=%ds, recipient=%s)",
                provider.name, CHECK_INTERVAL, EMAIL_TO)
    while True:
        try:
            check_once(provider)
        except requests.RequestException as exc:
            logger.error("[%s] Network error: %s", provider.name, exc)
        except Exception as exc:
            logger.error("[%s] Unexpected error: %s", provider.name, exc)
        now = time.time()
        sleep_secs = CHECK_INTERVAL - (now % CHECK_INTERVAL)
        logger.debug("Sleeping %.0fs until next check boundary", sleep_secs)
        time.sleep(sleep_secs)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    args = sys.argv[1:]
    if "--dry-run" in args:
        DRY_RUN = True
        args.remove("--dry-run")
        logger.info("[DRY-RUN] Email sending disabled.")

    # --provider <key>  (default: espn-nba)
    provider_key = DEFAULT_PROVIDER_KEY
    if "--provider" in args:
        idx = args.index("--provider")
        provider_key = args[idx + 1]
        del args[idx:idx + 2]

    if provider_key not in PROVIDERS:
        logger.error("Unknown provider '%s'. Available: %s", provider_key, ', '.join(PROVIDERS))
        sys.exit(1)
    active_provider = PROVIDERS[provider_key]

    mode = args[0] if args else "loop"
    if mode == "once":
        check_once(active_provider)
    elif mode == "loop":
        run_loop(active_provider)
    else:
        logger.error("Usage: %s [once|loop] [--dry-run] [--provider <key>]", sys.argv[0])
        logger.error("Available providers: %s", ', '.join(PROVIDERS))
        sys.exit(1)
