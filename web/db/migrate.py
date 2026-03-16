"""Migrate YAML strategies to SQLite (one-way import on startup)."""
import json
import logging
from strategy.registry import StrategyRegistry
from web.db.connection import get_db

logger = logging.getLogger("money_mani.web.db.migrate")


def _rebuild_table_fk(db, table_name: str, create_sql: str) -> None:
    """Recreate a table with corrected FK references (rename → create → copy → drop)."""
    old_name = f"{table_name}_fk_rebuild"
    db.execute(f"ALTER TABLE {table_name} RENAME TO {old_name}")
    db.execute(create_sql)
    db.execute(f"INSERT INTO {table_name} SELECT * FROM {old_name}")
    db.execute(f"DROP TABLE {old_name}")


def run_schema_migrations():
    """Run additive schema migrations (safe to call repeatedly)."""
    migrations = [
        ("scoring_results_ticker_name",
         "ALTER TABLE scoring_results ADD COLUMN ticker_name TEXT"),
        ("scoring_results_macro_score",
         "ALTER TABLE scoring_results ADD COLUMN macro_score REAL"),
    ]
    with get_db() as db:
        for name, sql in migrations:
            try:
                db.execute(sql)
                logger.info(f"Migration applied: {name}")
            except Exception as e:
                if "duplicate column" not in str(e).lower() and "already exists" not in str(e).lower():
                    logger.error(f"Migration failed: {e}")

        # Expand strategies.status CHECK constraint to include validated_v2 / rejected_v2 / archived
        try:
            row = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='strategies'"
            ).fetchone()
            if row and "validated_v2" not in row["sql"]:
                logger.info("Migrating strategies.status CHECK constraint...")
                db.execute("PRAGMA foreign_keys=OFF")
                db.execute("ALTER TABLE strategies RENAME TO strategies_old")
                db.execute("""
                    CREATE TABLE strategies (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        description TEXT,
                        source TEXT,
                        category TEXT,
                        status TEXT CHECK(status IN (
                            'draft','testing','validated','validated_v2',
                            'rejected_v2','archived','retired'
                        )),
                        rules_json TEXT,
                        indicators_json TEXT,
                        parameters_json TEXT,
                        backtest_results_json TEXT,
                        created_at TEXT DEFAULT (datetime('now')),
                        updated_at TEXT DEFAULT (datetime('now'))
                    )
                """)
                db.execute("INSERT INTO strategies SELECT * FROM strategies_old")
                db.execute("DROP TABLE strategies_old")
                # Fix FK references: SQLite auto-updated backtest_results/signals FKs to point
                # to strategies_old when we renamed. Rebuild them to point back to strategies.
                _rebuild_table_fk(db, "backtest_results",
                    """CREATE TABLE backtest_results (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        strategy_id INTEGER REFERENCES strategies(id) ON DELETE CASCADE,
                        strategy_name TEXT, ticker TEXT, market TEXT DEFAULT 'KRX',
                        period TEXT, total_return REAL, sharpe_ratio REAL,
                        max_drawdown REAL, win_rate REAL, num_trades INTEGER,
                        is_valid INTEGER, trades_json TEXT,
                        created_at TEXT DEFAULT (datetime('now'))
                    )""")
                _rebuild_table_fk(db, "signals",
                    """CREATE TABLE signals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        strategy_id INTEGER REFERENCES strategies(id) ON DELETE SET NULL,
                        strategy_name TEXT, ticker TEXT, ticker_name TEXT, market TEXT,
                        signal_type TEXT CHECK(signal_type IN ('BUY', 'SELL')),
                        price REAL, indicators_json TEXT, source TEXT DEFAULT 'daily_scan',
                        detected_at TEXT DEFAULT (datetime('now'))
                    )""")
                db.execute("PRAGMA foreign_keys=ON")
                logger.info("strategies.status CHECK constraint expanded successfully.")
        except Exception as e:
            logger.error(f"strategies status migration failed: {e}")

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
