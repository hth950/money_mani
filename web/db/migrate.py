"""Migrate YAML strategies to SQLite (one-way import on startup)."""
import json
import logging
from strategy.registry import StrategyRegistry
from web.db.connection import get_db

logger = logging.getLogger("money_mani.web.db.migrate")


def run_schema_migrations():
    """Run additive schema migrations (safe to call repeatedly)."""
    migrations = [
        ("scoring_results_ticker_name",
         "ALTER TABLE scoring_results ADD COLUMN ticker_name TEXT"),
    ]
    with get_db() as db:
        for name, sql in migrations:
            try:
                db.execute(sql)
                logger.info(f"Migration applied: {name}")
            except Exception:
                pass  # Column already exists

def migrate_yaml_strategies():
    """Import strategies from config/strategies/*.yaml into SQLite if not already present."""
    registry = StrategyRegistry()
    names = registry.list_strategies()
    if not names:
        logger.info("No YAML strategies found to migrate.")
        return

    with get_db() as db:
        existing = {row["name"] for row in db.execute("SELECT name FROM strategies").fetchall()}
        imported = 0
        for name in names:
            if name in existing:
                continue
            try:
                strat = registry.load(name)
                db.execute(
                    """INSERT INTO strategies (name, description, source, category, status,
                       rules_json, indicators_json, parameters_json, backtest_results_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        strat.name,
                        strat.description,
                        strat.source,
                        strat.category,
                        strat.status,
                        json.dumps(strat.rules, ensure_ascii=False),
                        json.dumps(strat.indicators, ensure_ascii=False),
                        json.dumps(strat.parameters, ensure_ascii=False),
                        json.dumps(strat.backtest_results, ensure_ascii=False) if strat.backtest_results else None,
                    ),
                )
                imported += 1
                logger.info(f"Imported strategy: {strat.name}")
            except Exception as e:
                logger.warning(f"Failed to import strategy '{name}': {e}")
        logger.info(f"Migration complete: {imported} new strategies imported ({len(existing)} already existed)")
