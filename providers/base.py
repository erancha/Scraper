"""
Abstract base class for data providers.

Each provider represents one URL/API to scrape.
To add a new provider, subclass Provider and implement all abstract methods,
then register it in providers/__init__.py.
"""

from abc import ABC, abstractmethod

from datetime import datetime
from datetime import timezone
from datetime import timedelta

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
        """Unique key used to namespace this provider's data: state.<provider-key>.json."""
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
    def get_only_completed_ids(self, items: list[dict]) -> set[str]:
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

    def rejected_ids_state_key(self) -> str:
        """state.<provider-key>.json key used for provider-level rejection bookkeeping.

        The agent loop stores IDs that should never be processed again under this key.
        This includes:
        - items rejected by provider filtering (e.g. too old / irrelevant)
        """
        return "rejected_ids"

    def notified_ids_state_key(self) -> str:
        """state.<provider-key>.json key used to store which item IDs have already been emailed.

        This key is expected to contain a dict grouped by day, e.g.
        {"YYYY-MM-DD": ["id1", "id2"]}.
        """
        return "notified_ids"

    def record_notified_ids(self, newly_notified_items: list[dict], day_key: str) -> None:
        """Persist newly_notified_items IDs under `notified_ids`, grouped by `day_key`.

        The agent loop is responsible for calling this *after* an email is sent, and then saving state to disk.
        """
        notified_key = self.notified_ids_state_key()
        state = self.provider_state
        notified_ids_by_days = state.get(notified_key) # e.g. {"2026-04-10": ["id1","id2"], "2026-04-09": ["id3", "id4"]}

        if notified_ids_by_days is None:
            notified_ids_by_days = {}

        notified_ids_for_day = notified_ids_by_days.get(day_key) or []
        notified_ids_for_day_set = {x for x in notified_ids_for_day} # Creates a set and implicitly dedupes
        for it in newly_notified_items:
            it_id = it.get("id")
            notified_ids_for_day_set.add(str(it_id))

        notified_ids_by_days[day_key] = sorted(notified_ids_for_day_set)
        state[notified_key] = notified_ids_by_days

    def prune_notified_ids_two_days_ago(self, today_utc: datetime) -> None:
        """Delete the `notified_ids` bucket for two days ago (UTC).

        Example: if today is 2026-04-10, remove the key "2026-04-08".
        Intended to be called once a day (e.g. around end-of-day) to keep state bounded.

        No-op when provider state is missing or `notified_ids` is not a day-grouped dict.

        Time zone semantics:
        - `today_utc` is expected to be **tz-aware UTC** (`datetime.now(timezone.utc)`).
        - The computed day key uses `today_utc.date()` (i.e. the **UTC calendar day**, not local time).
        """
        state = self.provider_state
        notified_ids_by_days = state.get(self.notified_ids_state_key())

        cutoff_day_key = (today_utc.date() - timedelta(days=2)).isoformat()
        if cutoff_day_key in notified_ids_by_days:
            del notified_ids_by_days[cutoff_day_key]
            logger.debug("[%s] prune_notified_ids_two_days_ago: removed day_key=%s", self.name, cutoff_day_key)
        else:
            logger.debug("[%s] prune_notified_ids_two_days_ago: nothing to remove (missing day_key=%s)", self.name, cutoff_day_key)

    @property
    def last_check_dt(self) -> datetime | None:
        """Return `last_check` parsed from the attached provider state.

        Time zone semantics:
        - The return value is always a **naive UTC** `datetime` (tzinfo is stripped).
        - If the stored value is tz-aware (e.g. ends with `Z`), it is converted to **UTC** first.
        - If the stored value is tz-naive, it is treated as already being **UTC** (no local-time assumption).

        Returns None when unavailable/unparseable.
        """
        state = self.provider_state
        if not isinstance(state, dict):
            return None

        raw = state.get("last_check")
        if not raw:
            return None

        try:
            dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
        except Exception:
            return None

        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    @property
    def provider_state(self) -> dict | None:
        """Provider-local view of its persisted state dict (when attached by the agent loop)."""
        return getattr(self, "_provider_state", None)

    def attach_state(self, state: dict) -> None:
        """Attach this provider's state dict for access to keys like `last_check`.
        The agent loop owns persistence; providers only read this for runtime decisions such as computing `last_check_dt` / `cutoff_dt`.
        """
        self._provider_state = state

    def cutoff_dt(self) -> datetime | None:
        """Return the active datetime cutoff for time-based filtering.

        Priority:
        - provider_state['last_check'] (via last_check_dt)
        - days_back rolling cutoff (if provider defines int days_back)
        - None

        Time zone semantics:
        - When derived from `last_check_dt`, the cutoff is **naive UTC**.
        - When derived from `days_back`, the cutoff uses `datetime.now()` which is **local time** and tz-naive.
          (This is historical behavior; callers compare naive datetimes as-is.)
        """
        return self.last_check_dt or (
            datetime.now() - timedelta(days=int(getattr(self, "days_back", 0) or 0))
            if isinstance(getattr(self, "days_back", None), int)
            else None
        )

    def reject_items(self, items: list[dict]) -> tuple[list[dict], set[str]]:
        """Optional hook to filter items and record which IDs were rejected.

        Args:
            items: candidate items that are not already in rejected_ids.

        Returns:
            (items_to_keep, rejected_ids_to_save)

            - items_to_keep: items that should continue through the pipeline.
            - rejected_ids_to_save: IDs to persist to rejected_ids so they are not
              processed again in future runs.
        """
        return items, set()

    def enrich_completed_items(self, items: list[dict]) -> list[dict]:
        """Optional hook to enrich/mutate items before rejection/completion logic.

        This hook should not filter items out (use reject_items for that). 
        It is useful for adding derived fields, fetching summaries, etc.
        """
        return items

    def _openai_api_key(self) -> str:
        return (os.getenv("OPENAI_API_KEY") or "").strip()

    def _openai_model(self) -> str:
        raw = (os.getenv("OPENAI_MODEL") or "").strip()
        if raw:
            raw = raw.split("#", 1)[0].strip()
        return raw or "gpt-4o-mini"

    def _estimate_openai_cost_usd(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """Estimate OpenAI request cost in USD based on token usage and configured pricing overrides."""
        input_per_1m_override = (os.getenv("OPENAI_INPUT_COST_PER_1M") or "").strip()
        output_per_1m_override = (os.getenv("OPENAI_OUTPUT_COST_PER_1M") or "").strip()

        def _to_float(v: str) -> float | None:
            """Parse a float from a string (returns None on failure)."""
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
        """OpenAI system prompt used when summarizing/classifying content."""
        return "Return ONLY valid JSON with keys: summary (string)."

    def openai_user_prompt_prefix(self) -> str:
        """Optional extra instruction prepended to the user prompt."""
        return ""

    def openai_summary_instruction(self) -> str:
        """Instruction describing the desired summary style/language."""
        return "Write a concise 3-5 sentence summary."

    def openai_article_text_label(self) -> str:
        """Label used for the article text section inside the user prompt."""
        return "Text"

    def openai_user_prompt(self, title: str, url: str, text: str) -> str:
        """Build the OpenAI user prompt for a given piece of content."""
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

    # def _openai_analyze_article(self, title: str, url: str, text: str) -> dict:
    #     """For testing purposes."""
    #     return {
    #         "title": title,
    #         "summary": (text or "")[:500],
    #     }

    def _openai_analyze_article(self, title: str, url: str, text: str) -> dict:
        """Call the OpenAI Chat Completions API and parse a best-effort JSON dict result."""
        api_key = self._openai_api_key()
        if not api_key:
            return {}

        if not hasattr(self, "_last_logged_openai_model"):
            self._last_logged_openai_model = None

        model = self._openai_model()
        if self._last_logged_openai_model != model:
            logger.info("[%s] OpenAI model=%s", self.name, model)
            self._last_logged_openai_model = model

        system = self.openai_system_prompt()
        user = self.openai_user_prompt(title=title, url=url, text=text)

        openai_timeout_s = 120
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

        return result

    def _html_to_text(self, html: str) -> str:
        """Convert HTML into normalized plain text (best-effort)."""
        if not html:
            return ""
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        article = soup.find("article")
        container = article if article is not None else soup
        text = container.get_text(" ", strip=True)
        return " ".join(text.split())
