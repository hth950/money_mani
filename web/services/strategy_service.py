"""CRUD service for strategies: SQLite with YAML write-through."""
from __future__ import annotations

import json

from web.db.connection import get_db
from strategy.models import Strategy
from strategy.registry import StrategyRegistry


class StrategyService:
    def __init__(self):
        self._registry = StrategyRegistry()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _row_to_dict(self, row) -> dict:
        """Convert a sqlite3.Row to a plain dict with JSON fields decoded."""
        d = dict(row)
        for field in ("rules_json", "indicators_json", "parameters_json", "backtest_results_json"):
            raw = d.pop(field, None)
            key = field.replace("_json", "")  # e.g. rules_json -> rules
            d[key] = json.loads(raw) if raw else ({} if key != "indicators" else [])
        return d

    def _sync_to_yaml(self, row: dict) -> None:
        """Write a strategy row to YAML via StrategyRegistry."""
        strat = Strategy(
            name=row["name"],
            description=row.get("description") or "",
            source=row.get("source") or "",
            category=row.get("category") or "",
            status=row.get("status") or "draft",
            rules=row.get("rules") or {},
            indicators=row.get("indicators") or [],
            parameters=row.get("parameters") or {},
            backtest_results=row.get("backtest_results"),
        )
        self._registry.save_strategy(strat)

    # ------------------------------------------------------------------
    # Public CRUD methods
    # ------------------------------------------------------------------

    def list_all(self, status: str | None = None, category: str | None = None) -> list[dict]:
        """Return all strategies, with optional status/category filters."""
        sql = "SELECT * FROM strategies WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if category:
            sql += " AND category = ?"
            params.append(category)
        sql += " ORDER BY created_at DESC"

        with get_db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_by_id(self, id: int) -> dict | None:
        """Return a single strategy by primary key, or None."""
        with get_db() as conn:
            row = conn.execute("SELECT * FROM strategies WHERE id = ?", (id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def get_by_name(self, name: str) -> dict | None:
        """Return a single strategy by name, or None."""
        with get_db() as conn:
            row = conn.execute("SELECT * FROM strategies WHERE name = ?", (name,)).fetchone()
        return self._row_to_dict(row) if row else None

    def create(self, data: dict) -> int:
        """Insert a new strategy into SQLite and write its YAML. Returns new id."""
        rules_json = json.dumps(data.get("rules") or {})
        indicators_json = json.dumps(data.get("indicators") or [])
        parameters_json = json.dumps(data.get("parameters") or {})
        backtest_results_json = (
            json.dumps(data["backtest_results"]) if data.get("backtest_results") else None
        )

        sql = """
            INSERT INTO strategies
                (name, description, source, category, status,
                 rules_json, indicators_json, parameters_json, backtest_results_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with get_db() as conn:
            cur = conn.execute(
                sql,
                (
                    data["name"],
                    data.get("description", ""),
                    data.get("source", ""),
                    data.get("category", ""),
                    data.get("status", "draft"),
                    rules_json,
                    indicators_json,
                    parameters_json,
                    backtest_results_json,
                ),
            )
            new_id = cur.lastrowid

        # Write-through to YAML
        row = {
            "name": data["name"],
            "description": data.get("description", ""),
            "source": data.get("source", ""),
            "category": data.get("category", ""),
            "status": data.get("status", "draft"),
            "rules": data.get("rules") or {},
            "indicators": data.get("indicators") or [],
            "parameters": data.get("parameters") or {},
            "backtest_results": data.get("backtest_results"),
        }
        self._sync_to_yaml(row)
        return new_id

    def update(self, id: int, data: dict) -> bool:
        """Apply partial update to a strategy. Returns True if a row was updated."""
        existing = self.get_by_id(id)
        if existing is None:
            return False

        # Merge incoming fields over existing values
        merged = {**existing, **{k: v for k, v in data.items() if v is not None}}

        sql = """
            UPDATE strategies SET
                description = ?,
                category = ?,
                status = ?,
                rules_json = ?,
                indicators_json = ?,
                parameters_json = ?,
                updated_at = datetime('now')
            WHERE id = ?
        """
        with get_db() as conn:
            cur = conn.execute(
                sql,
                (
                    merged.get("description", ""),
                    merged.get("category", ""),
                    merged.get("status", "draft"),
                    json.dumps(merged.get("rules") or {}),
                    json.dumps(merged.get("indicators") or []),
                    json.dumps(merged.get("parameters") or {}),
                    id,
                ),
            )
            updated = cur.rowcount > 0

        if updated:
            self._sync_to_yaml(merged)
        return updated

    def delete(self, id: int) -> bool:
        """Delete a strategy from SQLite and remove its YAML file. Returns True if deleted."""
        existing = self.get_by_id(id)
        if existing is None:
            return False

        with get_db() as conn:
            cur = conn.execute("DELETE FROM strategies WHERE id = ?", (id,))
            deleted = cur.rowcount > 0

        if deleted:
            # Remove YAML file if it exists
            yaml_path = self._registry._dir / f"{existing['name']}.yaml"
            if yaml_path.exists():
                yaml_path.unlink()
        return deleted
