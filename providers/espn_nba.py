"""
ESPN NBA Scoreboard provider.

All logic specific to https://www.espn.com/nba/scoreboard lives here:
URL, JSON parsing, game-completion detection, and text/HTML formatting.
"""

from datetime import datetime

import requests

from .base import Provider


class EspnNba(Provider):
    """Scrapes the ESPN NBA scoreboard via their public JSON API."""

    SCOREBOARD_URL = (
        "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
    )

    # -- Provider identity ---------------------------------------------------

    @property
    def name(self) -> str:
        return "ESPN NBA"

    @property
    def state_key(self) -> str:
        return "espn_nba"

    def heading(self, day_label: str) -> str:
        return f"NBA Scoreboard – {day_label}"

    # -- Fetch & parse -------------------------------------------------------

    def fetch(self) -> dict:
        """Fetch ESPN NBA scoreboard JSON for today."""
        resp = requests.get(self.SCOREBOARD_URL, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def parse(self, data: dict) -> list[dict]:
        """Parse the ESPN JSON into a flat list of game dicts."""
        games = []
        for event in data.get("events", []):
            game: dict = {"id": event["id"], "name": event.get("name", "")}

            competition = event["competitions"][0]
            game["date"] = competition.get("date", "")
            game["venue"] = competition.get("venue", {}).get("fullName", "")
            venue_address = competition.get("venue", {}).get("address", {})
            city = venue_address.get("city", "")
            state = venue_address.get("state", "")
            game["location"] = f"{city}, {state}" if city else ""

            status_obj = competition.get("status", {})
            status_type = status_obj.get("type", {})
            game["status"] = status_type.get("description", "")
            game["status_id"] = status_type.get("id", "0")  # 1=scheduled, 2=in-progress, 3=final
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
                game["ticketLink"] = (
                    tickets[0].get("links", [{}])[0].get("href", "")
                    if tickets[0].get("links") else ""
                )
            else:
                game["tickets"] = ""
                game["ticketLink"] = ""

            games.append(game)
        return games

    # -- Completion detection ------------------------------------------------

    def get_day_label(self, data: dict) -> str:
        return data.get("day", {}).get("date", "today")

    def get_completed_ids(self, items: list[dict]) -> set[str]:
        """status_id '3' means Final in ESPN's API."""
        return {g["id"] for g in items if str(g.get("status_id")) == "3"}

    # -- Formatting ----------------------------------------------------------

    def item_to_text(self, g: dict) -> str:
        """Render a single game as console-friendly plain text."""
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

    def items_to_html_table(self, items: list[dict]) -> str:
        """Build an HTML table summarising all games (used in the email body)."""
        rows = []
        for g in items:
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
                        f"{ldr['player']} #{ldr['jersey']} \u2013 "
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
