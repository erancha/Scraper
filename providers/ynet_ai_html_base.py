from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .base import Provider


logger = logging.getLogger(__name__)


class YnetAiHtmlProviderBase(Provider, ABC):
    def __init__(self) -> None:
        self._analysis_cache: dict[str, dict] = {}
        self._last_logged_openai_model: str | None = None

    @property
    @abstractmethod
    def allowed_path_prefixes(self) -> tuple[str, ...]:
        ...

    @property
    def max_listing_items(self) -> int:
        return 40

    @property
    def max_unevaluated_to_process(self) -> int:
        return 25

    @property
    def max_kept_items(self) -> int:
        return 40

    @property
    def min_title_len(self) -> int:
        return 10

    @property
    def days_back(self) -> int:
        return 1

    def is_rtl(self) -> bool:
        return True

    def evaluated_ids_state_key(self) -> str | None:
        return "evaluated_ids"

    def fetch(self) -> dict:
        resp = requests.get(
            self.url,
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
        return {"html": resp.text, "fetched_at": datetime.utcnow().isoformat()}

    def parse(self, data: dict) -> list[dict]:
        html = data.get("html") or ""
        soup = BeautifulSoup(html, "html.parser")

        items: list[dict] = []
        dedup_ids: set[str] = set()

        def normalize_href(href: str) -> str:
            abs_url = urljoin(self.url, href)
            parsed = urlparse(abs_url)
            return parsed._replace(fragment="").geturl()

        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href or href.startswith("javascript:") or href.startswith("#"):
                continue

            abs_url = normalize_href(href)
            parsed = urlparse(abs_url)
            if parsed.netloc and "ynet.co.il" not in parsed.netloc:
                continue

            path = parsed.path or ""
            if not any(path.startswith(p) for p in self.allowed_path_prefixes):
                continue

            title = " ".join(a.get_text(" ", strip=True).split())
            if len(title) < self.min_title_len:
                continue

            item_id = abs_url
            if item_id in dedup_ids:
                continue
            dedup_ids.add(item_id)

            items.append({"id": item_id, "title": title, "url": abs_url})
            if len(items) >= self.max_listing_items:
                break

        return items

    def process_unevaluated_items(self, items: list[dict], unevaluated_ids: set[str]) -> tuple[list[dict], set[str]]:
        if not unevaluated_ids:
            logger.debug("[%s] No unevaluated ids", self.name)
            return [], set()

        unevaluated_items = [it for it in items if str(it.get("id")) in unevaluated_ids]

        kept: list[dict] = []
        for it in unevaluated_items[: self.max_unevaluated_to_process]:
            url = str(it.get("url") or "")
            title = str(it.get("title") or "")
            if not url:
                continue

            cutoff_dt = datetime.now() - timedelta(days=int(self.days_back or 0))  # Keep only items newer than this rolling cutoff (resolution: days; comparison uses local time down to seconds).

            try:
                logger.debug("[%s] Fetching article: %s", self.name, url)
                soup = self._fetch_article_soup(url)
                published_at = self._extract_published_at(soup)
                if published_at:
                    it["published_at"] = published_at
                    # Keep only items newer than cutoff_dt:
                    try:
                        dt_raw = str(published_at).strip().replace("Z", "+00:00")
                        published_dt = datetime.fromisoformat(dt_raw)
                        if published_dt.tzinfo is not None:
                            published_dt = published_dt.astimezone().replace(tzinfo=None)
                        if published_dt < cutoff_dt:
                            logger.debug(
                                "[%s] Filtered out (too old): %s published_at=%s cutoff=%s",
                                self.name,
                                url,
                                published_at,
                                cutoff_dt.isoformat(timespec="seconds"),
                            )
                            continue
                    except Exception:
                        pass
                text = self._extract_article_text(soup)
            except Exception as exc:
                logger.warning("[%s] Failed to fetch article: %s (%s)", self.name, url, exc)
                text = ""

            analysis: dict = {}
            if self._openai_api_key() and text:
                try:
                    logger.debug("[%s] Summarizing/classifying via OpenAI: %s", self.name, url)
                    analysis = self._openai_analyze_article(title=title, url=url, text=text)
                except Exception as exc:
                    logger.warning(
                        "[%s] OpenAI analysis failed: %s (%s)",
                        self.name,
                        url,
                        exc.__class__.__name__,  # Keep the exception type in the log line for quick filtering/alerting.
                        exc_info=True,  # Include traceback to diagnose rare network/provider failures.
                    )
                    analysis = {}

            if not self.is_relevant(title=title, url=url, text=text, analysis=analysis):
                logger.debug("[%s] Filtered out (irrelevant): %s", self.name, url)
                continue

            summary = (analysis.get("summary") or "").strip() if analysis else ""
            if summary:
                it["summary"] = summary

            kept.append(it)
            logger.debug("[%s] Kept: %s", self.name, url)
            if len(kept) >= self.max_kept_items:
                break

        return kept, unevaluated_ids

    def is_relevant(self, title: str, url: str, text: str, analysis: dict) -> bool:
        return True

    def get_day_label(self, data: dict) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def get_completed_ids(self, items: list[dict]) -> set[str]:
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
                    + (
                        f"<div style='margin-top:4px;color:#666;font-size:12px'>{published_at}</div>"
                        if published_at
                        else ""
                    )
                    + f"<div style='margin-top:6px;color:#333;font-size:13px;line-height:1.35'>{summary}</div>"
                    "</td></tr>"
                )
            else:
                rows.append(
                    "<tr><td>"
                    f"<a href='{url}'>{title}</a>"
                    + (
                        f"<div style='margin-top:4px;color:#666;font-size:12px'>{published_at}</div>"
                        if published_at
                        else ""
                    )
                    + "</td></tr>"
                )

        return (
            "<table dir='rtl' border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;direction:rtl;text-align:right'>"
            "<thead><tr><th style='text-align:right'>New articles</th></tr></thead>"
            "<tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )

    def openai_system_prompt(self) -> str:
        return "Return ONLY valid JSON with keys: summary (string)."

    def openai_user_prompt_prefix(self) -> str:
        return ""

    def openai_summary_instruction(self) -> str:
        return "Write a concise 8-12 sentence summary in Hebrew."

    def openai_article_text_label(self) -> str:
        return "Text"

    def openai_user_prompt(self, title: str, url: str, text: str) -> str:
        prefix = (self.openai_user_prompt_prefix() or "").strip()
        if prefix:
            prefix = prefix + "\n\n"

        summary_instruction = (self.openai_summary_instruction() or "").strip()
        text_label = (self.openai_article_text_label() or "Text").strip()

        return (
            f"{prefix}{summary_instruction}\n\n"
            f"Title: {title}\n"
            f"URL: {url}\n"
            f"{text_label}: {text}"
        )

    def _fetch_article_soup(self, url: str) -> BeautifulSoup:
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
        return self._html_to_text(str(soup))

    def _extract_published_at(self, soup: BeautifulSoup) -> str:
        for script in soup.find_all("script"):
            txt = script.string or script.get_text(" ", strip=True)
            if not txt:
                continue
            m = re.search(
                r"['\"]dateModified['\"]\s*:\s*['\"]([^'\"]+)['\"]",
                txt,
            )
            if m:
                raw = m.group(1).strip()
                raw = raw.replace("/", "-")
                if "T" not in raw and re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}", raw):
                    raw = raw.replace(" ", "T", 1)
                return raw

        updated_meta_candidates = [
            ("property", "article:modified_time"),
            ("property", "og:updated_time"),
            ("name", "lastmod"),
            ("name", "last-modified"),
        ]

        published_meta_candidates = [
            ("property", "article:published_time"),
            ("property", "og:published_time"),
            ("name", "publish_date"),
            ("name", "pubdate"),
            ("name", "date"),
            ("name", "dc.date"),
            ("name", "DC.date.issued"),
        ]

        for attr, key in updated_meta_candidates:
            tag = soup.find("meta", attrs={attr: key})
            if tag and tag.get("content"):
                return str(tag.get("content") or "").strip()

        page_text = soup.get_text(" ", strip=True)
        m = re.search(r"עודכן\s*:??\s*(\d{1,2}:\d{2})", page_text)
        if m:
            hhmm = m.group(1)
            try:
                today = datetime.now().strftime("%Y-%m-%d")
                return f"{today}T{hhmm}:00"
            except Exception:
                return hhmm

        for attr, key in published_meta_candidates:
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
