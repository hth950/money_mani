"""Tests for TechnicalScorer with known synthetic data."""
import pandas as pd
import numpy as np
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_bullish_df():
    """DataFrame where technical indicators suggest bullish conditions."""
    n = 60
    # Steadily rising price above all MAs
    close = 100 + np.arange(n) * 0.5
    df = pd.DataFrame({
        "open": close * 0.999,
        "high": close * 1.005,
        "low": close * 0.990,
        "close": close,
        "volume": np.full(n, 200000.0),
    }, index=pd.date_range("2025-01-01", periods=n, freq="B"))
    return df


def make_bearish_df():
    """DataFrame where technical indicators suggest bearish conditions."""
    n = 60
    # Falling price below all MAs
    close = 130 - np.arange(n) * 0.5
    df = pd.DataFrame({
        "open": close * 1.001,
        "high": close * 1.002,
        "low": close * 0.995,
        "close": close,
        "volume": np.full(n, 100000.0),
    }, index=pd.date_range("2025-01-01", periods=n, freq="B"))
    return df


def test_technical_scorer_returns_score_in_range():
    """TechnicalScorer.score() returns a score between 0 and 1."""
    try:
        from scoring.technical_scorer import TechnicalScorer
    except ImportError:
        pytest.skip("TechnicalScorer not available")

    scorer = TechnicalScorer()
    df = make_bullish_df()
    result = scorer.score("TEST", df)
    assert "score" in result
    assert 0.0 <= result["score"] <= 1.0


def test_technical_scorer_bearish_score_in_range():
    """TechnicalScorer.score() returns a valid score for bearish data too."""
    try:
        from scoring.technical_scorer import TechnicalScorer
    except ImportError:
        pytest.skip("TechnicalScorer not available")

    scorer = TechnicalScorer()
    result = scorer.score("BEAR", make_bearish_df())
    assert "score" in result
    assert 0.0 <= result["score"] <= 1.0
