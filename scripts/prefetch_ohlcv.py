"""Pre-fetch and cache OHLCV data for revalidation.

Usage:
    python scripts/prefetch_ohlcv.py [--krx N] [--us N] [--start YYYY-MM-DD]
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from utils.config_loader import load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("prefetch_ohlcv")

CACHE_DIR = Path("output/data_cache")

# S&P 500 top 50 tickers (by market cap, as of 2024)
US_TOP_50 = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "BRK-B", "LLY", "AVGO", "TSLA",
    "WMT", "JPM", "UNH", "V", "XOM", "MA", "ORCL", "COST", "HD", "PG",
    "JNJ", "ABBV", "BAC", "NFLX", "CRM", "AMD", "KO", "CVX", "MRK", "ADBE",
    "PEP", "TMO", "ACN", "LIN", "MCD", "CSCO", "QCOM", "GE", "ABT", "DHR",
    "VZ", "TXN", "AMGN", "NOW", "PM", "IBM", "CAT", "INTU", "SPGI", "GS",
]


def get_krx_top_n(n: int) -> list[str]:
    """Get top N KOSPI tickers by market cap."""
    try:
        import pykrx.stock as krx
        tickers = krx.get_market_ticker_list(market="KOSPI")
        logger.info(f"Got {len(tickers)} KOSPI tickers from pykrx")
        return tickers[:n]
    except Exception as e:
        logger.warning(f"pykrx failed ({e}), using hardcoded top 50 KOSPI")
        return [
            "005930", "000660", "035420", "005380", "051910", "006400", "035720",
            "028260", "066570", "017670", "207940", "000270", "105560", "055550",
            "011200", "010130", "032830", "086790", "003550", "015760", "010950",
            "009830", "018260", "011780", "002790", "003490", "096770", "000810",
            "033780", "051600", "047050", "030200", "009150", "012330", "011170",
            "034730", "011170", "000100", "005490", "001040", "001450", "010140",
            "006360", "004020", "161390", "247540", "042660", "034220", "097950",
            "020150",
        ][:n]


def fetch_krx_ohlcv(ticker: str, start: str) -> pd.DataFrame:
    """Fetch KRX OHLCV via pykrx, fallback to yfinance .KS"""
    cache_path = CACHE_DIR / f"KRX_{ticker}_{start}.parquet"
    if cache_path.exists():
        return pd.read_parquet(cache_path)

    df = pd.DataFrame()
    # Try pykrx first
    try:
        import pykrx.stock as krx
        time.sleep(1.0)
        df = krx.get_market_ohlcv(start, "20251231", ticker)
        if not df.empty:
            df.index = pd.to_datetime(df.index)
            df.columns = ["Open", "High", "Low", "Close", "Volume", "Change"] if len(df.columns) == 6 else df.columns
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    except Exception as e:
        logger.warning(f"pykrx failed for {ticker}: {e}")

    # Fallback to yfinance
    if df.empty:
        try:
            import yfinance as yf
            yf_ticker = f"{ticker}.KS"
            df = yf.download(yf_ticker, start=start, progress=False, auto_adjust=True)
            if not df.empty:
                df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        except Exception as e:
            logger.warning(f"yfinance fallback also failed for {ticker}: {e}")

    if not df.empty:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cache_path)
        logger.info(f"Cached KRX {ticker}: {len(df)} rows")
    else:
        logger.warning(f"No data for KRX {ticker}")
    return df


def fetch_us_ohlcv(ticker: str, start: str) -> pd.DataFrame:
    """Fetch US OHLCV via yfinance."""
    cache_path = CACHE_DIR / f"US_{ticker}_{start}.parquet"
    if cache_path.exists():
        return pd.read_parquet(cache_path)

    try:
        import yfinance as yf
        df = yf.download(ticker, start=start, progress=False, auto_adjust=True)
        if not df.empty:
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            df.to_parquet(cache_path)
            logger.info(f"Cached US {ticker}: {len(df)} rows")
        else:
            logger.warning(f"No data for US {ticker}")
        return df
    except Exception as e:
        logger.error(f"Failed to fetch US {ticker}: {e}")
        return pd.DataFrame()


def main():
    parser = argparse.ArgumentParser(description="Pre-fetch OHLCV data for revalidation")
    parser.add_argument("--krx", type=int, default=100, help="Number of KRX tickers")
    parser.add_argument("--us", type=int, default=50, help="Number of US tickers")
    parser.add_argument("--start", default=None, help="Start date (YYYY-MM-DD)")
    args = parser.parse_args()

    cfg = load_config()
    start = args.start or cfg.get("backtest", {}).get("default_period", "2013-01-01")

    logger.info(f"Fetching data from {start}")
    logger.info(f"KRX: top {args.krx}, US: top {args.us}")

    # KRX
    krx_tickers = get_krx_top_n(args.krx)
    logger.info(f"Starting KRX prefetch ({len(krx_tickers)} tickers)...")
    krx_ok = 0
    for i, ticker in enumerate(krx_tickers):
        df = fetch_krx_ohlcv(ticker, start)
        if not df.empty:
            krx_ok += 1
        if (i + 1) % 10 == 0:
            logger.info(f"KRX progress: {i+1}/{len(krx_tickers)} ({krx_ok} with data)")

    # US
    us_tickers = US_TOP_50[:args.us]
    logger.info(f"Starting US prefetch ({len(us_tickers)} tickers)...")
    us_ok = 0
    for i, ticker in enumerate(us_tickers):
        df = fetch_us_ohlcv(ticker, start)
        if not df.empty:
            us_ok += 1

    logger.info(f"Prefetch complete: KRX {krx_ok}/{len(krx_tickers)}, US {us_ok}/{len(us_tickers)}")
    logger.info(f"Cache directory: {CACHE_DIR.absolute()}")


if __name__ == "__main__":
    main()
