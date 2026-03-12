"""Data collectors for scoring pipeline."""

import logging
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

logger = logging.getLogger("money_mani.scoring.data_collectors")

# Sector cache: refreshed once per day
_sector_cache: dict = {}
_sector_cache_date: str = ""


def _get_sector_map() -> dict:
    """Return KRX sector map, refreshing once per calendar day."""
    global _sector_cache, _sector_cache_date
    today = datetime.now(KST).strftime("%Y-%m-%d")
    if _sector_cache_date == today and _sector_cache:
        return _sector_cache
    try:
        from market_data.fdr_fetcher import FDRFetcher
        df = FDRFetcher().get_krx_listings("KRX")
        # Expected columns include 'Code' (or index) and 'Sector'
        if "Sector" not in df.columns:
            logger.warning("Sector column not found in KRX listings")
            return {}
        code_col = "Code" if "Code" in df.columns else df.index.name
        if code_col and code_col != df.index.name:
            df = df.set_index(code_col)
        # Build per-sector average PER/PBR/DIV if available
        sector_map: dict = {}
        for ticker_code, row in df.iterrows():
            sector_map[str(ticker_code)] = row.get("Sector", "Unknown")
        _sector_cache = sector_map
        _sector_cache_date = today
        logger.info(f"Loaded sector map with {len(sector_map)} tickers")
    except Exception as e:
        logger.warning(f"Failed to load sector map: {e}")
        _sector_cache = {}
        _sector_cache_date = today
    return _sector_cache


class FundamentalCollector:
    """Fundamental data via yfinance (works for both KRX and US)."""

    def __init__(self):
        self._cache: dict[str, dict] = {}

    def score(self, ticker: str, market: str) -> dict:
        neutral = {"score": 0.5, "details": {"per_score": 0.5, "roe_score": 0.5, "div_score": 0.5, "profit_score": 0.5}}

        cache_key = f"{market}:{ticker}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            result = self._score_yfinance(ticker, market, neutral)
            self._cache[cache_key] = result
            return result
        except Exception as e:
            logger.warning(f"FundamentalCollector.score failed for {ticker}: {e}")
            return neutral

    def _score_yfinance(self, ticker: str, market: str, neutral: dict) -> dict:
        import yfinance as yf

        if market == "KRX":
            # Try KOSPI (.KS) then KOSDAQ (.KQ)
            info = None
            for suffix in [".KS", ".KQ"]:
                try:
                    info = yf.Ticker(ticker + suffix).info
                    if info.get("forwardPE") or info.get("trailingPE"):
                        break
                except Exception:
                    continue
            if not info:
                return neutral
        else:
            try:
                info = yf.Ticker(ticker).info
            except Exception:
                return neutral

        per = info.get("forwardPE") or info.get("trailingPE") or 0
        roe = info.get("returnOnEquity") or 0
        div_yield = info.get("dividendYield") or 0
        profit_margin = info.get("profitMargins") or 0

        # PER score: lower is better (0 at PER=30, 1 at PER=0)
        per_score = max(0.0, min(1.0, 1.0 - (per / 30.0))) if per > 0 else 0.5
        # ROE score: higher is better (cap at 30%)
        roe_score = min(1.0, max(0.0, roe / 0.30)) if roe > 0 else 0.3
        # Dividend score: higher is better (cap at 5%)
        div_score = min(1.0, div_yield / 0.05) if div_yield > 0 else 0.0
        # Profitability score: higher is better (cap at 20%)
        profit_score = min(1.0, max(0.0, profit_margin / 0.20)) if profit_margin > 0 else 0.3

        fundamental_score = per_score * 0.3 + roe_score * 0.3 + div_score * 0.2 + profit_score * 0.2

        return {
            "score": round(fundamental_score, 4),
            "details": {
                "per_score": round(per_score, 4),
                "roe_score": round(roe_score, 4),
                "div_score": round(div_score, 4),
                "profit_score": round(profit_score, 4),
                "per": round(per, 2) if per else 0,
                "roe": round(roe, 4) if roe else 0,
                "div": round(div_yield, 4) if div_yield else 0,
                "profit_margin": round(profit_margin, 4) if profit_margin else 0,
                "sector": info.get("sector", "Unknown"),
            },
        }


class FlowCollector:
    """Volume-based flow proxy (KRX API blocked, use OHLCV volume instead)."""

    def __init__(self):
        self._cache: dict[str, dict] = {}

    def score(self, ticker: str, market: str) -> dict:
        neutral = {
            "score": 0.5,
            "details": {"volume_surge": 1.0, "price_momentum": 0.0},
        }

        if market != "KRX":
            return neutral

        cache_key = f"{market}:{ticker}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            result = self._score_volume_proxy(ticker, neutral)
            self._cache[cache_key] = result
            return result
        except Exception as e:
            logger.warning(f"FlowCollector.score failed for {ticker}: {e}")
            return neutral

    def _score_volume_proxy(self, ticker: str, neutral: dict) -> dict:
        from pykrx import stock
        end = datetime.now(KST).strftime("%Y%m%d")
        start = (datetime.now(KST) - timedelta(days=40)).strftime("%Y%m%d")

        df = stock.get_market_ohlcv_by_date(start, end, ticker)
        if df is None or len(df) < 20:
            return neutral

        # Volume columns: pykrx 1.2.4 may use Korean or English names
        vol_col = None
        close_col = None
        for col in df.columns:
            col_l = str(col).lower()
            if "거래량" in col_l or "volume" in col_l:
                vol_col = col
            if "종가" in col_l or "close" in col_l:
                close_col = col
        if vol_col is None or close_col is None:
            # Fallback: assume column order (Open, High, Low, Close, Volume)
            vol_col = df.columns[-1]
            close_col = df.columns[3]

        volumes = df[vol_col].astype(float)
        closes = df[close_col].astype(float)

        # Volume surge: 5-day avg / 20-day avg (>1 = increasing interest)
        vol_5d = volumes.tail(5).mean()
        vol_20d = volumes.tail(20).mean()
        volume_surge = vol_5d / vol_20d if vol_20d > 0 else 1.0

        # Price momentum: 5-day return
        if len(closes) >= 6 and closes.iloc[-6] > 0:
            price_momentum = (closes.iloc[-1] - closes.iloc[-6]) / closes.iloc[-6]
        else:
            price_momentum = 0.0

        # Volume surge score: 0.5 at ratio=1.0, 1.0 at ratio=2.0+
        vol_score = min(1.0, max(0.0, (volume_surge - 0.5) / 1.5))
        # Momentum score: -5% → 0.0, 0% → 0.5, +5% → 1.0
        mom_score = min(1.0, max(0.0, (price_momentum + 0.05) / 0.10))

        flow_score = vol_score * 0.5 + mom_score * 0.5

        return {
            "score": round(flow_score, 4),
            "details": {
                "volume_surge": round(volume_surge, 4),
                "price_momentum": round(price_momentum, 4),
                "vol_score": round(vol_score, 4),
                "mom_score": round(mom_score, 4),
            },
        }


class MacroCollector:
    """Macro environment score (cached daily). v1: always returns neutral 0.5."""

    def score(self) -> dict:
        return {"score": 0.5, "details": {"note": "macro scoring not yet implemented"}}
