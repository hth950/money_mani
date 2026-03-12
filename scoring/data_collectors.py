"""Data collectors for scoring pipeline."""

import logging
import math
from datetime import datetime, timedelta, timezone

from utils.cache import TTLCache

KST = timezone(timedelta(hours=9))

logger = logging.getLogger("money_mani.scoring.data_collectors")

# Sector cache: refreshed once per day
_sector_cache: dict = {}
_sector_cache_date: str = ""

# Module-level TTL caches — persist across scorer instances created per scan
_fundamental_cache: TTLCache = TTLCache(default_ttl=4 * 3600, maxsize=256)   # 4 hours
_flow_cache: TTLCache = TTLCache(default_ttl=4 * 3600, maxsize=256)           # 4 hours
_macro_cache: TTLCache = TTLCache(default_ttl=24 * 3600, maxsize=4)           # 24 hours

KRX_SECTOR_MAP = {
    "전기전자": "Technology",
    "금융업": "Financial Services",
    "보험": "Financial Services",
    "증권": "Financial Services",
    "은행": "Financial Services",
    "의약품": "Healthcare",
    "서비스업": "Consumer Cyclical",
    "유통업": "Consumer Cyclical",
    "섬유의복": "Consumer Cyclical",
    "운수장비": "Industrials",
    "기계": "Industrials",
    "운수창고업": "Industrials",
    "통신업": "Communication Services",
    "음식료품": "Consumer Defensive",
    "화학": "Basic Materials",
    "철강금속": "Basic Materials",
    "비금속광물": "Basic Materials",
    "종이목재": "Basic Materials",
    "건설업": "Real Estate",
    "전기가스업": "Utilities",
    "의료정밀": "Healthcare",
    "제약": "Healthcare",
}


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

    def __init__(self):
        pass  # Uses module-level _fundamental_cache

    def _load_benchmarks_config(self) -> dict:
        """Load sector_benchmarks section from config/scoring.yaml."""
        try:
            import yaml
            from pathlib import Path
            config_path = Path(__file__).parent.parent / "config" / "scoring.yaml"
            if config_path.exists():
                with open(config_path) as f:
                    full_config = yaml.safe_load(f) or {}
                return full_config.get("sector_benchmarks", {})
        except Exception as e:
            logger.warning(f"Failed to load sector benchmarks config: {e}")
        return {}

    def _get_sector_benchmarks(self, sector: str) -> dict:
        """Get PER/ROE benchmarks for the given sector."""
        if not hasattr(self, '_benchmarks_config'):
            self._benchmarks_config = self._load_benchmarks_config()
        config = self._benchmarks_config
        if not config.get("enabled", False):
            return {"per": 30.0, "roe": 0.30, "pbr": 1.5}
        sectors = config.get("sectors", {})
        defaults = config.get("defaults", {"per": 20.0, "roe": 0.15, "pbr": 1.5})
        return sectors.get(sector, defaults)

    def score(self, ticker: str, market: str) -> dict:
        """Return fundamental score and details.

        Returns:
            {"score": 0.0~1.0, "details": {"per_score": ..., "pbr_score": ..., "div_score": ...}}
        """
        neutral = {"score": 0.5, "details": {"per_score": 0.5, "pbr_score": 0.5, "div_score": 0.5}}

        cache_key = f"{market}:{ticker}"
        hit, cached = _fundamental_cache.get(cache_key)
        if hit:
            return cached

        try:
            if market == "KRX":
                result = self._score_krx(ticker, neutral)
            else:
                result = self._score_us(ticker, neutral)
            _fundamental_cache.set(cache_key, result)
            return result
        except Exception as e:
            logger.warning(f"FundamentalCollector.score failed for {ticker}: {e}")
            return neutral

    def _score_krx(self, ticker: str, neutral: dict) -> dict:
        # Primary: DART Open API (works on OCI; pykrx is IP-blocked by KRX)
        from scoring.dart_fundamental import DARTFundamentalClient
        dart = DARTFundamentalClient()
        fund_data = dart.get_fundamentals(ticker) if dart.enabled else None

        yf_sector: str | None = None
        if fund_data:
            ticker_per = float(fund_data.get("PER") or 0)
            ticker_pbr = float(fund_data.get("PBR") or 0)
            ticker_div = float(fund_data.get("DIV") or 0)
            # Get sector from yfinance (works on OCI; fdr.StockListing is blocked)
            try:
                import yfinance as yf
                yf_sector = yf.Ticker(f"{ticker}.KS").info.get("sector") or None
            except Exception:
                pass
        else:
            # Fallback: pykrx (may be blocked on cloud servers)
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
            row = df.iloc[-1]
            ticker_per = float(row.get("PER", 0) or 0)
            ticker_pbr = float(row.get("PBR", 0) or 0)
            ticker_div = float(row.get("DIV", 0) or 0)

        # Sector-aware benchmarks from config/scoring.yaml
        # yf_sector is English (e.g. "Technology"); fall back to KRX listing map
        if yf_sector:
            eng_sector = yf_sector
            raw_sector = eng_sector
        else:
            sector_map = _get_sector_map()
            raw_sector = sector_map.get(ticker, "Unknown")
            eng_sector = KRX_SECTOR_MAP.get(raw_sector, None)
            if eng_sector is None and raw_sector != "Unknown":
                logger.warning(f"Unmapped KRX sector: '{raw_sector}' for {ticker}")
                eng_sector = "Unknown"
        benchmarks = self._get_sector_benchmarks(eng_sector or "Unknown")
        sector_avg_per = benchmarks["per"]
        sector_avg_pbr = benchmarks.get("pbr", 1.5)

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
                "sector": raw_sector,
                "sector_eng": eng_sector,
                "sector_benchmark_per": sector_avg_per,
                "sector_benchmark_pbr": sector_avg_pbr,
            },
        }

    def _score_us(self, ticker: str, neutral: dict) -> dict:
        from market_data.us_fetcher import USFetcher
        try:
            data = USFetcher().get_fundamentals(ticker)
        except Exception as e:
            logger.warning(f"US get_fundamentals failed for {ticker}: {e}")
            return neutral

        per = float(data.get("PER") or 0)
        pbr = float(data.get("PBR") or 0)
        div_yield = float(data.get("DIV") or 0)
        roe = float(data.get("ROE") or 0)
        profit_margin = float(data.get("profit_margin") or 0)

        # Get sector-specific benchmarks
        sector = data.get("sector", "Unknown")
        benchmarks = self._get_sector_benchmarks(sector)
        benchmark_per = benchmarks["per"]
        benchmark_roe = benchmarks["roe"]

        # PER score: lower is better (0 at PER=benchmark, 1 at PER=0)
        per_score = max(0.0, min(1.0, 1.0 - (per / benchmark_per))) if per > 0 else 0.5
        # ROE score: higher is better (cap at sector benchmark)
        roe_score = min(1.0, max(0.0, roe / benchmark_roe)) if roe > 0 else 0.3
        # Dividend score: higher is better (cap at 5%)
        div_score = min(1.0, div_yield / 0.05) if div_yield > 0 else 0.0
        # Profitability score: higher is better (cap at 20%)
        profit_score = min(1.0, max(0.0, profit_margin / 0.20)) if profit_margin > 0 else 0.3

        fundamental_score = per_score * 0.35 + roe_score * 0.30 + div_score * 0.15 + profit_score * 0.20

        return {
            "score": round(fundamental_score, 4),
            "details": {
                "per_score": round(per_score, 4),
                "roe_score": round(roe_score, 4),
                "div_score": round(div_score, 4),
                "profit_score": round(profit_score, 4),
                "per": per,
                "pbr": pbr,
                "div": div_yield,
                "roe": roe,
                "profit_margin": profit_margin,
                "sector": sector,
                "sector_benchmark_per": benchmark_per,
                "sector_benchmark_roe": benchmark_roe,
            },
        }


# Market cap tier normalization for flow amounts (KRW)
_AMOUNT_SCALE = {"large": 1e11, "mid": 1e10, "small": 1e9}


class FlowCollector:
    """Investor flow data collection and scoring (0~1) - KRX only."""

    def __init__(self):
        pass  # Uses module-level _flow_cache

    def _get_amount_scale(self, ticker: str) -> float:
        """Get normalization scale based on market cap tier."""
        try:
            from market_data.fdr_fetcher import FDRFetcher
            df = FDRFetcher().get_krx_listings("KRX")
            row = df[df.index.astype(str) == ticker]
            if not row.empty:
                marcap = row.iloc[0].get("Marcap", 0) or 0
                if marcap > 10e12:  # 10조+
                    return _AMOUNT_SCALE["large"]
                elif marcap > 1e12:  # 1조+
                    return _AMOUNT_SCALE["mid"]
        except Exception:
            return _AMOUNT_SCALE["mid"]
        return _AMOUNT_SCALE["small"]

    def _load_flow_config(self) -> dict:
        """Load flow_scoring config section."""
        try:
            import yaml
            from pathlib import Path
            config_path = Path(__file__).parent.parent / "config" / "scoring.yaml"
            if config_path.exists():
                with open(config_path) as f:
                    return (yaml.safe_load(f) or {}).get("flow_scoring", {})
        except Exception:
            pass
        return {}

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

        cache_key = f"{market}:{ticker}"
        hit, cached = _flow_cache.get(cache_key)
        if hit:
            return cached

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

            # Load config (cached on first call)
            if not hasattr(self, '_flow_config'):
                self._flow_config = self._load_flow_config()
            flow_cfg = self._flow_config

            # Check feature flag
            if not flow_cfg.get("enabled", False):
                # Fallback: existing streak-only logic
                foreign_streak = consecutive_buy_days(recent[foreign_col]) if foreign_col is not None else 0
                inst_streak = consecutive_buy_days(recent[inst_col]) if inst_col is not None else 0
                foreign_streak_score = min(1.0, foreign_streak / 10)
                inst_streak_score = min(1.0, inst_streak / 10)
                flow_score = foreign_streak_score * 0.5 + inst_streak_score * 0.5
                result = {
                    "score": round(flow_score, 4),
                    "details": {
                        "foreign_streak_score": round(foreign_streak_score, 4),
                        "inst_streak_score": round(inst_streak_score, 4),
                        "foreign_consecutive_buy_days": foreign_streak,
                        "inst_consecutive_buy_days": inst_streak,
                        "mode": "streak_only",
                    },
                }
                self._cache[cache_key] = result
                return result

            # === Enhanced 4-component scoring ===
            components = flow_cfg.get("components", {"streak": 0.20, "amount": 0.35, "ratio": 0.25, "synergy": 0.20})

            # 1. Streak component (existing logic)
            foreign_streak = consecutive_buy_days(recent[foreign_col]) if foreign_col is not None else 0
            inst_streak = consecutive_buy_days(recent[inst_col]) if inst_col is not None else 0
            streak_score = (min(1.0, foreign_streak / 10) + min(1.0, inst_streak / 10)) / 2

            # 2. Amount component (NEW)
            amount_scale = self._get_amount_scale(ticker)
            foreign_amount = float(recent[foreign_col].sum()) if foreign_col is not None else 0
            inst_amount = float(recent[inst_col].sum()) if inst_col is not None else 0
            foreign_amount_score = 1 / (1 + math.exp(-foreign_amount / amount_scale))
            inst_amount_score = 1 / (1 + math.exp(-inst_amount / amount_scale))
            amount_score = (foreign_amount_score + inst_amount_score) / 2

            # 3. Ratio component (NEW) — positive days / total days
            total_days = len(recent)
            foreign_positive = int((recent[foreign_col] > 0).sum()) if foreign_col is not None else 0
            inst_positive = int((recent[inst_col] > 0).sum()) if inst_col is not None else 0
            ratio_score = ((foreign_positive / total_days) + (inst_positive / total_days)) / 2 if total_days > 0 else 0.5

            # 4. Synergy component (NEW) — both buying same day
            if foreign_col is not None and inst_col is not None:
                dual_buy = int(((recent[foreign_col] > 0) & (recent[inst_col] > 0)).sum())
                synergy_score = dual_buy / total_days if total_days > 0 else 0.0
            else:
                dual_buy = 0
                synergy_score = 0.0

            # Weighted composite
            flow_score = (
                streak_score * components.get("streak", 0.20)
                + amount_score * components.get("amount", 0.35)
                + ratio_score * components.get("ratio", 0.25)
                + synergy_score * components.get("synergy", 0.20)
            )

            result = {
                "score": round(flow_score, 4),
                "details": {
                    "streak_score": round(streak_score, 4),
                    "amount_score": round(amount_score, 4),
                    "ratio_score": round(ratio_score, 4),
                    "synergy_score": round(synergy_score, 4),
                    "foreign_streak": foreign_streak,
                    "inst_streak": inst_streak,
                    "foreign_net_amount": foreign_amount,
                    "inst_net_amount": inst_amount,
                    "foreign_positive_days": foreign_positive,
                    "inst_positive_days": inst_positive,
                    "synergy_days": dual_buy,
                    "total_days": total_days,
                    "mode": "enhanced",
                },
            }
            _flow_cache.set(cache_key, result)
            return result
        except Exception as e:
            logger.warning(f"FlowCollector scoring failed for {ticker}: {e}")
            return neutral


class MacroCollector:
    """Macro environment score based on VIX (piecewise-linear interpolation)."""

    def __init__(self):
        self._config = self._load_config()

    def _load_config(self) -> dict:
        """Load macro section from config/scoring.yaml."""
        try:
            import yaml
            from pathlib import Path
            config_path = Path(__file__).parent.parent / "config" / "scoring.yaml"
            if config_path.exists():
                with open(config_path) as f:
                    return (yaml.safe_load(f) or {}).get("macro", {})
        except Exception:
            pass
        return {}

    def _vix_to_score(self, vix: float, anchors: list[list]) -> tuple[float, str]:
        """Piecewise-linear interpolation over VIX anchor points.

        Args:
            vix: Current VIX value.
            anchors: Sorted list of [vix_level, score] pairs, e.g.
                     [[15, 0.80], [20, 0.70], [25, 0.50], [35, 0.15]]

        Returns:
            (score, regime_label)
        """
        # Clamp to boundaries
        if vix <= anchors[0][0]:
            return anchors[0][1], "calm"
        if vix >= anchors[-1][0]:
            return anchors[-1][1], "fear"

        # Determine regime label and interpolate
        regimes = ["calm", "caution", "elevated", "fear"]
        for i in range(len(anchors) - 1):
            x0, y0 = anchors[i]
            x1, y1 = anchors[i + 1]
            if x0 <= vix <= x1:
                ratio = (vix - x0) / (x1 - x0)
                score = y0 + ratio * (y1 - y0)
                regime = regimes[min(i + 1, len(regimes) - 1)]
                return round(score, 4), regime

        return anchors[-1][1], "fear"

    def score(self) -> dict:
        """Return macro environment score based on VIX.

        Returns:
            {"score": 0.0~1.0, "details": {"vix": float, "regime": str}}
        """
        if not self._config.get("enabled", False):
            return {"score": 0.5, "details": {"note": "macro disabled"}}

        cache_key = "macro"
        hit, cached = _macro_cache.get(cache_key)
        if hit:
            return cached

        try:
            from market_data.us_fetcher import USFetcher
            vix = USFetcher().get_vix()
        except Exception:
            vix = None

        if vix is None:
            return {"score": 0.5, "details": {"note": "VIX unavailable"}}

        vix_cfg = self._config.get("vix", {})
        anchors = vix_cfg.get("anchors", [[20, 0.70], [30, 0.20]])
        macro_score, regime = self._vix_to_score(float(vix), anchors)

        result = {"score": macro_score, "details": {"vix": round(vix, 2), "regime": regime}}
        _macro_cache.set(cache_key, result)
        return result
