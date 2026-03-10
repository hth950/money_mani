"""Web search engine using DuckDuckGo for market intelligence."""

import logging
from duckduckgo_search import DDGS

logger = logging.getLogger("money_mani.pipeline.web_search")


class WebSearcher:
    """Search the web for market news and issues."""

    def __init__(self, region: str = "kr-kr"):
        self._region = region

    def search(self, query: str, max_results: int = 10) -> list[dict]:
        """Search DuckDuckGo and return results.

        Args:
            query: Search query string.
            max_results: Maximum number of results to return.

        Returns:
            List of dicts with keys: title, url, snippet.
        """
        try:
            with DDGS() as ddgs:
                raw = ddgs.text(query, region=self._region, max_results=max_results)
            results = []
            for r in raw:
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                })
            logger.info(f"Web search '{query}': {len(results)} results")
            return results
        except Exception as e:
            logger.error(f"Web search failed for '{query}': {e}")
            return []

    def search_news(self, query: str, max_results: int = 10) -> list[dict]:
        """Search DuckDuckGo News for recent articles.

        Args:
            query: Search query string.
            max_results: Maximum number of results.

        Returns:
            List of dicts with keys: title, url, snippet, date, source.
        """
        try:
            with DDGS() as ddgs:
                raw = ddgs.news(query, region=self._region, max_results=max_results)
            results = []
            for r in raw:
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("body", ""),
                    "date": r.get("date", ""),
                    "source": r.get("source", ""),
                })
            logger.info(f"News search '{query}': {len(results)} results")
            return results
        except Exception as e:
            logger.error(f"News search failed for '{query}': {e}")
            return []

    def multi_search(self, queries: list[str], max_per_query: int = 5) -> list[dict]:
        """Run multiple searches and combine unique results.

        Args:
            queries: List of search queries.
            max_per_query: Max results per query.

        Returns:
            Combined deduplicated results.
        """
        seen_urls = set()
        all_results = []
        for q in queries:
            results = self.search_news(q, max_results=max_per_query)
            for r in results:
                if r["url"] not in seen_urls:
                    seen_urls.add(r["url"])
                    all_results.append(r)
        logger.info(f"Multi-search ({len(queries)} queries): {len(all_results)} unique results")
        return all_results
