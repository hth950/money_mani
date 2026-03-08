"""Discovery pipeline wrapper service."""
import json
import logging
from web.db.connection import get_db

logger = logging.getLogger("money_mani.web.services.discovery")


class DiscoveryService:
    """Wrap StrategyDiscovery and persist reports."""

    def run_discovery(self, queries: list[str] = None, market: str = "KRX",
                      top_n: int = 3, use_trends: bool = False) -> dict:
        """Run discovery pipeline and store report. Returns summary."""
        from pipeline.discovery import StrategyDiscovery
        discovery = StrategyDiscovery()
        report = discovery.run(queries=queries, market=market, top_n=top_n, use_trends=use_trends)

        # Store report
        report_id = self._store_report(report)

        return {
            "report_id": report_id,
            "date": report.date,
            "market": report.market,
            "videos_found": report.videos_found,
            "strategies_extracted": report.strategies_extracted,
            "strategies_ranked": report.strategies_ranked,
            "strategies_validated": report.strategies_validated,
        }

    def _store_report(self, report) -> int:
        with get_db() as db:
            rankings_data = []
            for r in report.rankings:
                rankings_data.append({
                    "strategy_name": r.strategy_name,
                    "composite_score": r.composite_score,
                    "avg_return": getattr(r, "avg_return", 0),
                    "avg_sharpe": getattr(r, "avg_sharpe", 0),
                    "valid_count": r.valid_count,
                })
            cursor = db.execute(
                """INSERT INTO discovery_reports
                   (run_date, market, queries_json, videos_found, strategies_extracted,
                    strategies_ranked, strategies_validated, rankings_json, trends_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    report.date,
                    report.market,
                    json.dumps(report.queries_used, ensure_ascii=False),
                    report.videos_found,
                    report.strategies_extracted,
                    report.strategies_ranked,
                    report.strategies_validated,
                    json.dumps(rankings_data, ensure_ascii=False, default=str),
                    json.dumps(report.trends, ensure_ascii=False, default=str),
                ),
            )
            return cursor.lastrowid

    def list_reports(self, limit: int = 20) -> list[dict]:
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM discovery_reports ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_report(self, report_id: int) -> dict | None:
        with get_db() as db:
            row = db.execute("SELECT * FROM discovery_reports WHERE id=?", (report_id,)).fetchone()
            return dict(row) if row else None
