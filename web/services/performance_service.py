"""Signal performance tracking service - records signals and calculates P&L."""

import json
import logging
from datetime import datetime, timedelta, timezone

from web.db.connection import get_db

logger = logging.getLogger("money_mani.web.services.performance")

KST = timezone(timedelta(hours=9))


def _now_kst() -> datetime:
    return datetime.now(KST)


def _today_kst() -> str:
    return _now_kst().strftime("%Y-%m-%d")


class PerformanceService:
    """Track signal performance and generate P&L reports."""

    def record_signal(self, signal_info: dict, signal_id: int = None) -> int:
        """Record a signal for performance tracking.

        Args:
            signal_info: Signal dict with strategy_name, ticker, ticker_name,
                         market, signal_type, price, date.
            signal_id: Optional FK to signals table.

        Returns:
            signal_performance row ID.
        """
        signal_date = signal_info.get("date", _today_kst())
        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO signal_performance
                   (signal_id, strategy_name, ticker, ticker_name, market,
                    signal_type, signal_price, signal_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal_id,
                    signal_info.get("strategy_name", ""),
                    signal_info["ticker"],
                    signal_info.get("ticker_name", ""),
                    signal_info.get("market", "KRX"),
                    signal_info["signal_type"],
                    signal_info["price"],
                    signal_date,
                ),
            )
            return cursor.lastrowid

    def update_close_price(self, perf_id: int, close_price: float) -> None:
        """Update closing price and calculate P&L for a signal."""
        now_str = _now_kst().strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as db:
            row = db.execute(
                "SELECT signal_type, signal_price FROM signal_performance WHERE id=?",
                (perf_id,),
            ).fetchone()
            if not row:
                return

            signal_price = row["signal_price"]
            signal_type = row["signal_type"]

            if signal_price and signal_price > 0:
                if signal_type == "BUY":
                    pnl_amount = close_price - signal_price
                else:  # SELL
                    pnl_amount = signal_price - close_price
                pnl_pct = (pnl_amount / signal_price) * 100
            else:
                pnl_amount = 0
                pnl_pct = 0

            db.execute(
                """UPDATE signal_performance
                   SET close_price=?, pnl_amount=?, pnl_pct=?, evaluated_at=?
                   WHERE id=?""",
                (close_price, round(pnl_amount, 2), round(pnl_pct, 4), now_str, perf_id),
            )

    def get_unevaluated(self, signal_date: str = None) -> list[dict]:
        """Get signals that haven't been evaluated (no close_price yet)."""
        date = signal_date or _today_kst()
        with get_db() as db:
            rows = db.execute(
                """SELECT * FROM signal_performance
                   WHERE signal_date=? AND close_price IS NULL""",
                (date,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_daily_performance(self, date: str = None) -> list[dict]:
        """Get all evaluated signals for a given date."""
        date = date or _today_kst()
        with get_db() as db:
            rows = db.execute(
                """SELECT * FROM signal_performance
                   WHERE signal_date=? AND close_price IS NOT NULL
                   ORDER BY pnl_pct DESC""",
                (date,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_weekly_performance(self, end_date: str = None) -> list[dict]:
        """Get all evaluated signals for the past 7 days."""
        if end_date:
            end = datetime.strptime(end_date, "%Y-%m-%d")
        else:
            end = _now_kst()
        start = (end - timedelta(days=7)).strftime("%Y-%m-%d")
        end_str = end.strftime("%Y-%m-%d")

        with get_db() as db:
            rows = db.execute(
                """SELECT * FROM signal_performance
                   WHERE signal_date BETWEEN ? AND ? AND close_price IS NOT NULL
                   ORDER BY signal_date DESC, pnl_pct DESC""",
                (start, end_str),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_performance_summary(self, date: str = None) -> dict:
        """Calculate summary stats for a date."""
        records = self.get_daily_performance(date)
        return self._summarize(records, date or _today_kst())

    def get_weekly_summary(self, end_date: str = None) -> dict:
        """Calculate weekly summary stats."""
        records = self.get_weekly_performance(end_date)
        if end_date:
            end = datetime.strptime(end_date, "%Y-%m-%d")
        else:
            end = _now_kst()
        start = (end - timedelta(days=7)).strftime("%Y-%m-%d")
        label = f"{start} ~ {end.strftime('%Y-%m-%d')}"
        return self._summarize(records, label)

    def _summarize(self, records: list[dict], label: str) -> dict:
        """Build summary dict from performance records."""
        if not records:
            return {
                "period": label,
                "total_signals": 0,
                "buy_signals": 0,
                "sell_signals": 0,
                "avg_pnl_pct": 0,
                "total_pnl_pct": 0,
                "best": None,
                "worst": None,
                "win_count": 0,
                "lose_count": 0,
                "win_rate": 0,
                "records": [],
            }

        pnls = [r["pnl_pct"] for r in records if r["pnl_pct"] is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        best = max(records, key=lambda r: r.get("pnl_pct", 0) or 0)
        worst = min(records, key=lambda r: r.get("pnl_pct", 0) or 0)

        return {
            "period": label,
            "total_signals": len(records),
            "buy_signals": sum(1 for r in records if r["signal_type"] == "BUY"),
            "sell_signals": sum(1 for r in records if r["signal_type"] == "SELL"),
            "avg_pnl_pct": round(sum(pnls) / len(pnls), 4) if pnls else 0,
            "total_pnl_pct": round(sum(pnls), 4),
            "best": {
                "ticker": best["ticker"],
                "ticker_name": best.get("ticker_name", ""),
                "pnl_pct": best["pnl_pct"],
                "signal_type": best["signal_type"],
            },
            "worst": {
                "ticker": worst["ticker"],
                "ticker_name": worst.get("ticker_name", ""),
                "pnl_pct": worst["pnl_pct"],
                "signal_type": worst["signal_type"],
            },
            "win_count": len(wins),
            "lose_count": len(losses),
            "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
            "records": records,
        }

    def save_report(self, summary: dict, report_type: str = "daily") -> int:
        """Save a performance report to DB."""
        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO performance_reports
                   (report_date, report_type, total_signals, buy_signals, sell_signals,
                    avg_pnl_pct, total_pnl_pct, best_pnl_pct, worst_pnl_pct,
                    win_count, lose_count, win_rate, details_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    summary["period"],
                    report_type,
                    summary["total_signals"],
                    summary["buy_signals"],
                    summary["sell_signals"],
                    summary["avg_pnl_pct"],
                    summary["total_pnl_pct"],
                    summary["best"]["pnl_pct"] if summary["best"] else 0,
                    summary["worst"]["pnl_pct"] if summary["worst"] else 0,
                    summary["win_count"],
                    summary["lose_count"],
                    summary["win_rate"],
                    json.dumps(
                        [
                            {k: v for k, v in r.items() if k != "id"}
                            for r in summary.get("records", [])
                        ],
                        ensure_ascii=False,
                        default=str,
                    ),
                ),
            )
            return cursor.lastrowid

    def mark_report_sent(self, report_id: int) -> None:
        """Mark a report as sent to Discord."""
        with get_db() as db:
            db.execute(
                "UPDATE performance_reports SET discord_sent=1 WHERE id=?",
                (report_id,),
            )

    def list_reports(self, report_type: str = None, limit: int = 30) -> list[dict]:
        """List performance reports."""
        with get_db() as db:
            query = "SELECT * FROM performance_reports WHERE 1=1"
            params = []
            if report_type:
                query += " AND report_type=?"
                params.append(report_type)
            query += " ORDER BY report_date DESC LIMIT ?"
            params.append(limit)
            rows = db.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_all_daily_records(self, limit: int = 200) -> list[dict]:
        """Get all signal performance records for the dashboard."""
        with get_db() as db:
            rows = db.execute(
                """SELECT * FROM signal_performance
                   WHERE close_price IS NOT NULL
                   ORDER BY signal_date DESC, created_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
