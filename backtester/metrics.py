"""BacktestResult dataclass extracted from backtesting.py Stats."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Thresholds for is_valid
_MIN_TRADES = 5
_MIN_RETURN = 0.0        # total return > 0%
_MIN_SHARPE = 0.5
_MAX_DRAWDOWN = -0.30    # max drawdown must be better than -30%


@dataclass
class BacktestResult:
    strategy_name: str
    ticker: str
    period: str
    total_return: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    num_trades: int
    trades: list[dict]
    is_valid: bool = field(init=False)

    def __post_init__(self):
        self.is_valid = (
            self.num_trades >= _MIN_TRADES
            and self.total_return > _MIN_RETURN
            and self.sharpe_ratio >= _MIN_SHARPE
            and self.max_drawdown >= _MAX_DRAWDOWN
        )

    @classmethod
    def from_stats(
        cls,
        stats: Any,
        strategy_name: str,
        ticker: str,
        period: str,
    ) -> "BacktestResult":
        """Build BacktestResult from backtesting.py _Stats object."""
        total_return = float(stats["Return [%]"]) / 100.0
        sharpe = float(stats["Sharpe Ratio"]) if stats["Sharpe Ratio"] == stats["Sharpe Ratio"] else 0.0
        max_dd = float(stats["Max. Drawdown [%]"]) / 100.0
        win_rate_raw = stats["Win Rate [%]"]
        win_rate = float(win_rate_raw) / 100.0 if win_rate_raw == win_rate_raw else 0.0
        num_trades = int(stats["# Trades"])

        trades_df = stats._trades
        trades = []
        if trades_df is not None and not trades_df.empty:
            for _, row in trades_df.iterrows():
                trades.append({
                    "entry_time": str(row["EntryTime"]),
                    "exit_time": str(row["ExitTime"]),
                    "entry_price": float(row["EntryPrice"]),
                    "exit_price": float(row["ExitPrice"]),
                    "size": float(row["Size"]),
                    "pnl": float(row["PnL"]),
                    "return_pct": float(row["ReturnPct"]),
                })

        return cls(
            strategy_name=strategy_name,
            ticker=ticker,
            period=period,
            total_return=total_return,
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            win_rate=win_rate,
            num_trades=num_trades,
            trades=trades,
        )
