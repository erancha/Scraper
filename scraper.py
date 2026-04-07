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
import subprocess
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
import re

import requests
from dotenv import load_dotenv

from providers import DEFAULT_PROVIDER_KEY, PROVIDERS
from providers.base import Provider

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_log_level_name = (os.getenv("LOG_LEVEL", "INFO") or "INFO").strip()
_log_level_name = _log_level_name.split("#", 1)[0].strip().upper()
LOG_LEVEL = getattr(logging, _log_level_name, logging.INFO)
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
STATE_FILE = Path(os.getenv("STATE_FILE", str(Path(__file__).parent / "state.json")))
EMAIL_TO: list[str] = []
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # seconds between checks
DRY_RUN = False  # Set via --dry-run CLI flag; skips actual email sending


def _provider_env_key(provider_key: str) -> str:
    # Convert provider keys like "espn-nba" into a safe env-var suffix: "ESPN_NBA".
    return re.sub(r"[^a-zA-Z0-9]+", "_", provider_key.strip()).strip("_").upper()


def _getenv_provider_scoped(name: str, provider_key: str) -> str:
    # Resolution order:
    # - <NAME>__<PROVIDER_ENV_KEY> (e.g. EMAIL_TO__ESPN_NBA)
    # - <NAME> (global default)
    scoped = f"{name}__{_provider_env_key(provider_key)}"
    return (os.getenv(scoped) or os.getenv(name) or "").strip()


# -------------------------------------------------------------------------------------------------------------------------------
# State helpers – Persist which events have already been reported as completed between runs, using a local JSON file (state.json).
# This prevents duplicate emails: an event is only emailed about once, the first time it appears as completed.
# State is keyed per source (e.g. state["espn_nba"]["completed_ids"]) so multiple sources coexist cleanly.
# -------------------------------------------------------------------------------------------------------------------------------
def _state_file_for_provider(provider_key: str) -> Path:
    suffix = re.sub(r"[^a-zA-Z0-9._-]+", "_", provider_key.strip())
    return STATE_FILE.with_name(f"{STATE_FILE.stem}.{suffix}{STATE_FILE.suffix}")


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
        # Initialize per-provider state on first run (notified/evaluated IDs + last check timestamp).
        provider_state_data = {provider.notified_ids_state_key(): [], "last_check": None}
        evaluated_key = provider.evaluated_ids_state_key()
        if evaluated_key:
            provider_state_data[evaluated_key] = []
        state[key] = provider_state_data
    return state[key]


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def send_email(subject: str, html_body: str, plain_body: str) -> None:
    """Send an email via SMTP (TLS). Skipped when DRY_RUN is True."""
    if DRY_RUN:
        logger.info("[DRY-RUN] Email would be sent \u2013 skipping actual send.\nSubject: %s\n%s", subject, plain_body)
        return

    if not EMAIL_TO:
        logger.warning("EMAIL_TO not configured \u2013 skipping email.")
        logger.info("Set EMAIL_TO in .env to enable email.")
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
    provider_state_data = provider_state(state, provider)
    notified_key = provider.notified_ids_state_key()
    known_notified_ids: set[str] = set(provider_state_data.get(notified_key, []))

    data = provider.fetch()
    items = provider.parse(data)

    evaluated_key = provider.evaluated_ids_state_key()
    if evaluated_key:
        known_evaluated_ids: set[str] = set(provider_state_data.get(evaluated_key, []))
        current_ids = {str(i.get("id")) for i in items if i.get("id")} # a set of the IDs currently present in this scrape, normalized to strings
        unevaluated_ids = current_ids - known_evaluated_ids # IDs that are present now but were not previously recorded as evaluated
        items, evaluated_ids_to_add = provider.process_unevaluated_items(items, unevaluated_ids)
        provider_state_data[evaluated_key] = list(known_evaluated_ids | evaluated_ids_to_add)
        save_state(state)

    current_completed_ids = provider.get_completed_ids(items)
    current_completed_items = [item for item in items if str(item.get("id")) in current_completed_ids]

    first_run = provider_state_data.get("last_check") is None
    if first_run:
        provider_state_data["last_check"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

    if not current_completed_items:
        return

    # ---- Completion evaluation ----
    # Compare the set of completed IDs from this fetch against the IDs stored in state.json.
    # An email should be sent ONLY when at least one new ID appears in the completed set that wasn't there before.
    newly_ids_to_be_notified = current_completed_ids - known_notified_ids

    if not newly_ids_to_be_notified:
        return

    logger.info("[%s] %d item(s) newly notified \u2013 sending email \u2026", provider.name, len(newly_ids_to_be_notified))

    day_label = provider.get_day_label(data)
    
    if not first_run:
        logger.info("%s", provider.items_to_plain_table(current_completed_items, provider.heading(day_label)))

    subject = f"{provider.name} Update \u2013 {day_label}"
    html_body = (
        f"<h2>{provider.heading(day_label)}</h2>"
        + provider.items_to_html_table(current_completed_items)
        + "<br><p style='color:gray;font-size:12px;'>Sent by Scraper Agent</p>"
    )
    plain_body = provider.items_to_plain_table(current_completed_items, provider.heading(day_label))
    send_email(subject, html_body, plain_body)

    # Update state only when new completions are found
    provider_state_data[notified_key] = list(known_notified_ids | current_completed_ids)
    provider_state_data["last_check"] = datetime.now(timezone.utc).isoformat()
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


def _spawn_provider_process(provider_key: str, mode: str) -> subprocess.Popen:
    script_path = str(Path(__file__).resolve())
    cmd = [sys.executable, script_path, mode, "--provider", provider_key]

    env = os.environ.copy()
    env["STATE_FILE"] = os.environ.get("STATE_FILE") or str(Path(__file__).parent / "state.json")
    return subprocess.Popen(cmd, env=env)


def run_all_isolated(mode: str) -> int:
    provider_keys = list(PROVIDERS.keys())
    logger.info("Starting %d isolated provider process(es): %s", len(provider_keys), ", ".join(provider_keys))

    procs: list[tuple[str, subprocess.Popen]] = []
    for key in provider_keys:
        procs.append((key, _spawn_provider_process(key, mode=mode)))

    try:
        while True:
            time.sleep(1)
            for key, p in procs:
                rc = p.poll()
                if rc is not None:
                    logger.error("Provider '%s' exited unexpectedly with code %s", key, rc)
                    return rc
    except KeyboardInterrupt:
        logger.info("Stopping all provider processes ...")
        for _, p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        for _, p in procs:
            try:
                p.wait(timeout=10)
            except Exception:
                pass
        return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    args = sys.argv[1:]
    if "--dry-run" in args:
        DRY_RUN = True
        args.remove("--dry-run")
        logger.info("[DRY-RUN] Email sending disabled.")

    run_all = False
    if "--all" in args:
        run_all = True
        args.remove("--all")

    # --provider <key>  (default: espn-nba)
    provider_key = DEFAULT_PROVIDER_KEY
    if "--provider" in args:
        idx = args.index("--provider")
        provider_key = args[idx + 1]
        del args[idx:idx + 2]

    if provider_key == "all":
        run_all = True

    mode = args[0] if args else "loop"

    if mode not in {"once", "loop"}:
        logger.error("Usage: %s [once|loop] [--dry-run] [--provider <key>|all] [--all]", sys.argv[0])
        logger.error("Available providers: %s", ', '.join(PROVIDERS))
        sys.exit(1)

    if run_all:
        if mode == "once" or DRY_RUN:
            if mode == "once":
                logger.error("'once' mode is intended for per-provider testing. Use: %s once --provider <key>", sys.argv[0])
            else:
                logger.error("'--dry-run' is intended for per-provider testing. Use: %s %s --provider <key> --dry-run", sys.argv[0], mode)
            sys.exit(1)
        sys.exit(run_all_isolated(mode=mode))

    if provider_key not in PROVIDERS:
        logger.error("Unknown provider '%s'. Available: %s", provider_key, ', '.join(PROVIDERS))
        sys.exit(1)

    # Resolve recipient list after provider selection (supports provider-scoped overrides).
    EMAIL_TO = [
        addr.strip()
        for addr in _getenv_provider_scoped("EMAIL_TO", provider_key).split(",")
        if addr.strip()
    ]

    # Make per-provider state files the default even when running a single provider.
    STATE_FILE = _state_file_for_provider(provider_key)
    active_provider = PROVIDERS[provider_key]

    if mode == "once":
        check_once(active_provider)
    else:
        run_loop(active_provider)
