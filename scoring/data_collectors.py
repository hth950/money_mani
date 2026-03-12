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
    """Fundamental data collection and scoring (0~1)."""

    def score(self, ticker: str, market: str) -> dict:
        """Return fundamental score and details.

        Returns:
            {"score": 0.0~1.0, "details": {"per_score": ..., "pbr_score": ..., "div_score": ...}}
        """
        neutral = {"score": 0.5, "details": {"per_score": 0.5, "pbr_score": 0.5, "div_score": 0.5}}

        try:
            if market == "KRX":
                return self._score_krx(ticker, neutral)
            else:
                return self._score_us(ticker, neutral)
        except Exception as e:
            logger.warning(f"FundamentalCollector.score failed for {ticker}: {e}")
            return neutral

    def _score_krx(self, ticker: str, neutral: dict) -> dict:
        from market_data.krx_fetcher import KRXFetcher
        today = datetime.now(KST).strftime("%Y-%m-%d")
        start = (datetime.now(KST) - timedelta(days=5)).strftime("%Y-%m-%d")
        try:
            df = KRXFetcher().get_fundamentals(ticker, start, today)
        except Exception as e:
            logger.warning(f"KRX get_fundamentals failed for {ticker}: {e}")
            return neutral

        if df is None or df.empty:
            return neutral

        # Use the latest row
        row = df.iloc[-1]
        ticker_per = float(row.get("PER", 0) or 0)
        ticker_pbr = float(row.get("PBR", 0) or 0)
        ticker_div = float(row.get("DIV", 0) or 0)

        # Sector averages from KRX listings fundamentals (simplified: use fixed benchmarks)
        # Full sector-avg computation requires joining fundamentals for all sector peers
        # which is too expensive for v1; use fixed sector benchmarks instead.
        sector_avg_per = 15.0
        sector_avg_pbr = 1.5

        per_score = max(0.0, 1.0 - (ticker_per / sector_avg_per)) if ticker_per > 0 else 0.5
        pbr_score = max(0.0, 1.0 - (ticker_pbr / sector_avg_pbr)) if ticker_pbr > 0 else 0.5
        div_score = min(1.0, ticker_div / 5.0)

        fundamental_score = per_score * 0.4 + pbr_score * 0.3 + div_score * 0.3

        return {
            "score": round(fundamental_score, 4),
            "details": {
                "per_score": round(per_score, 4),
                "pbr_score": round(pbr_score, 4),
                "div_score": round(div_score, 4),
                "per": ticker_per,
                "pbr": ticker_pbr,
                "div": ticker_div,
            },
        }

    def _score_us(self, ticker: str, neutral: dict) -> dict:
        from market_data.us_fetcher import USFetcher
        try:
            data = USFetcher().get_fundamentals(ticker)
        except Exception as e:
            logger.warning(f"US get_fundamentals failed for {ticker}: {e}")
            return neutral

        ticker_per = float(data.get("PER") or 0)
        ticker_pbr = float(data.get("PBR") or 0)
        ticker_div = float(data.get("DIV") or 0)

        # US: use 0.5 default for sector averages (simplified)
        per_score = 0.5
        pbr_score = 0.5
        div_score = min(1.0, ticker_div / 5.0)

        fundamental_score = per_score * 0.4 + pbr_score * 0.3 + div_score * 0.3

        return {
            "score": round(fundamental_score, 4),
            "details": {
                "per_score": round(per_score, 4),
                "pbr_score": round(pbr_score, 4),
                "div_score": round(div_score, 4),
                "per": ticker_per,
                "pbr": ticker_pbr,
                "div": ticker_div,
            },
        }


class FlowCollector:
    """Investor flow data collection and scoring (0~1) - KRX only."""

    def score(self, ticker: str, market: str) -> dict:
        """Return flow score and details.

        KRX only; US returns neutral 0.5.
        """
        neutral = {
            "score": 0.5,
            "details": {"foreign_streak_score": 0.5, "inst_streak_score": 0.5},
        }

        if market != "KRX":
            return neutral

        try:
            from market_data.krx_fetcher import KRXFetcher
            today = datetime.now(KST).strftime("%Y-%m-%d")
            start = (datetime.now(KST) - timedelta(days=14)).strftime("%Y-%m-%d")
            df = KRXFetcher().get_investor_flows(ticker, start, today)
        except Exception as e:
            logger.warning(f"FlowCollector get_investor_flows failed for {ticker}: {e}")
            return neutral

        if df is None or df.empty:
            return neutral

        try:
            # Identify foreign and institutional columns
            # pykrx returns columns like: 기관합계, 외국인합계, 개인, etc.
            foreign_col = None
            inst_col = None
            for col in df.columns:
                col_str = str(col)
                if "외국" in col_str:
                    foreign_col = col
                if "기관" in col_str:
                    inst_col = col

            recent = df.tail(10)

            def consecutive_buy_days(series) -> int:
                """Count trailing consecutive positive days."""
                count = 0
                for val in reversed(series.tolist()):
                    if val > 0:
                        count += 1
                    else:
                        break
                return count

            foreign_streak = consecutive_buy_days(recent[foreign_col]) if foreign_col is not None else 0
            inst_streak = consecutive_buy_days(recent[inst_col]) if inst_col is not None else 0

            foreign_streak_score = min(1.0, foreign_streak / 10)
            inst_streak_score = min(1.0, inst_streak / 10)
            flow_score = foreign_streak_score * 0.5 + inst_streak_score * 0.5

            return {
                "score": round(flow_score, 4),
                "details": {
                    "foreign_streak_score": round(foreign_streak_score, 4),
                    "inst_streak_score": round(inst_streak_score, 4),
                    "foreign_consecutive_buy_days": foreign_streak,
                    "inst_consecutive_buy_days": inst_streak,
                },
            }
        except Exception as e:
            logger.warning(f"FlowCollector scoring failed for {ticker}: {e}")
            return neutral


class MacroCollector:
    """Macro environment score (cached daily). v1: always returns neutral 0.5."""

    def score(self) -> dict:
        return {"score": 0.5, "details": {"note": "macro scoring not yet implemented"}}
