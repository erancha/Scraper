"""Ynet Sport provider.

Scrapes https://www.ynet.co.il/sport and reports newly published articles.

This provider treats "completion" as "seen": items are considered completed
immediately, and an email is sent when new item IDs appear.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urljoin, urlparse

# Ynet provides an HTML page (not a JSON API), so we parse the DOM to extract article links.
from bs4 import BeautifulSoup

from .base import Provider


class YnetSport(Provider):
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
        import requests

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

    def parse(self, data: dict) -> list[dict]:
        html = data.get("html") or ""
        soup = BeautifulSoup(html, "html.parser")

        items: list[dict] = []
        seen_ids: set[str] = set()

        # Normalize / clean article URLs (deduplication happens below via seen_ids).
        def normalize_href(href: str) -> str:
            abs_url = urljoin(self.url, href)
            parsed = urlparse(abs_url)
            # remove fragment; keep query as ynet may use it for canonicalization
            return parsed._replace(fragment="").geturl()

        # We scan all <a href=...> tags and turn only the ones that look like real article links
        # (domain/path checks + meaningful text) into an item {id=url, title, url}.
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
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)

            items.append(
                {
                    "id": item_id,
                    "title": title,
                    "url": abs_url,
                }
            )

        # Keep the list bounded to reduce email noise.
        return items[:40]

    def get_day_label(self, data: dict) -> str:
        # We don't have a structured "day"; show local date.
        return datetime.now().strftime("%Y-%m-%d")

    def get_completed_ids(self, items: list[dict]) -> set[str]:
        # "Completed" == "seen" for this provider.
        return {str(i.get("id")) for i in items if i.get("id")}

    def item_to_text(self, item: dict) -> str:
        title = item.get("title", "")
        url = item.get("url", "")
        return f"{title}\n{url}".strip()

    def items_to_html_table(self, items: list[dict]) -> str:
        rows = []
        for it in items:
            title = (it.get("title") or "").replace("<", "&lt;").replace(">", "&gt;")
            url = it.get("url") or ""
            rows.append(f"<tr><td><a href='{url}'>{title}</a></td></tr>")

        return (
            "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
            "<thead><tr><th>New articles</th></tr></thead>"
            "<tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
