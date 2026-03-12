"""Scoring results service for web API."""

import json
import logging
from web.db.connection import get_db

logger = logging.getLogger("money_mani.web.services.scoring_service")


class ScoringService:

    def save_scoring_result(self, ticker, market, scan_date, scores, decision,
                            ticker_name=None, block_reason=None, weights=None):
        """Save a scoring result to DB."""
        try:
            with get_db() as db:
                db.execute("""
                    INSERT INTO scoring_results
                    (ticker, ticker_name, market, scan_date, technical_score, fundamental_score,
                     flow_score, intel_score, composite_score, score_breakdown_json,
                     decision, block_reason, weights_used_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    ticker, ticker_name or ticker, market, scan_date,
                    scores.get("technical"), scores.get("fundamental"),
                    scores.get("flow"), scores.get("intel"),
                    scores.get("composite"),
                    json.dumps(scores, ensure_ascii=False),
                    decision, block_reason,
                    json.dumps(weights, ensure_ascii=False) if weights else None,
                ))
        except Exception as e:
            logger.error(f"Failed to save scoring result: {e}")

    def get_today_results(self, scan_date=None):
        """Get today's scoring results."""
        if not scan_date:
            from datetime import datetime, timedelta, timezone
            scan_date = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
        try:
            with get_db() as db:
                rows = db.execute("""
                    SELECT * FROM scoring_results
                    WHERE scan_date = ? ORDER BY composite_score DESC
                """, (scan_date,)).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"Failed to get today results: {e}")
            return []

    def get_history(self, days=30):
        """Get scoring history for last N days."""
        try:
            with get_db() as db:
                rows = db.execute("""
                    SELECT scan_date, market,
                           COUNT(*) as total,
                           SUM(CASE WHEN decision='EXECUTE' THEN 1 ELSE 0 END) as execute_count,
                           SUM(CASE WHEN decision='WATCH' THEN 1 ELSE 0 END) as watch_count,
                           SUM(CASE WHEN decision='SKIP' THEN 1 ELSE 0 END) as skip_count,
                           SUM(CASE WHEN decision='BLOCKED' THEN 1 ELSE 0 END) as blocked_count,
                           AVG(composite_score) as avg_score
                    FROM scoring_results
                    GROUP BY scan_date, market
                    ORDER BY scan_date DESC
                    LIMIT ?
                """, (days * 2,)).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"Failed to get history: {e}")
            return []

    def get_ticker_history(self, ticker, limit=30):
        """Get scoring history for a specific ticker."""
        try:
            with get_db() as db:
                rows = db.execute("""
                    SELECT * FROM scoring_results
                    WHERE ticker = ?
                    ORDER BY scan_date DESC LIMIT ?
                """, (ticker, limit)).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"Failed to get ticker history: {e}")
            return []

    def get_summary(self, days=30):
        """Get daily summary data for charts."""
        try:
            with get_db() as db:
                rows = db.execute("""
                    SELECT scan_date,
                           COUNT(*) as total,
                           SUM(CASE WHEN decision='EXECUTE' THEN 1 ELSE 0 END) as execute_count,
                           SUM(CASE WHEN decision='WATCH' THEN 1 ELSE 0 END) as watch_count,
                           AVG(composite_score) as avg_score,
                           AVG(technical_score) as avg_tech,
                           AVG(fundamental_score) as avg_fund,
                           AVG(flow_score) as avg_flow,
                           AVG(intel_score) as avg_intel
                    FROM scoring_results
                    GROUP BY scan_date
                    ORDER BY scan_date DESC
                    LIMIT ?
                """, (days,)).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"Failed to get summary: {e}")
            return []
