"""Ensemble strategy backtest: find the best combination of strategies.

Tests consensus-based trading rules:
  - "Execute BUY when N+ strategies agree" (vary N from 2 to all)
  - Compare single vs ensemble performance
  - Output best combination by Sharpe, win rate, return

Usage:
    python scripts/ensemble_backtest.py --ticker 005930
    python scripts/ensemble_backtest.py --ticker 005930,000660 --min-consensus 2 --max-consensus 10
    python scripts/ensemble_backtest.py --all-tickers
"""

from __future__ import annotations

import argparse
import gc
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtester.signals import SignalGenerator
from market_data import KRXFetcher
from strategy.registry import StrategyRegistry
from utils.config_loader import load_config

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class EnsembleResult:
    """Result of an ensemble backtest."""
    consensus_n: int
    total_strategies: int
    num_trades: int
    total_return: float  # cumulative %
    win_rate: float
    sharpe_ratio: float
    max_drawdown: float
    avg_holding_days: float
    trades: list[dict] = field(default_factory=list, repr=False)

    @property
    def is_good(self) -> bool:
        return self.num_trades >= 5 and self.total_return > 0 and self.sharpe_ratio >= 0.3


def load_validated_strategies(registry: StrategyRegistry) -> list:
    """Load all validated strategies."""
    strategies = []
    for name in registry.list_strategies():
        try:
            s = registry.load(name)
            if s.status == "validated":
                strategies.append(s)
        except Exception:
            pass
    return strategies


def compute_all_signals(strategies: list, df: pd.DataFrame) -> pd.DataFrame:
    """Compute signals for all strategies on the same data.

    Returns DataFrame with columns = strategy names, values = 1 (BUY), -1 (SELL), 0 (HOLD).
    """
    signal_df = pd.DataFrame(index=df.index)

    for strat in strategies:
        try:
            sig_gen = SignalGenerator(strat)
            df_ind = sig_gen.compute_indicators(df)
            sigs = sig_gen.generate_signals(df_ind)
            signal_df[strat.name] = sigs
        except Exception as e:
            logger.debug(f"Signal error for {strat.name}: {e}")
            signal_df[strat.name] = 0
        gc.collect()

    return signal_df


def ensemble_backtest(signal_df: pd.DataFrame, price_df: pd.DataFrame,
                      consensus_n: int, initial_capital: float = 10_000_000,
                      commission: float = 0.00015) -> EnsembleResult:
    """Run ensemble backtest with consensus-N rule.

    BUY when N+ strategies say BUY on the same day.
    SELL when N+ strategies say SELL on the same day.
    """
    total_strats = signal_df.shape[1]

    # Count BUY/SELL signals per day
    buy_count = (signal_df == 1).sum(axis=1)
    sell_count = (signal_df == -1).sum(axis=1)

    # Generate ensemble signals
    ensemble_signal = pd.Series(0, index=signal_df.index)
    ensemble_signal[buy_count >= consensus_n] = 1
    ensemble_signal[sell_count >= consensus_n] = -1

    # Simulate trades
    capital = initial_capital
    position = None  # {"entry_price": float, "entry_date": date, "entry_idx": int}
    trades = []
    equity_curve = [capital]

    close = price_df["Close"]

    for i in range(1, len(close)):
        sig = ensemble_signal.iloc[i]
        price = close.iloc[i]
        date = close.index[i]

        if sig == 1 and position is None:
            # Open position
            shares = int((capital * (1 - commission)) / price)
            if shares > 0:
                cost = shares * price * (1 + commission)
                position = {
                    "entry_price": price,
                    "entry_date": date,
                    "entry_idx": i,
                    "shares": shares,
                    "cost": cost,
                }
                capital -= cost

        elif sig == -1 and position is not None:
            # Close position
            proceeds = position["shares"] * price * (1 - commission)
            pnl = proceeds - position["cost"]
            pnl_pct = pnl / position["cost"] * 100
            holding_days = (date - position["entry_date"]).days

            trades.append({
                "entry_date": str(position["entry_date"].date()) if hasattr(position["entry_date"], "date") else str(position["entry_date"]),
                "exit_date": str(date.date()) if hasattr(date, "date") else str(date),
                "entry_price": position["entry_price"],
                "exit_price": price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "holding_days": holding_days,
                "buy_consensus": int(buy_count.iloc[position["entry_idx"]]),
                "sell_consensus": int(sell_count.iloc[i]),
            })

            capital += proceeds
            position = None

        # Track equity
        if position is not None:
            equity = capital + position["shares"] * price
        else:
            equity = capital
        equity_curve.append(equity)

    # Force close if still in position at end
    if position is not None:
        price = close.iloc[-1]
        proceeds = position["shares"] * price * (1 - commission)
        pnl = proceeds - position["cost"]
        pnl_pct = pnl / position["cost"] * 100
        date = close.index[-1]
        holding_days = (date - position["entry_date"]).days
        trades.append({
            "entry_date": str(position["entry_date"].date()) if hasattr(position["entry_date"], "date") else str(position["entry_date"]),
            "exit_date": str(date.date()) if hasattr(date, "date") else str(date),
            "entry_price": position["entry_price"],
            "exit_price": price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "holding_days": holding_days,
            "buy_consensus": int(buy_count.iloc[position["entry_idx"]]),
            "sell_consensus": int(sell_count.iloc[-1]),
        })
        capital += proceeds

    # Compute metrics
    total_return = (capital - initial_capital) / initial_capital * 100
    num_trades = len(trades)
    win_rate = len([t for t in trades if t["pnl"] > 0]) / num_trades * 100 if num_trades > 0 else 0

    # Sharpe ratio (annualized from daily equity returns)
    equity_arr = np.array(equity_curve)
    daily_returns = np.diff(equity_arr) / equity_arr[:-1]
    if len(daily_returns) > 1 and np.std(daily_returns) > 0:
        sharpe = np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(252)
    else:
        sharpe = 0.0

    # Max drawdown
    peak = np.maximum.accumulate(equity_arr)
    drawdown = (equity_arr - peak) / peak
    max_dd = float(np.min(drawdown)) * 100

    avg_hold = np.mean([t["holding_days"] for t in trades]) if trades else 0

    return EnsembleResult(
        consensus_n=consensus_n,
        total_strategies=total_strats,
        num_trades=num_trades,
        total_return=round(total_return, 2),
        win_rate=round(win_rate, 1),
        sharpe_ratio=round(sharpe, 3),
        max_drawdown=round(max_dd, 2),
        avg_holding_days=round(avg_hold, 1),
        trades=trades,
    )


def run_category_ensemble(strategies: list, df: pd.DataFrame, price_df: pd.DataFrame) -> dict:
    """Test ensembles grouped by category."""
    categories = defaultdict(list)
    for s in strategies:
        categories[s.category].append(s)

    results = {}
    for cat, cat_strats in sorted(categories.items()):
        if len(cat_strats) < 2:
            continue
        cat_signals = compute_all_signals(cat_strats, df)
        for n in range(2, min(len(cat_strats) + 1, 6)):
            result = ensemble_backtest(cat_signals, price_df, consensus_n=n)
            results[f"{cat}_N{n}"] = result

    return results


def main():
    parser = argparse.ArgumentParser(description="Ensemble strategy backtester")
    parser.add_argument("--ticker", type=str, default="005930", help="Ticker(s), comma-separated")
    parser.add_argument("--all-tickers", action="store_true", help="Use all config tickers")
    parser.add_argument("--start-date", type=str, default="2023-01-01", help="Start date")
    parser.add_argument("--min-consensus", type=int, default=2, help="Min consensus N")
    parser.add_argument("--max-consensus", type=int, default=15, help="Max consensus N")
    parser.add_argument("--by-category", action="store_true", help="Also test per-category ensembles")
    args = parser.parse_args()

    config = load_config()
    registry = StrategyRegistry()

    # Determine tickers
    if args.all_tickers:
        tickers = config["pipeline"]["targets"].get("custom_tickers", [])
    else:
        tickers = args.ticker.split(",")

    logger.info("Loading validated strategies...")
    strategies = load_validated_strategies(registry)
    logger.info(f"Loaded {len(strategies)} validated strategies")

    if len(strategies) < 2:
        logger.error("Need at least 2 validated strategies. Run --validate first.")
        return

    fetcher = KRXFetcher(delay=0.5)

    for ticker in tickers:
        logger.info(f"\n{'='*60}")
        logger.info(f"Ticker: {ticker}")
        logger.info(f"{'='*60}")

        try:
            df = fetcher.get_ohlcv(ticker, args.start_date)
        except Exception as e:
            logger.error(f"Failed to fetch {ticker}: {e}")
            continue

        if df is None or len(df) < 60:
            logger.warning(f"Insufficient data for {ticker}")
            continue

        logger.info(f"Data: {len(df)} rows ({df.index[0].date()} ~ {df.index[-1].date()})")

        # Compute all signals
        logger.info("Computing signals for all strategies...")
        signal_df = compute_all_signals(strategies, df)

        # Count how many strategies generate any signals
        active_strats = (signal_df != 0).any().sum()
        logger.info(f"Active strategies (with signals): {active_strats}/{len(strategies)}")

        # Show signal distribution
        buy_total = (signal_df == 1).sum().sum()
        sell_total = (signal_df == -1).sum().sum()
        logger.info(f"Total signals: BUY={buy_total}, SELL={sell_total}")

        price_df = df[["Open", "High", "Low", "Close", "Volume"]].copy()

        # Test different consensus levels
        logger.info(f"\n--- Consensus N sweep ({args.min_consensus} ~ {args.max_consensus}) ---")
        logger.info(f"{'N':>3} | {'Trades':>6} | {'Return':>8} | {'WinRate':>7} | {'Sharpe':>7} | {'MaxDD':>7} | {'AvgHold':>7}")
        logger.info("-" * 65)

        best_sharpe = None
        best_return = None

        max_n = min(args.max_consensus, active_strats)
        for n in range(args.min_consensus, max_n + 1):
            result = ensemble_backtest(signal_df, price_df, consensus_n=n)
            marker = ""
            if best_sharpe is None or result.sharpe_ratio > best_sharpe.sharpe_ratio:
                best_sharpe = result
            if best_return is None or result.total_return > best_return.total_return:
                best_return = result
            if result.is_good:
                marker = " <--"

            logger.info(
                f"{n:>3} | {result.num_trades:>6} | {result.total_return:>7.1f}% | {result.win_rate:>6.1f}% | "
                f"{result.sharpe_ratio:>7.3f} | {result.max_drawdown:>6.1f}% | {result.avg_holding_days:>6.1f}d{marker}"
            )

        # Summary
        logger.info(f"\n--- Best Results for {ticker} ---")
        if best_sharpe and best_sharpe.num_trades > 0:
            logger.info(f"Best Sharpe:  N={best_sharpe.consensus_n} -> Sharpe={best_sharpe.sharpe_ratio:.3f}, "
                        f"Return={best_sharpe.total_return:.1f}%, WinRate={best_sharpe.win_rate:.1f}%, "
                        f"Trades={best_sharpe.num_trades}")
        if best_return and best_return.num_trades > 0:
            logger.info(f"Best Return:  N={best_return.consensus_n} -> Return={best_return.total_return:.1f}%, "
                        f"Sharpe={best_return.sharpe_ratio:.3f}, WinRate={best_return.win_rate:.1f}%, "
                        f"Trades={best_return.num_trades}")

        # Top trades detail for best Sharpe
        if best_sharpe and best_sharpe.trades:
            logger.info(f"\nTop trades (N={best_sharpe.consensus_n}):")
            sorted_trades = sorted(best_sharpe.trades, key=lambda t: t["pnl_pct"], reverse=True)
            for t in sorted_trades[:5]:
                icon = "W" if t["pnl"] > 0 else "L"
                logger.info(f"  [{icon}] {t['entry_date']}~{t['exit_date']} "
                            f"@ {t['entry_price']:,.0f}->{t['exit_price']:,.0f} "
                            f"P&L={t['pnl_pct']:+.1f}% hold={t['holding_days']}d "
                            f"consensus=B{t['buy_consensus']}/S{t['sell_consensus']}")

        # Per-category ensemble
        if args.by_category:
            logger.info(f"\n--- Category Ensembles ---")
            cat_results = run_category_ensemble(strategies, df, price_df)
            for key, result in sorted(cat_results.items(), key=lambda x: x[1].sharpe_ratio, reverse=True):
                if result.num_trades > 0:
                    logger.info(f"  {key:>25}: Return={result.total_return:>7.1f}% "
                                f"Sharpe={result.sharpe_ratio:>6.3f} WR={result.win_rate:>5.1f}% "
                                f"Trades={result.num_trades}")

        gc.collect()


if __name__ == "__main__":
    main()
