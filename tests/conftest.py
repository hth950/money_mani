"""Shared fixtures for Money Mani unit tests."""
import pandas as pd
import numpy as np
import pytest


@pytest.fixture
def ohlcv_df():
    """Synthetic 60-bar OHLCV DataFrame with pre-computed indicator columns."""
    np.random.seed(42)
    n = 60
    close = 100 + np.cumsum(np.random.randn(n) * 0.5)
    df = pd.DataFrame({
        "open": close * 0.999,
        "high": close * 1.005,
        "low": close * 0.995,
        "close": close,
        "volume": np.random.randint(100000, 500000, n).astype(float),
    }, index=pd.date_range("2025-01-01", periods=n, freq="B"))
    # Add common indicator columns used by strategies
    df["sma_20"] = df["close"].rolling(20).mean()
    df["sma_60"] = df["close"].rolling(60).mean()
    df["rsi"] = 50.0  # neutral RSI
    return df


@pytest.fixture
def rsi_crossover_df():
    """DataFrame where RSI crosses above 30 at bar index 25."""
    n = 50
    df = pd.DataFrame({
        "close": np.linspace(90, 110, n),
        "rsi": [25.0] * 25 + [35.0] * 25,
    }, index=pd.date_range("2025-01-01", periods=n, freq="B"))
    return df
