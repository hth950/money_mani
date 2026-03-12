"""DART Open API based fundamental data collector for KRX stocks.

Provides PER, PBR, ROE, DIV for KRX tickers using:
- DART financial statement API (재무제표) for equity/net_income
- FinanceDataReader for market cap (works without KRX IP block)
- Shares outstanding from DART 주식총수 API

DART free tier: 10,000 req/day. Corp-code mapping cached daily.
Individual ticker data cached 4 hours (via module-level TTLCache).

Usage:
    client = DARTFundamentalClient()
    data = client.get_fundamentals("005930")
    # -> {"per": 12.3, "pbr": 1.1, "roe": 0.15, "div": 2.0}
"""

import io
import logging
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from utils.cache import TTLCache
from utils.config_loader import get_env

KST = timezone(timedelta(hours=9))
logger = logging.getLogger("money_mani.scoring.dart_fundamental")

DART_BASE = "https://opendart.fss.or.kr/api"

# Module-level caches
_corp_code_cache: TTLCache = TTLCache(default_ttl=24 * 3600, maxsize=4)   # mapping: daily
_financial_cache: TTLCache = TTLCache(default_ttl=4 * 3600, maxsize=512)  # per ticker: 4h


class DARTFundamentalClient:
    """Fetch KRX fundamental data (PER/PBR/ROE/DIV) via DART Open API."""

    def __init__(self):
        self._api_key = get_env("DART_API_KEY")
        if not self._api_key:
            logger.warning("DART_API_KEY not set — DARTFundamentalClient disabled")

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    # ------------------------------------------------------------------ #
    # Public                                                               #
    # ------------------------------------------------------------------ #

    def get_fundamentals(self, ticker: str) -> dict | None:
        """Return {per, pbr, roe, div} or None if unavailable.

        Tries latest available annual year (current-1 or current-2).
        """
        if not self.enabled:
            return None

        cache_key = f"fund:{ticker}"
        hit, cached = _financial_cache.get(cache_key)
        if hit:
            return cached

        result = self._fetch(ticker)
        if result:
            _financial_cache.set(cache_key, result)
        return result

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    def _fetch(self, ticker: str) -> dict | None:
        corp_code = self._corp_code(ticker)
        if not corp_code:
            logger.debug(f"No corp_code for {ticker}")
            return None

        now_year = datetime.now(KST).year
        # In Jan-Mar, the previous year's annual report may not be filed yet
        years_to_try = [now_year - 1, now_year - 2]
        for year in years_to_try:
            data = self._financial_statement(corp_code, year)
            if data:
                # Try to enrich with market cap-based ratios
                return self._build_ratios(ticker, data, year)

        logger.debug(f"No DART financial data for {ticker}")
        return None

    def _corp_code(self, ticker: str) -> str | None:
        """Look up DART corp_code from KRX ticker (6-digit string)."""
        mapping = self._load_corp_code_mapping()
        return mapping.get(ticker.zfill(6))

    def _load_corp_code_mapping(self) -> dict:
        """Download and cache DART corp_code → stock_code mapping."""
        hit, cached = _corp_code_cache.get("mapping")
        if hit:
            return cached

        try:
            url = f"{DART_BASE}/corpCode.xml"
            r = requests.get(url, params={"crtfc_key": self._api_key}, timeout=30)
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                xml_bytes = z.read(z.namelist()[0])
            root = ET.fromstring(xml_bytes)
            mapping: dict[str, str] = {}
            for item in root.findall(".//list"):
                stock_code = (item.findtext("stock_code") or "").strip()
                corp_code = (item.findtext("corp_code") or "").strip()
                if stock_code and corp_code:
                    mapping[stock_code] = corp_code
            logger.info(f"DART corp_code mapping loaded: {len(mapping)} entries")
            _corp_code_cache.set("mapping", mapping)
            return mapping
        except Exception as e:
            logger.warning(f"Failed to load DART corp_code mapping: {e}")
            return {}

    def _financial_statement(self, corp_code: str, year: int) -> dict | None:
        """Fetch consolidated annual financial statements.

        Returns dict with equity, net_income, shares, revenue or None.
        """
        try:
            url = f"{DART_BASE}/fnlttSinglAcntAll.json"
            params = {
                "crtfc_key": self._api_key,
                "corp_code": corp_code,
                "bsns_year": str(year),
                "reprt_code": "11011",  # 사업보고서 (annual)
                "fs_div": "CFS",        # 연결재무제표
            }
            r = requests.get(url, params=params, timeout=15)
            data = r.json()

            if data.get("status") != "000":
                # Fallback to OFS (별도재무제표) if CFS not available
                params["fs_div"] = "OFS"
                r = requests.get(url, params=params, timeout=15)
                data = r.json()

            if data.get("status") != "000" or not data.get("list"):
                return None

            # Keep the maximum value for duplicate account names
            # (CFS has subtotals; the consolidated total is the largest)
            items: dict[str, float] = {}
            for item in data["list"]:
                if item.get("thstrm_amount"):
                    name = item["account_nm"].strip()
                    val = self._parse_amount(item.get("thstrm_amount"))
                    if name not in items or val > items[name]:
                        items[name] = val

            equity = (
                items.get("자본총계")
                or items.get("지배기업소유주지분")
                or items.get("자본")
                or 0
            )
            net_income = (
                items.get("당기순이익(손실)")
                or items.get("당기순이익")
                or items.get("분기순이익")
                or 0
            )
            revenue = (
                items.get("매출액")
                or items.get("영업수익")
                or 0
            )

            if equity == 0 and net_income == 0:
                return None

            return {"equity": equity, "net_income": net_income, "revenue": revenue, "year": year}

        except Exception as e:
            logger.debug(f"DART financial_statement failed for corp {corp_code} {year}: {e}")
            return None

    def _get_shares(self, corp_code: str, year: int) -> int:
        """Fetch total issued shares from DART 주식총수 API."""
        try:
            url = f"{DART_BASE}/stockTotqySttus.json"
            params = {
                "crtfc_key": self._api_key,
                "corp_code": corp_code,
                "bsns_year": str(year),
                "reprt_code": "11011",
            }
            r = requests.get(url, params=params, timeout=10)
            data = r.json()
            if data.get("status") == "000" and data.get("list"):
                for item in data["list"]:
                    # 보통주 발행주식 총수
                    if "보통주" in (item.get("se") or ""):
                        val = item.get("istc_totqy", "0").replace(",", "")
                        return int(val)
        except Exception as e:
            logger.debug(f"DART stockTotqySttus failed: {e}")
        return 0

    def _get_market_cap(self, ticker: str) -> float:
        """Get current market cap via yfinance (works from any IP)."""
        try:
            import yfinance as yf
            t = yf.Ticker(f"{ticker}.KS")
            hist = t.history(period="5d")
            if hist.empty:
                return 0.0
            price = float(hist["Close"].iloc[-1])
            # Prefer yfinance shares; fall back to DART shares API
            shares = t.info.get("sharesOutstanding") or 0
            if not shares:
                corp_code = self._corp_code(ticker)
                if corp_code:
                    shares = self._get_shares(corp_code, datetime.now(KST).year - 1)
            return price * shares if shares else 0.0
        except Exception as e:
            logger.debug(f"MarketCap fetch failed for {ticker}: {e}")
        return 0.0

    def _build_ratios(self, ticker: str, fs: dict, year: int) -> dict | None:
        """Compute PER, PBR, ROE, DIV from financial statement + market cap."""
        equity = fs["equity"]
        net_income = fs["net_income"]
        market_cap = self._get_market_cap(ticker)

        if market_cap <= 0 or equity <= 0:
            return None

        pbr = market_cap / equity
        roe = net_income / equity if equity > 0 else 0

        # PER: needs shares or use market_cap / net_income directly
        per = market_cap / net_income if net_income > 0 else 0

        # DIV: try shares outstanding (best effort)
        div = 0.0
        shares = self._get_shares(
            self._corp_code(ticker) or "", year
        )
        if shares > 0 and market_cap > 0:
            price = market_cap / shares
            div = self._get_dividend_yield(ticker, year, price)

        result = {
            "PER": round(per, 2),
            "PBR": round(pbr, 2),
            "ROE": round(roe, 4),
            "DIV": round(div, 2),
            "source": "dart",
            "year": year,
        }
        logger.debug(f"DART fundamentals {ticker}: {result}")
        return result

    def _get_dividend_yield(self, ticker: str, year: int, price: float) -> float:
        """Estimate dividend yield from DART 배당 API (best effort)."""
        try:
            corp_code = self._corp_code(ticker)
            if not corp_code:
                return 0.0
            url = f"{DART_BASE}/alotMatter.json"
            params = {
                "crtfc_key": self._api_key,
                "corp_code": corp_code,
                "bsns_year": str(year),
                "reprt_code": "11011",
            }
            r = requests.get(url, params=params, timeout=10)
            data = r.json()
            if data.get("status") == "000" and data.get("list"):
                for item in data["list"]:
                    if "보통주" in (item.get("se") or ""):
                        dps = self._parse_amount(item.get("dps"))
                        if dps and price > 0:
                            return (dps / price) * 100
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _parse_amount(value: str | None) -> float:
        """Parse DART amount string (may include commas, minus, parentheses)."""
        if not value:
            return 0.0
        value = str(value).replace(",", "").replace(" ", "")
        if value.startswith("(") and value.endswith(")"):
            value = "-" + value[1:-1]
        try:
            return float(value)
        except ValueError:
            return 0.0
