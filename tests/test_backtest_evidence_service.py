"""Tests for append-only validation evidence and readiness reconciliation."""

import json
from types import SimpleNamespace

import pytest

from strategy.models import Strategy
from web.db import connection, migrate
from web.services.backtest_evidence_service import BacktestEvidenceService
from web.services.backtest_service import BacktestService


class _Registry:
    def __init__(self, strategies):
        self.strategies = {strategy.name: strategy for strategy in strategies}

    def list_strategies(self):
        return list(self.strategies)

    def load(self, name):
        return self.strategies[name]


def _strategy(name, status="validated_v2", strategy_type="indicator"):
    return Strategy(
        name=name,
        description="",
        source="test",
        category="factor" if strategy_type == "factor" else "trend",
        status=status,
        rules={},
        indicators=[],
        parameters={},
        market="KRX",
        strategy_type=strategy_type,
    )


@pytest.fixture
def evidence_db(tmp_path, monkeypatch):
    db_path = tmp_path / "evidence.db"
    monkeypatch.setattr(connection, "DB_PATH", db_path)
    connection.init_db()
    migrate.run_schema_migrations()
    return db_path


def test_schema_contains_canonical_walk_forward_and_decision_tables(evidence_db):
    # This also guards against semicolons inside schema comments breaking the
    # deliberately simple schema.sql statement splitter.
    migrate.run_schema_migrations()
    with connection.get_db() as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        wf_columns = {
            row[1] for row in db.execute("PRAGMA table_info(walk_forward_results)")
        }
        bt_columns = {
            row[1] for row in db.execute("PRAGMA table_info(backtest_results)")
        }
    assert {"walk_forward_results", "decision_events", "decision_outcomes"} <= tables
    assert {"train_days", "min_windows", "overfit_threshold"} <= wf_columns
    assert {
        "avg_holding_days",
        "annual_trade_rate",
        "validation_policy_json",
    } <= bt_columns


def test_evidence_report_uses_latest_rows_without_promoting_status(evidence_db):
    ready = _strategy("ready")
    below = _strategy("below")
    factor = _strategy("factor-draft", status="draft", strategy_type="factor")
    registry = _Registry([ready, below, factor])

    with connection.get_db() as db:
        for strategy in (ready, below, factor):
            db.execute(
                "INSERT INTO strategies(name, status, rules_json, indicators_json, parameters_json) "
                "VALUES (?, ?, '{}', '[]', '{}')",
                (strategy.name, strategy.status),
            )

        # A stale invalid row for K0 is superseded by the later valid row.  The
        # latest five tickers therefore have a 20% recorded pass rate.
        for created_at, ticker, valid in [
            ("2026-01-01", "K0", 0),
            ("2026-01-02", "K0", 1),
            ("2026-01-02", "K1", 0),
            ("2026-01-02", "K2", 0),
            ("2026-01-02", "K3", 0),
            ("2026-01-02", "K4", 0),
        ]:
            db.execute(
                """INSERT INTO backtest_results
                   (strategy_name, ticker, market, is_valid, created_at,
                    avg_holding_days, annual_trade_rate, validation_policy_json)
                   VALUES ('ready', ?, 'KRX', ?, ?, 8.0, 4.0, '{}')""",
                (ticker, valid, created_at),
            )
        for idx in range(5):
            db.execute(
                "INSERT INTO backtest_results(strategy_name, ticker, market, is_valid) "
                "VALUES ('below', ?, 'KRX', 0)",
                (f"B{idx}",),
            )
        db.execute(
            """INSERT INTO walk_forward_results
               (strategy_name, ticker, market, total_windows, valid_windows,
                is_overfit, min_windows)
               VALUES ('ready', 'K0', 'KRX', 4, 3, 0, 3)"""
        )

    service = BacktestEvidenceService(
        registry,
        min_tested_tickers=5,
        min_ticker_pass_rate=0.20,
    )
    report = service.build_report()
    by_name = {row["name"]: row for row in report["strategies"]}

    assert by_name["ready"]["evidence_state"] == "ready"
    assert by_name["ready"]["backtest"]["history_rows"] == 6
    assert by_name["ready"]["backtest"]["latest_results"] == 5
    assert by_name["ready"]["backtest"]["pass_rate"] == 0.2
    assert by_name["below"]["evidence_state"] == "backtest_below_threshold"
    assert by_name["factor-draft"]["evidence_state"] == "not_claimed"
    assert report["summary"]["validation_claims"] == 2
    assert report["summary"]["evidence_ready"] == 1
    assert report["summary"]["attention_required"] == 1
    assert report["summary"]["factor_drafts_without_backtests"] == 1

    # Reporting is read-only: no YAML/DB status is promoted or rewritten.
    with connection.get_db() as db:
        statuses = dict(db.execute("SELECT name, status FROM strategies").fetchall())
    assert statuses == {
        "ready": "validated_v2",
        "below": "validated_v2",
        "factor-draft": "draft",
    }


def test_walk_forward_overfit_is_not_ready(evidence_db):
    strategy = _strategy("overfit")
    with connection.get_db() as db:
        db.execute(
            "INSERT INTO strategies(name, status) VALUES ('overfit', 'validated_v2')"
        )
        for idx in range(5):
            db.execute(
                """INSERT INTO backtest_results
                   (strategy_name, ticker, market, is_valid, avg_holding_days,
                    annual_trade_rate, validation_policy_json)
                   VALUES ('overfit', ?, 'KRX', 1, 8.0, 4.0, '{}')""",
                (f"K{idx}",),
            )
        db.execute(
            """INSERT INTO walk_forward_results
               (strategy_name, ticker, market, total_windows, valid_windows,
                is_overfit, overfit_reason, min_windows)
               VALUES ('overfit', 'K0', 'KRX', 4, 4, 1, 'degradation', 3)"""
        )

    row = BacktestEvidenceService(
        _Registry([strategy]), min_ticker_pass_rate=0.20
    ).build_report()["strategies"][0]
    assert row["evidence_state"] == "walk_forward_overfit"
    assert not row["evidence_ready"]


def test_legacy_is_valid_rows_cannot_become_policy_ready(evidence_db):
    strategy = _strategy("legacy")
    with connection.get_db() as db:
        db.execute(
            "INSERT INTO strategies(name, status) VALUES ('legacy', 'validated_v2')"
        )
        for idx in range(5):
            # Legacy rows have a recorded is_valid bit but no threshold policy,
            # holding-period, or annual-trade metadata to audit that claim.
            db.execute(
                "INSERT INTO backtest_results(strategy_name, ticker, market, is_valid) "
                "VALUES ('legacy', ?, 'KRX', 1)",
                (f"K{idx}",),
            )
        db.execute(
            """INSERT INTO walk_forward_results
               (strategy_name, ticker, market, total_windows, valid_windows,
                is_overfit, min_windows)
               VALUES ('legacy', 'K0', 'KRX', 4, 4, 0, 3)"""
        )

    row = BacktestEvidenceService(
        _Registry([strategy]), min_ticker_pass_rate=0.20
    ).build_report()["strategies"][0]
    assert row["evidence_state"] == "legacy_backtest_evidence"
    assert not row["backtest"]["supported"]
    assert not row["evidence_ready"]


def test_yaml_alias_migration_compares_canonical_strategy_name(evidence_db, monkeypatch):
    canonical = _strategy("canonical", status="draft")

    class AliasRegistry:
        def list_strategies(self):
            return ["alias-filename", "second-alias"]

        def load(self, _name):
            return canonical

    with connection.get_db() as db:
        db.execute("INSERT INTO strategies(name, status) VALUES ('canonical', 'draft')")

    monkeypatch.setattr(migrate, "StrategyRegistry", AliasRegistry)
    migrate.migrate_yaml_strategies()
    migrate.migrate_yaml_strategies()

    with connection.get_db() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM strategies WHERE name='canonical'"
        ).fetchone()[0] == 1


def test_new_backtests_store_the_policy_needed_for_audit(evidence_db):
    with connection.get_db() as db:
        strategy_id = db.execute(
            "INSERT INTO strategies(name, status) VALUES ('new-result', 'testing')"
        ).lastrowid

    result = SimpleNamespace(
        strategy_name="new-result",
        ticker="AAA",
        period="2020-01-01~2026-01-01",
        total_return=0.2,
        sharpe_ratio=0.5,
        max_drawdown=-0.2,
        win_rate=0.45,
        num_trades=30,
        is_valid=True,
        trades=[],
        avg_holding_days=8.5,
        annual_trade_rate=5.0,
    )
    BacktestService()._store_result(strategy_id, result, "KRX")

    with connection.get_db() as db:
        row = db.execute(
            """SELECT avg_holding_days, annual_trade_rate,
                      validation_policy_json
               FROM backtest_results WHERE strategy_name='new-result'"""
        ).fetchone()
    assert row[0:2] == (8.5, 5.0)
    assert "min_ticker_pass_rate" in json.loads(row[2])
