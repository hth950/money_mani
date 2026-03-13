"""Naver Finance investor flow scraper for KRX stocks.

Fetches 외국인/기관 net buy data from finance.naver.com/item/frgn.naver
which is accessible from OCI servers (unlike pykrx's KRX data.krx.co.kr).

Returns a DataFrame compatible with KRXFetcher.get_investor_flows():
    columns: ['외국인합계', '기관합계']
    index: DatetimeIndex (KST)
    values: net buy amount in KRW (순매매량 × 종가)

Usage:
    fetcher = NaverFlowFetcher()
    df = fetcher.get_investor_flows("005930", "2026-02-01", "2026-03-13")
"""

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

import requests

logger = logging.getLogger("money_mani.market_data.naver_flow_fetcher")

KST = timezone(timedelta(hours=9))

# ── 파싱 실패 Alert 상태 ─────────────────────────────────────────────
_fail_lock = threading.Lock()
_consecutive_fail_count: int = 0
_last_alert_sent: Optional[float] = None
_FAIL_THRESHOLD: int = 3
_ALERT_COOLDOWN: float = 4 * 3600  # 4시간 쿨다운


def _on_naver_success():
    global _consecutive_fail_count
    with _fail_lock:
        _consecutive_fail_count = 0


def _on_naver_failure(reason: str):
    global _consecutive_fail_count, _last_alert_sent
    should_alert = False
    with _fail_lock:
        _consecutive_fail_count += 1
        count = _consecutive_fail_count
        now = time.monotonic()
        if (count >= _FAIL_THRESHOLD
                and (_last_alert_sent is None
                     or (now - _last_alert_sent) > _ALERT_COOLDOWN)):
            _last_alert_sent = now
            should_alert = True
    logger.warning(f"Naver scraper failure ({count}/{_FAIL_THRESHOLD}): {reason}")
    if should_alert:
        _send_naver_alert(count, reason)


def _send_naver_alert(count: int, reason: str):
    try:
        from alerts.discord_webhook import DiscordNotifier
        embed = {
            "title": "🚨 Naver 수급 스크래퍼 파싱 실패",
            "description": (
                f"연속 **{count}회** 파싱 실패가 발생했습니다.\n"
                "Naver Finance HTML 구조가 변경되었을 수 있습니다."
            ),
            "color": 0xFF0000,
            "fields": [
                {"name": "마지막 오류", "value": str(reason)[:200], "inline": False},
            ],
            "timestamp": datetime.now(KST).isoformat(),
        }
        DiscordNotifier().send(embed=embed)
        logger.info("Sent Naver scraper failure alert to Discord")
    except Exception as e:
        logger.error(f"Failed to send Naver alert: {e}")
_BASE_URL = "https://finance.naver.com/item/frgn.naver"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}


def _parse_int(text: str) -> int:
    """Parse signed integer from Naver table cell (e.g. '+406,662' or '-4,868,716')."""
    clean = re.sub(r"[,\s]", "", text.strip())
    clean = clean.replace("+", "")
    try:
        return int(clean)
    except ValueError:
        return 0


def _parse_price(text: str) -> float:
    """Parse price string (e.g. '187,900')."""
    try:
        return float(text.replace(",", "").strip())
    except ValueError:
        return 0.0


def _parse_date(text: str) -> datetime | None:
    """Parse 'YYYY.MM.DD' to date."""
    try:
        return datetime.strptime(text.strip(), "%Y.%m.%d").replace(tzinfo=KST)
    except ValueError:
        return None


class NaverFlowFetcher:
    """Scrape 외국인/기관 순매매 data from Naver Finance."""

    def __init__(self, delay: float = 0.5):
        self._delay = delay
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)

    def get_investor_flows(
        self, ticker: str, start: str, end: str, max_pages: int = 3
    ):
        """Return DataFrame with 외국인합계, 기관합계 columns (KRW net buy amounts).

        Args:
            ticker: 6-digit KRX code (e.g. "005930")
            start:  "YYYY-MM-DD" (inclusive)
            end:    "YYYY-MM-DD" (inclusive)
            max_pages: max Naver pages to scrape (1 page ≈ 20 trading days)

        Returns:
            pandas.DataFrame or None on failure
        """
        try:
            import pandas as pd
        except ImportError:
            logger.error("pandas not available")
            return None

        start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=KST)
        end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=KST)

        rows = []
        for page in range(1, max_pages + 1):
            page_rows, done = self._fetch_page(ticker, page, start_dt)
            rows.extend(page_rows)
            if done:
                break
            time.sleep(self._delay)

        if not rows:
            logger.debug(f"No flow data from Naver for {ticker}")
            _on_naver_failure(f"No rows parsed for {ticker}")
            return None

        df = pd.DataFrame(rows, columns=["date", "외국인합계", "기관합계"])
        df = df.set_index("date").sort_index()
        mask = (df.index >= start_dt) & (df.index <= end_dt)
        df = df[mask]

        if df.empty:
            _on_naver_failure(f"Empty DataFrame after date filter for {ticker}")
            return None

        _on_naver_success()
        logger.debug(f"NaverFlowFetcher {ticker}: {len(df)} rows ({start} ~ {end})")
        return df

    def _fetch_page(
        self, ticker: str, page: int, cutoff: datetime
    ) -> tuple[list, bool]:
        """Fetch one page. Returns (rows, should_stop)."""
        try:
            r = self._session.get(
                _BASE_URL,
                params={"code": ticker, "page": page},
                timeout=10,
            )
            r.raise_for_status()
        except Exception as e:
            logger.warning(f"Naver flow fetch failed for {ticker} page {page}: {e}")
            return [], True

        return self._parse_page(r.text, cutoff)

    @staticmethod
    def _parse_page(html: str, cutoff: datetime) -> tuple[list, bool]:
        """Parse HTML. Returns (rows, should_stop_fetching_older_pages)."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.error("beautifulsoup4 not installed")
            return [], True

        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", summary=lambda s: s and "외국인 기관" in s)
        if not table:
            return [], True

        rows = []
        stop = False
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 7:
                continue
            date = _parse_date(tds[0].get_text())
            if date is None:
                continue
            if date < cutoff:
                stop = True
                break

            close_price = _parse_price(tds[1].get_text())
            inst_vol = _parse_int(tds[5].get_text())   # 기관 순매매량
            frgn_vol = _parse_int(tds[6].get_text())   # 외국인 순매매량

            # Convert volume → KRW amount (approximation: vol × close_price)
            inst_krw = inst_vol * close_price
            frgn_krw = frgn_vol * close_price

            rows.append([date, frgn_krw, inst_krw])

        return rows, stop
