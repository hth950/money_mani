"""BacktestEngine: wraps backtesting.py Backtest with SignalGenerator."""

from __future__ import annotations

import logging

import pandas as pd
from backtesting import Backtest
from backtesting import Strategy as BtStrategy

from strategy.models import Strategy
from utils.config_loader import load_config
from .metrics import BacktestResult
from .signals import SignalGenerator

logger = logging.getLogger("money_mani.backtester.engine")
EXECUTION_MODEL = "long_only_next_open_v1"
WARMUP_BARS = 252


def history_start(evaluation_start: str, warmup_bars: int = WARMUP_BARS) -> str:
    """Request extra calendar days, then count actual sessions in the result."""
    return (pd.Timestamp(evaluation_start) - pd.Timedelta(days=2 * warmup_bars)).date().isoformat()


def evaluation_start_bar(index: pd.DatetimeIndex, start: str | None, warmup_bars: int) -> int:
    first_requested = index.tz_localize(None).searchsorted(pd.Timestamp(start)) if start else 0
    return max(int(first_requested), warmup_bars)


def _make_bt_strategy(signals: pd.Series, position_size: float) -> type:
    """Dynamically build a backtesting.py Strategy subclass from precomputed signals."""

    class _SignalStrategy(BtStrategy):
        _signals = None  # filled after class creation
        _position_size = 1.0

        def init(self):
            # backtesting.py starts next() on bar 1. A market order from the
            # first close can be queued here for that bar's open.
            self._handle_signal(0)

        def next(self):
            idx = len(self.data) - 1
            if idx < len(self._signals) - 1:
                self._handle_signal(idx)

        def _handle_signal(self, idx):
            sig = self._signals.iloc[idx]
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
        self._market: str | None = None

    @classmethod
    def for_market(cls, market: str, config: dict | None = None) -> BacktestEngine:
        """Use the same market-specific defaults in backtest and walk-forward."""
        if market not in {"KRX", "US"}:
            raise ValueError(f"Unsupported backtest market: {market}")
        if config is None:
            config = load_config().get("backtest", {})
        market_key = market.lower()
        default_capital = config.get("initial_capital", 10_000_000) if market == "KRX" else 100_000
        engine = cls(
            initial_capital=config.get(f"initial_capital_{market_key}", default_capital),
            commission=config.get(f"commission_{market_key}", 0.00105 if market == "KRX" else 0.0),
            slippage=config.get("slippage", 0.0),
        )
        engine._market = market
        return engine

    def execution_policy(self, market: str, warmup_bars: int) -> dict:
        return {
            "model": EXECUTION_MODEL,
            "market": market,
            "initial_capital": self.initial_capital,
            "commission": self._commission_for(market),
            "slippage": self.slippage,
            "warmup_bars": warmup_bars,
        }

    def run(
        self,
        df: pd.DataFrame,
        strategy: Strategy,
        ticker: str = "UNKNOWN",
        market: str = "KRX",
        *,
        evaluation_start: int = 0,
    ) -> BacktestResult:
        """Run on evaluated rows and use earlier rows for indicators only."""
        if not df.index.is_monotonic_increasing or not df.index.is_unique:
            raise ValueError("Backtest data must have a unique chronological index")
        if not 0 <= evaluation_start <= len(df) - 2:
            raise ValueError("Backtest requires at least two evaluated bars")
        sig_gen = SignalGenerator(strategy)
        df_ind = sig_gen.compute_indicators(df)
        signals = sig_gen.generate_signals(df_ind).iloc[evaluation_start:]

        position_size = float(strategy.parameters.get("position_size", 1.0))

        # backtesting.py requires OHLCV with capital letters
        bt_df = df.iloc[evaluation_start:][["Open", "High", "Low", "Close", "Volume"]].copy()

        # Build dynamic strategy class
        bt_strategy_cls = _make_bt_strategy(signals, position_size)

        bt = Backtest(
            bt_df,
            bt_strategy_cls,
            cash=self.initial_capital,
            commission=self._commission_for(market),
            spread=self.slippage,
            trade_on_close=False,
            exclusive_orders=True,
        )

        stats = bt.run()

        # Determine period string from data index
        start = str(bt_df.index[0].date()) if hasattr(bt_df.index[0], "date") else str(bt_df.index[0])
        end = str(bt_df.index[-1].date()) if hasattr(bt_df.index[-1], "date") else str(bt_df.index[-1])
        period = f"{start}~{end}"

        result = BacktestResult.from_stats(
            stats=stats,
            strategy_name=strategy.name,
            ticker=ticker,
            period=period,
        )
        return result

    def _commission_for(self, market: str) -> float:
        # Direct callers historically resolve configured market fees even when
        # the constructor receives a fallback. for_market() already resolves a
        # complete execution policy for the requested venue.
        if self._market is not None:
            if market != self._market:
                raise ValueError(f"Engine fee policy is for {self._market}, received {market}")
            return self.commission
        try:
            return load_config().get("backtest", {}).get(f"commission_{market.lower()}", self.commission)
        except (OSError, ValueError):
            return self.commission
