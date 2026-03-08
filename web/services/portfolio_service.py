"""Portfolio service: live holdings + snapshots."""
import logging
from web.db.connection import get_db

logger = logging.getLogger("money_mani.web.services.portfolio")


class PortfolioService:
    """Fetch live holdings from KIS API and store snapshots."""

    def fetch_live(self) -> list[dict]:
        """Fetch current holdings from KIS. Returns list of holding dicts."""
        try:
            from broker.kis_client import KISClient
            from broker.portfolio import PortfolioManager
            kis = KISClient()
            pm = PortfolioManager(kis)
            holdings = pm.fetch_all_holdings()
            result = []
            for ticker, h in holdings.items():
                result.append({
                    "ticker": h.ticker,
                    "name": h.name,
                    "market": h.market,
                    "quantity": h.quantity,
                    "avg_price": h.avg_price,
                    "current_price": h.current_price,
                    "pnl_pct": h.pnl_pct,
                })
            # Store snapshot
            self._store_snapshot(result)
            return result
        except Exception as e:
            logger.error(f"Failed to fetch live portfolio: {e}")
            return []

    def _store_snapshot(self, holdings: list[dict]):
        with get_db() as db:
            for h in holdings:
                db.execute(
                    """INSERT INTO portfolio_snapshots
                       (ticker, name, market, quantity, avg_price, current_price, pnl_pct)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (h["ticker"], h["name"], h["market"], h["quantity"],
                     h["avg_price"], h["current_price"], h["pnl_pct"]),
                )

    def list_snapshots(self, ticker: str = None, limit: int = 50) -> list[dict]:
        with get_db() as db:
            query = "SELECT * FROM portfolio_snapshots WHERE 1=1"
            params = []
            if ticker:
                query += " AND ticker=?"
                params.append(ticker)
            query += " ORDER BY snapshot_at DESC LIMIT ?"
            params.append(limit)
            rows = db.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_latest_snapshot(self) -> list[dict]:
        """Get most recent snapshot per ticker."""
        with get_db() as db:
            rows = db.execute(
                """SELECT p.* FROM portfolio_snapshots p
                   INNER JOIN (SELECT ticker, MAX(snapshot_at) as max_at
                               FROM portfolio_snapshots GROUP BY ticker) latest
                   ON p.ticker = latest.ticker AND p.snapshot_at = latest.max_at
                   ORDER BY p.pnl_pct DESC"""
            ).fetchall()
            return [dict(r) for r in rows]
