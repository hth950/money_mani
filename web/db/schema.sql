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

-- Signal performance tracking: records every signal + closing price + P&L
CREATE TABLE IF NOT EXISTS signal_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id) ON DELETE SET NULL,
    strategy_name TEXT,
    ticker TEXT,
    ticker_name TEXT,
    market TEXT,
    signal_type TEXT CHECK(signal_type IN ('BUY', 'SELL')),
    signal_price REAL,
    close_price REAL,
    pnl_amount REAL,
    pnl_pct REAL,
    signal_date TEXT,
    evaluated_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sigperf_date ON signal_performance (signal_date);
CREATE INDEX IF NOT EXISTS idx_sigperf_ticker ON signal_performance (ticker);
CREATE INDEX IF NOT EXISTS idx_sigperf_strategy ON signal_performance (strategy_name);

-- Daily/weekly performance reports
CREATE TABLE IF NOT EXISTS performance_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT,
    report_type TEXT CHECK(report_type IN ('daily', 'weekly')),
    total_signals INTEGER,
    buy_signals INTEGER,
    sell_signals INTEGER,
    avg_pnl_pct REAL,
    total_pnl_pct REAL,
    best_pnl_pct REAL,
    worst_pnl_pct REAL,
    win_count INTEGER,
    lose_count INTEGER,
    win_rate REAL,
    details_json TEXT,
    discord_sent INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_perfreport_date ON performance_reports (report_date, report_type);

-- Position tracking: entry-to-exit lifecycle per (strategy, ticker)
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    ticker TEXT NOT NULL,
    ticker_name TEXT,
    market TEXT DEFAULT 'KRX',
    status TEXT CHECK(status IN ('open', 'closed')) DEFAULT 'open',
    entry_signal_id INTEGER REFERENCES signals(id),
    entry_date TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_signal_id INTEGER REFERENCES signals(id),
    exit_date TEXT,
    exit_price REAL,
    holding_days INTEGER,
    max_holding_days INTEGER DEFAULT 30,
    pnl_amount REAL,
    pnl_pct REAL,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_unique_open
    ON positions (strategy_name, ticker) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_positions_strategy ON positions (strategy_name);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status);

-- Strategy analytics: aggregated performance stats
CREATE TABLE IF NOT EXISTS strategy_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    period TEXT NOT NULL,
    total_trades INTEGER DEFAULT 0,
    winning_trades INTEGER DEFAULT 0,
    losing_trades INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    total_pnl_pct REAL DEFAULT 0,
    avg_pnl_pct REAL DEFAULT 0,
    best_trade_pnl_pct REAL,
    worst_trade_pnl_pct REAL,
    avg_holding_days REAL DEFAULT 0,
    computed_at TEXT DEFAULT (datetime('now')),
    UNIQUE(strategy_name, period)
);

CREATE INDEX IF NOT EXISTS idx_stratstats_name ON strategy_stats (strategy_name);

-- Knowledge base: persistent insights across sessions
CREATE TABLE IF NOT EXISTS knowledge_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    subject TEXT,
    content TEXT NOT NULL,
    tags_json TEXT,
    source TEXT,
    valid_from TEXT,
    valid_until TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_knowledge_category ON knowledge_entries (category);
CREATE INDEX IF NOT EXISTS idx_knowledge_subject ON knowledge_entries (subject);

-- Market intelligence: LLM web search scan executions
CREATE TABLE IF NOT EXISTS market_intel_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_time TEXT NOT NULL,
    scan_type TEXT NOT NULL,
    model_used TEXT,
    raw_response TEXT,
    issues_count INTEGER DEFAULT 0,
    tickers_count INTEGER DEFAULT 0,
    status TEXT CHECK(status IN ('success', 'partial', 'failed')) DEFAULT 'success',
    error_message TEXT,
    discord_sent INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_intel_scan_time ON market_intel_scans (scan_time);
CREATE INDEX IF NOT EXISTS idx_intel_scan_created ON market_intel_scans (created_at);

-- Market intelligence: detected issues with affected tickers and price tracking
CREATE TABLE IF NOT EXISTS market_intel_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER REFERENCES market_intel_scans(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    summary TEXT,
    category TEXT,
    sentiment TEXT CHECK(sentiment IN ('positive', 'negative', 'neutral', 'mixed')),
    confidence REAL DEFAULT 0.0,
    source_info TEXT,
    affected_tickers_json TEXT,
    price_at_detection_json TEXT,
    price_after_1d_json TEXT,
    price_after_3d_json TEXT,
    price_after_5d_json TEXT,
    accuracy_score REAL,
    detection_date TEXT,
    content_hash TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_intel_issue_scan ON market_intel_issues (scan_id);
CREATE INDEX IF NOT EXISTS idx_intel_issue_date ON market_intel_issues (detection_date);
CREATE INDEX IF NOT EXISTS idx_intel_issue_category ON market_intel_issues (category);
CREATE INDEX IF NOT EXISTS idx_intel_issue_hash ON market_intel_issues (content_hash);

-- Intel-signal correlation tracking
CREATE TABLE IF NOT EXISTS intel_signal_correlation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    intel_issue_id INTEGER REFERENCES market_intel_issues(id),
    signal_id INTEGER,
    ensemble_signal TEXT,
    intel_direction TEXT,
    intel_confidence REAL,
    actual_1d_change REAL,
    matched INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_correlation_date ON intel_signal_correlation (date);
CREATE INDEX IF NOT EXISTS idx_correlation_ticker ON intel_signal_correlation (ticker);
