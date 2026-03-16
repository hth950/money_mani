"""Korean stock community sentiment collector (DCInside + FMKorea crawling + LLM analysis)."""
import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from utils.cache import TTLCache

KST = timezone(timedelta(hours=9))
logger = logging.getLogger("money_mani.scoring.community_sentiment")

_community_cache = TTLCache(ttl=1800, maxsize=4)  # 30분 TTL

COMMUNITY_SENTIMENT_PROMPT = """다음은 한국 주식 커뮤니티(DCInside, FMKorea) 최신 게시글 제목 목록입니다.
전체적인 투자자 심리를 분석하여 0.0(극도의 공포/패닉)~1.0(극도의 탐욕/불장)으로 평가하세요.

주의: 한국 커뮤니티의 비꼼/반어법을 반드시 고려하세요.
예시:
- "ㅋㅋ 불장이네" → 실제로는 부정적 (비꼼)
- "이게 반등이라고 ㄷㄷ" → 실제로는 부정적
- "손절각인듯" → 부정적
- "삼성 진짜 이제 반등각" → 긍정적

게시글 제목 목록:
{titles}

0.0부터 1.0 사이의 숫자(소수점 2자리)만 출력하세요. 예: 0.34"""


class CommunitySentimentCollector:
    """KRX-only: Crawl DCInside/FMKorea and analyze sentiment via LLM batch."""

    DCINSIDE_HTML_URL = "https://gall.dcinside.com/board/lists/?id=neostock"
    FMKOREA_RSS_URL = "https://www.fmkorea.com/index.php?mid=stock&act=rss"
    FMKOREA_HTML_URL = "https://www.fmkorea.com/stock"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    def __init__(self):
        self._request_delay = 2.5

    def _fetch_dcinside(self, max_posts: int = 30) -> list[str]:
        """Fetch post titles from DCInside neostock gallery."""
        try:
            resp = requests.get(
                self.DCINSIDE_HTML_URL,
                headers=self.HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            titles = []
            # DCInside uses <td class="gall_tit"> or <a class="ub-word"> for titles
            for el in soup.select("td.gall_tit a.ub-word, .gall_tit > a:not(.reply_numbox)"):
                text = el.get_text(strip=True)
                if text and len(text) > 2:
                    titles.append(text)
                if len(titles) >= max_posts:
                    break
            logger.debug(f"DCInside: fetched {len(titles)} titles")
            return titles
        except Exception as e:
            logger.warning(f"DCInside crawl failed: {e}")
            return []

    def _fetch_fmkorea(self, max_posts: int = 30) -> list[str]:
        """Fetch post titles from FMKorea stock board. Try RSS first, fall back to HTML."""
        # Try RSS first
        try:
            resp = requests.get(self.FMKOREA_RSS_URL, headers=self.HEADERS, timeout=8)
            if resp.status_code == 200 and "<?xml" in resp.text[:100] or "<rss" in resp.text[:200]:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(resp.text, "xml")
                items = soup.find_all("item")
                titles = []
                for item in items[:max_posts]:
                    title_tag = item.find("title")
                    if title_tag:
                        t = title_tag.get_text(strip=True)
                        if t and len(t) > 2:
                            titles.append(t)
                if titles:
                    logger.debug(f"FMKorea RSS: fetched {len(titles)} titles")
                    return titles
        except Exception as e:
            logger.debug(f"FMKorea RSS failed (will try HTML): {e}")

        # HTML fallback
        try:
            time.sleep(self._request_delay)
            resp = requests.get(self.FMKOREA_HTML_URL, headers=self.HEADERS, timeout=10)
            resp.raise_for_status()
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            titles = []
            # FMKorea uses <a class="title"> or similar structures
            for el in soup.select("h3.title a, .li_subject a, td.title a"):
                text = el.get_text(strip=True)
                if text and len(text) > 2 and not text.startswith("http"):
                    titles.append(text)
                if len(titles) >= max_posts:
                    break
            logger.debug(f"FMKorea HTML: fetched {len(titles)} titles")
            return titles
        except Exception as e:
            logger.warning(f"FMKorea HTML crawl failed: {e}")
            return []

    def _analyze_sentiment(self, titles: list[str]) -> float:
        """Use Gemini flash-lite to analyze sentiment of post titles."""
        if not titles:
            return 0.5
        try:
            from llm.client import OpenRouterClient
            llm = OpenRouterClient()
            titles_text = "\n".join(f"- {t}" for t in titles[:50])
            prompt = COMMUNITY_SENTIMENT_PROMPT.format(titles=titles_text)
            response = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                model="google/gemini-3.1-flash-lite-preview",
                temperature=0.1,
                max_tokens=16,
            )
            score = float(response.strip())
            score = max(0.0, min(1.0, score))
            return round(score, 4)
        except Exception as e:
            logger.warning(f"LLM sentiment analysis failed: {e}")
            return 0.5

    def score(self) -> dict:
        """Return community sentiment score (0=fear, 1=greed).

        Returns:
            {"score": 0.0~1.0, "details": {"dcinside_posts": N, "fmkorea_posts": N,
             "post_count": N, "posts_sample": [...], "method": str, "crawled_at": str}}
        """
        now = datetime.now(KST)
        cache_key = now.strftime("%Y-%m-%d:%H:") + str(now.minute // 30)
        hit, cached = _community_cache.get(cache_key)
        if hit:
            return cached

        # Fetch from both sources
        dcinside_titles = self._fetch_dcinside(max_posts=30)
        time.sleep(self._request_delay)
        fmkorea_titles = self._fetch_fmkorea(max_posts=30)

        all_titles = dcinside_titles + fmkorea_titles
        post_count = len(all_titles)

        if not all_titles:
            result = {
                "score": 0.5,
                "details": {
                    "dcinside_posts": 0,
                    "fmkorea_posts": 0,
                    "post_count": 0,
                    "posts_sample": [],
                    "method": "fallback_no_data",
                    "crawled_at": now.isoformat(),
                },
            }
            _community_cache.set(cache_key, result)
            return result

        # LLM batch analysis
        sentiment_score = self._analyze_sentiment(all_titles)

        result = {
            "score": sentiment_score,
            "details": {
                "dcinside_posts": len(dcinside_titles),
                "fmkorea_posts": len(fmkorea_titles),
                "post_count": post_count,
                "posts_sample": all_titles[:10],
                "method": "llm",
                "crawled_at": now.isoformat(),
            },
        }
        _community_cache.set(cache_key, result)
        logger.info(f"Community sentiment: {sentiment_score:.4f} ({post_count} posts)")
        return result
