"""Regression coverage for scan orchestration and decision cohort auditing."""

import importlib.util
import pathlib
import sys
import types
from types import SimpleNamespace

import pytest

from scoring import multi_layer_scorer, risk_manager
from web.db import connection
from web.services.scan_service import ScanService


@pytest.fixture
def scan_db(tmp_path, monkeypatch):
    db_path = tmp_path / "scan.db"
    monkeypatch.setattr(connection, "DB_PATH", db_path)
    connection.init_db()
    return db_path


@pytest.fixture
def daily_scan_module(monkeypatch):
    """Load daily_scan without executing pipeline/__init__.py dependencies."""
    package = types.ModuleType("pipeline")
    package.__path__ = [str(pathlib.Path(__file__).parents[1] / "pipeline")]
    monkeypatch.setitem(sys.modules, "pipeline", package)
    path = pathlib.Path(__file__).parents[1] / "pipeline" / "daily_scan.py"
    spec = importlib.util.spec_from_file_location("pipeline.daily_scan", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "pipeline.daily_scan", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scheduler_module(monkeypatch, daily_scan_module):
    """Load scheduler with optional job dependencies stubbed out."""
    dependency_classes = {
        "pipeline.evening_report": "EveningReport",
        "pipeline.nightly": "NightlyOrchestrator",
        "pipeline.runner": "PipelineRunner",
        "pipeline.market_intel": "MarketIntelScanner",
        "pipeline.intel_price_tracker": "IntelPriceTracker",
        "pipeline.correlation_logger": "CorrelationLogger",
    }
    for module_name, class_name in dependency_classes.items():
        module = types.ModuleType(module_name)
        setattr(module, class_name, type(class_name, (), {}))
        monkeypatch.setitem(sys.modules, module_name, module)

    apscheduler = types.ModuleType("apscheduler")
    schedulers = types.ModuleType("apscheduler.schedulers")
    blocking = types.ModuleType("apscheduler.schedulers.blocking")
    triggers = types.ModuleType("apscheduler.triggers")
    cron = types.ModuleType("apscheduler.triggers.cron")
    blocking.BlockingScheduler = type("BlockingScheduler", (), {})
    cron.CronTrigger = type("CronTrigger", (), {})
    for name, module in {
        "apscheduler": apscheduler,
        "apscheduler.schedulers": schedulers,
        "apscheduler.schedulers.blocking": blocking,
        "apscheduler.triggers": triggers,
        "apscheduler.triggers.cron": cron,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    path = pathlib.Path(__file__).parents[1] / "pipeline" / "scheduler.py"
    spec = importlib.util.spec_from_file_location("pipeline.scheduler", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "pipeline.scheduler", module)
    spec.loader.exec_module(module)
    return module


def _consensus_signal(strategy_name="ensemble"):
    return {
        "strategy_name": strategy_name,
        "ticker": "AAA",
        "ticker_name": "AAA",
        "market": "KRX",
        "signal_type": "BUY",
        "price": 100.0,
    }


def test_manual_scan_records_one_history_row_without_resaving_consensus(
    scan_db, monkeypatch, daily_scan_module
):
    class FakeDailyScan:
        def run(self):
            # DailyScan already owns persistence of its individual signals.
            with connection.get_db() as db:
                db.execute(
                    """INSERT INTO signals
                       (strategy_name, ticker, ticker_name, market, signal_type, price)
                       VALUES ('individual', 'AAA', 'AAA', 'KRX', 'BUY', 100.0)"""
                )
            return {
                "date": "2026-07-14",
                "signals": [_consensus_signal()],
                "skipped": False,
                "markets_open": ["KRX"],
            }

    monkeypatch.setattr(daily_scan_module, "DailyScan", FakeDailyScan)

    result = ScanService().run_scan(include_signals=True)

    with connection.get_db() as db:
        history = db.execute("SELECT * FROM scan_history").fetchall()
        signals = db.execute("SELECT strategy_name FROM signals").fetchall()
    assert result["scan_id"] == history[0]["id"]
    assert result["signals_count"] == 1
    assert result["signals"][0]["strategy_name"] == "ensemble"
    assert len(history) == 1
    assert history[0]["markets_open"] == "KRX"
    # The obsolete wrapper save would add the differently named consensus row.
    assert [row["strategy_name"] for row in signals] == ["individual"]


def test_scheduled_skipped_scan_records_one_history_row(
    scan_db, monkeypatch, daily_scan_module, scheduler_module
):
    class FakeDailyScan:
        def run(self):
            return {
                "date": "2026-07-14",
                "signals": [],
                "skipped": True,
                "markets_open": [],
            }

    monkeypatch.setattr(daily_scan_module, "DailyScan", FakeDailyScan)

    scheduler_module._run_daily_scan()

    with connection.get_db() as db:
        rows = db.execute("SELECT * FROM scan_history").fetchall()
    assert len(rows) == 1
    assert rows[0]["signals_count"] == 0
    assert rows[0]["markets_open"] == ""


def test_scheduled_exception_does_not_write_success_history(
    scan_db, monkeypatch, daily_scan_module, scheduler_module
):
    class FailingDailyScan:
        def run(self):
            raise RuntimeError("synthetic scan failure")

    monkeypatch.setattr(daily_scan_module, "DailyScan", FailingDailyScan)

    scheduler_module._run_daily_scan()

    with connection.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM scan_history").fetchone()[0] == 0


def test_daily_scan_result_reports_actual_open_market(daily_scan_module):
    class Calendar:
        def __init__(self, is_open):
            self.is_open = is_open

        def is_trading_day(self, _today):
            return self.is_open

    scanner = daily_scan_module.DailyScan.__new__(daily_scan_module.DailyScan)
    scanner.krx_cal = Calendar(False)
    scanner.nyse_cal = Calendar(True)
    scanner._get_top_strategies = lambda: []
    scanner.registry = SimpleNamespace(get_validated=lambda: [])

    result = scanner.run()

    assert result["skipped"] is False
    assert result["markets_open"] == ["US"]


def test_multi_layer_scoring_preserves_skip_for_audit(
    monkeypatch, daily_scan_module
):
    class FakeScorer:
        enabled = True

        def score(self, **_kwargs):
            return {
                "composite_score": 0.2,
                "decision": "SKIP",
                "scores": {"technical": 0.2},
                "details": {"reason": "low score"},
                "weights": {"technical": 1.0},
            }

    monkeypatch.setattr(multi_layer_scorer, "MultiLayerScorer", FakeScorer)
    scanner = daily_scan_module.DailyScan.__new__(daily_scan_module.DailyScan)
    scanner._last_krx_ohlcv = {}
    scanner._last_us_ohlcv = {}

    result = scanner._apply_multi_layer_scoring([_consensus_signal()], 5)

    assert len(result) == 1
    assert result[0]["score_decision"] == "SKIP"
    assert result[0]["composite_score"] == pytest.approx(0.2)


def test_risk_gate_does_not_call_short_circuiting_legacy_gate(
    monkeypatch, daily_scan_module
):
    checked = []

    class FakeRiskManager:
        enabled = True

        def check_can_buy(self, ticker, market):
            checked.append((ticker, market))
            return False, "capacity"

    monkeypatch.setattr(risk_manager, "PortfolioRiskManager", FakeRiskManager)
    scanner = daily_scan_module.DailyScan.__new__(daily_scan_module.DailyScan)
    watch = {**_consensus_signal("watch"), "ticker": "WATCH", "score_decision": "WATCH"}
    execute = {**_consensus_signal("execute"), "ticker": "EXEC", "score_decision": "EXECUTE"}

    passed, blocked = scanner._apply_risk_gate([watch, execute])

    assert checked == []
    assert passed == [watch, execute]
    assert blocked == []
    assert execute["score_decision"] == "EXECUTE"
    assert execute["opportunity_decision"] == "EXECUTE"
    assert execute["recommendation_tier"] == "BUY_READY"


def test_portfolio_policy_warnings_include_all_independent_limits(daily_scan_module):
    class RiskManager:
        config = {
            "max_positions": 1,
            "max_single_weight": 0.20,
            "max_sector_weight": 0.30,
            "max_daily_loss": -0.03,
        }

        def _get_daily_pnl(self):
            return -0.05

    warnings = daily_scan_module.DailyScan._portfolio_policy_warnings(
        RiskManager(),
        [{"ticker": "AAPL", "market": "US", "sector": "Technology"}],
        "AAPL", "US", "Technology",
    )

    assert any("포지션 한도" in warning for warning in warnings)
    assert any("이미 포지션" in warning for warning in warnings)
    assert any("섹터 집중도" in warning for warning in warnings)
    assert any("일일 손실" in warning for warning in warnings)


def test_entry_risk_context_merges_unique_strategy_and_paper_positions(
    scan_db, monkeypatch, daily_scan_module
):
    captured = []

    class FakeEntryRiskScorer:
        def assess(self, **kwargs):
            captured.append(kwargs)
            return {
                "opportunity_decision": "EXECUTE",
                "risk_score": 45.0,
                "risk_level": "MEDIUM",
                "risk_breakdown": {},
                "recommendation_tier": "BUY_CONDITIONAL",
                "hard_block_reason": None,
                "risk_model_version": "test",
            }

    entry_module = types.ModuleType("scoring.entry_risk_scorer")
    entry_module.EntryRiskScorer = FakeEntryRiskScorer
    monkeypatch.setitem(sys.modules, "scoring.entry_risk_scorer", entry_module)

    class FakeRiskManager:
        enabled = True

        def _get_open_positions(self):
            return [
                {"ticker": "AAPL", "market": "US"},
                {"ticker": "AUTO", "market": "US"},
            ]

        def _get_sector(self, ticker, _market):
            return {"AAPL": "Technology", "AUTO": "Industrials", "PAPER": "Technology"}.get(ticker, "Unknown")

        def check_can_buy(self, *_args):
            return True, "OK"

    monkeypatch.setattr(risk_manager, "PortfolioRiskManager", FakeRiskManager)
    with connection.get_db() as db:
        db.execute(
            """INSERT INTO paper_positions
               (market, ticker, ticker_name, status, quantity, avg_price, remaining_cost)
               VALUES ('US', 'AAPL', 'AAPL', 'open', 1, 100, 100)"""
        )
        db.execute(
            """INSERT INTO paper_positions
               (market, ticker, ticker_name, status, quantity, avg_price, remaining_cost)
               VALUES ('US', 'PAPER', 'PAPER', 'open', 1, 100, 100)"""
        )

    scanner = daily_scan_module.DailyScan.__new__(daily_scan_module.DailyScan)
    execute = {
        **_consensus_signal("execute"), "ticker": "TARGET", "market": "US",
        "score_decision": "EXECUTE", "composite_score": 0.66,
        "score_breakdown": {"technical": 0.66}, "score_details": {},
    }
    passed, blocked = scanner._apply_risk_gate([execute])

    assert passed == [execute]
    assert blocked == []
    positions = captured[0]["positions"]
    assert {(row["market"], row["ticker"]) for row in positions} == {
        ("US", "AAPL"), ("US", "AUTO"), ("US", "PAPER"),
    }
    assert {row["ticker"]: row["sector"] for row in positions} == {
        "AAPL": "Technology", "AUTO": "Industrials", "PAPER": "Technology",
    }


def test_run_audits_skip_without_signal_alert_or_position_execution(
    daily_scan_module,
):
    raw = _consensus_signal("individual")
    skipped = {**_consensus_signal(), "score_decision": "SKIP", "composite_score": 0.2}
    saved = []
    risk_inputs = []

    class Calendar:
        def __init__(self, is_open):
            self.is_open = is_open

        def is_trading_day(self, _today):
            return self.is_open

    class Discord:
        def send(self, **_kwargs):
            # A generic completion message is allowed; it is not a SKIP signal
            # alert and carries no position side effect.
            return None

    scanner = daily_scan_module.DailyScan.__new__(daily_scan_module.DailyScan)
    scanner.krx_cal = Calendar(True)
    scanner.nyse_cal = Calendar(False)
    scanner._get_top_strategies = lambda: [SimpleNamespace(market="KRX")]
    scanner.config = {"pipeline": {"targets": {"custom_tickers": []}}}
    scanner._scan_factor_strategies = lambda *_args: []
    scanner._scan_market = lambda *_args: [raw]
    scanner._apply_ensemble_filter = lambda _signals: ([skipped], {})
    scanner._apply_multi_layer_scoring = lambda candidates, _total: candidates

    def risk_gate(candidates):
        risk_inputs.extend(candidates)
        return candidates, []

    scanner._apply_risk_gate = risk_gate
    scanner._evaluate_open_positions = lambda *_args: []
    scanner._save_scoring_results = lambda signals, _date: saved.extend(signals)
    scanner._save_signals_to_db = lambda *_args: None
    scanner.perf_service = SimpleNamespace(record_signal=lambda _signal: None)
    scanner.discord = Discord()
    scanner._send_alerts = lambda *_args: pytest.fail("SKIP reached signal alerts")
    scanner._send_exit_alerts = lambda *_args: None

    result = scanner.run()

    # Risk snapshots are also kept for excluded scores; they still must never
    # reach alerting or position execution.
    assert risk_inputs == [skipped]
    assert saved == [skipped]
    assert result["signals"] == []
    assert result["scored_signals"] == [skipped]
