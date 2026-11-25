"""Integration tests for the read-only strategy validation centre."""

import json

import pytest

from backtester.engine import EXECUTION_MODEL, WARMUP_BARS
from strategy.models import Strategy
from web.db import connection, migrate
from web.services.backtest_evidence_service import BacktestEvidenceService
from web.services.decision_label_service import LABEL_VERSION, OUTCOME_LABEL_DDL, exchange_schedule
from web.services.strategy_validation_service import StrategyValidationService


class _Registry:
    def __init__(self, strategies):
        self.strategies = {strategy.name: strategy for strategy in strategies}

    def list_strategies(self):
        return list(self.strategies)

    def load(self, name):
        return self.strategies[name]


@pytest.fixture
def validation_db(tmp_path, monkeypatch):
    monkeypatch.setattr(connection, "DB_PATH", tmp_path / "validation.db")
    connection.init_db()
    migrate.run_schema_migrations()
    with connection.get_db() as db:
        db.execute(OUTCOME_LABEL_DDL)


def _strategy(name="ready", market="KRX"):
    return Strategy(
        name=name, description="", source="test", category="trend",
        status="validated_v2", rules={}, indicators=[], parameters={}, market=market,
    )


def _save_evidence(db, strategy, market):
    strategy_id = db.execute(
        "INSERT INTO strategies(name, status) VALUES (?, ?)",
        (strategy.name, strategy.status),
    ).lastrowid
    policy = {"execution": {"model": EXECUTION_MODEL, "market": market, "warmup_bars": WARMUP_BARS}}
    db.executemany(
        """INSERT INTO backtest_results
           (strategy_id, strategy_name, ticker, market, is_valid, num_trades,
            avg_holding_days, annual_trade_rate, validation_policy_json)
           VALUES (?, ?, ?, ?, 1, 15, 8, 4, ?)""",
        [(strategy_id, strategy.name, f"K{number}", market, json.dumps(policy)) for number in range(5)],
    )
    policy["windows"] = [{"valid": True, "train_trades": 12, "test_trades": 3}] * 3
    db.execute(
        """INSERT INTO walk_forward_results
           (strategy_name, ticker, market, total_windows, valid_windows,
            is_overfit, min_windows, windows_json) VALUES (?, 'K0', ?, 3, 3, 0, 3, ?)""",
        (strategy.name, market, json.dumps(policy)),
    )


def _signal(db, strategy, ticker, market="KRX", detected_at="2026-03-09 23:00:00"):
    return db.execute(
        """INSERT INTO signals(strategy_name, ticker, market, signal_type, price, detected_at)
           VALUES (?, ?, ?, 'BUY', 100, ?)""", (strategy, ticker, market, detected_at),
    ).lastrowid


def _label(db, ticker, *, market="KRX", signal_id=None, action="BUY", ensemble=False,
           entry="2026-03-10", version=LABEL_VERSION):
    event_id = db.execute(
        """INSERT INTO decision_events(signal_id, ticker, market, signal_action, recommendation,
           execution_state, scan_date, composite_score, consensus_strategies_json)
           VALUES (?, ?, ?, ?, 'EXECUTE', 'EXECUTED', '2026-03-10', .7, ?)""",
        (signal_id, ticker, market, action, '["ready", "also"]' if ensemble else None),
    ).lastrowid
    sessions = exchange_schedule(market).index
    entry_pos = sessions.get_loc(entry)
    db.execute(
        """INSERT INTO decision_outcome_labels
           (decision_event_id, horizon_days, label_version, status, net_return_pct,
            excess_return_pct, entry_date, exit_date, observed_at)
           VALUES (?, 10, ?, 'evaluated', .5, .2, ?, ?, '2026-03-30T00:00:00Z')""",
        (event_id, version, entry, sessions[entry_pos + 9].date().isoformat()),
    )


def test_overview_requires_evidence_and_own_forward_sample(validation_db):
    strategy = _strategy()
    with connection.get_db() as db:
        _save_evidence(db, strategy, "KRX")
        for number in range(30):
            ticker = f"K{number}"
            _signal(db, strategy.name, ticker)
            _label(db, ticker, ensemble=True)
        _signal(db, strategy.name, "K0", detected_at="2026-03-10 01:00:00")

    evidence = BacktestEvidenceService(_Registry([strategy]), min_ticker_pass_rate=.20)
    result = StrategyValidationService(evidence).build_overview(horizon=10, market="KRX")
    assert result["policy"]["rollout_mode"] == "shadow"
    assert result["summary"] == {
        "strategies": 1,
        "activation_ready": 1,
        "evidence_ready": 1,
        "forward_ready": 1,
        "shadow_only": True,
    }
    row = result["strategies"][0]
    assert row["forward"]["evaluated"] == row["forward"]["total"] == 30
    assert row["forward"]["coverage_pct"] == 100.0
    assert row["activation_ready"] is True
    assert result["ensemble_forward_by_market"]["KRX"]["evaluated"] == 30


def test_ensemble_membership_and_mismatched_windows_confer_no_individual_evidence(validation_db):
    strategy = _strategy()
    with connection.get_db() as db:
        _save_evidence(db, strategy, "KRX")
        late = _signal(db, strategy.name, "LATE", detected_at="2026-03-10 00:00:00")
        _label(db, "LATE", signal_id=late, ensemble=True)
        _signal(db, strategy.name, "SELL")
        _label(db, "SELL", action="SELL")
        _signal(db, strategy.name, "OLD")
        _label(db, "OLD", version="legacy")
        _label(db, "NO_OWN_SIGNAL", ensemble=True)

    result = StrategyValidationService(BacktestEvidenceService(_Registry([strategy]))).build_overview()
    assert result["ensemble_forward_by_market"]["KRX"]["evaluated"] == 2
    strategy = result["strategies"][0]
    assert strategy["forward"]["total"] == 3
    assert strategy["forward"]["evaluated"] == 0
    assert strategy["forward"]["evidence_state"] == "insufficient_independent_evidence"
    assert not strategy["activation_ready"]


def test_activation_uses_a_single_market(validation_db):
    strategy = _strategy(market="ALL")
    with connection.get_db() as db:
        _save_evidence(db, strategy, "KRX")
        for number in range(30):
            ticker = f"K{number}"
            _signal(db, strategy.name, ticker, market="US")
            _label(db, ticker, market="US")
    service = StrategyValidationService(BacktestEvidenceService(_Registry([strategy])))
    row = service.build_overview()["strategies"][0]
    assert row["by_market"]["US"]["forward"]["meets_forward_gate"]
    assert row["by_market"]["KRX"]["evidence_ready"]
    assert not row["activation_ready"]
    assert not service.build_overview(market="US")["strategies"][0]["evidence_ready"]


def test_overview_rejects_non_product_horizons(validation_db):
    service = StrategyValidationService(BacktestEvidenceService(_Registry([])))
    with pytest.raises(ValueError):
        service.build_overview(horizon=1)
    with pytest.raises(ValueError):
        service.build_overview(market="crypto")
