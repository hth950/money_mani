"""Daily scan wrapper service."""
import logging
from datetime import datetime, timedelta, timezone
from web.db.connection import get_db

KST = timezone(timedelta(hours=9))


def _today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


logger = logging.getLogger("money_mani.web.services.scan")


class ScanService:
    """Wrap DailyScan and persist exactly one history row per completed run."""

    def run_scan(self, include_signals: bool = False) -> dict:
        """Run DailyScan and store results. Returns scan summary."""
        from pipeline.daily_scan import DailyScan
        scanner = DailyScan()
        result = scanner.run()

        # DailyScan owns signal persistence.  This wrapper records only the
        # execution summary so manual and scheduled scans cannot save the same
        # consensus signal a second time.
        signals = result.get("signals", [])
        scan_id = self._store_scan_history(result)

        summary = {
            "scan_id": scan_id,
            "date": result.get("date", _today_kst()),
            "signals_count": len(signals),
            "skipped": result.get("skipped", False),
        }
        if include_signals:
            summary["signals"] = signals
        return summary

    def _store_scan_history(self, result: dict) -> int:
        markets_open = result.get("markets_open", [])
        if isinstance(markets_open, str):
            markets_open_text = markets_open
        else:
            markets_open_text = ",".join(markets_open)

        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO scan_history (scan_date, signals_count, markets_open)
                   VALUES (?, ?, ?)""",
                (
                    result.get("date", _today_kst()),
                    len(result.get("signals", [])),
                    markets_open_text,
                ),
            )
            return cursor.lastrowid

    def list_scans(self, limit: int = 30) -> list[dict]:
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM scan_history ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
