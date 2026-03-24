"""Market trend scanner: detect hot sectors via YouTube + LLM analysis."""

from __future__ import annotations

import json
import logging

from youtube_scraper import YouTubeScraper
from llm.client import create_llm_client
from llm.prompts import TREND_EXTRACT_PROMPT, QUERY_GENERATE_PROMPT

logger = logging.getLogger("money_mani.pipeline.trend_scanner")

# Fixed queries to detect current market trends
TREND_SCAN_QUERIES = [
    "주식 핫섹터 2026",
    "테마주 추천 2026",
    "주식 시장 전망",
    "외국인 매수 종목",
    "주식 뉴스 이슈 종목",
]


class TrendScanner:
    """Detect hot market sectors/themes using YouTube search + LLM analysis."""

    def __init__(self, config: dict = None):
        self.scraper = YouTubeScraper()
        self.llm = create_llm_client()

    def scan_trends(self, max_videos: int = 20) -> list[dict]:
        """Search YouTube for recent market trend videos and extract hot sectors.

        Args:
            max_videos: Max videos to collect across all trend queries.

        Returns:
            List of trend dicts with sector, keywords, confidence, reason.
            Empty list on failure.
        """
        logger.info("Scanning market trends from YouTube...")

        # Collect recent videos about market trends
        videos = []
        per_query = max(3, max_videos // len(TREND_SCAN_QUERIES))
        for query in TREND_SCAN_QUERIES:
            try:
                results = self.scraper.search(query, max_results=per_query)
                videos.extend(results)
                logger.debug(f"  '{query}': {len(results)} videos")
            except Exception as e:
                logger.warning(f"Search failed for '{query}': {e}")

        if not videos:
            logger.warning("No trend videos found.")
            return []

        # Deduplicate by video ID
        seen = set()
        unique = []
        for v in videos:
            vid = v.get("id", v.get("url", ""))
            if vid not in seen:
                seen.add(vid)
                unique.append(v)
        videos = unique[:max_videos]
        logger.info(f"Collected {len(videos)} unique trend videos")

        # Format video metadata for LLM
        video_lines = []
        for v in videos:
            title = v.get("title", "")
            desc = (v.get("description", "") or "")[:200]
            views = v.get("view_count", 0)
            date = v.get("upload_date", "")
            video_lines.append(f"- [{date}] {title} (조회수: {views:,})\n  {desc}")

        video_list = "\n".join(video_lines)

        # LLM: extract trends from video metadata
        try:
            prompt = TREND_EXTRACT_PROMPT.format(video_list=video_list)
            response = self.llm.chat([{"role": "user", "content": prompt}])
            trends = self._parse_json(response)
            if not isinstance(trends, list):
                logger.warning("LLM returned non-list for trends")
                return []
            logger.info(f"Detected {len(trends)} trending sectors")
            for t in trends:
                logger.info(f"  {t.get('sector', '?')} "
                            f"(confidence: {t.get('confidence', 0):.1f}) "
                            f"- {t.get('reason', '')}")
            return trends
        except Exception as e:
            logger.error(f"Trend extraction failed: {e}")
            return []

    def generate_queries(self, trends: list[dict]) -> list[str]:
        """Generate YouTube search queries from detected trends via LLM.

        Args:
            trends: List of trend dicts from scan_trends().

        Returns:
            List of search query strings. Empty list on failure.
        """
        if not trends:
            return []

        trends_text = json.dumps(trends, ensure_ascii=False, indent=2)

        try:
            prompt = QUERY_GENERATE_PROMPT.format(trends=trends_text)
            response = self.llm.chat([{"role": "user", "content": prompt}])
            queries = self._parse_json(response)
            if not isinstance(queries, list):
                logger.warning("LLM returned non-list for queries")
                return []
            # Ensure all items are strings
            queries = [str(q) for q in queries if q][:10]
            logger.info(f"Generated {len(queries)} search queries from trends")
            for q in queries:
                logger.info(f"  -> {q}")
            return queries
        except Exception as e:
            logger.error(f"Query generation failed: {e}")
            return []

    @staticmethod
    def _parse_json(text: str):
        """Parse JSON from LLM response, stripping markdown fences."""
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)
        return json.loads(text)
