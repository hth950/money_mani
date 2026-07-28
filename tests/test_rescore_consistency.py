"""Regression tests for scheduled and signal-triggered rescore semantics."""

import importlib.util
import json
import pathlib
import sys
import types

import pytest

from web.db import connection
from web.db.migrate import run_schema_migrations


@pytest.fixture
def rescore_module(monkeypatch):
    package = types.ModuleType("pipeline")
    package.__path__ = [str(pathlib.Path(__file__).parents[1] / "pipeline")]
    monkeypatch.setitem(sys.modules, "pipeline", package)
    spec = importlib.util.spec_from_file_location(
        "pipeline.rescore",
        pathlib.Path(__file__).parents[1] / "pipeline" / "rescore.py",
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "pipeline.rescore", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def rescore_db(tmp_path, monkeypatch):
    db_path = tmp_path / "rescore.db"
    monkeypatch.setattr(connection, "DB_PATH", db_path)
    connection.init_db()
    run_schema_migrations()
    return db_path


class _Collector:
    def __init__(self, score):
        self.value = score

    def score(self, *args, **kwargs):
        return {"score": self.value}


def _patch_collectors(monkeypatch, score):
    import scoring.data_collectors as data_collectors
    import scoring.intel_scorer as intel_scorer
    import scoring.exit_scorer as exit_scorer
    import web.services.macro_service as macro_service

    monkeypatch.setattr(data_collectors, "FundamentalCollector", lambda: _Collector(score))
    monkeypatch.setattr(data_collectors, "FlowCollector", lambda: _Collector(score))
    monkeypatch.setattr(data_collectors, "MacroCollector", lambda: _Collector(score))
    monkeypatch.setattr(intel_scorer, "IntelScorer", lambda: _Collector(score))
    monkeypatch.setattr(
        exit_scorer, "ExitScorer", lambda: types.SimpleNamespace(enabled=False)
    )
    monkeypatch.setattr(
        macro_service,
        "MacroService",
        lambda: types.SimpleNamespace(save_snapshot=lambda *args, **kwargs: None),
    )


def _insert_score(module, ticker, technical, decision="BLOCKED"):
    today = module.datetime.now(module.KST).strftime("%Y-%m-%d")
    weights = {
        "technical": 0.5,
        "fundamental": 0.1,
        "flow": 0.2,
        "intel": 0.1,
        "macro": 0.1,
    }
    with connection.get_db() as db:
        return db.execute(
            """INSERT INTO scoring_results
               (ticker, ticker_name, market, scan_date, technical_score,
                fundamental_score, flow_score, intel_score, macro_score,
                composite_score, decision, block_reason, weights_used_json)
               VALUES (?, ?, 'KRX', ?, ?, 0.1, 0.1, 0.1, 0.1,
                       0.1, ?, 'stale block', ?)""",
            (ticker, ticker, today, technical, decision, json.dumps(weights)),
        ).lastrowid


def test_scheduled_watch_is_not_risk_blocked_or_added_to_outcome_cohort(
    rescore_db, rescore_module, monkeypatch
):
    score_id = _insert_score(rescore_module, "WATCH", 0.5)
    with connection.get_db() as db:
        db.execute(
            """INSERT INTO decision_events
               (scoring_result_id, ticker, market, signal_action,
                recommendation, scan_date)
               VALUES (?, 'WATCH', 'KRX', 'BUY', 'BLOCKED', ?)""",
            (
                score_id,
                rescore_module.datetime.now(rescore_module.KST).strftime("%Y-%m-%d"),
            ),
        )

    _patch_collectors(monkeypatch, 0.5)
    import scoring.risk_manager as risk_manager

    class DenyRisk:
        calls = 0

        def check_can_buy(self, ticker, market):
            DenyRisk.calls += 1
            return False, "position limit"

    monkeypatch.setattr(risk_manager, "PortfolioRiskManager", DenyRisk)

    assert rescore_module.run_rescore(tickers=["WATCH"]) == 1
    with connection.get_db() as db:
        row = db.execute(
            "SELECT composite_score, decision, block_reason, score_breakdown_json "
            "FROM scoring_results WHERE id=?",
            (score_id,),
        ).fetchone()
        event_count = db.execute(
            "SELECT COUNT(*) FROM decision_events WHERE ticker='WATCH'"
        ).fetchone()[0]
    assert row[0] == pytest.approx(0.5)
    assert row[1] == "WATCH"
    assert row[2] is None
    assert json.loads(row[3])["composite"] == pytest.approx(0.5)
    assert DenyRisk.calls == 0
    # Scheduled cache refreshes update the latest row but do not create outcome
    # cohort duplicates every scheduler interval.
    assert event_count == 1


def test_signal_rescore_updates_decision_and_appends_linked_event(
    rescore_db, rescore_module, monkeypatch
):
    score_id = _insert_score(rescore_module, "EXEC", 0.1, decision="WATCH")
    _patch_collectors(monkeypatch, 1.0)
    import scoring.risk_manager as risk_manager

    class DenyRisk:
        calls = 0

        def check_can_buy(self, ticker, market):
            DenyRisk.calls += 1
            return False, "position limit"

    monkeypatch.setattr(risk_manager, "PortfolioRiskManager", DenyRisk)

    assert rescore_module.rescore_ticker_by_signal("EXEC", "KRX", "BUY")
    with connection.get_db() as db:
        score = db.execute(
            "SELECT composite_score, decision, block_reason, score_breakdown_json "
            "FROM scoring_results WHERE id=?",
            (score_id,),
        ).fetchone()
        event = db.execute(
            """SELECT scoring_result_id, signal_action, recommendation,
                      execution_state, composite_score, provenance_json
               FROM decision_events WHERE ticker='EXEC'"""
        ).fetchone()
    assert score[0] == pytest.approx(0.875)
    # Portfolio capacity is a soft entry risk now.  The opportunity decision is
    # retained for the UI/API instead of being hidden behind legacy BLOCKED.
    assert score[1:3] == ("EXECUTE", None)
    assert json.loads(score[3])["composite"] == pytest.approx(0.875)
    assert event[0] == score_id
    assert event[1:4] == ("BUY", "EXECUTE", "RESCORE_ONLY")
    assert event[4] == pytest.approx(0.875)
    assert json.loads(event[5]) == {
        "pipeline": "rescore",
        "trigger": "consensus_signal",
    }
    # The short-circuiting legacy gate is no longer called; independent policy
    # warnings are collected by the two-axis context instead.
    assert DenyRisk.calls == 0


def test_rescore_preserves_hard_block_until_verified_recovery(
    rescore_db, rescore_module
):
    score_id = _insert_score(rescore_module, "HARD", 0.8, decision="BLOCKED")
    with connection.get_db() as db:
        db.execute(
            "UPDATE scoring_results SET hard_block_reason='가격 데이터 검증 실패' WHERE id=?",
            (score_id,),
        )
        item = dict(db.execute("SELECT * FROM scoring_results WHERE id=?", (score_id,)).fetchone())

    captured = {}

    class Risk:
        config = {}

        def _get_open_positions(self):
            return []

        def _get_sector(self, *_args):
            return "Unknown"

    class Entry:
        def assess(self, **kwargs):
            captured.update(kwargs)
            return {
                "opportunity_decision": "EXECUTE",
                "risk_score": 90.0,
                "risk_level": "VERY_HIGH",
                "risk_breakdown": {},
                "recommendation_tier": "UNAVAILABLE",
                "hard_block_reason": kwargs["hard_block_reason"],
                "risk_model_version": "test",
            }

    class Service:
        def update_scoring_result(self, *_args, **kwargs):
            captured["update"] = kwargs
            return 1

    collectors = tuple(_Collector(1.0) for _ in range(4))
    result = rescore_module._rescore_item(
        item, collectors, Risk(), Service(), Entry(), trigger="test"
    )

    assert captured["hard_block_reason"] == "가격 데이터 검증 실패"
    assert captured["update"]["hard_block_reason"] == "가격 데이터 검증 실패"
    assert result["decision"] == "BLOCKED"


def test_rescore_policy_warnings_are_not_short_circuited(rescore_module):
    class Risk:
        config = {
            "max_positions": 1,
            "max_single_weight": 0.20,
            "max_sector_weight": 0.30,
            "max_daily_loss": -0.03,
        }

        def _get_daily_pnl(self):
            return -0.05

    warnings = rescore_module._portfolio_policy_warnings(
        Risk(),
        [{"ticker": "AAPL", "market": "US", "sector": "Technology"}],
        "AAPL", "US", "Technology",
    )
    assert len(warnings) == 4


def test_skip_and_watch_never_call_buy_risk_gate(rescore_module):
    class ExplodingRisk:
        def check_can_buy(self, ticker, market):
            raise AssertionError("risk gate must not run")

    assert rescore_module._resolve_decision(
        0.39, "AAA", "KRX", "BUY", ExplodingRisk()
    ) == ("SKIP", None)
    assert rescore_module._resolve_decision(
        0.64, "AAA", "KRX", "BUY", ExplodingRisk()
    ) == ("WATCH", None)
