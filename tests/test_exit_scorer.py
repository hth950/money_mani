"""Tests for ExitScorer._check_overrides()."""

import numpy as np
import pandas as pd
import pytest

from scoring.exit_scorer import ExitScorer


def _make_df(n: int = 30, base_price: float = 100.0) -> pd.DataFrame:
    """Create a minimal OHLCV DataFrame with n rows."""
    prices = [base_price] * n
    return pd.DataFrame(
        {
            "Open": prices,
            "High": [p * 1.01 for p in prices],
            "Low": [p * 0.99 for p in prices],
            "Close": prices,
            "Volume": [1_000_000] * n,
        },
        index=pd.date_range("2024-01-01", periods=n, freq="D"),
    )


class TestCheckOverrides:
    """Tests for ExitScorer._check_overrides()."""

    def setup_method(self):
        # Instantiate with explicit config so tests are independent of scoring.yaml
        self.scorer = ExitScorer(
            config={
                "enabled": True,
                "stop_loss_pct": -0.05,
                "take_profit_pct": 0.15,
                "min_holding_days": 0,
                "atr_multiplier": 2.0,
                "weights": {"trend": 0.35, "momentum": 0.30, "trailing_stop": 0.35},
                "thresholds": {"sell_execute": 0.25, "sell_watch": 0.40},
            }
        )

    # ------------------------------------------------------------------
    # Stop-loss
    # ------------------------------------------------------------------

    def test_stop_loss_triggers_sell_execute(self):
        """Price 6% below entry should trigger SELL_EXECUTE via stop-loss."""
        entry_price = 1000.0
        current_price = 940.0  # -6%
        pnl_pct = (current_price - entry_price) / entry_price  # -0.06

        df = _make_df(n=30, base_price=current_price)
        result = self.scorer._check_overrides(
            current_price, entry_price, pnl_pct, df, "2024-01-01"
        )

        assert result is not None, "Expected an override result, got None"
        assert result["decision"] == "SELL_EXECUTE"
        assert "STOP_LOSS" in result["reason"]
        assert result["exit_score"] == 0.0
        assert result["details"]["override"] == "STOP_LOSS"

    def test_stop_loss_exact_boundary_triggers(self):
        """Price exactly at stop-loss boundary (-5%) should trigger."""
        entry_price = 1000.0
        current_price = 950.0  # -5.0%
        pnl_pct = (current_price - entry_price) / entry_price

        df = _make_df(n=30, base_price=current_price)
        result = self.scorer._check_overrides(
            current_price, entry_price, pnl_pct, df, "2024-01-01"
        )

        assert result is not None
        assert result["decision"] == "SELL_EXECUTE"

    def test_just_above_stop_loss_no_override(self):
        """Price just above stop-loss (-4%) should NOT trigger stop-loss override."""
        entry_price = 1000.0
        current_price = 960.0  # -4%
        pnl_pct = (current_price - entry_price) / entry_price

        df = _make_df(n=30, base_price=current_price)
        # May still return None or a take-profit result, but must not be STOP_LOSS
        result = self.scorer._check_overrides(
            current_price, entry_price, pnl_pct, df, "2024-01-01"
        )

        if result is not None:
            assert result["details"].get("override") != "STOP_LOSS"

    # ------------------------------------------------------------------
    # Take-profit
    # ------------------------------------------------------------------

    def test_take_profit_triggers_when_trend_declining(self):
        """Price 15%+ above entry with declining trend should trigger SELL_EXECUTE."""
        entry_price = 1000.0
        current_price = 1160.0  # +16%
        pnl_pct = (current_price - entry_price) / entry_price

        # Build a DataFrame where EMA5 < EMA20 (declining trend):
        # Start high, end low so short EMA is below long EMA
        n = 30
        start = 1300.0
        end = 1100.0
        prices = list(np.linspace(start, end, n))
        # Last price is current_price
        prices[-1] = current_price

        df = pd.DataFrame(
            {
                "Open": prices,
                "High": [p * 1.005 for p in prices],
                "Low": [p * 0.995 for p in prices],
                "Close": prices,
                "Volume": [1_000_000] * n,
            },
            index=pd.date_range("2024-01-01", periods=n, freq="D"),
        )

        result = self.scorer._check_overrides(
            current_price, entry_price, pnl_pct, df, "2024-01-01"
        )

        assert result is not None, "Expected take-profit override, got None"
        assert result["decision"] == "SELL_EXECUTE"
        assert "TAKE_PROFIT" in result["reason"]
        assert result["details"]["override"] == "TAKE_PROFIT"

    def test_take_profit_no_trigger_when_trend_rising(self):
        """Price 15%+ above entry but rising trend should NOT trigger take-profit."""
        entry_price = 1000.0
        current_price = 1160.0  # +16%
        pnl_pct = (current_price - entry_price) / entry_price

        # Rising trend: prices go from low to high so EMA5 > EMA20
        n = 30
        prices = list(np.linspace(900.0, current_price, n))

        df = pd.DataFrame(
            {
                "Open": prices,
                "High": [p * 1.005 for p in prices],
                "Low": [p * 0.995 for p in prices],
                "Close": prices,
                "Volume": [1_000_000] * n,
            },
            index=pd.date_range("2024-01-01", periods=n, freq="D"),
        )

        result = self.scorer._check_overrides(
            current_price, entry_price, pnl_pct, df, "2024-01-01"
        )

        # With rising trend, no override should fire
        assert result is None, f"Expected None (no override), got: {result}"

    # ------------------------------------------------------------------
    # Hold case
    # ------------------------------------------------------------------

    def test_hold_case_returns_none(self):
        """Normal price within range should return None (no override)."""
        entry_price = 1000.0
        current_price = 1050.0  # +5%, well within both bounds
        pnl_pct = (current_price - entry_price) / entry_price

        df = _make_df(n=30, base_price=current_price)
        result = self.scorer._check_overrides(
            current_price, entry_price, pnl_pct, df, "2024-01-01"
        )

        assert result is None

    def test_flat_price_no_override(self):
        """Price unchanged from entry should not trigger any override."""
        entry_price = 500.0
        current_price = 500.0
        pnl_pct = 0.0

        df = _make_df(n=30, base_price=current_price)
        result = self.scorer._check_overrides(
            current_price, entry_price, pnl_pct, df, "2024-01-01"
        )

        assert result is None

    # ------------------------------------------------------------------
    # Return shape validation
    # ------------------------------------------------------------------

    def test_stop_loss_result_has_required_keys(self):
        """Stop-loss result must contain all required keys."""
        entry_price = 1000.0
        current_price = 920.0  # -8%
        pnl_pct = (current_price - entry_price) / entry_price

        df = _make_df(n=30, base_price=current_price)
        result = self.scorer._check_overrides(
            current_price, entry_price, pnl_pct, df, "2024-01-01"
        )

        assert result is not None
        for key in ("exit_score", "decision", "reason", "scores", "details"):
            assert key in result, f"Missing key: {key}"
        for sub in ("trend", "momentum", "trailing_stop"):
            assert sub in result["scores"], f"Missing scores key: {sub}"
