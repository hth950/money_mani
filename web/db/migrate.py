"""Migrate YAML strategies to SQLite (one-way import on startup)."""
import json
import logging
from strategy.registry import StrategyRegistry
from web.db.connection import get_db

logger = logging.getLogger("money_mani.web.db.migrate")


def _decision_events_create_sql(table_name: str, *, if_not_exists: bool = False) -> str:
    """Return the canonical decision-event table DDL for migrations/rebuilds."""
    exists_clause = "IF NOT EXISTS " if if_not_exists else ""
    table = _quote_identifier(table_name)
    return f"""CREATE TABLE {exists_clause}{table} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scoring_result_id INTEGER REFERENCES scoring_results(id) ON DELETE SET NULL,
    signal_id INTEGER REFERENCES signals(id) ON DELETE SET NULL,
    signal_price REAL,
    ticker TEXT NOT NULL,
    ticker_name TEXT,
    market TEXT NOT NULL,
    signal_action TEXT,
    recommendation TEXT,
    execution_state TEXT,
    scan_date TEXT NOT NULL,
    detected_at TEXT DEFAULT (datetime('now')),
    composite_score REAL,
    score_breakdown_json TEXT,
    score_details_json TEXT,
    weights_used_json TEXT,
    consensus_count INTEGER,
    consensus_strategies_json TEXT,
    block_reason TEXT,
    opportunity_decision TEXT,
    risk_score REAL,
    risk_level TEXT,
    risk_breakdown_json TEXT,
    risk_snapshot_json TEXT,
    recommendation_tier TEXT,
    hard_block_reason TEXT,
    risk_model_version TEXT,
    provenance_json TEXT,
    data_quality_json TEXT,
    execution_error TEXT,
    created_at TEXT DEFAULT (datetime('now'))
 )"""


def _rebuild_table_fk(db, table_name: str, create_sql: str) -> None:
    """Recreate a table with corrected FK references (rename → create → copy → drop)."""
    old_name = f"{table_name}_fk_rebuild"
    db.execute(f"ALTER TABLE {table_name} RENAME TO {old_name}")
    db.execute(create_sql)
    db.execute(f"INSERT INTO {table_name} SELECT * FROM {old_name}")
    db.execute(f"DROP TABLE {old_name}")


def _quote_identifier(name: str) -> str:
    """Quote a SQLite identifier without allowing it to become SQL syntax."""
    return '"' + name.replace('"', '""') + '"'


def _repair_table_foreign_keys(db, table_name: str, target_table: str, replacement: str) -> bool:
    """Rebuild a table whose FK target was rewritten to a temporary table name.

    SQLite rewrites FK clauses when a referenced table is renamed.  The strategy
    status migration therefore left ``positions`` and ``signal_performance``
    pointing at ``signals_old``.  This helper clones the live table definition,
    replaces only the broken target, and preserves user data, indexes, and
    triggers.  It is intentionally data-preserving and idempotent: callers only
    invoke it when the broken target is actually present.
    """
    table_row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    if not table_row or not table_row["sql"]:
        return False

    create_sql = table_row["sql"]
    if target_table not in create_sql:
        return False

    # Capture auxiliary objects before renaming.  Index names remain attached to
    # the renamed table, so they must be dropped before recreating the new one.
    index_rows = db.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table_name,),
    ).fetchall()
    trigger_rows = db.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='trigger' AND tbl_name=? AND sql IS NOT NULL",
        (table_name,),
    ).fetchall()

    old_name = f"{table_name}_signal_fk_rebuild"
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (old_name,)
    ).fetchone():
        raise RuntimeError(f"stale migration table exists: {old_name}")

    for row in index_rows:
        db.execute(f"DROP INDEX {_quote_identifier(row['name'])}")
    for row in trigger_rows:
        db.execute(f"DROP TRIGGER {_quote_identifier(row['name'])}")

    db.execute(f"ALTER TABLE {_quote_identifier(table_name)} RENAME TO {_quote_identifier(old_name)}")
    db.execute(create_sql.replace(target_table, replacement))

    columns = [row["name"] for row in db.execute(f"PRAGMA table_info({_quote_identifier(table_name)})").fetchall()]
    if not columns:
        raise RuntimeError(f"rebuilt table has no columns: {table_name}")
    column_sql = ", ".join(_quote_identifier(column) for column in columns)
    db.execute(
        f"INSERT INTO {_quote_identifier(table_name)} ({column_sql}) "
        f"SELECT {column_sql} FROM {_quote_identifier(old_name)}"
    )
    db.execute(f"DROP TABLE {_quote_identifier(old_name)}")

    for row in index_rows:
        db.execute(row["sql"])
    for row in trigger_rows:
        db.execute(row["sql"])
    return True


def _repair_signal_foreign_keys(db) -> int:
    """Repair all known tables that still reference ``signals_old``."""
    broken_tables = []
    for table_name in ("positions", "signal_performance"):
        rows = db.execute(f"PRAGMA foreign_key_list({_quote_identifier(table_name)})").fetchall()
        if any(row["table"] == "signals_old" for row in rows):
            broken_tables.append(table_name)

    if not broken_tables:
        return 0

    # PRAGMA foreign_keys cannot change inside a transaction.  The migration is
    # run before the surrounding DB context commits, so explicitly commit the
    # additive DDL before temporarily disabling enforcement.
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        repaired = 0
        for table_name in broken_tables:
            if _repair_table_foreign_keys(db, table_name, "signals_old", "signals"):
                repaired += 1
        db.commit()
    finally:
        db.execute("PRAGMA foreign_keys=ON")
    return repaired


def _ensure_decision_event_foreign_keys(db) -> bool:
    """Add nullable scoring/signal FKs without losing audit or outcome rows.

    SQLite cannot add a foreign key to an existing column.  Build a new table,
    copy by column name, null any already-dangling legacy ids, then swap the
    table while FK enforcement is temporarily disabled.  Unlike renaming the
    old table first, this sequence leaves ``decision_outcomes`` referencing the
    stable ``decision_events`` name.
    """
    table_exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_events'"
    ).fetchone()
    if not table_exists:
        return False

    foreign_keys = {
        (row["from"], row["table"], (row["on_delete"] or "").upper())
        for row in db.execute("PRAGMA foreign_key_list(decision_events)").fetchall()
    }
    expected = {
        ("scoring_result_id", "scoring_results", "SET NULL"),
        ("signal_id", "signals", "SET NULL"),
    }
    if expected <= foreign_keys:
        return False

    indexes = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND tbl_name='decision_events' AND sql IS NOT NULL"
    ).fetchall()
    triggers = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name='decision_events' AND sql IS NOT NULL"
    ).fetchall()
    new_table = "decision_events_fk_new"
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (new_table,)
    ).fetchone():
        raise RuntimeError(f"stale migration table exists: {new_table}")

    columns = (
        "id", "scoring_result_id", "signal_id", "signal_price", "ticker",
        "ticker_name", "market", "signal_action", "recommendation",
        "execution_state", "scan_date", "detected_at", "composite_score",
        "score_breakdown_json", "score_details_json", "weights_used_json",
        "consensus_count", "consensus_strategies_json", "block_reason",
        "opportunity_decision", "risk_score", "risk_level",
        "risk_breakdown_json", "recommendation_tier", "hard_block_reason",
        "risk_model_version",
        "provenance_json", "data_quality_json", "execution_error", "created_at",
    )
    column_sql = ", ".join(_quote_identifier(column) for column in columns)
    select_columns = [
        _quote_identifier(column) for column in columns
    ]
    select_columns[1] = (
        "CASE WHEN scoring_result_id IS NULL OR EXISTS "
        "(SELECT 1 FROM scoring_results sr WHERE sr.id=decision_events.scoring_result_id) "
        "THEN scoring_result_id ELSE NULL END"
    )
    select_columns[2] = (
        "CASE WHEN signal_id IS NULL OR EXISTS "
        "(SELECT 1 FROM signals s WHERE s.id=decision_events.signal_id) "
        "THEN signal_id ELSE NULL END"
    )

    # PRAGMA foreign_keys cannot be changed inside the transaction opened by
    # preceding additive migrations.
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(_decision_events_create_sql(new_table))
        db.execute(
            f"INSERT INTO {_quote_identifier(new_table)} ({column_sql}) "
            f"SELECT {', '.join(select_columns)} FROM decision_events"
        )
        db.execute("DROP TABLE decision_events")
        db.execute(
            f"ALTER TABLE {_quote_identifier(new_table)} RENAME TO decision_events"
        )
        for row in indexes:
            db.execute(row["sql"])
        for row in triggers:
            db.execute(row["sql"])
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")
    return True


def _assert_foreign_key_integrity(db) -> None:
    """Fail startup rather than silently operating on an inconsistent ledger."""
    violations = db.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        preview = [tuple(row) for row in violations[:5]]
        raise RuntimeError(
            f"SQLite foreign-key integrity check failed: {len(violations)} violations; "
            f"first={preview}"
        )


def run_schema_migrations():
    """Run additive schema migrations (safe to call repeatedly)."""
    migrations = [
        ("backtest_results_avg_holding_days",
         "ALTER TABLE backtest_results ADD COLUMN avg_holding_days REAL"),
        ("backtest_results_annual_trade_rate",
         "ALTER TABLE backtest_results ADD COLUMN annual_trade_rate REAL"),
        ("backtest_results_validation_policy",
         "ALTER TABLE backtest_results ADD COLUMN validation_policy_json TEXT"),
        ("walk_forward_results_table",
         """CREATE TABLE IF NOT EXISTS walk_forward_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    ticker TEXT NOT NULL,
    market TEXT DEFAULT 'KRX',
    total_windows INTEGER,
    valid_windows INTEGER,
    avg_train_sharpe REAL,
    avg_test_sharpe REAL,
    sharpe_degradation REAL,
    is_overfit INTEGER DEFAULT 0,
    overfit_reason TEXT,
    windows_json TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    train_days INTEGER,
    test_days INTEGER,
    step_days INTEGER,
    overfit_threshold REAL,
    min_windows INTEGER
 )"""),
        ("walk_forward_results_train_days",
         "ALTER TABLE walk_forward_results ADD COLUMN train_days INTEGER"),
        ("walk_forward_results_test_days",
         "ALTER TABLE walk_forward_results ADD COLUMN test_days INTEGER"),
        ("walk_forward_results_step_days",
         "ALTER TABLE walk_forward_results ADD COLUMN step_days INTEGER"),
        ("walk_forward_results_overfit_threshold",
         "ALTER TABLE walk_forward_results ADD COLUMN overfit_threshold REAL"),
        ("walk_forward_results_min_windows",
         "ALTER TABLE walk_forward_results ADD COLUMN min_windows INTEGER"),
        ("walk_forward_results_strategy_index",
         "CREATE INDEX IF NOT EXISTS idx_wf_strategy "
         "ON walk_forward_results (strategy_name, created_at)"),
        ("walk_forward_results_overfit_index",
         "CREATE INDEX IF NOT EXISTS idx_wf_overfit "
         "ON walk_forward_results (is_overfit, created_at)"),
        ("walk_forward_results_strategy_ticker_index",
         "CREATE INDEX IF NOT EXISTS idx_wf_strategy_ticker "
         "ON walk_forward_results (strategy_name, market, ticker, created_at)"),
        ("scoring_results_ticker_name",
         "ALTER TABLE scoring_results ADD COLUMN ticker_name TEXT"),
        ("scoring_results_macro_score",
         "ALTER TABLE scoring_results ADD COLUMN macro_score REAL"),
        ("scoring_results_exit_score",
         "ALTER TABLE scoring_results ADD COLUMN exit_score REAL"),
        ("scoring_results_exit_decision",
         "ALTER TABLE scoring_results ADD COLUMN exit_decision TEXT"),
        ("scoring_results_source",
         "ALTER TABLE scoring_results ADD COLUMN source TEXT DEFAULT 'live'"),
        ("scoring_results_opportunity_decision",
         "ALTER TABLE scoring_results ADD COLUMN opportunity_decision TEXT"),
        ("scoring_results_risk_score",
         "ALTER TABLE scoring_results ADD COLUMN risk_score REAL"),
        ("scoring_results_risk_level",
         "ALTER TABLE scoring_results ADD COLUMN risk_level TEXT"),
        ("scoring_results_risk_breakdown",
         "ALTER TABLE scoring_results ADD COLUMN risk_breakdown_json TEXT"),
        ("scoring_results_recommendation_tier",
         "ALTER TABLE scoring_results ADD COLUMN recommendation_tier TEXT"),
        ("scoring_results_hard_block_reason",
         "ALTER TABLE scoring_results ADD COLUMN hard_block_reason TEXT"),
        ("scoring_results_risk_model_version",
         "ALTER TABLE scoring_results ADD COLUMN risk_model_version TEXT"),
        ("macro_snapshots_table",
         """CREATE TABLE IF NOT EXISTS macro_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at TEXT DEFAULT (datetime('now')),
    vix REAL, vix_score REAL, community_score REAL,
    macro_score REAL, regime TEXT,
    dcinside_posts INTEGER, fmkorea_posts INTEGER,
    post_count INTEGER, posts_sample_json TEXT,
    market TEXT DEFAULT 'KRX'
 )"""),
        ("macro_snapshots_llm_comment",
         "ALTER TABLE macro_snapshots ADD COLUMN llm_comment TEXT"),
        ("decision_events_table",
         _decision_events_create_sql("decision_events", if_not_exists=True)),
        ("decision_events_signal_price",
         "ALTER TABLE decision_events ADD COLUMN signal_price REAL"),
        ("decision_events_opportunity_decision",
         "ALTER TABLE decision_events ADD COLUMN opportunity_decision TEXT"),
        ("decision_events_risk_score",
         "ALTER TABLE decision_events ADD COLUMN risk_score REAL"),
        ("decision_events_risk_level",
         "ALTER TABLE decision_events ADD COLUMN risk_level TEXT"),
        ("decision_events_risk_breakdown",
         "ALTER TABLE decision_events ADD COLUMN risk_breakdown_json TEXT"),
        ("decision_events_risk_snapshot",
         "ALTER TABLE decision_events ADD COLUMN risk_snapshot_json TEXT"),
        ("decision_events_recommendation_tier",
         "ALTER TABLE decision_events ADD COLUMN recommendation_tier TEXT"),
        ("decision_events_hard_block_reason",
         "ALTER TABLE decision_events ADD COLUMN hard_block_reason TEXT"),
        ("decision_events_risk_model_version",
         "ALTER TABLE decision_events ADD COLUMN risk_model_version TEXT"),
        ("paper_trades_risk_score",
         "ALTER TABLE paper_trades ADD COLUMN risk_score REAL"),
        ("paper_trades_risk_snapshot",
         "ALTER TABLE paper_trades ADD COLUMN risk_snapshot_json TEXT"),
        ("paper_trades_risk_acknowledged_at",
         "ALTER TABLE paper_trades ADD COLUMN risk_acknowledged_at TEXT"),
        ("paper_trades_risk_acknowledgement_version",
         "ALTER TABLE paper_trades ADD COLUMN risk_acknowledgement_version TEXT"),
        ("paper_trades_risk_snapshot_hash",
         "ALTER TABLE paper_trades ADD COLUMN risk_snapshot_hash TEXT"),
        ("decision_events_ticker_date_index",
         "CREATE INDEX IF NOT EXISTS idx_decision_events_ticker_date "
         "ON decision_events (ticker, scan_date, created_at)"),
        ("decision_events_recommendation_index",
         "CREATE INDEX IF NOT EXISTS idx_decision_events_recommendation "
         "ON decision_events (recommendation, created_at)"),
        ("decision_events_execution_state_index",
         "CREATE INDEX IF NOT EXISTS idx_decision_events_execution_state "
         "ON decision_events (execution_state, created_at)"),
        ("decision_events_scoring_result_index",
         "CREATE INDEX IF NOT EXISTS idx_decision_events_scoring_result "
         "ON decision_events (scoring_result_id)"),
        ("decision_events_signal_index",
         "CREATE INDEX IF NOT EXISTS idx_decision_events_signal "
         "ON decision_events (signal_id)"),
        ("decision_outcomes_table",
         """CREATE TABLE IF NOT EXISTS decision_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_event_id INTEGER NOT NULL REFERENCES decision_events(id) ON DELETE CASCADE,
    horizon_days INTEGER NOT NULL CHECK (horizon_days IN (1, 5, 10, 20)),
    status TEXT NOT NULL CHECK (status IN ('pending', 'evaluated', 'unavailable', 'invalid')) DEFAULT 'pending',
    entry_date TEXT,
    exit_date TEXT,
    entry_price REAL,
    exit_price REAL,
    raw_return_pct REAL,
    benchmark_return_pct REAL,
    excess_return_pct REAL,
    transaction_cost_pct REAL DEFAULT 0.0,
    net_return_pct REAL,
    mfe_pct REAL,
    mae_pct REAL,
    price_source TEXT,
    benchmark_source TEXT,
    label_source TEXT DEFAULT 'decision_outcome_labeler',
    reason TEXT,
    observed_at TEXT,
    evaluated_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE (decision_event_id, horizon_days)
 )"""),
        ("decision_outcomes_event_index",
         "CREATE INDEX IF NOT EXISTS idx_decision_outcomes_event "
         "ON decision_outcomes (decision_event_id)"),
        ("decision_outcomes_status_index",
         "CREATE INDEX IF NOT EXISTS idx_decision_outcomes_status "
         "ON decision_outcomes (status, horizon_days)"),
        ("decision_outcomes_entry_date_index",
         "CREATE INDEX IF NOT EXISTS idx_decision_outcomes_entry_date "
         "ON decision_outcomes (entry_date)"),
        ("app_users_table",
         """CREATE TABLE IF NOT EXISTS app_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('owner', 'viewer')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    password_changed_at TEXT NOT NULL,
    last_login_at TEXT
 )"""),
        ("app_users_active_role_index",
         "CREATE INDEX IF NOT EXISTS idx_app_users_active_role "
         "ON app_users (is_active, role)"),
        ("app_sessions_table",
         """CREATE TABLE IF NOT EXISTS app_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    csrf_token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    client_ip TEXT,
    user_agent TEXT
 )"""),
        ("app_sessions_user_active_index",
         "CREATE INDEX IF NOT EXISTS idx_app_sessions_user_active "
         "ON app_sessions (user_id, revoked_at, expires_at)"),
        ("app_sessions_expiry_index",
         "CREATE INDEX IF NOT EXISTS idx_app_sessions_expiry "
         "ON app_sessions (expires_at, revoked_at)"),
        ("auth_audit_events_table",
         """CREATE TABLE IF NOT EXISTS auth_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES app_users(id) ON DELETE SET NULL,
    username TEXT COLLATE NOCASE,
    event_type TEXT NOT NULL,
    ip_address TEXT,
    user_agent TEXT,
    detail_json TEXT,
    created_at TEXT NOT NULL
 )"""),
        ("auth_audit_username_event_time_index",
         "CREATE INDEX IF NOT EXISTS idx_auth_audit_username_event_time "
         "ON auth_audit_events (username, event_type, created_at DESC)"),
        ("auth_audit_ip_event_time_index",
         "CREATE INDEX IF NOT EXISTS idx_auth_audit_ip_event_time "
         "ON auth_audit_events (ip_address, event_type, created_at DESC)"),
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
                        created_at TEXT DEFAULT (datetime('now')),
                        avg_holding_days REAL, annual_trade_rate REAL,
                        validation_policy_json TEXT
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

        repaired = _repair_signal_foreign_keys(db)
        if repaired:
            logger.info("Repaired broken signal foreign keys in %d table(s).", repaired)
        if _ensure_decision_event_foreign_keys(db):
            logger.info("Added nullable foreign keys to decision_events.")
        _assert_foreign_key_integrity(db)

def migrate_yaml_strategies():
    """Import strategies from config/strategies/*.yaml into SQLite if not already present."""
    registry = StrategyRegistry()
    names = registry.list_strategies()
    if not names:
        logger.info("No YAML strategies found to migrate.")
        return

    with get_db() as db:
        existing = {row["name"] for row in db.execute("SELECT name FROM strategies").fetchall()}
        existing_before_import = len(existing)
        imported = 0
        for registry_name in names:
            try:
                # Registry entries are YAML filenames, while the database key is
                # the strategy's internal ``name``.  Load first and compare the
                # canonical name so alias filenames remain idempotent.
                strat = registry.load(registry_name)
                if strat.name in existing:
                    continue
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
                existing.add(strat.name)
                imported += 1
                logger.info(f"Imported strategy: {strat.name}")
            except Exception as e:
                logger.warning(f"Failed to import strategy '{registry_name}': {e}")
        logger.info(
            "Migration complete: %d new strategies imported (%d already existed)",
            imported,
            existing_before_import,
        )
