PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    source TEXT,
    category TEXT,
    status TEXT CHECK(status IN ('draft', 'testing', 'validated', 'retired')),
    rules_json TEXT,
    indicators_json TEXT,
    parameters_json TEXT,
    backtest_results_json TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS backtest_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER REFERENCES strategies(id) ON DELETE CASCADE,
    strategy_name TEXT,
    ticker TEXT,
    market TEXT DEFAULT 'KRX',
    period TEXT,
    total_return REAL,
    sharpe_ratio REAL,
    max_drawdown REAL,
    win_rate REAL,
    num_trades INTEGER,
    is_valid INTEGER,
    trades_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_backtest_strategy_date ON backtest_results (strategy_name, created_at);
CREATE INDEX IF NOT EXISTS idx_backtest_ticker ON backtest_results (ticker);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER REFERENCES strategies(id) ON DELETE SET NULL,
    strategy_name TEXT,
    ticker TEXT,
    ticker_name TEXT,
    market TEXT,
    signal_type TEXT CHECK(signal_type IN ('BUY', 'SELL')),
    price REAL,
    indicators_json TEXT,
    source TEXT DEFAULT 'daily_scan',
    detected_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_signals_detected_at ON signals (detected_at);
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals (ticker);

CREATE TABLE IF NOT EXISTS discovery_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT,
    market TEXT,
    queries_json TEXT,
    videos_found INTEGER,
    strategies_extracted INTEGER,
    strategies_ranked INTEGER,
    strategies_validated INTEGER,
    rankings_json TEXT,
    trends_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_date TEXT,
    signals_count INTEGER,
    markets_open TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    name TEXT,
    market TEXT,
    quantity REAL,
    avg_price REAL,
    current_price REAL,
    pnl_pct REAL,
    snapshot_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_portfolio_ticker_snapshot ON portfolio_snapshots (ticker, snapshot_at);

CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT,
    status TEXT CHECK(status IN ('running', 'success', 'failed')),
    result_summary TEXT,
    error_message TEXT,
    started_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT
);
