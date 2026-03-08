"""Strategy ranking: score and compare backtest results across strategies."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from backtester.metrics import BacktestResult

logger = logging.getLogger("money_mani.pipeline.ranking")


@dataclass
class StrategyScore:
    """Aggregated score for a strategy across multiple tickers."""
    strategy_name: str
    avg_return: float
    avg_sharpe: float
    avg_mdd: float
    avg_win_rate: float
    avg_trades: float
    composite_score: float
    num_tickers: int
    valid_count: int


class StrategyRanker:
    """Rank strategies by composite score from backtest results."""

    # Weights for composite score
    W_SHARPE = 0.35
    W_RETURN = 0.25
    W_MDD = 0.20
    W_WIN_RATE = 0.20

    MIN_AVG_TRADES = 5

    def rank(self, results: list[BacktestResult]) -> list[StrategyScore]:
        """Group results by strategy, compute averages, score, and rank.

        Args:
            results: List of BacktestResult from backtesting multiple
                     strategies across multiple tickers.

        Returns:
            Sorted list of StrategyScore (best first).
        """
        if not results:
            return []

        # Group by strategy name
        groups: dict[str, list[BacktestResult]] = {}
        for r in results:
            groups.setdefault(r.strategy_name, []).append(r)

        scores = []
        for name, group in groups.items():
            n = len(group)
            avg_ret = sum(r.total_return for r in group) / n
            avg_sharpe = sum(r.sharpe_ratio for r in group) / n
            avg_mdd = sum(r.max_drawdown for r in group) / n
            avg_wr = sum(r.win_rate for r in group) / n
            avg_trades = sum(r.num_trades for r in group) / n
            valid_count = sum(1 for r in group if r.is_valid)

            if avg_trades < self.MIN_AVG_TRADES:
                logger.debug(f"Skipping {name}: avg_trades={avg_trades:.0f} < {self.MIN_AVG_TRADES}")
                continue

            composite = self._compute_score(avg_ret, avg_sharpe, avg_mdd, avg_wr)
            scores.append(StrategyScore(
                strategy_name=name,
                avg_return=avg_ret,
                avg_sharpe=avg_sharpe,
                avg_mdd=avg_mdd,
                avg_win_rate=avg_wr,
                avg_trades=avg_trades,
                composite_score=composite,
                num_tickers=n,
                valid_count=valid_count,
            ))

        scores.sort(key=lambda s: s.composite_score, reverse=True)
        logger.info(f"Ranked {len(scores)} strategies (from {len(results)} results)")
        return scores

    def _compute_score(self, ret: float, sharpe: float,
                       mdd: float, win_rate: float) -> float:
        """Compute composite score from normalized metrics.

        Each metric is normalized to a 0-1 scale:
        - return: sigmoid-like clamping to [-1, 2] range
        - sharpe: clamp to [0, 3] range
        - mdd: 0 = worst (-50%), 1 = best (0%)
        - win_rate: already 0-1
        """
        # Normalize return: map [-1, 2] → [0, 1]
        ret_norm = max(0.0, min(1.0, (ret + 1.0) / 3.0))

        # Normalize sharpe: map [0, 3] → [0, 1]
        sharpe_norm = max(0.0, min(1.0, sharpe / 3.0))

        # Normalize MDD: map [-0.5, 0] → [0, 1] (less drawdown = better)
        mdd_norm = max(0.0, min(1.0, 1.0 + mdd * 2.0))

        # Win rate already 0-1
        wr_norm = max(0.0, min(1.0, win_rate))

        return (
            self.W_SHARPE * sharpe_norm
            + self.W_RETURN * ret_norm
            + self.W_MDD * mdd_norm
            + self.W_WIN_RATE * wr_norm
        )
