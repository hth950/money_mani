"""Web search engine using DuckDuckGo + Naver News for market intelligence."""

import logging

import requests
from duckduckgo_search import DDGS

from utils.config_loader import get_env

logger = logging.getLogger("money_mani.pipeline.web_search")


class WebSearcher:
    """Search the web for market news and issues using multiple sources."""

    def __init__(self, region: str = "kr-kr"):
        self._region = region
        self._naver_client_id = get_env("NAVER_CLIENT_ID")
        self._naver_client_secret = get_env("NAVER_CLIENT_SECRET")
        self._naver_available = bool(
            self._naver_client_id and self._naver_client_secret
        )
        if self._naver_available:
            logger.info("Naver News API enabled")
        else:
            logger.info("Naver News API not configured, using DuckDuckGo only")

    def search(self, query: str, max_results: int = 10) -> list[dict]:
        """Search DuckDuckGo and return results."""
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
            logger.info(f"DuckDuckGo search '{query}': {len(results)} results")
            return results
        except Exception as e:
            logger.error(f"DuckDuckGo search failed for '{query}': {e}")
            return []

    def search_news(self, query: str, max_results: int = 10) -> list[dict]:
        """Search DuckDuckGo News for recent articles."""
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
            logger.info(f"DuckDuckGo news '{query}': {len(results)} results")
            return results
        except Exception as e:
            logger.error(f"DuckDuckGo news failed for '{query}': {e}")
            return []

    def search_naver_news(self, query: str, max_results: int = 10) -> list[dict]:
        """Search Naver News API for Korean articles.

        Args:
            query: Search query string.
            max_results: Maximum results (max 100 per Naver API).

        Returns:
            List of dicts with keys: title, url, snippet, date, source.
        """
        if not self._naver_available:
            return []

        try:
            resp = requests.get(
                "https://openapi.naver.com/v1/search/news.json",
                params={
                    "query": query,
                    "display": min(max_results, 100),
                    "sort": "date",
                },
                headers={
                    "X-Naver-Client-Id": self._naver_client_id,
                    "X-Naver-Client-Secret": self._naver_client_secret,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            results = []
            for item in data.get("items", []):
                title = item.get("title", "")
                # Strip HTML tags from Naver response
                title = title.replace("<b>", "").replace("</b>", "")
                title = title.replace("&quot;", '"').replace("&amp;", "&")
                desc = item.get("description", "")
                desc = desc.replace("<b>", "").replace("</b>", "")
                desc = desc.replace("&quot;", '"').replace("&amp;", "&")

                results.append({
                    "title": title,
                    "url": item.get("originallink", item.get("link", "")),
                    "snippet": desc,
                    "date": item.get("pubDate", ""),
                    "source": "Naver",
                })
            logger.info(f"Naver news '{query}': {len(results)} results")
            return results
        except Exception as e:
            logger.error(f"Naver news failed for '{query}': {e}")
            return []

    def multi_search(self, queries: list[str], max_per_query: int = 5) -> list[dict]:
        """Run multiple searches across both sources and combine unique results.

        Strategy: Naver for Korean queries, DuckDuckGo for all queries.
        Results are deduplicated by URL.
        """
        seen_urls = set()
        all_results = []

        for q in queries:
            # Naver News (Korean news priority)
            if self._naver_available:
                naver_results = self.search_naver_news(q, max_results=max_per_query)
                for r in naver_results:
                    if r["url"] and r["url"] not in seen_urls:
                        seen_urls.add(r["url"])
                        all_results.append(r)

            # DuckDuckGo News (global + supplement)
            ddg_results = self.search_news(q, max_results=max_per_query)
            for r in ddg_results:
                if r["url"] and r["url"] not in seen_urls:
                    seen_urls.add(r["url"])
                    all_results.append(r)

        logger.info(
            f"Multi-search ({len(queries)} queries): {len(all_results)} unique results "
            f"(Naver: {'enabled' if self._naver_available else 'disabled'})"
        )
        return all_results
