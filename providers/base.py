"""
Abstract base class for data providers.

Each provider represents one URL/API to scrape.
To add a new provider, subclass Provider and implement all abstract methods,
then register it in providers/__init__.py.
"""

from abc import ABC, abstractmethod

import json
import logging
import os
import time
import requests
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)


class Provider(ABC):
    """Interface that every data provider must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name, e.g. 'ESPN NBA'."""
        ...

    @property
    @abstractmethod
    def state_key(self) -> str:
        """Unique key used to namespace this provider's data in state.json."""
        ...

    @property
    @abstractmethod
    def url(self) -> str:
        """The remote URL/API endpoint to fetch data from."""
        ...

    @abstractmethod
    def parse(self, data: dict) -> list[dict]:
        """Parse the raw payload into a list of normalised item dicts."""
        ...

    @abstractmethod
    def get_day_label(self, data: dict) -> str:
        """Extract a display-friendly date label from the raw payload."""
        ...

    @abstractmethod
    def get_completed_ids(self, items: list[dict]) -> set[str]:
        """Return the set of IDs for items that are finished/completed."""
        ...

    @abstractmethod
    def item_to_text(self, item: dict) -> str:
        """Render a single item as console-friendly plain text."""
        ...

    @abstractmethod
    def items_to_html_table(self, items: list[dict]) -> str:
        """Build an HTML table of all items (used in the email body)."""
        ...

    # ------------------------------------------------------------------
    # Default implementations (can be overridden)
    # ------------------------------------------------------------------
    def is_rtl(self) -> bool:
        """Whether this provider's human-facing output should be rendered RTL."""
        return False

    def fetch(self) -> dict:
        """Fetch raw JSON data from self.url. Override for non-JSON APIs."""
        resp = requests.get(self.url, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def heading(self, day_label: str) -> str:
        """Display heading for output/emails. Override for custom labels."""
        if not self.is_rtl():
            return f"{self.name} \u2013 {day_label}"

        # Use bidi isolates so mixed RTL/LTR content (e.g. Hebrew name + numeric date)
        # keeps the dash and ordering stable in terminals and emails.
        rli = "\u2067"  # Right-to-Left Isolate
        lri = "\u2066"  # Left-to-Right Isolate
        pdi = "\u2069"  # Pop Directional Isolate
        return f"{rli}{self.name}{pdi} \u2013 {lri}{day_label}{pdi}"

    def items_to_plain_table(self, items: list[dict], heading: str) -> str:
        """All items as plain text. Override for custom layout."""
        sections = [heading, "-" * len(heading), ""]
        for item in items:
            sections.append(self.item_to_text(item))
            sections.append("-" * 100)
        sections.append("=" * 100)
        return "\n".join(sections)

    def notified_ids_state_key(self) -> str:
        """state.json key used for notification bookkeeping.

        The agent loop stores IDs that were already notified about under this key.
        """
        return "notified_ids"

    # ------------------------------------------------------------------
    # Optional hooks
    # ------------------------------------------------------------------
    def evaluated_ids_state_key(self) -> str | None:
        """Optional state.json key to track all evaluated item IDs (even if filtered out).

        If provided, the agent loop can avoid re-processing expensive items (e.g. LLM calls)
        by only processing IDs that are unevaluated relative to this state.
        """
        return None

    def process_unevaluated_items(self, items: list[dict], unevaluated_ids: set[str]) -> tuple[list[dict], set[str]]:
        """Optional hook to process only unevaluated items.

        Returns:
        - notify_items: items that should proceed to the normal notification pipeline
        - evaluated_ids_to_add: IDs that should be added to the evaluated set
        """
        notify_items = [it for it in items if str(it.get("id")) in unevaluated_ids]
        return notify_items, set(unevaluated_ids)

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
        return "Write a concise 3-5 sentence summary."

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

        if not hasattr(self, "_analysis_cache") or getattr(self, "_analysis_cache") is None:
            self._analysis_cache = {}
        if not hasattr(self, "_last_logged_openai_model"):
            self._last_logged_openai_model = None

        cached = self._analysis_cache.get(url)
        if cached is not None:
            return cached

        model = self._openai_model()
        if self._last_logged_openai_model != model:
            logger.info("[%s] OpenAI model=%s", self.name, model)
            self._last_logged_openai_model = model

        system = self.openai_system_prompt()
        user = self.openai_user_prompt(title=title, url=url, text=text)

        openai_timeout_s = 60
        t0 = time.monotonic()

        def _log_openai_request_exception(prefix: str, exc: requests.exceptions.RequestException) -> None:
            elapsed_s = time.monotonic() - t0
            logger.warning(
                "[%s] OpenAI %s after %0.3fs url=%s model=%s (%s)",
                self.name,
                prefix,
                elapsed_s,
                url,
                model,
                exc.__class__.__name__,
                exc_info=True,
            )

        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                timeout=openai_timeout_s,
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
        except requests.exceptions.Timeout as exc:
            elapsed_s = time.monotonic() - t0
            logger.warning(
                "[%s] OpenAI request timed out after %0.3fs (timeout=%ss) url=%s model=%s system_len=%d user_len=%d text_len=%d",
                self.name,
                elapsed_s,
                openai_timeout_s,
                url,
                model,
                len(system or ""),
                len(user or ""),
                len(text or ""),
                exc_info=True,
            )
            raise
        except requests.exceptions.ConnectionError as exc:
            _log_openai_request_exception("connection error", exc)
            raise
        except requests.exceptions.RequestException as exc:
            _log_openai_request_exception("request error", exc)
            raise

        elapsed_s = time.monotonic() - t0
        if not resp.ok:
            body_preview = (resp.text or "").strip()
            if len(body_preview) > 2000:
                body_preview = body_preview[:2000] + "..."
            openai_request_id = resp.headers.get("x-request-id") or resp.headers.get("request-id") or ""
            if openai_request_id:
                openai_request_id = openai_request_id.strip()
            logger.warning(
                "[%s] OpenAI HTTP %s after %0.3fs for %s (request_id=%s): %s",
                self.name,
                resp.status_code,
                elapsed_s,
                url,
                openai_request_id,
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

    def _html_to_text(self, html: str) -> str:
        if not html:
            return ""
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        article = soup.find("article")
        container = article if article is not None else soup
        text = container.get_text(" ", strip=True)
        return " ".join(text.split())
