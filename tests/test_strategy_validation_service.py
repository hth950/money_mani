"""Integration tests for the read-only strategy validation centre."""

import pytest

from strategy.models import Strategy
from web.db import connection, migrate
from web.services.backtest_evidence_service import BacktestEvidenceService
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


def _strategy(name="ready"):
    return Strategy(
        name=name, description="", source="test", category="trend",
        status="validated_v2", rules={}, indicators=[], parameters={}, market="KRX",
    )


def test_overview_requires_evidence_and_forward_outcomes(validation_db):
    strategy = _strategy()
    registry = _Registry([strategy])
    with connection.get_db() as db:
        strategy_id = db.execute(
            "INSERT INTO strategies(name, status) VALUES (?, ?)",
            (strategy.name, strategy.status),
        ).lastrowid
        for number in range(5):
            db.execute(
                """INSERT INTO backtest_results
                   (strategy_id, strategy_name, ticker, market, is_valid,
                    avg_holding_days, annual_trade_rate, validation_policy_json)
                   VALUES (?, ?, ?, 'KRX', 1, 8, 4, '{}')""",
                (strategy_id, strategy.name, f"K{number}"),
            )
        db.execute(
            """INSERT INTO walk_forward_results
               (strategy_name, ticker, market, total_windows, valid_windows,
                is_overfit, min_windows) VALUES (?, 'K0', 'KRX', 4, 3, 0, 3)""",
            (strategy.name,),
        )
        for number in range(30):
            signal_id = db.execute(
                """INSERT INTO signals(strategy_name, ticker, market, signal_type, price)
                   VALUES (?, ?, 'KRX', 'BUY', 100)""",
                (strategy.name, f"K{number}"),
            ).lastrowid
            event_id = db.execute(
                """INSERT INTO decision_events(signal_id, ticker, market, signal_action,
                   recommendation, execution_state, scan_date, composite_score)
                   VALUES (?, ?, 'KRX', 'BUY', 'EXECUTE', 'EXECUTED', '2026-03-10', .7)""",
                (signal_id, f"K{number}"),
            ).lastrowid
            db.execute(
                """INSERT INTO decision_outcomes
                   (decision_event_id, horizon_days, status, net_return_pct,
                    excess_return_pct, entry_date, exit_date)
                   VALUES (?, 10, 'evaluated', .5, .2, '2026-03-10', '2026-03-24')""",
                (event_id,),
            )

    evidence = BacktestEvidenceService(registry, min_ticker_pass_rate=.20)
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
    assert row["forward"]["evaluated"] == 30
    assert row["forward"]["coverage_pct"] == 100.0
    assert row["activation_ready"] is True


def test_overview_rejects_non_product_horizons(validation_db):
    service = StrategyValidationService(BacktestEvidenceService(_Registry([])))
    with pytest.raises(ValueError):
        service.build_overview(horizon=1)
    with pytest.raises(ValueError):
        service.build_overview(market="crypto")
