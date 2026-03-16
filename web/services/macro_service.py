"""Macro monitoring service - snapshots and history."""
import json
import logging
from datetime import datetime, timedelta, timezone

from web.db.connection import get_db

logger = logging.getLogger("money_mani.web.services.macro_service")
KST = timezone(timedelta(hours=9))


class MacroService:

    def save_snapshot(self, macro_result: dict, market: str = "KRX") -> None:
        """Save a macro score snapshot to DB."""
        try:
            details = macro_result.get("details", {})
            community = details.get("community") or {}
            with get_db() as db:
                db.execute("""
                    INSERT INTO macro_snapshots
                    (vix, vix_score, community_score, macro_score, regime,
                     dcinside_posts, fmkorea_posts, post_count, posts_sample_json, market)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    details.get("vix"),
                    details.get("vix_score"),
                    details.get("community_score"),
                    macro_result.get("score"),
                    details.get("regime"),
                    community.get("dcinside_posts"),
                    community.get("fmkorea_posts"),
                    community.get("post_count"),
                    json.dumps(community.get("posts_sample", []), ensure_ascii=False),
                    market,
                ))
        except Exception as e:
            logger.error(f"Failed to save macro snapshot: {e}")

    def get_current(self, market: str = "KRX") -> dict:
        """Get latest macro snapshot."""
        try:
            with get_db() as db:
                row = db.execute("""
                    SELECT * FROM macro_snapshots
                    WHERE market = ?
                    ORDER BY snapshot_at DESC LIMIT 1
                """, (market,)).fetchone()
            if row:
                d = dict(row)
                if d.get("posts_sample_json"):
                    d["posts_sample"] = json.loads(d["posts_sample_json"])
                return d
        except Exception as e:
            logger.warning(f"Failed to get current macro: {e}")
        return {}

    def get_history(self, hours: int = 48, market: str = "KRX") -> list:
        """Get macro history for last N hours."""
        try:
            since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
            with get_db() as db:
                rows = db.execute("""
                    SELECT snapshot_at, vix, vix_score, community_score,
                           macro_score, regime, post_count, market
                    FROM macro_snapshots
                    WHERE market = ? AND snapshot_at >= ?
                    ORDER BY snapshot_at ASC
                """, (market, since)).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"Failed to get macro history: {e}")
            return []

    def get_community_posts(self, market: str = "KRX") -> dict:
        """Get latest community posts sample."""
        try:
            with get_db() as db:
                row = db.execute("""
                    SELECT snapshot_at, posts_sample_json, dcinside_posts,
                           fmkorea_posts, post_count, community_score
                    FROM macro_snapshots
                    WHERE market = ? AND posts_sample_json IS NOT NULL
                    ORDER BY snapshot_at DESC LIMIT 1
                """, (market,)).fetchone()
            if row:
                d = dict(row)
                d["posts_sample"] = json.loads(d.get("posts_sample_json") or "[]")
                return d
        except Exception as e:
            logger.warning(f"Failed to get community posts: {e}")
        return {}

    def trigger_refresh(self) -> dict:
        """Trigger a fresh macro score computation and save snapshot."""
        try:
            from scoring.data_collectors import MacroCollector
            result = MacroCollector().score(market="KRX")
            self.save_snapshot(result, market="KRX")
            return {"status": "ok", "score": result.get("score"), "details": result.get("details")}
        except Exception as e:
            logger.error(f"Macro refresh failed: {e}")
            return {"status": "error", "error": str(e)}
