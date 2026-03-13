"""Naver Finance 기반 외국인/기관 수급 데이터 스크래퍼.

pykrx가 OCI 클라우드에서 IP 차단될 때 fallback으로 사용.
KRXFetcher.get_investor_flows()와 동일한 DataFrame 포맷으로 반환.
"""

import logging
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
logger = logging.getLogger("money_mani.market_data.naver_flow")

# ── 파싱 실패 Alert 상태 ─────────────────────────────────────────────
_fail_lock = threading.Lock()
_consecutive_fail_count: int = 0
_last_alert_sent: Optional[float] = None
_FAIL_THRESHOLD: int = 3           # 연속 N회 실패 시 Discord 알림
_ALERT_COOLDOWN: float = 4 * 3600  # 4시간 쿨다운 (alert storm 방지)


def _on_parse_success():
    """파싱 성공 시 연속 실패 카운터 리셋."""
    global _consecutive_fail_count
    with _fail_lock:
        _consecutive_fail_count = 0


def _on_parse_failure(ticker: str, reason: str):
    """파싱 실패 시 카운터 증가. 임계값 초과 시 Discord 알림."""
    global _consecutive_fail_count, _last_alert_sent
    should_alert = False
    with _fail_lock:
        _consecutive_fail_count += 1
        count = _consecutive_fail_count
        now = time.monotonic()
        should_alert = (
            count >= _FAIL_THRESHOLD
            and (_last_alert_sent is None or (now - _last_alert_sent) > _ALERT_COOLDOWN)
        )
        if should_alert:
            _last_alert_sent = now

    logger.warning(f"Naver scraper parse failure ({count}/{_FAIL_THRESHOLD}): {ticker} - {reason}")
    if should_alert:
        _send_naver_alert(count, reason)


def _send_naver_alert(count: int, last_reason: str):
    """Discord로 Naver 스크래퍼 파싱 실패 알림."""
    try:
        from alerts.discord_webhook import DiscordNotifier
        notifier = DiscordNotifier()
        embed = {
            "title": "🚨 Naver 수급 스크래퍼 파싱 실패",
            "description": f"연속 **{count}회** 파싱 실패가 발생했습니다.\n"
                           f"Naver Finance HTML 구조가 변경되었을 수 있습니다.",
            "color": 0xFF0000,  # 빨강
            "fields": [
                {"name": "마지막 오류", "value": str(last_reason)[:200], "inline": False},
                {"name": "확인 URL", "value": "https://finance.naver.com/item/frgn.naver", "inline": False},
            ],
            "timestamp": datetime.now(KST).isoformat(),
        }
        notifier.send(embed=embed)
        logger.info("Sent Naver scraper failure alert to Discord")
    except Exception as e:
        logger.error(f"Failed to send Naver alert: {e}")


class NaverFlowFetcher:
    """Naver Finance에서 외국인/기관 순매매 데이터 스크래핑."""

    BASE_URL = "https://finance.naver.com/item/frgn.naver"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    def get_investor_flows(self, ticker: str, start: str, end: str = None) -> Optional[pd.DataFrame]:
        """외국인/기관 순매매 DataFrame 반환.

        KRXFetcher.get_investor_flows()와 동일한 포맷:
        - index: Date
        - columns: 외국인합계, 기관합계 (KRW 금액)

        Args:
            ticker: KRX 6자리 코드 (e.g. '005930')
            start: 'YYYY-MM-DD'
            end: 'YYYY-MM-DD' (기본: 오늘)

        Returns:
            DataFrame or None (실패 시)
        """
        try:
            start_dt = datetime.strptime(start, "%Y-%m-%d")
            end_dt = datetime.strptime(end, "%Y-%m-%d") if end else datetime.now(KST).replace(tzinfo=None)

            all_rows = []
            page = 1

            while True:
                url = f"{self.BASE_URL}?code={ticker}&page={page}"
                try:
                    resp = requests.get(url, headers=self.HEADERS, timeout=10)
                    resp.raise_for_status()
                except requests.RequestException as e:
                    _on_parse_failure(ticker, f"Network error: {e}")
                    return None

                soup = BeautifulSoup(resp.text, "html.parser")

                # <table summary="외국인 기관 투자자 매매 현황"> 테이블 파싱
                table = soup.find("table", {"summary": lambda s: s and "외국인" in s})
                if not table:
                    # HTML 구조 변경 - 다른 셀렉터 시도
                    table = soup.find("table", class_="type2")
                if not table:
                    _on_parse_failure(ticker, "Table not found - HTML structure may have changed")
                    return None

                rows = table.find_all("tr")
                page_rows = []

                for row in rows:
                    cols = row.find_all("td")
                    if len(cols) < 5:
                        continue

                    try:
                        date_str = cols[0].get_text(strip=True)
                        # 날짜 파싱 (형식: YYYY.MM.DD 또는 YY.MM.DD)
                        if len(date_str) == 8:  # YY.MM.DD
                            date_dt = datetime.strptime(date_str, "%y.%m.%d")
                        elif len(date_str) == 10:  # YYYY.MM.DD
                            date_dt = datetime.strptime(date_str, "%Y.%m.%d")
                        else:
                            continue

                        if date_dt < start_dt:
                            # 페이지가 시작일 이전으로 넘어감 → 중단
                            break

                        if date_dt > end_dt:
                            continue

                        # 종가 (단위: 원)
                        close_price_str = cols[1].get_text(strip=True).replace(",", "")
                        close_price = float(close_price_str) if close_price_str else 0

                        # 외국인 순매수량 (주)
                        foreign_vol_str = cols[3].get_text(strip=True).replace(",", "").replace("+", "")
                        foreign_vol = int(foreign_vol_str) if foreign_vol_str else 0

                        # 기관 순매수량 (주)
                        inst_vol_str = cols[4].get_text(strip=True).replace(",", "").replace("+", "")
                        inst_vol = int(inst_vol_str) if inst_vol_str else 0

                        # KRW 금액 변환
                        foreign_amount = foreign_vol * close_price
                        inst_amount = inst_vol * close_price

                        page_rows.append({
                            "Date": date_dt.strftime("%Y-%m-%d"),
                            "외국인합계": foreign_amount,
                            "기관합계": inst_amount,
                        })

                    except (ValueError, IndexError):
                        continue

                if not page_rows:
                    break

                all_rows.extend(page_rows)

                # 마지막 날짜가 시작일 이전이면 중단
                last_date = datetime.strptime(page_rows[-1]["Date"], "%Y-%m-%d")
                if last_date <= start_dt:
                    break

                page += 1
                time.sleep(0.5)  # rate limiting

                if page > 20:  # 최대 20페이지 (약 400거래일)
                    break

            if not all_rows:
                _on_parse_failure(ticker, "No data rows parsed")
                return None

            df = pd.DataFrame(all_rows)
            df["Date"] = pd.to_datetime(df["Date"])
            df = df.set_index("Date").sort_index()

            _on_parse_success()
            return df

        except Exception as e:
            _on_parse_failure(ticker, str(e))
            logger.error(f"NaverFlowFetcher failed for {ticker}: {e}")
            return None
