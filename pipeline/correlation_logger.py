"""Correlation logger: compare intel predictions vs ensemble signals (DB-only reads)."""

import json
import logging
from datetime import datetime, timedelta, timezone

from web.db.connection import get_db

KST = timezone(timedelta(hours=9))
logger = logging.getLogger("money_mani.pipeline.correlation_logger")


class CorrelationLogger:
    """Log correlations between intel predictions and ensemble signals.

    Runs as independent scheduled job at 18:00 KST.
    Reads from DB only - zero imports from pipeline.daily_scan or pipeline.market_intel.
    """

    def run(self) -> dict:
        """Compare today's intel predictions with today's ensemble signals."""
        today = datetime.now(KST).strftime("%Y-%m-%d")
        logger.info(f"=== Correlation Logger Started ({today}) ===")

        with get_db() as conn:
            # Get today's intel issues with their affected tickers
            intel_rows = conn.execute(
                """SELECT id, affected_tickers_json, confidence
                   FROM market_intel_issues
                   WHERE detection_date = ?""",
                (today,),
            ).fetchall()

            # Get today's ensemble signals (consensus signals saved by daily_scan)
            signal_rows = conn.execute(
                """SELECT id, ticker, signal_type
                   FROM signals
                   WHERE DATE(detected_at) = ?""",
                (today,),
            ).fetchall()

        if not intel_rows and not signal_rows:
            logger.info("No intel issues or signals today")
            return {"date": today, "correlations": 0}

        # Build signal map: ticker -> (signal_id, signal_type)
        signal_map = {}
        for sig in signal_rows:
            signal_map.setdefault(sig["ticker"], (sig["id"], sig["signal_type"]))

        # Build intel map: ticker -> [(issue_id, direction, confidence)]
        intel_map = {}
        for row in intel_rows:
            tickers_json = row["affected_tickers_json"]
            if not tickers_json:
                continue
            try:
                tickers = json.loads(tickers_json)
            except json.JSONDecodeError:
                continue
            for t in tickers:
                code = t.get("ticker", "")
                if code:
                    intel_map.setdefault(code, []).append({
                        "issue_id": row["id"],
                        "direction": t.get("direction", ""),
                        "confidence": row["confidence"] or 0.0,
                    })

        # Find all tickers that appear in either system
        all_tickers = set(signal_map.keys()) | set(intel_map.keys())
        correlations = []

        for ticker in all_tickers:
            sig = signal_map.get(ticker)
            intels = intel_map.get(ticker, [])

            signal_id = sig[0] if sig else None
            ensemble_signal = sig[1] if sig else "none"

            if intels:
                for intel in intels:
                    intel_dir = intel["direction"]
                    # Check direction match
                    matched = 0
                    if ensemble_signal == "BUY" and intel_dir == "up":
                        matched = 1
                    elif ensemble_signal == "SELL" and intel_dir == "down":
                        matched = 1

                    correlations.append({
                        "date": today,
                        "ticker": ticker,
                        "intel_issue_id": intel["issue_id"],
                        "signal_id": signal_id,
                        "ensemble_signal": ensemble_signal,
                        "intel_direction": intel_dir,
                        "intel_confidence": intel["confidence"],
                        "matched": matched,
                    })
            else:
                # Signal exists but no intel coverage
                correlations.append({
                    "date": today,
                    "ticker": ticker,
                    "intel_issue_id": None,
                    "signal_id": signal_id,
                    "ensemble_signal": ensemble_signal,
                    "intel_direction": "none",
                    "intel_confidence": 0.0,
                    "matched": 0,
                })

        # Save to DB
        if correlations:
            with get_db() as conn:
                for c in correlations:
                    conn.execute(
                        """INSERT OR IGNORE INTO intel_signal_correlation
                           (date, ticker, intel_issue_id, signal_id,
                            ensemble_signal, intel_direction, intel_confidence, matched)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (c["date"], c["ticker"], c["intel_issue_id"],
                         c["signal_id"], c["ensemble_signal"], c["intel_direction"],
                         c["intel_confidence"], c["matched"]),
                    )

        logger.info(f"Correlation logger done: {len(correlations)} entries "
                    f"({sum(1 for c in correlations if c['matched'])} matched)")
        return {"date": today, "correlations": len(correlations)}
