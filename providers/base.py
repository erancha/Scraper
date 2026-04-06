"""
Abstract base class for data providers.

Each provider represents one URL/API to scrape.
To add a new provider, subclass Provider and implement all abstract methods,
then register it in providers/__init__.py.
"""

from abc import ABC, abstractmethod

import requests


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
    def fetch(self) -> dict:
        """Fetch raw JSON data from self.url. Override for non-JSON APIs."""
        resp = requests.get(self.url, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def heading(self, day_label: str) -> str:
        """Display heading for output/emails. Override for custom labels."""
        return f"{self.name} \u2013 {day_label}"

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
