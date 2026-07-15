"""Scheduler integration tests for paper position refreshes."""

import importlib.util
import pathlib
import sys
import types

import pytest


@pytest.fixture
def scheduler_module(monkeypatch):
    """Load scheduler without executing optional pipeline dependencies."""
    package = types.ModuleType("pipeline")
    package.__path__ = [str(pathlib.Path(__file__).parents[1] / "pipeline")]
    monkeypatch.setitem(sys.modules, "pipeline", package)

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


def _install_rescore(monkeypatch, run_rescore):
    module = types.ModuleType("pipeline.rescore")
    module.run_rescore = run_rescore
    monkeypatch.setitem(sys.modules, "pipeline.rescore", module)


def _install_paper_service(monkeypatch, service_class):
    module = types.ModuleType("web.services.paper_trading_service")
    module.PaperTradingService = service_class
    monkeypatch.setitem(sys.modules, "web.services.paper_trading_service", module)


def test_each_scheduler_rescore_refreshes_paper_positions_once(
    scheduler_module, monkeypatch
):
    calls = []
    _install_rescore(
        monkeypatch,
        # Even a successful no-op rescore is a completed refresh boundary.
        lambda: calls.append(("rescore", None)) or 0,
    )

    class PaperTradingService:
        def refresh_open_positions(self, *, refresh_source):
            calls.append(("paper", refresh_source))
            return {"total": 2, "succeeded": 2, "failed": 0, "errors": []}

    _install_paper_service(monkeypatch, PaperTradingService)

    scheduler_module._run_rescore()

    assert calls == [("rescore", None), ("paper", "scheduled")]


def test_paper_refresh_is_best_effort_after_rescore_failure(
    scheduler_module, monkeypatch, caplog
):
    calls = []

    def fail_rescore():
        calls.append("rescore")
        raise RuntimeError("synthetic rescore failure")

    _install_rescore(monkeypatch, fail_rescore)

    class PaperTradingService:
        def refresh_open_positions(self, *, refresh_source):
            calls.append(f"paper:{refresh_source}")
            raise RuntimeError("synthetic paper failure")

    _install_paper_service(monkeypatch, PaperTradingService)

    # Both operations remain best-effort and preserve the legacy no-raise job
    # boundary. The paper refresh still runs once after rescore completion.
    scheduler_module._run_rescore()

    assert calls == ["rescore", "paper:scheduled"]
    assert "Rescore failed: synthetic rescore failure" in caplog.text
    assert "Paper position refresh after rescore failed" in caplog.text
