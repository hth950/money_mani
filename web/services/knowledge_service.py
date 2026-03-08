"""Knowledge base: persistent insights across sessions."""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from web.db.connection import get_db

KST = timezone(timedelta(hours=9))
MEMORY_MD_PATH = Path(__file__).parent.parent.parent / "MEMORY.md"

logger = logging.getLogger("money_mani.web.services.knowledge")


class KnowledgeService:
    """CRUD + auto-generation for knowledge entries."""

    def add_entry(
        self,
        category: str,
        subject: str,
        content: str,
        tags: list[str] = None,
        source: str = "auto",
    ) -> int:
        """Add a knowledge entry."""
        tags_json = json.dumps(tags, ensure_ascii=False) if tags else None
        now = datetime.now(KST).strftime("%Y-%m-%d")

        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO knowledge_entries
                   (category, subject, content, tags_json, source, valid_from)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (category, subject, content, tags_json, source, now),
            )
            return cursor.lastrowid

    def search(self, query: str, category: str = None, limit: int = 10) -> list[dict]:
        """Search knowledge entries using LIKE."""
        clauses = ["(content LIKE ? OR subject LIKE ?)"]
        params: list = [f"%{query}%", f"%{query}%"]

        if category:
            clauses.append("category = ?")
            params.append(category)

        clauses.append("(valid_until IS NULL OR valid_until >= date('now'))")
        where = " AND ".join(clauses)
        params.append(limit)

        with get_db() as db:
            rows = db.execute(
                f"""SELECT * FROM knowledge_entries
                    WHERE {where}
                    ORDER BY created_at DESC LIMIT ?""",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def get_entries(
        self,
        category: str = None,
        subject: str = None,
        since: str = None,
        limit: int = 50,
    ) -> list[dict]:
        """List knowledge entries with optional filters."""
        clauses = []
        params: list = []

        if category:
            clauses.append("category = ?")
            params.append(category)
        if subject:
            clauses.append("subject = ?")
            params.append(subject)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)

        where = " AND ".join(clauses) if clauses else "1=1"
        params.append(limit)

        with get_db() as db:
            rows = db.execute(
                f"""SELECT * FROM knowledge_entries
                    WHERE {where}
                    ORDER BY created_at DESC LIMIT ?""",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def generate_strategy_insights(self, strategy_name: str = None):
        """Auto-generate insights from strategy_stats and closed positions."""
        with get_db() as db:
            if strategy_name:
                rows = db.execute(
                    "SELECT * FROM strategy_stats WHERE strategy_name = ? AND period = '30d'",
                    (strategy_name,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM strategy_stats WHERE period = '30d' AND total_trades >= 3"
                ).fetchall()
            stats = [dict(r) for r in rows]

        for row in stats:
            name = row["strategy_name"]
            trades = row["total_trades"]
            win_rate = row["win_rate"]
            avg_pnl = row["avg_pnl_pct"]
            avg_hold = row["avg_holding_days"]

            content = (
                f"{name}: 최근 30일 거래 {trades}건, "
                f"승률 {win_rate:.1f}%, 평균 P&L {avg_pnl:+.2f}%, "
                f"평균 보유기간 {avg_hold:.0f}일"
            )

            # Best/worst trade
            best = row.get("best_trade_pnl_pct")
            worst = row.get("worst_trade_pnl_pct")
            if best is not None:
                content += f" | 최고: {best:+.2f}%, 최저: {worst:+.2f}%"

            # Check for existing recent insight
            existing = self.search(f"{name}: 최근 30일", category="strategy_insight", limit=1)
            if existing:
                # Update instead of duplicate
                with get_db() as db:
                    db.execute(
                        "UPDATE knowledge_entries SET content = ?, created_at = datetime('now') WHERE id = ?",
                        (content, existing[0]["id"]),
                    )
            else:
                self.add_entry(
                    category="strategy_insight",
                    subject=name,
                    content=content,
                    tags=[name.lower().replace(" ", "_"), "30d"],
                    source="nightly",
                )

        logger.info(f"Generated insights for {len(stats)} strategies")

    def update_memory_md(self):
        """Write latest insights to MEMORY.md for cross-session context."""
        insights = self.get_entries(category="strategy_insight", limit=20)
        summaries = self.get_entries(category="session_summary", limit=5)

        lines = [
            "# Money Mani - Strategy Knowledge Base",
            f"_Last updated: {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST_",
            "",
            "## Strategy Performance (30d)",
            "",
        ]

        if insights:
            for entry in insights:
                lines.append(f"- {entry['content']}")
        else:
            lines.append("- No strategy performance data yet.")

        lines.extend(["", "## Recent Session Summaries", ""])

        if summaries:
            for entry in summaries:
                lines.append(f"- [{entry.get('created_at', '')}] {entry['content']}")
        else:
            lines.append("- No session summaries yet.")

        lines.extend(["", "## Notes", ""])

        observations = self.get_entries(category="market_observation", limit=5)
        if observations:
            for entry in observations:
                lines.append(f"- {entry['content']}")

        content = "\n".join(lines) + "\n"
        MEMORY_MD_PATH.write_text(content, encoding="utf-8")
        logger.info(f"Updated {MEMORY_MD_PATH}")

    def save_session_summary(self, summary: str) -> int:
        """Store a session summary for cross-session context."""
        return self.add_entry(
            category="session_summary",
            subject="session",
            content=summary,
            source="manual",
        )

    def invalidate_old_entries(self, days: int = 90):
        """Mark entries older than N days as potentially stale."""
        cutoff = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
        with get_db() as db:
            result = db.execute(
                """UPDATE knowledge_entries
                   SET valid_until = date('now')
                   WHERE valid_until IS NULL AND created_at < ?""",
                (cutoff,),
            )
            if result.rowcount > 0:
                logger.info(f"Invalidated {result.rowcount} entries older than {days} days")
