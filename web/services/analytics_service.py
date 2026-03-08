"""Strategy analytics: aggregate performance stats from closed positions."""

import logging
from datetime import datetime, timedelta, timezone

from web.db.connection import get_db

KST = timezone(timedelta(hours=9))

logger = logging.getLogger("money_mani.web.services.analytics")


class AnalyticsService:
    """Compute and store strategy-level analytics from position data."""

    def compute_strategy_stats(
        self, strategy_name: str = None, period: str = "all_time"
    ) -> dict | None:
        """Compute stats for a strategy over a period and upsert into strategy_stats."""
        date_filter = self._period_to_date(period)
        params: list = []
        clauses = ["status = 'closed'"]

        if strategy_name:
            clauses.append("strategy_name = ?")
            params.append(strategy_name)
        if date_filter:
            clauses.append("exit_date >= ?")
            params.append(date_filter)

        where = " AND ".join(clauses)

        with get_db() as db:
            rows = db.execute(
                f"""SELECT strategy_name,
                           COUNT(*) as total_trades,
                           SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as winning,
                           SUM(CASE WHEN pnl_pct <= 0 THEN 1 ELSE 0 END) as losing,
                           AVG(pnl_pct) as avg_pnl,
                           SUM(pnl_pct) as total_pnl,
                           MAX(pnl_pct) as best,
                           MIN(pnl_pct) as worst,
                           AVG(holding_days) as avg_hold
                    FROM positions
                    WHERE {where}
                    GROUP BY strategy_name""",
                params,
            ).fetchall()

            for row in rows:
                total = row["total_trades"]
                win_rate = (row["winning"] / total * 100) if total > 0 else 0

                db.execute(
                    """INSERT INTO strategy_stats
                       (strategy_name, period, total_trades, winning_trades, losing_trades,
                        win_rate, total_pnl_pct, avg_pnl_pct, best_trade_pnl_pct,
                        worst_trade_pnl_pct, avg_holding_days, computed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                       ON CONFLICT(strategy_name, period) DO UPDATE SET
                        total_trades = excluded.total_trades,
                        winning_trades = excluded.winning_trades,
                        losing_trades = excluded.losing_trades,
                        win_rate = excluded.win_rate,
                        total_pnl_pct = excluded.total_pnl_pct,
                        avg_pnl_pct = excluded.avg_pnl_pct,
                        best_trade_pnl_pct = excluded.best_trade_pnl_pct,
                        worst_trade_pnl_pct = excluded.worst_trade_pnl_pct,
                        avg_holding_days = excluded.avg_holding_days,
                        computed_at = datetime('now')""",
                    (
                        row["strategy_name"], period, total,
                        row["winning"], row["losing"], win_rate,
                        row["total_pnl"], row["avg_pnl"],
                        row["best"], row["worst"], row["avg_hold"],
                    ),
                )

            logger.info(f"Computed stats for {len(rows)} strategies (period={period})")

    def get_strategy_leaderboard(
        self, period: str = "30d", limit: int = 10
    ) -> list[dict]:
        """Get strategies ranked by composite score."""
        with get_db() as db:
            rows = db.execute(
                """SELECT * FROM strategy_stats
                   WHERE period = ? AND total_trades >= 1
                   ORDER BY (win_rate * 0.4 + avg_pnl_pct * 0.6) DESC
                   LIMIT ?""",
                (period, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_ticker_affinity(self, strategy_name: str) -> list[dict]:
        """Which tickers does this strategy perform best on?"""
        with get_db() as db:
            rows = db.execute(
                """SELECT ticker, ticker_name,
                          COUNT(*) as trades,
                          AVG(pnl_pct) as avg_pnl,
                          SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as win_rate
                   FROM positions
                   WHERE strategy_name = ? AND status = 'closed'
                   GROUP BY ticker
                   ORDER BY avg_pnl DESC""",
                (strategy_name,),
            ).fetchall()
            return [dict(r) for r in rows]

    def refresh_all_stats(self):
        """Recompute stats for all periods."""
        for period in ("all_time", "30d", "7d"):
            self.compute_strategy_stats(period=period)
        logger.info("All strategy stats refreshed.")

    def _period_to_date(self, period: str) -> str | None:
        if period == "all_time":
            return None
        today = datetime.now(KST).date()
        if period == "30d":
            return str(today - timedelta(days=30))
        elif period == "7d":
            return str(today - timedelta(days=7))
        return None
