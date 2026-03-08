"""Daily scan wrapper service."""
import logging
from datetime import date
from web.db.connection import get_db
from web.services.signal_service import SignalService

logger = logging.getLogger("money_mani.web.services.scan")


class ScanService:
    """Wrap DailyScan and persist scan history + signals."""

    def __init__(self):
        self.signal_service = SignalService()

    def run_scan(self) -> dict:
        """Run DailyScan and store results. Returns scan summary."""
        from pipeline.daily_scan import DailyScan
        scanner = DailyScan()
        result = scanner.run()

        # Store scan history
        signals = result.get("signals", [])
        scan_id = self._store_scan_history(result)

        # Store signals
        if signals:
            self.signal_service.save_signals(signals, source="daily_scan")

        return {
            "scan_id": scan_id,
            "date": result.get("date", str(date.today())),
            "signals_count": len(signals),
            "skipped": result.get("skipped", False),
        }

    def _store_scan_history(self, result: dict) -> int:
        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO scan_history (scan_date, signals_count, markets_open)
                   VALUES (?, ?, ?)""",
                (
                    result.get("date", str(date.today())),
                    len(result.get("signals", [])),
                    "KRX,US" if not result.get("skipped") else "",
                ),
            )
            return cursor.lastrowid

    def list_scans(self, limit: int = 30) -> list[dict]:
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM scan_history ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
