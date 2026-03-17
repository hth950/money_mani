"""Tests for SignalGenerator._crossover() — verifies the b_raw fix."""
import pandas as pd
import numpy as np
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtester.signals import SignalGenerator
from strategy.models import Strategy


def _minimal_strategy() -> Strategy:
    return Strategy(
        name="test",
        description="",
        source="test",
        category="momentum",
        status="active",
        rules={},
        indicators=[],
        parameters={},
    )


@pytest.fixture
def sg():
    return SignalGenerator(_minimal_strategy())


def test_crossover_numeric_threshold_above(sg, rsi_crossover_df):
    """indicator_b is a numeric string '30' — tests the b_raw fix."""
    result = sg._crossover(rsi_crossover_df, {
        "indicator_a": "rsi",
        "indicator_b": "30",
        "direction": "above",
    })
    assert isinstance(result, pd.Series)
    # RSI crosses from 25 to 35 at index 25 — should detect exactly one crossover
    assert result.sum() == 1, f"Expected 1 crossover, got {result.sum()}"
    assert result.iloc[25], "Crossover should be at index 25"


def test_crossover_numeric_threshold_below(sg):
    """RSI crosses below 70."""
    n = 50
    df = pd.DataFrame({
        "close": np.ones(n) * 100,
        "rsi": [75.0] * 25 + [65.0] * 25,
    }, index=pd.date_range("2025-01-01", periods=n, freq="B"))
    result = sg._crossover(df, {
        "indicator_a": "rsi",
        "indicator_b": "70",
        "direction": "below",
    })
    assert result.sum() == 1
    assert result.iloc[25]


def test_crossover_column_reference(sg):
    """indicator_b references another column (not a number)."""
    n = 50
    df = pd.DataFrame({
        "close": np.linspace(90, 110, n),
        "sma_20": [100.0] * 25 + [95.0] * 25,
    }, index=pd.date_range("2025-01-01", periods=n, freq="B"))
    result = sg._crossover(df, {
        "indicator_a": "close",
        "indicator_b": "sma_20",
        "direction": "above",
    })
    assert isinstance(result, pd.Series)
    assert result.sum() >= 1


def test_crossover_missing_indicator_a_returns_false(sg, rsi_crossover_df):
    """Missing indicator_a returns all-False series."""
    result = sg._crossover(rsi_crossover_df, {
        "indicator_a": "nonexistent_col",
        "indicator_b": "30",
        "direction": "above",
    })
    assert not result.any()


def test_crossover_missing_both_returns_false(sg, rsi_crossover_df):
    """Missing indicator_b column that is also non-numeric returns all-False."""
    result = sg._crossover(rsi_crossover_df, {
        "indicator_a": "rsi",
        "indicator_b": "nonexistent_col_xyz",
        "direction": "above",
    })
    assert not result.any()
