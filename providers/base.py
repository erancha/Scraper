"""
Abstract base class for data providers.

Each provider represents one URL/API to scrape.
To add a new provider, subclass Provider and implement all abstract methods,
then register it in providers/__init__.py.
"""

from abc import ABC, abstractmethod


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

    @abstractmethod
    def fetch(self) -> dict:
        """Fetch raw data from the remote URL/API. Returns the raw payload."""
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
    def heading(self, day_label: str) -> str:
        """Display heading for output/emails. Override for custom labels."""
        return f"{self.name} – {day_label}"

    def format_text(self, items: list[dict], heading: str) -> str:
        """All items as plain text. Override for custom layout."""
        sections = [heading, "=" * len(heading), ""]
        for item in items:
            sections.append(self.item_to_text(item))
            sections.append("-" * 60)
        return "\n".join(sections)
