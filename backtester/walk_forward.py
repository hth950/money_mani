"""Walk-forward validation to detect overfitting strategies."""

import logging
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd
import numpy as np

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


class WalkForwardValidator:
    """Sliding window walk-forward validation."""

    def __init__(
        self,
        train_days: int = 252,
        test_days: int = 63,
        step_days: int = 63,
        overfit_threshold: float = 0.5,
        min_windows: int = 3,
    ):
        self.train_days = train_days
        self.test_days = test_days
        self.step_days = step_days
        self.overfit_threshold = overfit_threshold
        self.min_windows = min_windows

    def validate(self, strategy, df: pd.DataFrame, ticker: str = "") -> WalkForwardResult:
        """Run walk-forward validation on a strategy with given data.

        Args:
            strategy: Strategy object
            df: Full OHLCV DataFrame (should be 3+ years)
            ticker: Ticker symbol for logging

        Returns:
            WalkForwardResult with overfit assessment
        """
        result = WalkForwardResult(
            strategy_name=strategy.name,
            ticker=ticker,
        )

        if len(df) < self.train_days + self.test_days:
            result.overfit_reason = f"insufficient data: {len(df)} < {self.train_days + self.test_days}"
            return result

        # Generate sliding windows
        windows = self._generate_windows(df)
        result.total_windows = len(windows)

        if len(windows) < self.min_windows:
            result.overfit_reason = f"too few windows: {len(windows)} < {self.min_windows}"
            return result

        # Run backtest on each window
        from backtester.signals import SignalGenerator
        sig_gen = SignalGenerator(strategy)

        for idx, (train_df, test_df) in enumerate(windows):
            wr = WindowResult(
                window_idx=idx,
                train_start=str(train_df.index[0].date()),
                train_end=str(train_df.index[-1].date()),
                test_start=str(test_df.index[0].date()),
                test_end=str(test_df.index[-1].date()),
            )

            try:
                # Train period backtest
                train_metrics = self._run_backtest(sig_gen, train_df)
                wr.train_return = train_metrics.get("total_return", 0)
                wr.train_sharpe = train_metrics.get("sharpe_ratio", 0)
                wr.train_trades = train_metrics.get("num_trades", 0)

                # Test period backtest
                test_metrics = self._run_backtest(sig_gen, test_df)
                wr.test_return = test_metrics.get("total_return", 0)
                wr.test_sharpe = test_metrics.get("sharpe_ratio", 0)
                wr.test_trades = test_metrics.get("num_trades", 0)

                result.windows.append(wr)
                result.valid_windows += 1

            except Exception as e:
                logger.warning(f"Window {idx} failed for {strategy.name}/{ticker}: {e}")
                continue

        # Aggregate results
        if result.valid_windows >= self.min_windows:
            train_sharpes = [w.train_sharpe for w in result.windows if w.train_sharpe != 0]
            test_sharpes = [w.test_sharpe for w in result.windows]

            result.avg_train_sharpe = np.mean(train_sharpes) if train_sharpes else 0
            result.avg_test_sharpe = np.mean(test_sharpes) if test_sharpes else 0

            # Overfit detection
            if result.avg_train_sharpe > 0:
                result.sharpe_degradation = result.avg_test_sharpe / result.avg_train_sharpe
                if result.sharpe_degradation < self.overfit_threshold:
                    result.is_overfit = True
                    result.overfit_reason = (
                        f"sharpe degradation {result.sharpe_degradation:.2f} "
                        f"< threshold {self.overfit_threshold}"
                    )

            # Also flag if test sharpe is consistently negative
            negative_test_windows = sum(1 for w in result.windows if w.test_sharpe < 0)
            if negative_test_windows > len(result.windows) * 0.7:
                result.is_overfit = True
                result.overfit_reason = (
                    f"test sharpe negative in {negative_test_windows}/{len(result.windows)} windows"
                )

        logger.info(
            f"WF {strategy.name}/{ticker}: "
            f"windows={result.valid_windows}/{result.total_windows} "
            f"train_sharpe={result.avg_train_sharpe:.2f} "
            f"test_sharpe={result.avg_test_sharpe:.2f} "
            f"overfit={result.is_overfit}"
        )

        return result

    def _generate_windows(self, df: pd.DataFrame) -> list[tuple[pd.DataFrame, pd.DataFrame]]:
        """Generate train/test window pairs from data."""
        windows = []
        total_len = len(df)
        window_size = self.train_days + self.test_days

        start = 0
        while start + window_size <= total_len:
            train_end = start + self.train_days
            test_end = train_end + self.test_days

            train_df = df.iloc[start:train_end].copy()
            test_df = df.iloc[train_end:test_end].copy()

            windows.append((train_df, test_df))
            start += self.step_days

        return windows

    def _run_backtest(self, sig_gen: "SignalGenerator", df: pd.DataFrame) -> dict:
        """Run a simple backtest on a DataFrame slice.

        Returns dict with total_return, sharpe_ratio, num_trades.
        """
        try:
            df_ind = sig_gen.compute_indicators(df.copy())
            signals = sig_gen.generate_signals(df_ind)

            if len(signals) == 0 or signals.sum() == 0:
                return {"total_return": 0, "sharpe_ratio": 0, "num_trades": 0}

            # Simple signal-based return calculation
            returns = df_ind["Close"].pct_change().fillna(0)
            # Signal: 1 = long, -1 = short, 0 = flat
            position = signals.shift(1).fillna(0)  # Enter next day
            strategy_returns = returns * position

            total_return = (1 + strategy_returns).prod() - 1
            num_trades = (position.diff().abs() > 0).sum()

            # Sharpe ratio (annualized)
            if strategy_returns.std() > 0:
                sharpe = (strategy_returns.mean() / strategy_returns.std()) * (252 ** 0.5)
            else:
                sharpe = 0.0

            return {
                "total_return": float(total_return),
                "sharpe_ratio": float(sharpe),
                "num_trades": int(num_trades),
            }
        except Exception as e:
            logger.warning(f"Backtest failed: {e}")
            return {"total_return": 0, "sharpe_ratio": 0, "num_trades": 0}
