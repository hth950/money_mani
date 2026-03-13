"""DART Open API 기반 KRX 펀더멘탈 데이터 수집 및 스코어링."""

import io
import logging
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Optional
import xml.etree.ElementTree as ET

import requests

KST = timezone(timedelta(hours=9))
logger = logging.getLogger("money_mani.scoring.dart_fundamental")

# ── DART Rate Limit 모니터링 ─────────────────────────────────────────
_dart_counter_lock = threading.Lock()
_dart_daily_counter: int = 0
_dart_counter_date: str = ""  # YYYY-MM-DD, 날짜 변경 시 자동 리셋
_DART_DAILY_LIMIT: int = 40_000
_DART_WARN_THRESHOLD: float = 0.80  # 32,000건 도달 시 경고
_dart_warned_today: bool = False  # 당일 경고 중복 방지

# ── TTLCache 캐시 설정 ───────────────────────────────────────────────
try:
    from utils.cache import TTLCache
    _corp_code_cache = TTLCache(ttl=24 * 3600, maxsize=1)   # corpCode 매핑 (24h)
    _financial_cache = TTLCache(ttl=4 * 3600, maxsize=512)   # 재무제표 (4h)
except ImportError:
    # Fallback to simple dict if TTLCache not available
    _corp_code_cache = None
    _financial_cache = None


def _increment_dart_counter() -> int:
    """API 호출 카운터 증가. 날짜 변경 시 자동 리셋."""
    global _dart_daily_counter, _dart_counter_date, _dart_warned_today
    today = datetime.now(KST).strftime("%Y-%m-%d")
    with _dart_counter_lock:
        if _dart_counter_date != today:
            _dart_daily_counter = 0
            _dart_counter_date = today
            _dart_warned_today = False
        _dart_daily_counter += 1
        count = _dart_daily_counter

    warn_at = int(_DART_DAILY_LIMIT * _DART_WARN_THRESHOLD)
    if count >= warn_at and not _dart_warned_today:
        _dart_warned_today = True  # 중복 방지
        _send_dart_limit_warning(count)
    return count


def reset_dart_counter():
    """스케줄러에서 자정에 호출 (매일 00:05 KST)."""
    global _dart_daily_counter, _dart_counter_date, _dart_warned_today
    with _dart_counter_lock:
        _dart_daily_counter = 0
        _dart_counter_date = datetime.now(KST).strftime("%Y-%m-%d")
        _dart_warned_today = False
    logger.info("DART daily counter reset")


def _send_dart_limit_warning(count: int):
    """Discord로 DART rate limit 경고 발송."""
    try:
        from alerts.discord_webhook import DiscordNotifier
        notifier = DiscordNotifier()
        embed = {
            "title": "⚠️ DART API 일일 한도 경고",
            "description": f"오늘 DART API 호출이 **{count:,}건**에 달했습니다.\n"
                           f"일일 한도: {_DART_DAILY_LIMIT:,}건 (경고 기준: {int(_DART_DAILY_LIMIT * _DART_WARN_THRESHOLD):,}건)",
            "color": 0xF39C12,  # 주황
            "fields": [
                {"name": "현재 사용량", "value": f"{count:,} / {_DART_DAILY_LIMIT:,}", "inline": True},
                {"name": "사용률", "value": f"{count / _DART_DAILY_LIMIT * 100:.1f}%", "inline": True},
            ],
            "timestamp": datetime.now(KST).isoformat(),
        }
        notifier.send(embed=embed)
    except Exception as e:
        logger.error(f"Failed to send DART limit warning: {e}")


class DARTFundamentalFetcher:
    """DART API로 KRX 종목 펀더멘탈 데이터 수집."""

    BASE_URL = "https://opendart.fss.or.kr/api"

    def __init__(self, api_key: str = None):
        """api_key: 환경변수 DART_API_KEY 또는 직접 전달."""
        if api_key:
            self._api_key = api_key
        else:
            from utils.config_loader import get_env
            self._api_key = get_env("DART_API_KEY", "")
        if not self._api_key:
            logger.warning("DART API key not configured (DART_API_KEY)")

    def _get(self, endpoint: str, params: dict) -> dict:
        """DART API GET 요청. 호출 카운터 증가."""
        _increment_dart_counter()
        params["crtfc_key"] = self._api_key
        url = f"{self.BASE_URL}/{endpoint}"
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"DART API error ({endpoint}): {e}")
            return {}

    def get_corp_code_map(self) -> dict[str, str]:
        """종목코드 → corp_code 매핑 (일 1회 캐시).

        Returns:
            {"005930": "00126380", ...}
        """
        # 캐시 확인
        if _corp_code_cache is not None:
            hit, cached = _corp_code_cache.get("corp_code_map")
            if hit:
                return cached

        # DART에서 corpCode.xml 다운로드 (zip)
        _increment_dart_counter()
        try:
            url = f"{self.BASE_URL}/corpCode.xml"
            resp = requests.get(url, params={"crtfc_key": self._api_key}, timeout=30)
            resp.raise_for_status()

            with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                xml_data = z.read("CORPCODE.xml")

            root = ET.fromstring(xml_data)
            corp_map = {}
            for item in root.findall("list"):
                stock_code = item.findtext("stock_code", "").strip()
                corp_code = item.findtext("corp_code", "").strip()
                if stock_code and corp_code:
                    corp_map[stock_code] = corp_code

            if _corp_code_cache is not None:
                _corp_code_cache.set("corp_code_map", corp_map)
            logger.info(f"Loaded {len(corp_map)} corp codes from DART")
            return corp_map
        except Exception as e:
            logger.error(f"Failed to download DART corp codes: {e}")
            return {}

    def get_financial_data(self, ticker: str, bsns_year: str = None) -> dict:
        """PER/PBR/ROE 계산용 재무 데이터 반환.

        캐시: 4시간 TTL.
        연결재무제표(CFS) 우선, 없으면 별도재무제표(OFS) 폴백.

        Args:
            ticker: KRX 종목코드
            bsns_year: 회계연도 (e.g. "2023"). None이면 현재 날짜 기반 자동 결정.

        Returns:
            {"per": float, "pbr": float, "roe": float, "div_yield": float,
             "market_cap": float, "source": "dart"}
        """
        cache_key = f"{ticker}_{bsns_year or 'current'}"
        if _financial_cache is not None:
            hit, cached = _financial_cache.get(cache_key)
            if hit:
                return cached

        result = self._fetch_fundamentals(ticker, bsns_year=bsns_year)
        if result and _financial_cache is not None:
            _financial_cache.set(cache_key, result)
        return result

    def _fetch_fundamentals(self, ticker: str, bsns_year: str = None) -> dict:
        """실제 DART API 호출로 펀더멘탈 계산."""
        # 1. corp_code 조회
        corp_map = self.get_corp_code_map()
        corp_code = corp_map.get(ticker)
        if not corp_code:
            logger.warning(f"No corp_code for {ticker}")
            return {}

        # 2. 회계연도 결정: 파라미터 있으면 사용, 없으면 현재 날짜 기반
        if bsns_year is None:
            now = datetime.now(KST)
            year = now.year - 1 if now.month < 4 else now.year - 1
            bsns_year = str(year)

        # 3. 재무제표 (CFS 우선, OFS 폴백)
        financials = {}
        for fs_div in ["CFS", "OFS"]:
            data = self._get("fnlttSinglAcntAll.json", {
                "corp_code": corp_code,
                "bsns_year": bsns_year,
                "reprt_code": "11011",  # 사업보고서
                "fs_div": fs_div,
            })
            if data.get("status") == "000" and data.get("list"):
                financials = self._parse_financials(data["list"])
                if financials:
                    break

        if not financials:
            return {}

        # 4. 시가총액 (yfinance .KS)
        market_cap = self._get_market_cap_yfinance(ticker)
        if not market_cap:
            return {}

        # 5. PER/PBR/ROE 계산
        net_income = financials.get("net_income", 0)
        equity = financials.get("equity", 0)

        per = market_cap / net_income if net_income > 0 else 0
        pbr = market_cap / equity if equity > 0 else 0
        roe = net_income / equity if equity > 0 else 0

        # 6. 배당수익률 (DART 배당 API)
        div_yield = self._get_dividend_yield(corp_code, bsns_year, market_cap)

        return {
            "per": round(per, 2),
            "pbr": round(pbr, 2),
            "roe": round(roe, 4),
            "div_yield": round(div_yield, 4),
            "market_cap": market_cap,
            "net_income": net_income,
            "equity": equity,
            "source": "dart",
            "year": bsns_year,
        }

    def _parse_financials(self, items: list) -> dict:
        """재무제표 항목에서 자본총계/당기순이익/매출액 추출."""
        result = {}
        target_accounts = {
            "ifrs-full_Equity": "equity",
            "dart_TotalEquity": "equity",
            "ifrs-full_ProfitLoss": "net_income",
            "dart_NetIncome": "net_income",
            "ifrs-full_Revenue": "revenue",
            "dart_Revenue": "revenue",
        }

        for item in items:
            account_id = item.get("account_id", "")
            account_nm = item.get("account_nm", "")
            thstrm_amount = item.get("thstrm_amount", "0") or "0"

            # account_id 기반 매핑
            key = target_accounts.get(account_id)
            if not key:
                # 계정명 기반 폴백
                if "자본총계" in account_nm:
                    key = "equity"
                elif "당기순이익" in account_nm:
                    key = "net_income"
                elif "매출액" in account_nm or "수익(매출액)" in account_nm:
                    key = "revenue"

            if key and key not in result:
                try:
                    val = int(thstrm_amount.replace(",", ""))
                    result[key] = val * 1_000_000  # 단위: 백만원 → 원
                except (ValueError, AttributeError):
                    pass

        return result

    def _get_market_cap_yfinance(self, ticker: str) -> float:
        """yfinance로 시가총액 조회 (KRX IP 차단 우회)."""
        try:
            import yfinance as yf
            t = yf.Ticker(f"{ticker}.KS")
            info = t.info
            return float(info.get("marketCap") or 0)
        except Exception as e:
            logger.warning(f"yfinance market cap failed for {ticker}: {e}")
            return 0.0

    def _get_dividend_yield(self, corp_code: str, bsns_year: str, market_cap: float) -> float:
        """DART 배당 API에서 보통주 DPS 추출 → 배당수익률 계산."""
        if not market_cap:
            return 0.0
        try:
            data = self._get("alotMatter.json", {
                "corp_code": corp_code,
                "bsns_year": bsns_year,
                "reprt_code": "11011",
            })
            if data.get("status") != "000" or not data.get("list"):
                return 0.0

            # 보통주 현금배당금 총액
            for item in data["list"]:
                se = item.get("se", "")
                if "보통주" in se and "현금" in se:
                    amount_str = item.get("dps", "0") or "0"
                    dps = float(amount_str.replace(",", ""))
                    # 발행주식수로 배당수익률 계산 (근사값)
                    shares = self._get_shares_outstanding(corp_code, bsns_year)
                    if shares and dps:
                        price = market_cap / shares
                        return dps / price if price > 0 else 0.0
        except Exception as e:
            logger.warning(f"Failed to get dividend yield: {e}")
        return 0.0

    def _get_shares_outstanding(self, corp_code: str, bsns_year: str) -> int:
        """발행주식수 조회 (DART stockTotqySttus API)."""
        try:
            data = self._get("stockTotqySttus.json", {
                "corp_code": corp_code,
                "bsns_year": bsns_year,
                "reprt_code": "11011",
            })
            if data.get("status") == "000" and data.get("list"):
                for item in data["list"]:
                    if "보통주" in item.get("se", ""):
                        shares_str = item.get("istc_totqy", "0") or "0"
                        return int(shares_str.replace(",", ""))
        except Exception as e:
            logger.warning(f"Failed to get shares outstanding: {e}")
        return 0


def score_dart_fundamental(ticker: str, neutral: dict) -> dict:
    """DART 기반 펀더멘탈 스코어 반환. data_collectors.py에서 호출."""
    fetcher = DARTFundamentalFetcher()
    data = fetcher.get_financial_data(ticker)
    if not data:
        return neutral

    per = data.get("per", 0)
    pbr = data.get("pbr", 0)
    div_yield = data.get("div_yield", 0)

    # sector_avg는 기본값 사용 (DART 데이터는 섹터 벤치마크 없이 절대값 기준)
    per_score = max(0.0, 1.0 - (per / 20.0)) if per > 0 else 0.5
    pbr_score = max(0.0, 1.0 - (pbr / 1.5)) if pbr > 0 else 0.5
    div_score = min(1.0, div_yield / 0.05) if div_yield > 0 else 0.0

    fund_score = per_score * 0.4 + pbr_score * 0.3 + div_score * 0.3

    return {
        "score": round(fund_score, 4),
        "details": {
            "per_score": round(per_score, 4),
            "pbr_score": round(pbr_score, 4),
            "div_score": round(div_score, 4),
            "per": per,
            "pbr": pbr,
            "div": div_yield * 100,
            "source": "dart",
        },
    }
