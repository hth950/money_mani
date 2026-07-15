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
    assert score[1:3] == ("BLOCKED", "position limit")
    assert json.loads(score[3])["composite"] == pytest.approx(0.875)
    assert event[0] == score_id
    assert event[1:4] == ("BUY", "BLOCKED", "BLOCKED")
    assert event[4] == pytest.approx(0.875)
    assert json.loads(event[5]) == {
        "pipeline": "rescore",
        "trigger": "consensus_signal",
    }
    assert DenyRisk.calls == 1


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
