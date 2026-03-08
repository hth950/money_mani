"""Position lifecycle tracking: open on BUY, close on SELL per (strategy, ticker)."""

import logging
from datetime import datetime, timedelta, timezone

from web.db.connection import get_db

KST = timezone(timedelta(hours=9))

logger = logging.getLogger("money_mani.web.services.position")


class PositionService:
    """Track open/closed positions per (strategy, ticker) pair."""

    def open_position(
        self,
        strategy_name: str,
        ticker: str,
        ticker_name: str,
        market: str,
        entry_price: float,
        entry_date: str,
        signal_id: int = None,
    ) -> int:
        """Open a new position. Idempotent: returns existing id if already open."""
        existing = self._get_open(strategy_name, ticker)
        if existing:
            logger.warning(
                f"Position already open for {strategy_name}/{ticker} (id={existing['id']}). Skipping."
            )
            return existing["id"]

        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO positions
                   (strategy_name, ticker, ticker_name, market, status,
                    entry_signal_id, entry_date, entry_price)
                   VALUES (?, ?, ?, ?, 'open', ?, ?, ?)""",
                (strategy_name, ticker, ticker_name, market,
                 signal_id, entry_date, entry_price),
            )
            pos_id = cursor.lastrowid
            logger.info(f"Opened position {pos_id}: {strategy_name} BUY {ticker} @ {entry_price}")
            return pos_id

    def close_position(
        self,
        strategy_name: str,
        ticker: str,
        exit_price: float,
        exit_date: str,
        signal_id: int = None,
    ) -> int | None:
        """Close an open position for (strategy, ticker). Returns position_id or None."""
        existing = self._get_open(strategy_name, ticker)
        if not existing:
            logger.info(f"No open position for {strategy_name}/{ticker}. Nothing to close.")
            return None

        entry_price = existing["entry_price"]
        entry_date = existing["entry_date"]
        pnl_amount = exit_price - entry_price
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price else 0

        try:
            holding_days = (
                datetime.strptime(exit_date, "%Y-%m-%d")
                - datetime.strptime(entry_date, "%Y-%m-%d")
            ).days
        except (ValueError, TypeError):
            holding_days = 0

        pos_id = existing["id"]
        with get_db() as db:
            db.execute(
                """UPDATE positions
                   SET status = 'closed',
                       exit_signal_id = ?,
                       exit_date = ?,
                       exit_price = ?,
                       holding_days = ?,
                       pnl_amount = ?,
                       pnl_pct = ?,
                       updated_at = datetime('now')
                   WHERE id = ?""",
                (signal_id, exit_date, exit_price, holding_days,
                 pnl_amount, pnl_pct, pos_id),
            )
            logger.info(
                f"Closed position {pos_id}: {strategy_name} SELL {ticker} "
                f"@ {exit_price} | P&L: {pnl_pct:+.2f}% ({holding_days}d)"
            )
            return pos_id

    def close_expired_positions(self, close_price_fetcher=None) -> list[int]:
        """Auto-close positions exceeding max_holding_days.

        Args:
            close_price_fetcher: callable(ticker, market) -> float or None
        Returns:
            List of closed position ids.
        """
        today = datetime.now(KST).strftime("%Y-%m-%d")
        with get_db() as db:
            rows = db.execute(
                """SELECT * FROM positions
                   WHERE status = 'open'
                   AND julianday(?) - julianday(entry_date) > max_holding_days""",
                (today,),
            ).fetchall()

        closed_ids = []
        for row in rows:
            close_price = None
            if close_price_fetcher:
                try:
                    close_price = close_price_fetcher(row["ticker"], row["market"])
                except Exception as e:
                    logger.error(f"Failed to fetch close price for {row['ticker']}: {e}")

            if close_price is None:
                close_price = row["entry_price"]  # fallback: no P&L

            pos_id = self.close_position(
                row["strategy_name"], row["ticker"],
                close_price, today,
            )
            if pos_id:
                closed_ids.append(pos_id)
                logger.info(
                    f"Auto-closed expired position {pos_id}: "
                    f"{row['strategy_name']}/{row['ticker']} after {row['max_holding_days']}d"
                )
        return closed_ids

    def get_open_positions(
        self, strategy_name: str = None, ticker: str = None
    ) -> list[dict]:
        """Get all open positions, optionally filtered."""
        return self._query_positions("open", strategy_name, ticker)

    def get_closed_positions(
        self, strategy_name: str = None, ticker: str = None, limit: int = 100
    ) -> list[dict]:
        """Get closed positions, optionally filtered."""
        return self._query_positions("closed", strategy_name, ticker, limit)

    def has_open_position(self, strategy_name: str, ticker: str) -> bool:
        """Check if an open position exists for (strategy, ticker)."""
        return self._get_open(strategy_name, ticker) is not None

    def get_position(self, position_id: int) -> dict | None:
        """Get a single position by id."""
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM positions WHERE id = ?", (position_id,)
            ).fetchone()
            return dict(row) if row else None

    # --- internals ---

    def _get_open(self, strategy_name: str, ticker: str) -> dict | None:
        with get_db() as db:
            row = db.execute(
                """SELECT * FROM positions
                   WHERE strategy_name = ? AND ticker = ? AND status = 'open'""",
                (strategy_name, ticker),
            ).fetchone()
            return dict(row) if row else None

    def _query_positions(
        self, status: str, strategy_name: str = None,
        ticker: str = None, limit: int = 100,
    ) -> list[dict]:
        clauses = ["status = ?"]
        params: list = [status]
        if strategy_name:
            clauses.append("strategy_name = ?")
            params.append(strategy_name)
        if ticker:
            clauses.append("ticker = ?")
            params.append(ticker)

        where = " AND ".join(clauses)
        params.append(limit)

        with get_db() as db:
            rows = db.execute(
                f"SELECT * FROM positions WHERE {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
