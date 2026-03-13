"""Signal persistence service."""
import json
import logging
from web.db.connection import get_db

logger = logging.getLogger("money_mani.web.services.signal")


class SignalService:
    """Persist and query trading signals."""

    def save_signal(self, signal_info: dict) -> int:
        """Save a signal dict to the signals table. Returns signal ID.

        Skips if the same strategy+ticker+signal_type already exists today (DB-level dedup).
        """
        with get_db() as db:
            existing = db.execute(
                """SELECT id FROM signals
                   WHERE strategy_name = ? AND ticker = ? AND signal_type = ?
                     AND date(detected_at) = date('now')""",
                (signal_info.get("strategy_name", ""), signal_info["ticker"], signal_info["signal_type"]),
            ).fetchone()
            if existing:
                logger.debug(f"Signal already exists today: {signal_info.get('strategy_name')}/{signal_info['ticker']}")
                return existing["id"]

            cursor = db.execute(
                """INSERT INTO signals (strategy_name, ticker, ticker_name, market,
                   signal_type, price, indicators_json, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal_info.get("strategy_name", ""),
                    signal_info["ticker"],
                    signal_info.get("ticker_name", ""),
                    signal_info.get("market", "KRX"),
                    signal_info["signal_type"],
                    signal_info["price"],
                    json.dumps(signal_info.get("indicators", {}), ensure_ascii=False, default=str),
                    signal_info.get("source", "daily_scan"),
                ),
            )
            return cursor.lastrowid

    def save_signals(self, signals: list[dict], source: str = "daily_scan") -> int:
        """Save multiple signals. Returns count saved."""
        count = 0
        for sig in signals:
            sig["source"] = source
            try:
                self.save_signal(sig)
                count += 1
            except Exception as e:
                logger.error(f"Failed to save signal: {e}")
        return count

    def list_signals(self, ticker: str = None, signal_type: str = None,
                     date_from: str = None, date_to: str = None, limit: int = 100) -> list[dict]:
        """List signals with optional filters."""
        with get_db() as db:
            query = "SELECT * FROM signals WHERE 1=1"
            params = []
            if ticker:
                query += " AND ticker=?"
                params.append(ticker)
            if signal_type:
                query += " AND signal_type=?"
                params.append(signal_type)
            if date_from:
                query += " AND detected_at >= ?"
                params.append(date_from)
            if date_to:
                query += " AND detected_at <= ?"
                params.append(date_to + " 23:59:59")
            query += " ORDER BY detected_at DESC LIMIT ?"
            params.append(limit)
            rows = db.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_signal(self, signal_id: int) -> dict | None:
        with get_db() as db:
            row = db.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
            return dict(row) if row else None

    def get_actions(self, days: int = 7) -> list[dict]:
        """Return latest scoring-based actions per ticker for the trading dashboard.

        Queries scoring_results directly (no signal_id JOIN required).
        signal_id is NULL for BLOCKED decisions, so we derive action from composite_score.
        Returns one entry per ticker (latest by scan_date).
        """
        import json as _json
        since = f"-{days} days"
        with get_db() as db:
            rows = db.execute(
                """
                SELECT
                    sr.ticker,
                    sr.ticker_name,
                    sr.market,
                    sr.composite_score,
                    sr.decision,
                    sr.scan_date,
                    sr.score_breakdown_json,
                    p.status  AS position_status,
                    p.pnl_pct
                FROM scoring_results sr
                LEFT JOIN positions p
                    ON sr.ticker = p.ticker AND p.status = 'open'
                WHERE sr.scan_date >= DATE('now', ?)
                ORDER BY sr.scan_date DESC, sr.composite_score DESC
                """,
                (since,),
            ).fetchall()

        seen: set[str] = set()
        actions: list[dict] = []
        for row in rows:
            ticker = row["ticker"]
            if ticker in seen:
                continue
            seen.add(ticker)

            score = row["composite_score"] or 0.0
            if score >= 0.65:
                conviction = "HIGH"
            elif score >= 0.50:
                conviction = "MED"
            else:
                conviction = "LOW"

            # Derive action from score (signal_id may be NULL for BLOCKED decisions)
            if score >= 0.60:
                action = "BUY"
            elif score <= 0.40:
                action = "SELL"
            else:
                action = "WATCH"

            try:
                breakdown = _json.loads(row["score_breakdown_json"] or "{}")
            except Exception:
                breakdown = {}

            actions.append({
                "ticker": ticker,
                "ticker_name": row["ticker_name"] or ticker,
                "market": row["market"] or "KRX",
                "action": action,
                "conviction": conviction,
                "composite_score": score,
                "score_breakdown": breakdown,
                "signal_price": None,
                "last_signal_date": str(row["scan_date"] or "")[:10],
                "is_holding": row["position_status"] == "open",
                "pnl_pct": row["pnl_pct"],
            })

        return actions
