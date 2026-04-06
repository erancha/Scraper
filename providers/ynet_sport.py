"""Ynet Sport provider.

Scrapes https://www.ynet.co.il/sport and reports newly published articles.

This provider tracks two sets of IDs in state.json:
- evaluated_ids: article URLs we already evaluated (including non-NBA) to avoid repeated LLM work
- notified_ids: NBA-related articles that were already emailed
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
# Ynet provides an HTML page (not a JSON API), so we parse the DOM to extract article links.
from bs4 import BeautifulSoup

from .base import Provider


logger = logging.getLogger(__name__)


class YnetSport(Provider):
    def __init__(self) -> None:
        self._analysis_cache: dict[str, dict] = {}
        self._last_logged_openai_model: str | None = None

    @property
    def name(self) -> str:
        return "Ynet Sport"

    @property
    def state_key(self) -> str:
        return "ynet_sport"

    @property
    def url(self) -> str:
        return "https://www.ynet.co.il/sport/worldbasketball"

    def fetch(self) -> dict:
        """Fetch HTML (non-JSON) and wrap it in a dict for the base interface."""
        resp = requests.get(
            self.url,
            timeout=30,
            # Use a browser-like UA to avoid bot blocks / simplified HTML variants.
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                )
            },
        )
        resp.raise_for_status()
        return {"html": resp.text, "fetched_at": datetime.utcnow().isoformat()}

    def evaluated_ids_state_key(self) -> str | None:
        return "evaluated_ids"

    def process_unevaluated_items(self, items: list[dict], unevaluated_ids: set[str]) -> tuple[list[dict], set[str]]:
        """Process only newly-unevaluated candidate items.

        Parameters:
        - items: all candidate items extracted from the listing page (from parse()).
        - unevaluated_ids: IDs that appear in this fetch but are not yet in state.json's evaluated set.

        Flow:
        - Filter items keeping only the items in unevaluated_ids.
        - For each unevaluated item: fetch article text, summarize/classify via OpenAI (if configured), and keep only NBA-related items.

        Returns:
        - kept_items: NBA-only items (optionally enriched with summary) to be considered for notification/email.
        - evaluated_ids_to_add: IDs that should be added to the evaluated set (we return all unevaluated_ids, including non-NBA,
          so we don't re-run LLM on the same URL in future polls).
        """
        # Mark every new URL as evaluated (including non-NBA) to avoid repeated LLM calls.
        # But only return NBA-related items to be considered for completion/email.
        if not unevaluated_ids:
            logger.debug("[%s] No unevaluated ids", self.name)
            return [], set()

        # - Filter items keeping only the items in unevaluated_ids.
        unevaluated_items = [it for it in items if str(it.get("id")) in unevaluated_ids]

        # - For each unevaluated item: fetch article text, summarize/classify via OpenAI (if configured), and keep only NBA-related items.
        kept: list[dict] = []
        for it in unevaluated_items[:25]:
            url = str(it.get("url") or "")
            title = str(it.get("title") or "")
            if not url:
                continue

            try:
                logger.debug("[%s] Fetching article: %s", self.name, url)
                soup = self._fetch_article_soup(url)
                text = self._extract_article_text(soup)
                published_at = self._extract_published_at(soup)
                if published_at:
                    it["published_at"] = published_at
            except Exception as exc:
                logger.warning("[%s] Failed to fetch article: %s (%s)", self.name, url, exc)
                text = ""

            analysis = {}
            if self._openai_api_key() and text:
                try:
                    logger.debug("[%s] Summarizing/classifying via OpenAI: %s", self.name, url)
                    analysis = self._openai_analyze_article(title=title, url=url, text=text)
                except Exception as exc:
                    logger.warning("[%s] OpenAI analysis failed: %s (%s)", self.name, url, exc)
                    analysis = {}

            is_nba = bool(analysis.get("is_nba")) if analysis else self._is_nba_fallback(title, text)
            if not is_nba:
                logger.debug("[%s] Filtered out (non-NBA): %s", self.name, url)
                continue

            summary = (analysis.get("summary") or "").strip() if analysis else ""
            if summary:
                it["summary"] = summary

            kept.append(it)
            logger.debug("[%s] Kept (NBA): %s", self.name, url)
            if len(kept) >= 40:
                break

        return kept, unevaluated_ids

    def _openai_api_key(self) -> str:
        return (os.getenv("OPENAI_API_KEY") or "").strip()

    def _openai_model(self) -> str:
        raw = (os.getenv("OPENAI_MODEL") or "").strip()
        if raw:
            # Be robust to inline comments in .env like: OPENAI_MODEL=gpt-4o-mini  # optional
            raw = raw.split("#", 1)[0].strip()
        return raw or "gpt-4o-mini"

    def _estimate_openai_cost_usd(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        # Optional overrides for pricing (USD per 1M tokens); if unset/invalid, fall back to model defaults below.
        input_per_1m_override = (os.getenv("OPENAI_INPUT_COST_PER_1M") or "").strip()
        output_per_1m_override = (os.getenv("OPENAI_OUTPUT_COST_PER_1M") or "").strip()

        def _to_float(v: str) -> float | None:
            try:
                return float(v)
            except Exception:
                return None

        input_per_1m = _to_float(input_per_1m_override)
        output_per_1m = _to_float(output_per_1m_override)

        if input_per_1m is None or output_per_1m is None:
            pricing_per_1m = {
                "gpt-4o-mini": (0.15, 0.60),
                "gpt-4o": (5.00, 15.00),
            }
            pricing_model = model if model in pricing_per_1m else "gpt-4o-mini"
            default_in, default_out = pricing_per_1m[pricing_model]
            if input_per_1m is None:
                input_per_1m = default_in
            if output_per_1m is None:
                output_per_1m = default_out

        return (prompt_tokens / 1_000_000.0) * float(input_per_1m) + (completion_tokens / 1_000_000.0) * float(output_per_1m)

    def _fetch_article_text(self, url: str) -> str:
        """Fetch an article and return a truncated plain-text version suitable for LLM input."""
        soup = self._fetch_article_soup(url)
        text = self._extract_article_text(soup)
        return text[:8000]

    def _fetch_article_soup(self, url: str) -> BeautifulSoup:
        """HTTP-fetch an article URL and parse it into a BeautifulSoup document."""
        resp = requests.get(
            url,
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
        return BeautifulSoup(resp.text, "html.parser")

    def _extract_article_text(self, soup: BeautifulSoup) -> str:
        """Extract readable text from a soup document, stripping scripts/styles and collapsing whitespace."""
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        article = soup.find("article")
        container = article if article is not None else soup
        text = container.get_text(" ", strip=True)
        return " ".join(text.split())

    def _extract_published_at(self, soup: BeautifulSoup) -> str:
        """Best-effort extraction of the published timestamp from common meta tags or a <time> element."""
        meta_candidates = [
            ("property", "article:published_time"),
            ("property", "og:published_time"),
            ("name", "publish_date"),
            ("name", "pubdate"),
            ("name", "date"),
            ("name", "dc.date"),
            ("name", "DC.date.issued"),
        ]

        for attr, key in meta_candidates:
            tag = soup.find("meta", attrs={attr: key})
            if tag and tag.get("content"):
                return str(tag.get("content") or "").strip()

        time_tag = soup.find("time")
        if time_tag is not None:
            dt = time_tag.get("datetime")
            if dt:
                return str(dt).strip()
            text = time_tag.get_text(" ", strip=True)
            if text:
                return " ".join(text.split())

        return ""

    def _format_published_at(self, published_at: str) -> str:
        """Format an ISO-ish published timestamp to a local '%Y-%m-%d %H:%M' string when possible."""
        if not published_at:
            return ""

        raw = published_at.strip()
        try:
            iso = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is not None:
                dt = dt.astimezone()
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return raw

    def _openai_analyze_article(self, title: str, url: str, text: str) -> dict:
        """Call OpenAI to classify whether an article is NBA-related and to produce a Hebrew summary.

        Returns an empty dict when no API key is configured. Results are cached per-URL.
        """
        api_key = self._openai_api_key()
        if not api_key:
            return {}

        cached = self._analysis_cache.get(url)
        if cached is not None:
            return cached

        model = self._openai_model()
        if self._last_logged_openai_model != model:
            logger.info("[%s] OpenAI model=%s", self.name, model)
            self._last_logged_openai_model = model

        system = (
            "You are a strict classifier and summarizer for sports news. "
            "Return ONLY valid JSON with keys: is_nba (boolean), summary (string)."
        )
        user = (
            "Determine whether the following article is primarily about the NBA (teams, players, games, trades, draft, "
            "coaching, injuries, analysis). If it is about Euroleague / FIBA / NCAA / WNBA / general basketball, mark false. "
            "Then write a concise 8-12 sentence summary in Hebrew.\n\n"
            f"Title: {title}\n"
            f"URL: {url}\n"
            f"Article text: {text}"
        )

        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            timeout=60,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
            },
        )
        # Fail fast on non-2xx OpenAI responses: log a body preview for debugging and raise.
        # No retry is implemented here; higher-level code (process_unevaluated_items) decides whether to skip,
        # fall back (keyword check), or retry on a future poll.
        if not resp.ok:
            body_preview = (resp.text or "").strip()
            if len(body_preview) > 2000:
                body_preview = body_preview[:2000] + "..."
            logger.warning(
                "[%s] OpenAI HTTP %s for %s: %s",
                self.name,
                resp.status_code,
                url,
                body_preview,
            )
            resp.raise_for_status()

        payload = resp.json()

        usage = payload.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        est_cost_usd = self._estimate_openai_cost_usd(model, prompt_tokens, completion_tokens)

        url_width = 70
        url_display = url
        if len(url_display) > url_width:
            url_display = url_display[: url_width - 1] + "…"

        logger.info(
            "[%s] OpenAI usage for %-*s  prompt=%5d  completion=%5d  total=%5d  est_cost=$%0.6f",
            self.name,
            url_width,
            url_display,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            est_cost_usd,
        )

        content = (
            payload.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        try:
            parsed = json.loads(content) if content else {}
        except Exception:
            parsed = {}

        result = {
            "is_nba": bool(parsed.get("is_nba")) if isinstance(parsed, dict) else False,
            "summary": (parsed.get("summary") or "").strip() if isinstance(parsed, dict) else "",
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "estimated_cost_usd": est_cost_usd,
                "model": model,
            },
        }

        if result["summary"]:
            summary = result["summary"]
            summary = re.sub(r"\bNBA\b", "נ.ב.א", summary, flags=re.IGNORECASE)
            summary = re.sub(r"\bMVP\b", "מצטיין העונה", summary, flags=re.IGNORECASE)
            summary = re.sub(r"\bMRI\b", "תהודה מגנטית", summary, flags=re.IGNORECASE)
            summary = re.sub(r"\bGM\b", "ג'נרל מנג'ר", summary, flags=re.IGNORECASE)
            result["summary"] = summary.strip()

        self._analysis_cache[url] = result
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

    def parse(self, data: dict) -> list[dict]:
        html = data.get("html") or ""
        soup = BeautifulSoup(html, "html.parser")

        items: list[dict] = []
        dedup_ids: set[str] = set()

        # Normalize / clean article URLs (deduplication happens below via dedup_ids).
        def normalize_href(href: str) -> str:
            abs_url = urljoin(self.url, href)
            parsed = urlparse(abs_url)
            # remove fragment; keep query as ynet may use it for canonicalization
            return parsed._replace(fragment="").geturl()

        # We scan all <a href=...> tags and turn only the ones that look like real article links
        # (domain/path checks + meaningful text) into an item {id=url, title, url}.
        candidate_items: list[dict] = []
        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href or href.startswith("javascript:") or href.startswith("#"):
                continue

            abs_url = normalize_href(href)
            parsed = urlparse(abs_url)
            if parsed.netloc and "ynet.co.il" not in parsed.netloc:
                continue

            # Require it to be under /sport or be a typical ynet article path
            path = parsed.path or ""
            if not (path.startswith("/sport/worldbasketball")):
                continue

            title = " ".join(a.get_text(" ", strip=True).split())
            if len(title) < 10:
                continue

            item_id = abs_url
            if item_id in dedup_ids:
                continue
            dedup_ids.add(item_id)

            candidate_items.append(
                {
                    "id": item_id,
                    "title": title,
                    "url": abs_url,
                }
            )

        # Only the agent loop processes unevaluated IDs (via process_unevaluated_items), to avoid repeated LLM calls.
        return candidate_items[:40]

    def get_day_label(self, data: dict) -> str:
        # We don't have a structured "day"; show local date.
        return datetime.now().strftime("%Y-%m-%d")

    def get_completed_ids(self, items: list[dict]) -> set[str]:
        # "Completed" means "eligible for notification" (used to compute notified_ids).
        return {str(i.get("id")) for i in items if i.get("id")}

    def item_to_text(self, item: dict) -> str:
        title = item.get("title", "")
        url = item.get("url", "")
        summary = item.get("summary", "")
        published_at = self._format_published_at(str(item.get("published_at") or ""))

        header = title
        if published_at:
            header = f"[{published_at}] {title}"

        if summary:
            return f"{header}\n{url}\n\n{summary}".strip()
        return f"{header}\n{url}".strip()

    def items_to_html_table(self, items: list[dict]) -> str:
        rows = []
        for it in items:
            title = (it.get("title") or "").replace("<", "&lt;").replace(">", "&gt;")
            summary = (it.get("summary") or "").replace("<", "&lt;").replace(">", "&gt;")
            published_at = self._format_published_at(str(it.get("published_at") or ""))
            url = it.get("url") or ""
            if summary:
                rows.append(
                    "<tr><td>"
                    f"<a href='{url}'>{title}</a>"
                    + (f"<div style='margin-top:4px;color:#666;font-size:12px'>{published_at}</div>" if published_at else "")
                    + f"<div style='margin-top:6px;color:#333;font-size:13px;line-height:1.35'>{summary}</div>"
                    "</td></tr>"
                )
            else:
                rows.append(
                    "<tr><td>"
                    f"<a href='{url}'>{title}</a>"
                    + (f"<div style='margin-top:4px;color:#666;font-size:12px'>{published_at}</div>" if published_at else "")
                    + "</td></tr>"
                )

        return (
            "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
            "<thead><tr><th>New articles</th></tr></thead>"
            "<tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
