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

    def __init__(self) -> None:
        self._analysis_cache: dict[str, dict] = {}
        self._last_logged_openai_model: str | None = None

    # -- Provider identity ---------------------------------------------------

    @property
    def name(self) -> str:
        return "ESPN NBA"

    @property
    def state_key(self) -> str:
        return "espn_nba"

    @property
    def url(self) -> str:
        return "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"

    @property
    def standings_url(self) -> str:
        return "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"

    def fetch(self) -> dict:
        """Fetch scoreboard JSON and cache the standings JSON for formatting."""
        resp = requests.get(self.url, timeout=30)
        resp.raise_for_status()
        scoreboard = resp.json()

        try:
            standings_resp = requests.get(self.standings_url, timeout=30)
            standings_resp.raise_for_status()
            self._standings_data = standings_resp.json()
        except Exception:
            self._standings_data = None

        return scoreboard

    def heading(self, day_label: str) -> str:
        return f"NBA Scoreboard – {day_label}"

    # -- Parse ---------------------------------------------------------------

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

            game_id = str(game.get("id") or "")
            game["recapUrl"] = (
                f"https://www.espn.com/nba/recap/_/gameId/{game_id}"
                if game_id
                else ""
            )

            games.append(game)
        return games

    def openai_summary_instruction(self) -> str:
        return "Write a concise 3-5 sentence recap summary in Hebrew."

    def _openai_max_recap_chars(self) -> int:
        return 8000

    def _fetch_recap_summary(self, game: dict) -> str:
        recap_url = str(game.get("recapUrl") or "").strip()
        if not recap_url:
            return ""

        if not self._openai_api_key():
            return ""

        try:
            resp = requests.get(
                recap_url,
                timeout=30,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/123.0.0.0 Safari/537.36"
                    )
                },
            )
            resp.raise_for_status()
        except Exception:
            return ""

        text = self._html_to_text(resp.text)
        if not text:
            return ""

        max_chars = int(self._openai_max_recap_chars() or 0)
        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars]

        title = str(game.get("name") or "NBA game")
        analysis = self._openai_analyze_article(title=title, url=recap_url, text=text)
        summary = (analysis.get("summary") or "").strip() if isinstance(analysis, dict) else ""
        return summary

    def process_unevaluated_items(self, items: list[dict], unevaluated_ids: set[str]) -> tuple[list[dict], set[str]]:
        notify_items = [it for it in items if str(it.get("id")) in unevaluated_ids]
        for g in notify_items:
            if str(g.get("status_id")) != "3":
                continue
            try:
                summary = self._fetch_recap_summary(g)
            except Exception:
                summary = ""
            if summary:
                g["recapSummary"] = summary
        return notify_items, set(unevaluated_ids)

    # -- Completion detection ------------------------------------------------

    def get_day_label(self, data: dict) -> str:
        return data.get("day", {}).get("date", "today")

    def evaluated_ids_state_key(self) -> str | None:
        return "evaluated_ids"

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
                dt_obj = datetime.fromisoformat(dt.replace("Z", "+00:00")).astimezone()
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
                    f"  {t['abbreviation']} - {ldr['player']}  "
                    f"{ldr['category']}: {ldr['value']}"
                )

        recap_summary = (g.get("recapSummary") or "").strip()
        if recap_summary:
            rli = "\u2067"  # Right-to-Left Isolate
            pdi = "\u2069"  # Pop Directional Isolate
            lines.append("")
            lines.append(f"{rli}{recap_summary}{pdi}")

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
                dt_obj = datetime.fromisoformat(dt.replace("Z", "+00:00")).astimezone()
                time_str = dt_obj.strftime("%I:%M %p")
            except Exception:
                time_str = dt

            box_score_url = f"https://www.espn.com/nba/boxscore/_/gameId/{g['id']}"
            recap_url = g.get("recapUrl") or ""
            recap_summary = (g.get("recapSummary") or "").replace("<", "&lt;").replace(">", "&gt;")

            # Build per-team leader summaries for the email (single-line each team)
            def leaders_inline(team: dict) -> str:
                parts = []
                for ldr in team.get("leaders", []):
                    parts.append(
                        f"{ldr['player']} \u2013 "
                        f"{ldr['category']}: {ldr['value']}"
                    )
                return " | ".join(parts)

            away_leaders = leaders_inline(away)
            home_leaders = leaders_inline(home)
            leaders_row_html = ""
            if away_leaders or home_leaders:
                away_prefix = away.get("abbreviation") or away.get("name") or "Away"
                home_prefix = home.get("abbreviation") or home.get("name") or "Home"
                leaders_row_html = (
                    "<tr><td colspan='7' style='font-size:12px;line-height:1.35'>"
                    f"<b>{away_prefix}</b>: {away_leaders}<br>"
                    f"<b>{home_prefix}</b>: {home_leaders}"
                    "</td></tr>"
                )

            rows.append(
                f"<tr>"
                f"<td>{time_str}</td>"
                f"<td>{away['name']} ({away['record']})</td>"
                f"<td style='text-align:center;font-weight:bold'>{away.get('score', '-')}</td>"
                f"<td style='text-align:center;font-weight:bold'>{home.get('score', '-')}</td>"
                f"<td>{home['name']} ({home['record']})</td>"
                f"<td>{g.get('venue', '')}<br><span style='color:gray;font-size:11px'>"
                f"({home['abbreviation']} home)</span></td>"
                f"<td><a href='{box_score_url}'>Box Score</a>" + (f"<br><a href='{recap_url}'>Recap</a>" if recap_url else "") + "</td>"
                f"</tr>"
                + (f"<tr><td colspan='7' dir='rtl' style='direction:rtl;text-align:right;font-size:13px;line-height:1.35;color:#222'>{recap_summary}</td></tr>" if recap_summary else "")
                + leaders_row_html
            )

        games_table = (
            "<table border='1' cellpadding='6' cellspacing='0' "
            "style='border-collapse:collapse;font-family:Arial,sans-serif;'>"
            "<tr style='background:#1a1a2e;color:#fff;'>"
            "<th>Time</th><th>Away</th><th>Score</th><th>Score</th>"
            "<th>Home</th><th>Venue</th><th>Box Score</th></tr>"
            + "\n".join(rows)
            + "</table>"
        )

        standings_section = ""
        standings_data = getattr(self, "_standings_data", None)
        if standings_data:
            standings = self._parse_standings(standings_data)
            if standings and (standings.get("conferences") or []):
                season_label = standings.get("seasonDisplayName", "")
                standings_section += "<br><h3>NBA Standings" + (f" {season_label}" if season_label else "") + "</h3>"
                for conf in standings.get("conferences", []):
                    standings_section += "<h4>" + conf.get("name", "") + "</h4>"
                    standings_section += self._standings_to_html_table(conf)

        return games_table + standings_section

    def _parse_standings(self, data: dict) -> dict:
        conferences = []
        for child in data.get("children", []):
            if not child.get("isConference"):
                continue

            standings_obj = (child.get("standings") or {})
            entries = standings_obj.get("entries", [])
            rows = []
            for e in entries:
                team = (e.get("team") or {})
                stats = e.get("stats", [])

                def stat_display(name: str) -> str:
                    """Return the human-formatted (string) display value for a given stat.

                    Uses ESPN's `displayValue` field (e.g. ".727", "W2", "4.5"), which is
                    intended for presentation.
                    """
                    for s in stats:
                        if s.get("name") == name or s.get("type") == name:
                            return str(s.get("displayValue", ""))
                    return ""

                def stat_value(name: str) -> float:
                    """Return the numeric value for a given stat (for sorting/math).

                    Uses ESPN's raw `value` field, which is suitable for comparisons and
                    ordering (e.g. winPercent as 0.72727275).
                    """
                    for s in stats:
                        if s.get("name") == name or s.get("type") == name:
                            try:
                                return float(s.get("value", 0.0) or 0.0)
                            except Exception:
                                return 0.0
                    return 0.0

                def record_summary(record_type: str) -> str:
                    """Return the record summary string for record-type stats.

                    Example: lasttengames -> "8-2".
                    """
                    for s in stats:
                        if s.get("type") == record_type:
                            return str(s.get("summary", s.get("displayValue", "")))
                    return ""

                seed = stat_display("playoffSeed")
                clincher = stat_display("clincher")
                rows.append(
                    {
                        "seed": seed,
                        "clincher": clincher,
                        "abbr": team.get("abbreviation", ""),
                        "team": team.get("displayName", ""),
                        "wins": stat_display("wins"),
                        "losses": stat_display("losses"),
                        "pct": stat_display("winPercent"),
                        "pct_value": stat_value("winPercent"),
                        "gb": stat_display("gamesBehind"),
                        "streak": stat_display("streak"),
                        "l10": record_summary("lasttengames"),
                    }
                )

            """Sort standings within the conference by winning percentage (PCT) descending.

            - Primary key: `pct_value` (float), sorted descending by using `-pct_value` (Python sorts ascending by default.)
            - Secondary key: `seed` as a stable tiebreaker to keep deterministic output when two teams have the same PCT.

            The `key` function is called once per element in `rows`.
            Each element is a single standings row dict (one team).
            """
            rows.sort(key=lambda row: (-float(row.get("pct_value", 0.0) or 0.0), str(row.get("seed", ""))))

            conferences.append(
                {
                    "name": child.get("name", ""),
                    "abbreviation": child.get("abbreviation", ""),
                    "rows": rows,
                }
            )

        season_display_name = ""
        for child in data.get("children", []):
            standings_obj = (child.get("standings") or {})
            season_display_name = standings_obj.get("seasonDisplayName") or season_display_name

        return {"seasonDisplayName": season_display_name, "conferences": conferences}

    def _standings_to_html_table(self, conf: dict) -> str:
        rows = []
        for r in conf.get("rows", []):
            seed = r.get("seed", "")
            clincher = r.get("clincher", "")
            seed_cell = (clincher + " " if clincher else "") + str(seed)
            rows.append(
                "<tr>"
                f"<td style='text-align:center'>{seed_cell}</td>"
                f"<td>{r.get('abbr','')} &nbsp; {r.get('team','')}</td>"
                f"<td style='text-align:center'>{r.get('wins','')}</td>"
                f"<td style='text-align:center'>{r.get('losses','')}</td>"
                f"<td style='text-align:center'>{r.get('pct','')}</td>"
                f"<td style='text-align:center'>{r.get('gb','')}</td>"
                f"<td style='text-align:center'>{r.get('streak','')}</td>"
                f"<td style='text-align:center'>{r.get('l10','')}</td>"
                "</tr>"
            )

        return (
            "<table border='1' cellpadding='6' cellspacing='0' "
            "style='border-collapse:collapse;font-family:Arial,sans-serif;'>"
            "<tr style='background:#1a1a2e;color:#fff;'>"
            "<th>Seed</th><th>Team</th><th>W</th><th>L</th><th>PCT</th><th>GB</th><th>STRK</th><th>L10</th>"
            "</tr>"
            + "\n".join(rows)
            + "</table>"
        )
