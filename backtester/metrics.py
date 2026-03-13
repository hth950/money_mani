"""BacktestResult dataclass extracted from backtesting.py Stats."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _load_thresholds() -> dict:
    """Load validation thresholds from config/settings.yaml."""
    try:
        from utils.config_loader import load_config
        cfg = load_config()
        return cfg.get("backtest", {}).get("validation_thresholds", {})
    except Exception:
        return {}


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
    avg_holding_days: float = 0.0
    annual_trade_rate: float = 0.0
    is_valid: bool = field(init=False)

    def __post_init__(self):
        th = _load_thresholds()
        min_trades = th.get("min_trades", 10)
        min_return = th.get("min_return", 0.0)
        min_sharpe = th.get("min_sharpe", 0.7)
        max_dd = th.get("max_drawdown", 0.25)
        min_win_rate = th.get("min_win_rate", 0.40)
        min_annual_trades = th.get("min_annual_trades", 4)
        min_avg_holding = th.get("min_avg_holding_days", 5)

        self.is_valid = (
            self.num_trades >= min_trades
            and self.total_return > min_return
            and self.sharpe_ratio >= min_sharpe
            and self.max_drawdown >= -max_dd
            and self.win_rate >= min_win_rate
            and self.annual_trade_rate >= min_annual_trades
            and self.avg_holding_days >= min_avg_holding
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
        avg_holding_days = 0.0
        if trades_df is not None and not trades_df.empty:
            holding_days_list = []
            for _, row in trades_df.iterrows():
                entry = row["EntryTime"]
                exit_ = row["ExitTime"]
                try:
                    days = (exit_ - entry).days
                    holding_days_list.append(max(days, 1))
                except Exception:
                    holding_days_list.append(1)
                trades.append({
                    "entry_time": str(entry),
                    "exit_time": str(exit_),
                    "entry_price": float(row["EntryPrice"]),
                    "exit_price": float(row["ExitPrice"]),
                    "size": float(row["Size"]),
                    "pnl": float(row["PnL"]),
                    "return_pct": float(row["ReturnPct"]),
                })
            if holding_days_list:
                avg_holding_days = sum(holding_days_list) / len(holding_days_list)

        # Calculate annual trade rate from period string "YYYY-MM-DD~YYYY-MM-DD"
        annual_trade_rate = 0.0
        try:
            parts = period.split("~")
            if len(parts) == 2:
                from datetime import datetime
                start = datetime.strptime(parts[0].strip(), "%Y-%m-%d")
                end = datetime.strptime(parts[1].strip(), "%Y-%m-%d")
                years = max((end - start).days / 365.25, 0.1)
                annual_trade_rate = num_trades / years
        except Exception:
            annual_trade_rate = num_trades / 5.0  # fallback: assume 5 years

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
            avg_holding_days=avg_holding_days,
            annual_trade_rate=annual_trade_rate,
        )
