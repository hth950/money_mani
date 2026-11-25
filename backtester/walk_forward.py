"""Walk-forward validation using the executable backtest model."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from statistics import mean

import pandas as pd

from backtester.engine import WARMUP_BARS, BacktestEngine, evaluation_start_bar

logger = logging.getLogger("money_mani.backtester.walk_forward")


@dataclass
class WindowResult:
    """Result for a single train/test window."""

    window_idx: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    train_return: float = 0.0
    train_sharpe: float = 0.0
    train_trades: int = 0
    test_return: float = 0.0
    test_sharpe: float = 0.0
    test_trades: int = 0
    valid: bool = False
    invalid_reason: str = ""


@dataclass
class WalkForwardResult:
    """Aggregated walk-forward validation result."""

    strategy_name: str
    ticker: str
    windows: list[WindowResult] = field(default_factory=list)
    avg_train_sharpe: float = 0.0
    avg_test_sharpe: float = 0.0
    sharpe_degradation: float = 0.0  # avg_test / avg_train
    is_overfit: bool = False
    overfit_reason: str = ""
    total_windows: int = 0
    valid_windows: int = 0
    execution_policy: dict = field(default_factory=dict)


class WalkForwardValidator:
    """Evaluate successive chronological train/test pairs from flat."""

    def __init__(
        self,
        train_days: int = 252,
        test_days: int = 63,
        step_days: int = 63,
        overfit_threshold: float = 0.5,
        min_windows: int = 3,
        warmup_bars: int = WARMUP_BARS,
    ):
        if train_days < 2 or test_days < 2 or step_days < 1 or min_windows < 1 or warmup_bars < 0:
            raise ValueError("Invalid walk-forward window configuration")
        self.train_days = train_days
        self.test_days = test_days
        self.step_days = step_days
        self.overfit_threshold = overfit_threshold
        self.min_windows = min_windows
        self.warmup_bars = warmup_bars

    def validate(
        self,
        strategy,
        df: pd.DataFrame,
        ticker: str = "",
        market: str = "KRX",
        *,
        evaluation_start: str | None = None,
    ) -> WalkForwardResult:
        """Measure each leg on its own dates with prior bars for indicators."""
        engine = BacktestEngine.for_market(market)
        result = WalkForwardResult(
            strategy_name=strategy.name,
            ticker=ticker,
            execution_policy=engine.execution_policy(market, self.warmup_bars),
        )
        if df.empty:
            result.overfit_reason = "no market data"
            return result
        if not df.index.is_monotonic_increasing or not df.index.is_unique:
            result.overfit_reason = "market data must have a unique chronological index"
            return result

        first_bar = evaluation_start_bar(df.index, evaluation_start, self.warmup_bars)
        window_size = self.train_days + self.test_days
        for start in range(first_bar, len(df) - window_size + 1, self.step_days):
            test_start = start + self.train_days
            end = test_start + self.test_days
            wr = WindowResult(
                window_idx=len(result.windows),
                train_start=df.index[start].date().isoformat(),
                train_end=df.index[test_start - 1].date().isoformat(),
                test_start=df.index[test_start].date().isoformat(),
                test_end=df.index[end - 1].date().isoformat(),
            )
            result.windows.append(wr)
            try:
                train = engine.run(
                    df.iloc[start - self.warmup_bars:test_start],
                    strategy, ticker, market, evaluation_start=self.warmup_bars,
                )
                wr.train_return = train.total_return
                wr.train_sharpe = train.sharpe_ratio
                wr.train_trades = train.num_trades
                test = engine.run(
                    df.iloc[test_start - self.warmup_bars:end],
                    strategy, ticker, market, evaluation_start=self.warmup_bars,
                )
                wr.test_return = test.total_return
                wr.test_sharpe = test.sharpe_ratio
                wr.test_trades = test.num_trades
                if not wr.train_trades or not wr.test_trades:
                    wr.invalid_reason = "no closed trades in one or both legs"
                elif not all(math.isfinite(value) for value in (
                    wr.train_return, wr.train_sharpe, wr.test_return, wr.test_sharpe,
                )):
                    wr.invalid_reason = "non-finite metrics in one or both legs"
                else:
                    wr.valid = True
            except Exception as exc:
                wr.invalid_reason = f"backtest failed: {exc}"
                logger.warning("Window %s failed for %s/%s: %s", wr.window_idx, strategy.name, ticker, exc)

        valid_windows = [window for window in result.windows if window.valid]
        result.total_windows = len(result.windows)
        result.valid_windows = len(valid_windows)
        if not result.total_windows:
            result.overfit_reason = "insufficient bars for warmup and a complete train/test window"
        elif result.valid_windows < self.min_windows:
            result.overfit_reason = f"too few valid windows: {result.valid_windows} < {self.min_windows}"
        else:
            result.avg_train_sharpe = mean(window.train_sharpe for window in valid_windows)
            result.avg_test_sharpe = mean(window.test_sharpe for window in valid_windows)
            if result.avg_train_sharpe > 0:
                result.sharpe_degradation = result.avg_test_sharpe / result.avg_train_sharpe
                if result.sharpe_degradation < self.overfit_threshold:
                    result.is_overfit = True
                    result.overfit_reason = (
                        f"sharpe degradation {result.sharpe_degradation:.2f} "
                        f"< threshold {self.overfit_threshold}"
                    )

            negative_test_windows = sum(window.test_sharpe < 0 for window in valid_windows)
            if negative_test_windows > result.valid_windows * 0.7:
                result.is_overfit = True
                result.overfit_reason = (
                    f"test sharpe negative in {negative_test_windows}/{result.valid_windows} windows"
                )

        logger.info(
            "WF %s/%s: windows=%s/%s train_sharpe=%.2f test_sharpe=%.2f overfit=%s",
            strategy.name, ticker, result.valid_windows, result.total_windows,
            result.avg_train_sharpe, result.avg_test_sharpe, result.is_overfit,
        )
        return result
