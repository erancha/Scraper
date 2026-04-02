#!/usr/bin/env python3
"""
Scraper Agent
--------------
Generic scraper that polls one or more Sources (see sources/), detects newly completed events since the last check, and emails a summary when new results exist.
All URL-specific logic lives in source plugins under providers/.
"""

import json
import os
import smtplib
import sys
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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
STATE_FILE = Path(__file__).parent / "state.json"
EMAIL_TO = os.getenv("EMAIL_TO", "erancha@gmail.com")
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
        print("[DRY-RUN] Email would be sent – skipping actual send.")
        return

    if not SMTP_USER or not SMTP_PASS:
        print("[WARN] SMTP credentials not configured – skipping email.")
        print("[INFO] Set SMTP_USER and SMTP_PASS in .env to enable email.")
        return

    msg = MIMEMultipart("alternative")  # plain text + HTML; email client picks the best it can render
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
    print(f"[OK] Email sent to {EMAIL_TO}")


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

    # On first run, print the current state so the user sees something
    if first_run:
        print(f"\n[{datetime.now(timezone.utc).isoformat()}] "
              f"[{provider.name}] Initial fetch ({len(items)} item(s)):")
        print(provider.format_text(items, provider.heading(day_label)))

    # ---- Completion evaluation ----
    # Compare the set of completed IDs from this fetch against the IDs stored
    # in state.json.  An email is sent ONLY when at least one new ID appears
    # in the completed set that wasn't there before.
    current_completed = provider.get_completed_ids(items)
    newly_completed = current_completed - known_completed

    if not newly_completed:
        # On first run, save state even without completions so we don't repeat the initial print
        if first_run:
            prov_state["last_check"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
        return

    print(f"\n[{datetime.now(timezone.utc).isoformat()}] "
          f"[{provider.name}] {len(newly_completed)} item(s) newly completed \u2013 sending email \u2026")
    if not first_run:
        print(provider.format_text(items, provider.heading(day_label)))

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
    print(f"Scraper Agent started (provider={provider.name}, interval={CHECK_INTERVAL}s, recipient={EMAIL_TO})")
    while True:
        try:
            check_once(provider)
        except requests.RequestException as exc:
            print(f"[ERR] [{provider.name}] Network error: {exc}")
        except Exception as exc:
            print(f"[ERR] [{provider.name}] Unexpected error: {exc}")
        print(f"\n… sleeping {CHECK_INTERVAL}s …")
        time.sleep(CHECK_INTERVAL)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    args = sys.argv[1:]
    if "--dry-run" in args:
        DRY_RUN = True
        args.remove("--dry-run")
        print("[DRY-RUN] Email sending disabled.")

    # --provider <key>  (default: espn-nba)
    provider_key = DEFAULT_PROVIDER_KEY
    if "--provider" in args:
        idx = args.index("--provider")
        provider_key = args[idx + 1]
        del args[idx:idx + 2]

    if provider_key not in PROVIDERS:
        print(f"[ERR] Unknown provider '{provider_key}'. Available: {', '.join(PROVIDERS)}")
        sys.exit(1)
    active_provider = PROVIDERS[provider_key]

    mode = args[0] if args else "loop"
    if mode == "once":
        check_once(active_provider)
    elif mode == "loop":
        run_loop(active_provider)
    else:
        print(f"Usage: {sys.argv[0]} [once|loop] [--dry-run] [--provider <key>]")
        print(f"Available providers: {', '.join(PROVIDERS)}")
        sys.exit(1)
