"""Risk management service for web API."""

import logging
from scoring.risk_manager import PortfolioRiskManager
from web.db.connection import get_db

logger = logging.getLogger("money_mani.web.services.risk_service")


class RiskService:
    def __init__(self):
        self._manager = PortfolioRiskManager()

    def get_status(self) -> dict:
        return self._manager.get_risk_status()

    def get_block_history(self, limit: int = 50) -> list[dict]:
        """Get recent risk-blocked signals from scoring_results."""
        try:
            with get_db() as db:
                rows = db.execute("""
                    SELECT ticker, market, scan_date, composite_score, decision, block_reason, created_at
                    FROM scoring_results
                    WHERE decision = 'BLOCKED'
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (limit,)).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"Failed to get block history: {e}")
            return []

    def check_ticker(self, ticker: str, market: str = "KRX") -> dict:
        allowed, reason = self._manager.check_can_buy(ticker, market)
        return {"ticker": ticker, "allowed": allowed, "reason": reason}
