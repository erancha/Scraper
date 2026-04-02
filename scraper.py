#!/usr/bin/env python3
"""
NBA Scoreboard Scraper Agent
-----------------------------
Scrapes ESPN NBA scoreboard data via their public API, detects newly completed
games since the last check, and emails a summary table when new results exist.
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

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
)
STATE_FILE = Path(__file__).parent / "state.json"
EMAIL_TO = os.getenv("EMAIL_TO", "erancha@gmail.com")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # seconds between checks


# -------------------------------------------------------------------------------------------------------------------------------
# State helpers – Persist which games have already been reported as completed between runs, using a local JSON file (state.json). 
# This prevents duplicate emails: a game is only emailed about once, the first time it appears as Final.
# -------------------------------------------------------------------------------------------------------------------------------
def load_state() -> dict:
    """Return previously recorded completed-game IDs and last-check timestamp."""
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"completed_game_ids": [], "last_check": None}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ------------------------------------------------------------------------------------------------------------------------------------
# ESPN API helpers – Fetch and parse the NBA scoreboard from ESPN's public JSON API (no API key required). 
# fetch_scoreboard() retrieves the raw JSON;
# parse_games() flattens it into a list of game dicts with teams, scores, status, odds, venue, broadcast, tickets, and player leaders.
# ------------------------------------------------------------------------------------------------------------------------------------
def fetch_scoreboard(date_str: str | None = None) -> dict:
    """Fetch ESPN NBA scoreboard JSON. *date_str* format: YYYYMMDD (optional)."""
    params = {}
    if date_str:
        params["dates"] = date_str
    resp = requests.get(SCOREBOARD_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def parse_games(data: dict) -> list[dict]:
    """Parse the ESPN JSON into a flat list of game dicts."""
    games = []
    for event in data.get("events", []):
        game: dict = {"id": event["id"], "name": event.get("name", "")}

        competition = event["competitions"][0]
        game["date"] = competition.get("date", "")
        game["venue"] = (
            competition.get("venue", {}).get("fullName", "")
        )
        venue_address = competition.get("venue", {}).get("address", {})
        city = venue_address.get("city", "")
        state = venue_address.get("state", "")
        game["location"] = f"{city}, {state}" if city else ""

        status_obj = competition.get("status", {})
        status_type = status_obj.get("type", {})
        game["status"] = status_type.get("description", "")
        game["status_id"] = status_type.get("id", "0")  # 1=scheduled,2=in-progress,3=final
        game["clock"] = status_obj.get("displayClock", "")
        game["period"] = status_obj.get("period", 0)

        # Broadcast
        broadcasts = competition.get("broadcasts", [])
        broadcast_names = []
        for b in broadcasts:
            for n in b.get("names", []):
                broadcast_names.append(n)
        game["broadcast"] = ", ".join(broadcast_names)

        # Odds
        odds_list = competition.get("odds", [])
        if odds_list:
            odds = odds_list[0]
            game["spread"] = odds.get("details", "")
            game["overUnder"] = odds.get("overUnder", "")
            game["provider"] = odds.get("provider", {}).get("name", "")
        else:
            game["spread"] = ""
            game["overUnder"] = ""
            game["provider"] = ""

        # Teams & scores
        teams_info = []
        for comp_team in competition.get("competitors", []):
            team_data = comp_team.get("team", {})
            record_items = comp_team.get("records", [])
            overall_record = record_items[0]["summary"] if record_items else ""
            home_away_record = record_items[1]["summary"] if len(record_items) > 1 else ""
            ha_label = comp_team.get("homeAway", "")

            # Leaders / players to watch
            leaders = []
            for leader_cat in comp_team.get("leaders", []):
                cat_name = leader_cat.get("abbreviation", leader_cat.get("name", ""))
                for ldr in leader_cat.get("leaders", [])[:1]:
                    athlete = ldr.get("athlete", {})
                    leaders.append({
                        "category": cat_name,
                        "value": ldr.get("displayValue", ""),
                        "player": athlete.get("displayName", ""),
                        "jersey": athlete.get("jersey", ""),
                    })

            teams_info.append({
                "name": team_data.get("displayName", ""),
                "abbreviation": team_data.get("abbreviation", ""),
                "score": comp_team.get("score", ""),
                "homeAway": ha_label,
                "record": overall_record,
                "homeAwayRecord": home_away_record,
                "leaders": leaders,
            })

        game["teams"] = teams_info

        # Tickets
        tickets = competition.get("tickets", [])
        if tickets:
            game["tickets"] = tickets[0].get("summary", "")
            game["ticketLink"] = tickets[0].get("links", [{}])[0].get("href", "") if tickets[0].get("links") else ""
        else:
            game["tickets"] = ""
            game["ticketLink"] = ""

        games.append(game)
    return games


# -------------------------------------------------------------------------------------------------------------------
# Display helpers – Format game data for output. game_to_text() renders a single game as console-friendly plain text; 
# scoreboard_text() combines all games into a full scoreboard; 
# games_to_html_table() builds the HTML table used in email notifications.
# -------------------------------------------------------------------------------------------------------------------
def game_to_text(g: dict) -> str:
    """Render a single game dict as human-readable plain text."""
    lines: list[str] = []

    dt = g.get("date", "")
    if dt:
        try:
            dt_obj = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            dt_display = dt_obj.strftime("%I:%M %p  %b %d, %Y %Z")
        except Exception:
            dt_display = dt
    else:
        dt_display = ""

    away = next((t for t in g["teams"] if t["homeAway"] == "away"), None)
    home = next((t for t in g["teams"] if t["homeAway"] == "home"), None)

    def team_line(t: dict, label: str) -> str:
        score_part = f"  {t['score']}" if t.get("score") else ""
        rec_part = f"  ({t['record']}  {t['homeAwayRecord']} {label})" if t.get("record") else ""
        return f"  {t['name']}{score_part}{rec_part}"

    status_line = g["status"]
    if g.get("broadcast"):
        status_line += f"  [{g['broadcast']}]"

    lines.append(f"{dt_display}   {status_line}")
    if away:
        lines.append(team_line(away, "Away"))
    if home:
        lines.append(team_line(home, "Home"))

    if g.get("venue"):
        loc = f"{g['venue']}, {g['location']}" if g["location"] else g["venue"]
        lines.append(f"  Venue: {loc}")

    if g.get("spread") or g.get("overUnder"):
        odds_parts = []
        if g["spread"]:
            odds_parts.append(f"Spread: {g['spread']}")
        if g["overUnder"]:
            odds_parts.append(f"O/U: {g['overUnder']}")
        prov = f" ({g['provider']})" if g.get("provider") else ""
        lines.append(f"  Odds{prov}: {' | '.join(odds_parts)}")

    if g.get("tickets"):
        lines.append(f"  Tickets: {g['tickets']}")

    # Players to watch
    for t in g["teams"]:
        for ldr in t.get("leaders", []):
            lines.append(
                f"  {t['abbreviation']} - {ldr['player']} #{ldr['jersey']}  "
                f"{ldr['category']}: {ldr['value']}"
            )

    return "\n".join(lines)


def scoreboard_text(games: list[dict], heading: str) -> str:
    """Full scoreboard as plain text."""
    sections = [heading, "=" * len(heading), ""]
    for g in games:
        sections.append(game_to_text(g))
        sections.append("-" * 60)
    return "\n".join(sections)


def games_to_html_table(games: list[dict]) -> str:
    """Build an HTML table summarising all games (used in the email body)."""
    rows = []
    for g in games:
        away = next((t for t in g["teams"] if t["homeAway"] == "away"), None)
        home = next((t for t in g["teams"] if t["homeAway"] == "home"), None)
        if not away or not home:
            continue

        dt = g.get("date", "")
        try:
            dt_obj = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            time_str = dt_obj.strftime("%I:%M %p")
        except Exception:
            time_str = dt

        box_score_url = f"https://www.espn.com/nba/boxscore/_/gameId/{g['id']}"

        # Build per-team leader summaries for the email
        def leaders_html(team: dict) -> str:
            parts = []
            for ldr in team.get("leaders", []):
                parts.append(
                    f"{ldr['player']} #{ldr['jersey']} – "
                    f"{ldr['category']}: {ldr['value']}"
                )
            return "<br>".join(parts)

        away_leaders = leaders_html(away)
        home_leaders = leaders_html(home)

        rows.append(
            f"<tr>"
            f"<td>{time_str}</td>"
            f"<td>{away['name']} ({away['record']})</td>"
            f"<td style='text-align:center;font-weight:bold'>{away.get('score', '-')}</td>"
            f"<td style='text-align:center;font-weight:bold'>{home.get('score', '-')}</td>"
            f"<td>{home['name']} ({home['record']})</td>"
            f"<td style='font-size:12px'>{away_leaders}</td>"
            f"<td style='font-size:12px'>{home_leaders}</td>"
            f"<td>{g.get('venue', '')}<br><span style='color:gray;font-size:11px'>"
            f"({home['abbreviation']} home)</span></td>"
            f"<td><a href='{box_score_url}'>Box Score</a></td>"
            f"</tr>"
        )

    return (
        "<table border='1' cellpadding='6' cellspacing='0' "
        "style='border-collapse:collapse;font-family:Arial,sans-serif;'>"
        "<tr style='background:#1a1a2e;color:#fff;'>"
        "<th>Time</th><th>Away</th><th>Score</th><th>Score</th>"
        "<th>Home</th><th>Away Leaders</th><th>Home Leaders</th>"
        "<th>Venue</th><th>Box Score</th></tr>"
        + "\n".join(rows)
        + "</table>"
    )


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
DRY_RUN = False  # Set via --dry-run CLI flag; skips actual email sending


def send_email(subject: str, html_body: str, plain_body: str) -> None:
    """Send an email via SMTP (TLS). Skipped when DRY_RUN is True."""
    if DRY_RUN:
        print("[DRY-RUN] Email would be sent – skipping actual send.")
        return

    if not SMTP_USER or not SMTP_PASS:
        print("[WARN] SMTP credentials not configured – skipping email.")
        print("[INFO] Set SMTP_USER and SMTP_PASS in .env to enable email.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
    print(f"[OK] Email sent to {EMAIL_TO}")


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------
def check_once() -> None:
    """Run a single scrape-check-email cycle."""
    state = load_state()
    known_completed: set[str] = set(state.get("completed_game_ids", []))

    data = fetch_scoreboard()
    games = parse_games(data)

    if not games:
        state["last_check"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    day_label = data.get("day", {}).get("date", "today")

    # ---- Game-completion evaluation ----
    # Each game has a status_id from ESPN: 1 = Scheduled, 2 = In Progress, 3 = Final.  
    # We compare the set of Final game IDs from this fetch against the IDs stored in state.json.  
    # An email is sent ONLY when at least one new game ID appears in the Final set that wasn't there before.
    current_completed = {g["id"] for g in games if str(g.get("status_id")) == "3"}
    newly_completed = current_completed - known_completed

    if newly_completed:
        print(f"\n[{datetime.now(timezone.utc).isoformat()}] "
              f"{len(newly_completed)} game(s) newly completed – sending email …")
        print(scoreboard_text(games, f"NBA Scoreboard – {day_label}"))
        subject = f"NBA Scoreboard Update – {day_label}"
        html_body = (
            f"<h2>NBA Scoreboard – {day_label}</h2>"
            + games_to_html_table(games)
            + "<br><p style='color:gray;font-size:12px;'>Sent by NBA Scraper Agent</p>"
        )
        plain_body = scoreboard_text(games, f"NBA Scoreboard – {day_label}")
        send_email(subject, html_body, plain_body)

    # Update state
    state["completed_game_ids"] = list(known_completed | current_completed)
    state["last_check"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


def run_loop() -> None:
    """Continuously poll the scoreboard at CHECK_INTERVAL seconds."""
    print(f"NBA Scraper Agent started (interval={CHECK_INTERVAL}s, recipient={EMAIL_TO})")
    while True:
        try:
            check_once()
        except requests.RequestException as exc:
            print(f"[ERR] Network error: {exc}")
        except Exception as exc:
            print(f"[ERR] Unexpected error: {exc}")
        print(f"\n… sleeping {CHECK_INTERVAL}s …\n")
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

    mode = args[0] if args else "loop"
    if mode == "once":
        check_once()
    elif mode == "loop":
        run_loop()
    else:
        print(f"Usage: {sys.argv[0]} [once|loop] [--dry-run]")
        sys.exit(1)
