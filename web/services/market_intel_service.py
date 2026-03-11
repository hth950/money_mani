"""Service layer for market intelligence data."""

import json
import logging

from web.db.connection import get_db

logger = logging.getLogger("money_mani.web.services.market_intel")


class MarketIntelService:
    """Query and manage market intelligence data."""

    def list_scans(self, limit: int = 20) -> list[dict]:
        """Get recent scan records."""
        with get_db() as conn:
            rows = conn.execute(
                """SELECT id, scan_time, scan_type, model_used,
                          issues_count, tickers_count, status,
                          error_message, discord_sent, created_at
                   FROM market_intel_scans
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_scan(self, scan_id: int) -> dict | None:
        """Get a single scan with its issues."""
        with get_db() as conn:
            scan = conn.execute(
                "SELECT * FROM market_intel_scans WHERE id = ?", (scan_id,)
            ).fetchone()
            if not scan:
                return None
            issues = conn.execute(
                """SELECT * FROM market_intel_issues
                   WHERE scan_id = ? ORDER BY confidence DESC""",
                (scan_id,),
            ).fetchall()
        result = dict(scan)
        result["issues"] = [self._parse_issue(dict(i)) for i in issues]
        return result

    def get_issues(self, days: int = 7, category: str = None) -> list[dict]:
        """Get recent issues with optional category filter."""
        query = """SELECT i.*, s.scan_type, s.scan_time
                   FROM market_intel_issues i
                   JOIN market_intel_scans s ON i.scan_id = s.id
                   WHERE i.created_at >= datetime('now', ?)"""
        params = [f"-{days} days"]

        if category:
            query += " AND i.category = ?"
            params.append(category)

        query += " ORDER BY i.created_at DESC"

        with get_db() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._parse_issue(dict(r)) for r in rows]

    def get_issue(self, issue_id: int) -> dict | None:
        """Get a single issue with full details."""
        with get_db() as conn:
            row = conn.execute(
                """SELECT i.*, s.scan_type, s.scan_time
                   FROM market_intel_issues i
                   JOIN market_intel_scans s ON i.scan_id = s.id
                   WHERE i.id = ?""",
                (issue_id,),
            ).fetchone()
        if not row:
            return None
        return self._parse_issue(dict(row))

    def get_accuracy_stats(self) -> dict:
        """Get accuracy statistics for completed predictions."""
        with get_db() as conn:
            rows = conn.execute(
                """SELECT category, COUNT(*) as total,
                          AVG(accuracy_score) as avg_accuracy,
                          SUM(CASE WHEN accuracy_score >= 0.5 THEN 1 ELSE 0 END)
                              as correct_count
                   FROM market_intel_issues
                   WHERE accuracy_score IS NOT NULL
                   GROUP BY category"""
            ).fetchall()
            overall = conn.execute(
                """SELECT COUNT(*) as total,
                          AVG(accuracy_score) as avg_accuracy
                   FROM market_intel_issues
                   WHERE accuracy_score IS NOT NULL"""
            ).fetchone()
            ticker_rows = conn.execute(
                """SELECT i.affected_tickers_json, i.accuracy_score
                   FROM market_intel_issues i
                   WHERE i.accuracy_score IS NOT NULL"""
            ).fetchall()
            recent = conn.execute(
                """SELECT id, title, category, sentiment, confidence,
                          accuracy_score, detection_date
                   FROM market_intel_issues
                   WHERE accuracy_score IS NOT NULL
                   ORDER BY detection_date DESC LIMIT 10"""
            ).fetchall()

        # Aggregate accuracy by ticker
        ticker_acc: dict = {}
        for row in ticker_rows:
            raw = row["affected_tickers_json"]
            if not raw:
                continue
            try:
                tickers = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for t in tickers:
                code = t.get("ticker", "")
                if not code:
                    continue
                entry = ticker_acc.setdefault(code, {
                    "ticker": code,
                    "name": t.get("name", ""),
                    "count": 0,
                    "total_score": 0.0,
                })
                entry["count"] += 1
                entry["total_score"] += row["accuracy_score"] or 0.0
        by_ticker = [
            {
                "ticker": v["ticker"],
                "name": v["name"],
                "count": v["count"],
                "avg_accuracy": v["total_score"] / v["count"] if v["count"] else 0.0,
            }
            for v in ticker_acc.values()
        ]

        return {
            "by_category": [dict(r) for r in rows],
            "overall": dict(overall) if overall else {"total": 0, "avg_accuracy": 0},
            "by_ticker": by_ticker,
            "recent_issues": [dict(r) for r in recent],
        }

    def get_issues_by_ticker(self, days: int = 7) -> dict[str, list[dict]]:
        """Get recent issues indexed by ticker code for cache use."""
        issues = self.get_issues(days=days)
        by_ticker: dict[str, list[dict]] = {}
        for issue in issues:
            tickers = issue.get("affected_tickers") or []
            for t in tickers:
                code = t.get("ticker", "")
                if code:
                    by_ticker.setdefault(code, []).append({
                        "title": issue.get("title", ""),
                        "category": issue.get("category", ""),
                        "sentiment": issue.get("sentiment", ""),
                        "confidence": issue.get("confidence", 0),
                        "direction": t.get("direction", ""),
                        "detection_date": issue.get("detection_date", ""),
                    })
        return by_ticker

    def run_scan_now(self, scan_type: str = "pre_market") -> dict:
        """Trigger a manual scan."""
        from pipeline.market_intel import MarketIntelScanner
        scanner = MarketIntelScanner()
        return scanner.scan(scan_type)

    def _parse_issue(self, issue: dict) -> dict:
        """Parse JSON fields in an issue dict."""
        for field in ("affected_tickers_json", "price_at_detection_json",
                      "price_after_1d_json", "price_after_3d_json",
                      "price_after_5d_json"):
            raw = issue.get(field)
            parsed_key = field.replace("_json", "")
            if raw:
                try:
                    issue[parsed_key] = json.loads(raw)
                except json.JSONDecodeError:
                    issue[parsed_key] = None
            else:
                issue[parsed_key] = None
        return issue
