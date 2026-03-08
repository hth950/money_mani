"""Local CSV cache for market data to avoid repeated API calls."""

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger("money_mani.market_data.cache")

CACHE_DIR = Path(__file__).parent.parent / "output" / "cache"


class DataCache:
    """Simple file-based cache for OHLCV DataFrames."""

    def __init__(self, cache_dir: Path = CACHE_DIR):
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _key(self, ticker: str, market: str, start: str, end: str) -> Path:
        safe_start = start.replace("-", "")
        safe_end = end.replace("-", "")
        return self._dir / f"{market}_{ticker}_{safe_start}_{safe_end}.csv"

    def get(self, ticker: str, market: str, start: str, end: str) -> pd.DataFrame | None:
        path = self._key(ticker, market, start, end)
        if path.exists():
            logger.debug(f"Cache hit: {path.name}")
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            return df
        return None

    def put(self, df: pd.DataFrame, ticker: str, market: str, start: str, end: str):
        if df.empty:
            return
        path = self._key(ticker, market, start, end)
        df.to_csv(path, encoding="utf-8")
        logger.debug(f"Cached: {path.name}")

    def clear(self):
        for f in self._dir.glob("*.csv"):
            f.unlink()
        logger.info("Cache cleared")
