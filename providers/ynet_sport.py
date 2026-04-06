"""Ynet Sport provider.

Scrapes https://www.ynet.co.il/sport and reports newly published articles.

This provider tracks two sets of IDs in state.json:
- evaluated_ids: article URLs we already evaluated (including non-NBA) to avoid repeated LLM work
- notified_ids: NBA-related articles that were already emailed
"""

from __future__ import annotations

import logging
import re
from .ynet_ai_html_base import YnetAiHtmlProviderBase


logger = logging.getLogger(__name__)

class YnetSport(YnetAiHtmlProviderBase):
    @property
    def name(self) -> str:
        return "Ynet Sport"

    @property
    def state_key(self) -> str:
        return "ynet_sport"

    @property
    def url(self) -> str:
        return "https://www.ynet.co.il/sport/worldbasketball"

    @property
    def allowed_path_prefixes(self) -> tuple[str, ...]:
        return ("/sport/worldbasketball",)

    def is_relevant(self, title: str, url: str, text: str, analysis: dict) -> bool:
        if analysis:
            return bool(analysis.get("is_nba"))
        return self._is_nba_fallback(title, text)

    def openai_system_prompt(self) -> str:
        return (
            "You are a strict classifier and summarizer for sports news. "
            "Return ONLY valid JSON with keys: is_nba (boolean), summary (string)."
        )

    def openai_user_prompt_prefix(self) -> str:
        return (
            "Determine whether the following article is primarily about the NBA (teams, players, games, trades, draft, "
            "coaching, injuries, analysis). If it is about Euroleague / FIBA / NCAA / WNBA / general basketball, mark false."
        )

    def _openai_analyze_article(self, title: str, url: str, text: str) -> dict:
        result = super()._openai_analyze_article(title=title, url=url, text=text)
        summary = (result.get("summary") or "").strip()
        if summary:
            summary = re.sub(r"\bNBA\b", "נ.ב.א", summary, flags=re.IGNORECASE)
            summary = re.sub(r"\bMVP\b", "מצטיין העונה", summary, flags=re.IGNORECASE)
            summary = re.sub(r"\bMRI\b", "תהודה מגנטית", summary, flags=re.IGNORECASE)
            summary = re.sub(r"\bGM\b", "ג'נרל מנג'ר", summary, flags=re.IGNORECASE)
            result["summary"] = summary.strip()
        return result

    def _is_nba_fallback(self, title: str, text: str) -> bool:
        """Keyword-based NBA classifier used when OpenAI analysis is unavailable or skipped."""
        haystack = f"{title} {text}".lower()
        keywords = [
            "nba",
            "playoffs",
            "finals",
            "lakers",
            "warriors",
            "celtics",
            "lebron",
            "curry",
            "durant",
            "giannis",
            "jokic",
            "doncic",
            "wembanyama",
        ]
        return any(k in haystack for k in keywords)
