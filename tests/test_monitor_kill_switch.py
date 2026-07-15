"""Fail-closed tests for the realtime monitor kill switch."""

import asyncio
import importlib.util
import pathlib
import sys
import types

import pytest

from utils.config_loader import load_config
from web.routers import monitor as monitor_router
from web.services import monitor_service


DISABLED_CONFIG = {
    "realtime": {
        "enabled": False,
        "disabled_reason": "synthetic safety hold",
    }
}


@pytest.fixture
def service(monkeypatch):
    previous = monitor_service.MonitorService._instance
    monitor_service.MonitorService._instance = None
    monkeypatch.setattr(monitor_service, "load_config", lambda: DISABLED_CONFIG)
    instance = monitor_service.MonitorService()
    yield instance
    monitor_service.MonitorService._instance = previous


@pytest.fixture
def scheduler_module(monkeypatch):
    """Load scheduler without importing optional runtime dependencies."""
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

    class DummyCronTrigger:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class DummyScheduler:
        instances = []

        def __init__(self, **_kwargs):
            self.jobs = []
            self.instances.append(self)

        def add_job(self, func, trigger, **kwargs):
            self.jobs.append({"func": func, "trigger": trigger, **kwargs})

        def start(self):
            return None

    apscheduler = types.ModuleType("apscheduler")
    schedulers = types.ModuleType("apscheduler.schedulers")
    blocking = types.ModuleType("apscheduler.schedulers.blocking")
    triggers = types.ModuleType("apscheduler.triggers")
    cron = types.ModuleType("apscheduler.triggers.cron")
    blocking.BlockingScheduler = DummyScheduler
    cron.CronTrigger = DummyCronTrigger
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
    module._dummy_scheduler_class = DummyScheduler
    return module


def test_project_config_disables_realtime_with_reason():
    realtime = load_config()["realtime"]
    assert realtime["enabled"] is False
    assert realtime["disabled_reason"].strip()


def test_policy_load_failure_is_fail_closed(monkeypatch):
    def fail_load():
        raise OSError("synthetic config failure")

    monkeypatch.setattr(monitor_service, "load_config", fail_load)

    policy = monitor_service.get_realtime_policy()

    assert policy["enabled"] is False
    assert policy["disabled_reason"]


def test_start_and_force_start_fail_before_loop_thread_or_monitor_import(
    service, monkeypatch
):
    monkeypatch.setattr(
        monitor_service.asyncio,
        "get_running_loop",
        lambda: pytest.fail("event loop requested while disabled"),
    )
    monkeypatch.setattr(
        monitor_service.threading,
        "Thread",
        lambda *_args, **_kwargs: pytest.fail("thread created while disabled"),
    )
    fake_runtime = types.ModuleType("monitor.realtime_monitor")

    class ForbiddenRealtimeMonitor:
        def __init__(self, **_kwargs):
            pytest.fail("RealtimeMonitor/KIS path constructed while disabled")

    fake_runtime.RealtimeMonitor = ForbiddenRealtimeMonitor
    monkeypatch.setitem(sys.modules, "monitor.realtime_monitor", fake_runtime)

    started = service.start(market_filter="KRX")
    forced = service.force_start(market_filter="US")

    assert started == {
        "status": "disabled",
        "running": False,
        "enabled": False,
        "disabled_reason": "synthetic safety hold",
    }
    assert forced == started
    assert service._monitor is None
    assert service._thread is None


def test_disabled_force_start_does_not_restart_but_stop_remains_available(
    service,
):
    stopped = []
    service._running = True
    service._monitor = types.SimpleNamespace(stop=lambda: stopped.append(True))

    result = service.force_start()

    assert result["status"] == "disabled"
    assert result["running"] is True
    assert stopped == []
    assert service.stop() == {"status": "stopped"}
    assert stopped == [True]
    assert service.is_running() is False


def test_status_api_exposes_policy_and_reason(service, monkeypatch):
    monkeypatch.setattr(monitor_router, "service", service)

    status = asyncio.run(monitor_router.monitor_status())

    assert status == {
        "running": False,
        "enabled": False,
        "disabled_reason": "synthetic safety hold",
    }


def test_scheduled_and_startup_auto_start_are_noops(
    scheduler_module, monkeypatch
):
    policy = {
        "enabled": False,
        "disabled_reason": "synthetic safety hold",
    }
    monkeypatch.setattr(
        scheduler_module, "get_realtime_policy", lambda *_args: policy
    )

    requests = types.ModuleType("requests")
    requests.post = lambda *_args, **_kwargs: pytest.fail(
        "scheduled auto-start made an HTTP request"
    )
    monkeypatch.setitem(sys.modules, "requests", requests)

    import threading

    monkeypatch.setattr(
        threading,
        "Timer",
        lambda *_args, **_kwargs: pytest.fail("startup auto-start created a timer"),
    )

    scheduled = scheduler_module._start_monitor()
    startup = scheduler_module._auto_start_monitor_if_market_open()

    assert scheduled["status"] == "disabled"
    assert startup["status"] == "disabled"


def test_disabled_scheduler_registers_stops_but_not_start_jobs(
    scheduler_module, monkeypatch
):
    monkeypatch.setattr(
        scheduler_module,
        "load_config",
        lambda: {
            "schedule": {},
            "market_intel": {"enabled": False},
            **DISABLED_CONFIG,
        },
    )
    monkeypatch.setattr(scheduler_module, "_preload_sent_signals", lambda: None)

    scheduler_module.start_scheduler()

    scheduler = scheduler_module._dummy_scheduler_class.instances[-1]
    ids = {job["id"] for job in scheduler.jobs}
    assert "monitor_krx_start" not in ids
    assert "monitor_us_start" not in ids
    assert {"monitor_krx_stop", "monitor_us_stop"} <= ids


def test_scheduler_registers_fixed_intraday_full_rescore(
    scheduler_module, monkeypatch
):
    monkeypatch.setattr(
        scheduler_module,
        "load_config",
        lambda: {
            "schedule": {},
            "market_intel": {"enabled": False},
            **DISABLED_CONFIG,
        },
    )
    monkeypatch.setattr(scheduler_module, "_preload_sent_signals", lambda: None)

    scheduler_module.start_scheduler()

    scheduler = scheduler_module._dummy_scheduler_class.instances[-1]
    jobs = {job["id"]: job for job in scheduler.jobs}
    assert "rescore_10min" not in jobs
    assert "full_rescore_intraday" in jobs
    full_rescore = jobs["full_rescore_intraday"]
    assert full_rescore["name"] == "Full Rescore Intraday"
    assert full_rescore["func"] is scheduler_module._run_rescore
    assert full_rescore["trigger"].kwargs == {
        "minute": "30",
        "hour": "9,11,13,15",
        "day_of_week": "mon-fri",
        "timezone": "Asia/Seoul",
    }
    assert jobs["flow_rescore"]["func"] is scheduler_module._run_flow_and_rescore


def test_intel_scan_still_triggers_immediate_rescore(
    scheduler_module, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        scheduler_module,
        "MarketIntelScanner",
        lambda: types.SimpleNamespace(scan=lambda scan_type: {"type": scan_type}),
    )
    monkeypatch.setattr(scheduler_module, "_run_rescore", lambda: calls.append(True))

    scheduler_module._run_intel_scan("midday")

    assert calls == [True]


def test_monitor_page_contains_disabled_reason_and_start_guard():
    template = (
        pathlib.Path(__file__).parents[1]
        / "web"
        / "templates"
        / "monitor"
        / "index.html"
    ).read_text(encoding="utf-8")
    assert 'id="disabled-reason"' in template
    assert 'id="start-btn" onclick="startMonitor()" disabled' in template
    assert "if (!data.enabled)" in template
    assert "startBtn.disabled = true" in template
