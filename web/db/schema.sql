PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    source TEXT,
    category TEXT,
    status TEXT CHECK(status IN ('draft', 'testing', 'validated', 'validated_v2', 'rejected_v2', 'archived', 'retired')),
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
    created_at TEXT DEFAULT (datetime('now')),
    avg_holding_days REAL,
    annual_trade_rate REAL,
    validation_policy_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_backtest_strategy_date ON backtest_results (strategy_name, created_at);
CREATE INDEX IF NOT EXISTS idx_backtest_ticker ON backtest_results (ticker);

-- Append-only walk-forward validation evidence.  The CLI and daily scan both
-- use this canonical table instead of creating an ad-hoc schema at runtime.
CREATE TABLE IF NOT EXISTS walk_forward_results (
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
);

CREATE INDEX IF NOT EXISTS idx_wf_strategy
    ON walk_forward_results (strategy_name, created_at);
CREATE INDEX IF NOT EXISTS idx_wf_overfit
    ON walk_forward_results (is_overfit, created_at);
CREATE INDEX IF NOT EXISTS idx_wf_strategy_ticker
    ON walk_forward_results (strategy_name, market, ticker, created_at);

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

-- Multi-layer scoring results (Phase 5)
CREATE TABLE IF NOT EXISTS scoring_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    ticker TEXT NOT NULL,
    ticker_name TEXT,
    market TEXT NOT NULL,
    scan_date TEXT NOT NULL,
    technical_score REAL,
    fundamental_score REAL,
    flow_score REAL,
    intel_score REAL,
    macro_score REAL,
    composite_score REAL,
    score_breakdown_json TEXT,
    decision TEXT,
    block_reason TEXT,
    -- The legacy decision/block reason remain for compatibility.  The fields
    -- below separate score opportunity from the risk of entering now.
    opportunity_decision TEXT,
    risk_score REAL,
    risk_level TEXT,
    risk_breakdown_json TEXT,
    risk_snapshot_json TEXT,
    recommendation_tier TEXT,
    hard_block_reason TEXT,
    risk_model_version TEXT,
    weights_used_json TEXT,
    source TEXT DEFAULT 'live',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scoring_date ON scoring_results (scan_date);
CREATE INDEX IF NOT EXISTS idx_scoring_ticker ON scoring_results (ticker);
CREATE INDEX IF NOT EXISTS idx_scoring_decision ON scoring_results (decision);

-- Append-only decision audit trail.  scoring_results remains the latest-row
-- compatibility view for the UI, and this table preserves every scoring snapshot.
CREATE TABLE IF NOT EXISTS decision_events (
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
    recommendation_tier TEXT,
    hard_block_reason TEXT,
    risk_model_version TEXT,
    provenance_json TEXT,
    data_quality_json TEXT,
    execution_error TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_decision_events_ticker_date
    ON decision_events (ticker, scan_date, created_at);
CREATE INDEX IF NOT EXISTS idx_decision_events_recommendation
    ON decision_events (recommendation, created_at);
CREATE INDEX IF NOT EXISTS idx_decision_events_execution_state
    ON decision_events (execution_state, created_at);
CREATE INDEX IF NOT EXISTS idx_decision_events_scoring_result
    ON decision_events (scoring_result_id);
CREATE INDEX IF NOT EXISTS idx_decision_events_signal
    ON decision_events (signal_id);

-- Forward outcome labels for immutable decision snapshots.  One row is kept
-- per (decision event, trading-day horizon), and pending rows are revisited by the
-- nightly labeler once enough bars have matured.
CREATE TABLE IF NOT EXISTS decision_outcomes (
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
);

CREATE INDEX IF NOT EXISTS idx_decision_outcomes_event
    ON decision_outcomes (decision_event_id);
CREATE INDEX IF NOT EXISTS idx_decision_outcomes_status
    ON decision_outcomes (status, horizon_days);
CREATE INDEX IF NOT EXISTS idx_decision_outcomes_entry_date
    ON decision_outcomes (entry_date);

-- Daily scoring summary (Phase 5)
CREATE TABLE IF NOT EXISTS daily_scoring_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT NOT NULL,
    market TEXT NOT NULL,
    total_signals INTEGER,
    execute_count INTEGER,
    watch_count INTEGER,
    skip_count INTEGER,
    blocked_count INTEGER,
    avg_composite_score REAL,
    top_scores_json TEXT,
    risk_status_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scoring_summary_date ON daily_scoring_summary (report_date);

-- Macro environment snapshots (VIX + community sentiment)
CREATE TABLE IF NOT EXISTS macro_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at TEXT DEFAULT (datetime('now')),
    vix REAL,
    vix_score REAL,
    community_score REAL,
    macro_score REAL,
    regime TEXT,
    dcinside_posts INTEGER,
    fmkorea_posts INTEGER,
    post_count INTEGER,
    posts_sample_json TEXT,
    market TEXT DEFAULT 'KRX'
);

CREATE INDEX IF NOT EXISTS idx_macro_snapshot_at ON macro_snapshots (snapshot_at);
CREATE INDEX IF NOT EXISTS idx_macro_market ON macro_snapshots (market);

-- Manual paper-trading ledger.  This is intentionally independent from both
-- the strategy ``positions`` table and the KIS live-account snapshots.
CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL CHECK(market IN ('KRX', 'US')),
    ticker TEXT NOT NULL,
    ticker_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'closed')) DEFAULT 'open',
    quantity INTEGER NOT NULL CHECK(quantity >= 0),
    avg_price REAL NOT NULL CHECK(avg_price >= 0),
    remaining_cost REAL NOT NULL CHECK(remaining_cost >= 0),
    cumulative_realized_pnl REAL NOT NULL DEFAULT 0,
    total_buy_fees REAL NOT NULL DEFAULT 0,
    total_sell_fees REAL NOT NULL DEFAULT 0,
    opened_at TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at TEXT,
    last_refresh_attempt_at TEXT,
    last_refresh_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_positions_unique_open
    ON paper_positions (market, ticker) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_paper_positions_status
    ON paper_positions (status, market, ticker);

-- Append-only simulated fills.  No code path updates or deletes these rows.
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES paper_positions(id) ON DELETE RESTRICT,
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    market TEXT NOT NULL CHECK(market IN ('KRX', 'US')),
    ticker TEXT NOT NULL,
    ticker_name TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    price REAL NOT NULL CHECK(price > 0),
    gross_amount REAL NOT NULL CHECK(gross_amount >= 0),
    fee REAL NOT NULL CHECK(fee >= 0),
    allocated_cost REAL,
    realized_pnl REAL,
    price_source TEXT NOT NULL,
    price_at TEXT NOT NULL,
    is_delayed INTEGER NOT NULL DEFAULT 0 CHECK(is_delayed IN (0, 1)),
    recommendation_date TEXT,
    recommendation TEXT,
    composite_score REAL,
    score_snapshot_json TEXT,
    -- Conditional-entry acknowledgement snapshot.  These are deliberately
    -- append-only alongside the rest of the simulated fill ledger.
    risk_score REAL,
    risk_snapshot_json TEXT,
    risk_acknowledged_at TEXT,
    risk_acknowledgement_version TEXT,
    risk_snapshot_hash TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_paper_trades_position
    ON paper_trades (position_id, created_at);
CREATE INDEX IF NOT EXISTS idx_paper_trades_market_ticker
    ON paper_trades (market, ticker, created_at);

-- Point-in-time valuation and scoring marks for open paper positions.
CREATE TABLE IF NOT EXISTS paper_position_marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES paper_positions(id) ON DELETE CASCADE,
    current_price REAL NOT NULL CHECK(current_price > 0),
    market_value REAL NOT NULL,
    estimated_sell_fee REAL NOT NULL DEFAULT 0,
    unrealized_pnl REAL NOT NULL,
    composite_score REAL,
    technical_score REAL,
    fundamental_score REAL,
    flow_score REAL,
    intel_score REAL,
    macro_score REAL,
    score_decision TEXT CHECK(score_decision IN ('EXECUTE', 'WATCH', 'SKIP')),
    score_snapshot_json TEXT,
    exit_score REAL,
    exit_decision TEXT CHECK(exit_decision IN ('HOLD', 'SELL_WATCH', 'SELL_EXECUTE')),
    exit_reason TEXT,
    exit_snapshot_json TEXT,
    price_source TEXT NOT NULL,
    price_at TEXT NOT NULL,
    is_delayed INTEGER NOT NULL DEFAULT 0 CHECK(is_delayed IN (0, 1)),
    refresh_source TEXT NOT NULL CHECK(refresh_source IN ('order', 'manual', 'scheduled')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_paper_marks_position_date
    ON paper_position_marks (position_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_paper_marks_exit
    ON paper_position_marks (exit_decision, created_at DESC);

-- Local application accounts. Passwords are always Argon2id hashes, and there is
-- deliberately no public registration flow.
CREATE TABLE IF NOT EXISTS app_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('owner', 'viewer')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    password_changed_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_app_users_active_role
    ON app_users (is_active, role);

-- Only hashes of bearer session tokens are persisted. CSRF tokens use the
-- same model: the browser receives the random value while SQLite keeps a hash.
CREATE TABLE IF NOT EXISTS app_sessions (
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
);

CREATE INDEX IF NOT EXISTS idx_app_sessions_user_active
    ON app_sessions (user_id, revoked_at, expires_at);
CREATE INDEX IF NOT EXISTS idx_app_sessions_expiry
    ON app_sessions (expires_at, revoked_at);

-- Login and account lifecycle audit trail. This table contains no passwords,
-- raw session tokens, CSRF tokens, or application secrets.
CREATE TABLE IF NOT EXISTS auth_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES app_users(id) ON DELETE SET NULL,
    username TEXT COLLATE NOCASE,
    event_type TEXT NOT NULL,
    ip_address TEXT,
    user_agent TEXT,
    detail_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_audit_username_event_time
    ON auth_audit_events (username, event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_auth_audit_ip_event_time
    ON auth_audit_events (ip_address, event_type, created_at DESC);
