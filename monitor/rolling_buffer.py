"""Rolling window buffer for OHLCV data."""

import logging
from collections import deque
from datetime import datetime

import pandas as pd

logger = logging.getLogger("money_mani.monitor.rolling_buffer")


class RollingBuffer:
    """Fixed-size circular buffer of OHLCV bars for indicator computation."""

    def __init__(self, ticker: str, max_size: int = 200, warmup_bars: int = 60):
        self.ticker = ticker
        self.max_size = max_size
        self.warmup_bars = warmup_bars
        self._bars: deque[dict] = deque(maxlen=max_size)

    def seed(self, df: pd.DataFrame) -> None:
        """Load historical data into buffer.

        Args:
            df: DataFrame with Open, High, Low, Close, Volume columns and DatetimeIndex.
        """
        rows = df.tail(self.max_size)
        for idx, row in rows.iterrows():
            self._bars.append({
                "Open": float(row["Open"]),
                "High": float(row["High"]),
                "Low": float(row["Low"]),
                "Close": float(row["Close"]),
                "Volume": int(row["Volume"]),
                "timestamp": idx,
            })
        logger.debug(f"Seeded {self.ticker}: {len(self._bars)} bars")

    def append(self, bar: dict) -> None:
        """Add a new OHLCV bar. Oldest bar is evicted if buffer is full.

        Args:
            bar: Dict with Open, High, Low, Close, Volume keys.
        """
        if "timestamp" not in bar:
            bar["timestamp"] = pd.Timestamp.now()
        self._bars.append(bar)

    def is_warm(self) -> bool:
        """Return True if enough bars for indicator computation."""
        return len(self._bars) >= self.warmup_bars

    def to_dataframe(self) -> pd.DataFrame:
        """Convert buffer to DataFrame matching KRXFetcher/USFetcher output format."""
        if not self._bars:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        df = pd.DataFrame(list(self._bars))
        if "timestamp" in df.columns:
            df.index = pd.DatetimeIndex(df["timestamp"])
            df.index.name = "Date"
            df = df.drop(columns=["timestamp"])
        return df[["Open", "High", "Low", "Close", "Volume"]]

    def __len__(self) -> int:
        return len(self._bars)
