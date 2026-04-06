"""Ynet News provider.

Scrapes https://www.ynet.co.il/news and reports newly published articles.

This provider tracks two sets of IDs in state.json:
- evaluated_ids: article URLs we already evaluated to avoid repeated LLM work
- notified_ids: articles that were already emailed
"""

from __future__ import annotations

from .ynet_ai_html_base import YnetAiHtmlProviderBase


class YnetNews(YnetAiHtmlProviderBase):
    @property
    def name(self) -> str:
        return "Ynet News"

    @property
    def state_key(self) -> str:
        return "ynet_news"

    @property
    def url(self) -> str:
        # This is the *listing page* that the base class fetches.
        # The base parser also uses this value as the base URL for url-joining relative <a href="..."> links.
        return "https://www.ynet.co.il/news/"

    @property
    def allowed_path_prefixes(self) -> tuple[str, ...]:
        # These prefixes are matched against the URL path of *links found inside* the fetched listing page.
        # The listing page is /news/247, but it can contain <a href="/article/..."> links; those are the ones
        # we want to keep as candidate items.
        return ("/news/article", "/news/blog/article")
