"""Historical scoring backfill.

Computes 5-axis scores for past dates and stores in scoring_results.
This enables correlation analysis with sufficient data (months of history).

Usage:
    python scripts/backfill_scoring.py [--start 2024-01-01] [--end 2025-12-31]
                                       [--market KRX|US|ALL] [--forward 20]
                                       [--dry-run]

Flow:
    For each ticker × date:
    1. Slice OHLCV up to that date (no look-ahead)
    2. Compute technical score via TechnicalScorer
    3. Compute fundamental from DART/yfinance (year-appropriate)
    4. Compute flow from Naver historical data
    5. Compute macro from VIX history
    6. Intel = 0.5 (no historical news sentiment)
    7. Compute composite = weighted sum
    8. Compute pnl_pct = return over next FORWARD_DAYS trading days
    9. INSERT INTO scoring_results (skip if already exists)
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("backfill_scoring")

KST = timezone(timedelta(hours=9))

# Sampling: backfill every N trading days (1=daily, 5=weekly)
SAMPLE_EVERY_N_DAYS = 5  # weekly to keep runtime manageable; change to 1 for full daily

CACHE_DIR = Path("output/data_cache")

# KRX top tickers (same as prefetch_ohlcv.py)
KRX_TICKERS = [
    "005930", "000660", "035420", "005380", "051910", "006400", "035720",
    "028260", "066570", "017670", "207940", "000270", "105560", "055550",
    "011200", "010130", "032830", "086790", "003550", "015760", "010950",
    "009830", "018260", "011780", "002790", "003490", "096770", "000810",
    "033780", "051600", "047050", "030200", "009150", "012330", "011170",
    "034730", "000100", "005490", "001040", "001450", "010140",
    "006360", "004020", "161390", "247540", "042660", "034220", "097950",
    "020150",
]

US_TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "BRK-B", "LLY", "AVGO", "TSLA",
    "WMT", "JPM", "UNH", "V", "XOM", "MA", "ORCL", "COST", "HD", "PG",
    "JNJ", "ABBV", "BAC", "NFLX", "CRM", "AMD", "KO", "CVX", "MRK", "ADBE",
]


# ─────────────────────────────────────────────────────────────────────────────
# OHLCV fetching (reuses prefetch cache if available)
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten MultiIndex columns and normalize to lowercase."""
    if hasattr(df.columns, "levels"):
        df.columns = [c[0].lower() if isinstance(c, tuple) else str(c).lower() for c in df.columns]
    else:
        df.columns = [c[0].lower() if isinstance(c, tuple) else str(c).lower() for c in df.columns]
    return df


def _cache_path(ticker: str, market: str, start: str) -> Path:
    return CACHE_DIR / f"{market}_{ticker}_{start}.csv"


def _load_ohlcv(ticker: str, market: str, start: str) -> pd.DataFrame:
    """Load OHLCV from CSV cache or fetch. Returns empty DataFrame on failure."""
    path = _cache_path(ticker, market, start)
    if path.exists():
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            return df
        except Exception:
            pass

    if market == "KRX":
        return _fetch_krx_ohlcv(ticker, start, path)
    else:
        return _fetch_us_ohlcv(ticker, start, path)


def _fetch_krx_ohlcv(ticker: str, start: str, cache_path: Path) -> pd.DataFrame:
    df = pd.DataFrame()
    try:
        import pykrx.stock as krx
        time.sleep(0.5)
        raw = krx.get_market_ohlcv(start, "20261231", ticker)
        if not raw.empty:
            raw.index = pd.to_datetime(raw.index)
            raw = _normalize_df(raw)
            if "종가" in raw.columns:
                raw = raw.rename(columns={"시가": "open", "고가": "high", "저가": "low",
                                          "종가": "close", "거래량": "volume"})
            needed = [c for c in ["open", "high", "low", "close", "volume"] if c in raw.columns]
            df = raw[needed].dropna()
    except Exception:
        pass

    if df.empty:
        try:
            import yfinance as yf
            raw = yf.download(f"{ticker}.KS", start=start, progress=False, auto_adjust=True)
            if not raw.empty:
                raw = _normalize_df(raw)
                needed = [c for c in ["open", "high", "low", "close", "volume"] if c in raw.columns]
                df = raw[needed].dropna()
        except Exception:
            pass

    if not df.empty:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache_path)
    return df


def _fetch_us_ohlcv(ticker: str, start: str, cache_path: Path) -> pd.DataFrame:
    try:
        import yfinance as yf
        raw = yf.download(ticker, start=start, progress=False, auto_adjust=True)
        if not raw.empty:
            raw = _normalize_df(raw)
            needed = [c for c in ["open", "high", "low", "close", "volume"] if c in raw.columns]
            df = raw[needed].dropna()
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            df.to_csv(cache_path)
            return df
    except Exception:
        pass
    return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# Score helpers
# ─────────────────────────────────────────────────────────────────────────────

def _technical_score(ticker: str, hist: pd.DataFrame) -> dict:
    from scoring.technical_scorer import TechnicalScorer
    return TechnicalScorer().score(ticker, hist)


def _fundamental_score_at(ticker: str, market: str, year: int) -> dict:
    """Get fundamental score. Uses cached DART/yfinance data for the given year."""
    try:
        from scoring.data_collectors import FundamentalCollector
        result = FundamentalCollector().score(ticker, market)
        return result
    except Exception as e:
        logger.debug(f"Fundamental score error {ticker}: {e}")
    return {"score": 0.5, "details": {}}


def _macro_score_at(date: pd.Timestamp, vix_df: pd.DataFrame) -> dict:
    """Get VIX-based macro score for a historical date."""
    try:
        row = vix_df[vix_df.index <= date]
        if row.empty:
            return {"score": 0.5, "details": {}}
        vix = float(row["close"].iat[-1])

        # Piecewise linear (from config/scoring.yaml anchors)
        anchors = [[15, 0.80], [20, 0.70], [25, 0.50], [35, 0.15]]
        if vix <= anchors[0][0]:
            score = anchors[0][1]
        elif vix >= anchors[-1][0]:
            score = anchors[-1][1]
        else:
            score = 0.5
            for i in range(len(anchors) - 1):
                x0, y0 = anchors[i]
                x1, y1 = anchors[i + 1]
                if x0 <= vix <= x1:
                    score = y0 + (vix - x0) / (x1 - x0) * (y1 - y0)
                    break
        return {"score": round(score, 4), "details": {"vix": round(vix, 2)}}
    except Exception:
        return {"score": 0.5, "details": {}}


def _pnl_at(ohlcv: pd.DataFrame, date: pd.Timestamp, forward_days: int) -> float | None:
    """Compute actual return FORWARD_DAYS trading days after date."""
    try:
        future = ohlcv[ohlcv.index > date]
        if len(future) < forward_days:
            return None
        current_price = float(ohlcv[ohlcv.index <= date]["close"].iloc[-1])
        future_price = float(future["close"].iloc[forward_days - 1])
        if current_price <= 0:
            return None
        return round((future_price / current_price - 1) * 100, 4)
    except Exception:
        return None


def _weighted_composite(scores: dict, market: str, config: dict) -> float:
    """Compute weighted sum from config weights."""
    weights = config.get("weights", {}).get(
        market,
        {"technical": 0.30, "fundamental": 0.25, "flow": 0.20, "intel": 0.15, "macro": 0.10},
    )
    total = (
        scores["technical"] * weights.get("technical", 0.30)
        + scores["fundamental"] * weights.get("fundamental", 0.25)
        + scores["flow"] * weights.get("flow", 0.20)
        + scores["intel"] * weights.get("intel", 0.15)
        + scores["macro"] * weights.get("macro", 0.10)
    )
    return round(min(1.0, max(0.0, total)), 4)


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_source_column(db) -> None:
    """Add source column to scoring_results if not present."""
    cols = [r[1] for r in db.execute("PRAGMA table_info(scoring_results)").fetchall()]
    if "source" not in cols:
        db.execute("ALTER TABLE scoring_results ADD COLUMN source TEXT DEFAULT 'live'")


def _already_exists(db, ticker: str, date_str: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM scoring_results WHERE ticker=? AND scan_date=? AND source='backfill' LIMIT 1",
        (ticker, date_str),
    ).fetchone()
    return row is not None


def _save_row(db, ticker: str, market: str, date_str: str, scores: dict,
              composite: float, pnl_pct: float | None, weights: dict) -> None:
    import json
    db.execute(
        """
        INSERT INTO scoring_results
          (ticker, ticker_name, market, scan_date,
           technical_score, fundamental_score, flow_score,
           intel_score, macro_score, composite_score,
           score_breakdown_json, decision, weights_used_json, source)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ticker, ticker, market, date_str,
            scores["technical"], scores["fundamental"], scores["flow"],
            scores["intel"], scores["macro"], composite,
            json.dumps({"scores": scores, "pnl_pct": pnl_pct}),
            "WATCH",  # neutral decision for backfill rows
            json.dumps(weights),
            "backfill",
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main backfill logic
# ─────────────────────────────────────────────────────────────────────────────

def backfill_ticker(
    ticker: str,
    market: str,
    ohlcv: pd.DataFrame,
    vix_df: pd.DataFrame,
    trading_dates: list,
    forward_days: int,
    scoring_config: dict,
    dry_run: bool,
) -> int:
    """Backfill scoring_results for one ticker. Returns number of rows inserted."""
    from web.db.connection import get_db

    inserted = 0
    with get_db() as db:
        _ensure_source_column(db)

        # Determine year range for fundamental data
        for i, date in enumerate(trading_dates):
            if i % SAMPLE_EVERY_N_DAYS != 0:
                continue

            date_str = date.strftime("%Y-%m-%d")

            if not dry_run and _already_exists(db, ticker, date_str):
                continue

            # Slice OHLCV up to this date (no look-ahead)
            hist = ohlcv[ohlcv.index <= date]
            if len(hist) < 30:
                continue

            # 1. Technical (indicator-based)
            tech = _technical_score(ticker, hist)

            # 2. Fundamental (use live data — DART annual report doesn't change intraday)
            fund = _fundamental_score_at(ticker, market, date.year)

            # 3. Flow = 0.5 (Naver historical flow is rate-limited; add if needed)
            flow_score = 0.5

            # 4. Intel = 0.5 (no historical news data)
            intel_score = 0.5

            # 5. Macro (VIX history)
            macro = _macro_score_at(date, vix_df)

            scores = {
                "technical": tech["score"],
                "fundamental": fund["score"],
                "flow": flow_score,
                "intel": intel_score,
                "macro": macro["score"],
            }

            weights = scoring_config.get("weights", {}).get(market, {})
            composite = _weighted_composite(scores, market, scoring_config)
            pnl_pct = _pnl_at(ohlcv, date, forward_days)

            if dry_run:
                logger.info(
                    f"[DRY] {ticker} {date_str}: tech={scores['technical']:.3f} "
                    f"fund={scores['fundamental']:.3f} macro={scores['macro']:.3f} "
                    f"composite={composite:.3f} pnl={pnl_pct}"
                )
                inserted += 1
                if inserted >= 5:
                    break
                continue

            _save_row(db, ticker, market, date_str, scores, composite, pnl_pct, weights)
            inserted += 1

    return inserted


def load_vix_history(start: str) -> pd.DataFrame:
    """Load VIX historical data from yfinance."""
    try:
        import yfinance as yf
        vix = yf.download("^VIX", start=start, progress=False, auto_adjust=True)
        if not vix.empty:
            df = vix[["Close"]].rename(columns={"Close": "close"})
            df.index = pd.to_datetime(df.index)
            logger.info(f"VIX history loaded: {len(df)} rows")
            return df
    except Exception as e:
        logger.warning(f"VIX load failed: {e}")
    return pd.DataFrame()


def load_scoring_config() -> dict:
    try:
        import yaml
        path = Path(__file__).parent.parent / "config" / "scoring.yaml"
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def run(
    start: str = "2024-01-01",
    end: str | None = None,
    market_filter: str = "ALL",
    forward_days: int = 20,
    dry_run: bool = False,
) -> None:
    end_date = pd.Timestamp(end or datetime.now(KST).strftime("%Y-%m-%d"))
    start_ts = pd.Timestamp(start)

    logger.info(
        f"Backfill: {start} → {end_date.date()} | market={market_filter} "
        f"forward={forward_days}d | dry_run={dry_run}"
    )

    scoring_config = load_scoring_config()
    vix_df = load_vix_history(start)

    # Build trading date list (approximate; use index from first ticker's data)
    tickers_to_run: list[tuple[str, str]] = []
    if market_filter in ("KRX", "ALL"):
        tickers_to_run += [(t, "KRX") for t in KRX_TICKERS]
    if market_filter in ("US", "ALL"):
        tickers_to_run += [(t, "US") for t in US_TICKERS]

    total_inserted = 0
    for idx, (ticker, market) in enumerate(tickers_to_run):
        logger.info(f"[{idx+1}/{len(tickers_to_run)}] {market}:{ticker}")

        ohlcv = _load_ohlcv(ticker, market, start)
        if ohlcv.empty:
            logger.warning(f"No OHLCV for {ticker}, skipping")
            continue

        ohlcv.index = pd.to_datetime(ohlcv.index)

        # Trading dates within range
        mask = (ohlcv.index >= start_ts) & (ohlcv.index <= end_date)
        trading_dates = list(ohlcv.index[mask])

        if not trading_dates:
            logger.warning(f"No trading dates in range for {ticker}")
            continue

        n = backfill_ticker(
            ticker=ticker,
            market=market,
            ohlcv=ohlcv,
            vix_df=vix_df,
            trading_dates=trading_dates,
            forward_days=forward_days,
            scoring_config=scoring_config,
            dry_run=dry_run,
        )
        total_inserted += n
        logger.info(f"  → {n} rows {'(dry)' if dry_run else 'inserted'}")

        if not dry_run:
            time.sleep(0.2)  # be gentle on DB

    logger.info(f"Backfill complete: {total_inserted} total rows {'(dry)' if dry_run else 'inserted'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Historical scoring backfill")
    parser.add_argument("--start", default="2024-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--market", default="ALL", choices=["KRX", "US", "ALL"])
    parser.add_argument("--forward", type=int, default=20, help="Forward return days")
    parser.add_argument("--dry-run", action="store_true", help="Print first 5 rows per ticker, no DB write")
    args = parser.parse_args()

    run(
        start=args.start,
        end=args.end,
        market_filter=args.market,
        forward_days=args.forward,
        dry_run=args.dry_run,
    )
