from __future__ import annotations

import json
import logging
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime
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

            try:
                logger.debug("[%s] Fetching article: %s", self.name, url)
                soup = self._fetch_article_soup(url)
                published_at = self._extract_published_at(soup)
                if published_at:
                    it["published_at"] = published_at
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
                    logger.warning("[%s] OpenAI analysis failed: %s (%s)", self.name, url, exc)
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

    def _openai_api_key(self) -> str:
        return (os.getenv("OPENAI_API_KEY") or "").strip()

    def _openai_model(self) -> str:
        raw = (os.getenv("OPENAI_MODEL") or "").strip()
        if raw:
            raw = raw.split("#", 1)[0].strip()
        return raw or "gpt-4o-mini"

    def _estimate_openai_cost_usd(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
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

    def _openai_analyze_article(self, title: str, url: str, text: str) -> dict:
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

        system = self.openai_system_prompt()
        user = self.openai_user_prompt(title=title, url=url, text=text)

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

        content = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
        try:
            parsed = json.loads(content) if content else {}
        except Exception:
            parsed = {}

        if not isinstance(parsed, dict):
            parsed = {}

        result = dict(parsed)
        result["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": est_cost_usd,
            "model": model,
        }

        self._analysis_cache[url] = result
        return result

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
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        article = soup.find("article")
        container = article if article is not None else soup
        text = container.get_text(" ", strip=True)
        return " ".join(text.split())

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
