"""BacktestEngine: wraps backtesting.py Backtest with SignalGenerator."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from backtesting import Backtest
from backtesting import Strategy as BtStrategy

from strategy.models import Strategy
from .metrics import BacktestResult
from .signals import SignalGenerator

logger = logging.getLogger("money_mani.backtester.engine")


def _make_bt_strategy(signals: pd.Series, position_size: float) -> type:
    """Dynamically build a backtesting.py Strategy subclass from precomputed signals."""

    class _SignalStrategy(BtStrategy):
        _signals = None  # filled after class creation
        _position_size = 1.0

        def init(self):
            # Store signals array aligned to backtesting internal index
            self._sig = self._signals

        def next(self):
            idx = len(self.data) - 1
            if idx >= len(self._sig):
                return
            sig = self._sig.iloc[idx]
            if sig == 1 and not self.position:
                size = self._position_size
                # backtesting.py: size is a fraction only when 0 < size < 1.
                # size >= 1.0 is treated as number of shares, not equity fraction.
                # Clamp to 0.9999 to represent "full equity".
                if size <= 0 or size >= 1.0:
                    size = 0.9999
                self.buy(size=size)
            elif sig == -1 and self.position:
                self.position.close()

    _SignalStrategy._signals = signals
    _SignalStrategy._position_size = position_size
    return _SignalStrategy


class BacktestEngine:
    def __init__(
        self,
        initial_capital: float = 10_000_000,
        commission: float = 0.00015,
        slippage: float = 0.0,
    ):
        self.initial_capital = initial_capital
        self.commission = commission
        self.slippage = slippage

    def run(
        self,
        df: pd.DataFrame,
        strategy: Strategy,
        ticker: str = "UNKNOWN",
        market: str = "KRX",
    ) -> BacktestResult:
        """Run backtest and return BacktestResult."""
        # Compute indicators + signals
        sig_gen = SignalGenerator(strategy)
        df_ind = sig_gen.compute_indicators(df)
        signals = sig_gen.generate_signals(df_ind)

        position_size = float(strategy.parameters.get("position_size", 1.0))

        # Market-aware commission
        try:
            from utils.config_loader import load_config
            _cfg = load_config()
            commission = _cfg["backtest"].get(f"commission_{market.lower()}", self.commission)
        except Exception:
            commission = self.commission

        # backtesting.py requires OHLCV with capital letters
        bt_df = df[["Open", "High", "Low", "Close", "Volume"]].copy()

        # Build dynamic strategy class
        bt_strategy_cls = _make_bt_strategy(signals, position_size)

        bt = Backtest(
            bt_df,
            bt_strategy_cls,
            cash=self.initial_capital,
            commission=commission,
            spread=self.slippage,
            trade_on_close=False,
            exclusive_orders=True,
        )

        stats = bt.run()

        # Determine period string from data index
        start = str(df.index[0].date()) if hasattr(df.index[0], "date") else str(df.index[0])
        end = str(df.index[-1].date()) if hasattr(df.index[-1], "date") else str(df.index[-1])
        period = f"{start}~{end}"

        result = BacktestResult.from_stats(
            stats=stats,
            strategy_name=strategy.name,
            ticker=ticker,
            period=period,
        )
        return result
