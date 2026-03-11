"""APScheduler setup for periodic pipeline runs."""

import gc
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from pipeline.daily_scan import DailyScan
from pipeline.evening_report import EveningReport
from pipeline.nightly import NightlyOrchestrator
from pipeline.runner import PipelineRunner
from pipeline.market_intel import MarketIntelScanner
from pipeline.intel_price_tracker import IntelPriceTracker
from pipeline.correlation_logger import CorrelationLogger
from utils.config_loader import load_config

logger = logging.getLogger("money_mani.pipeline.scheduler")


def _run_daily_scan():
    """Job: daily morning scan."""
    try:
        logger.info("=== Daily Scan Job Started ===")
        scan = DailyScan()
        result = scan.run()
        logger.info(f"Daily scan result: {result}")
    except Exception as e:
        logger.error(f"Daily scan job failed: {e}", exc_info=True)
    finally:
        gc.collect()


def _run_intraday_scan():
    """Job: intraday 30-min scan during market hours."""
    try:
        logger.info("=== Intraday Scan (30min) Started ===")
        scan = DailyScan()
        result = scan.run()
        logger.info(f"Intraday scan result: {result}")
    except Exception as e:
        logger.error(f"Intraday scan failed: {e}", exc_info=True)
    finally:
        gc.collect()


def _run_evening_report():
    """Job: nightly orchestrator (19:00 KST) - P&L, positions, analytics, knowledge."""
    try:
        logger.info("=== Nightly Orchestrator Job Started ===")
        orchestrator = NightlyOrchestrator()
        result = orchestrator.run()
        logger.info(f"Nightly orchestrator result: {result}")
    except Exception as e:
        logger.error(f"Nightly orchestrator job failed: {e}", exc_info=True)
    finally:
        gc.collect()


def _run_intel_scan(scan_type: str = "pre_market"):
    """Job: market intelligence scan."""
    try:
        logger.info(f"=== Intel Scan Job Started ({scan_type}) ===")
        scanner = MarketIntelScanner()
        result = scanner.scan(scan_type)
        logger.info(f"Intel scan result: {result}")
    except Exception as e:
        logger.error(f"Intel scan job failed: {e}", exc_info=True)
    finally:
        gc.collect()


def _run_intel_price_tracker():
    """Job: update intel issue price tracking."""
    try:
        logger.info("=== Intel Price Tracker Job Started ===")
        tracker = IntelPriceTracker()
        result = tracker.run()
        logger.info(f"Price tracker result: {result}")
    except Exception as e:
        logger.error(f"Intel price tracker job failed: {e}", exc_info=True)
    finally:
        gc.collect()


def _run_correlation_logger():
    """Job: log intel-signal correlations."""
    try:
        logger.info("=== Correlation Logger Job Started ===")
        cl = CorrelationLogger()
        result = cl.run()
        logger.info(f"Correlation logger result: {result}")
    except Exception as e:
        logger.error(f"Correlation logger job failed: {e}", exc_info=True)
    finally:
        gc.collect()


def _run_research_refresh():
    """Job: weekly research refresh."""
    try:
        logger.info("=== Research Refresh Job Started ===")
        runner = PipelineRunner()
        result = runner.run_full()
        logger.info(f"Research refresh result: {result}")
    except Exception as e:
        logger.error(f"Research refresh job failed: {e}", exc_info=True)
    finally:
        gc.collect()


def start_scheduler():
    """Start the APScheduler with configured jobs."""
    config = load_config()
    sched_cfg = config.get("schedule", {})
    tz = "Asia/Seoul"

    job_defaults = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}
    scheduler = BlockingScheduler(job_defaults=job_defaults)

    # Daily scan job (8:00 KST, weekdays)
    daily_cfg = sched_cfg.get("daily_scan", {})
    if daily_cfg.get("cron"):
        parts = daily_cfg["cron"].split()
        tz = daily_cfg.get("timezone", tz)
        trigger = CronTrigger(
            minute=parts[0], hour=parts[1], day=parts[2],
            month=parts[3], day_of_week=parts[4], timezone=tz
        )
        scheduler.add_job(_run_daily_scan, trigger, id="daily_scan", name="Daily Scan")
        logger.info(f"Scheduled daily scan: {daily_cfg['cron']} ({tz})")

    # KRX intraday scan (every 30min, 9:30~15:00 KST, weekdays)
    scheduler.add_job(
        _run_intraday_scan,
        CronTrigger(minute="0,30", hour="9-14", day_of_week="mon-fri", timezone=tz),
        id="krx_intraday",
        name="KRX Intraday Scan (30min)",
    )
    logger.info("Scheduled KRX intraday scan: every 30min 09:00-14:30 KST (weekdays)")

    # US intraday scan (every 30min, 23:30~05:30 KST, weekdays - US market hours)
    scheduler.add_job(
        _run_intraday_scan,
        CronTrigger(minute="0,30", hour="23", day_of_week="mon-fri", timezone=tz),
        id="us_intraday_night",
        name="US Intraday Scan (night)",
    )
    scheduler.add_job(
        _run_intraday_scan,
        CronTrigger(minute="0,30", hour="0-5", day_of_week="tue-sat", timezone=tz),
        id="us_intraday_early",
        name="US Intraday Scan (early morning)",
    )
    logger.info("Scheduled US intraday scan: every 30min 23:00-05:30 KST (weekdays)")

    # Evening performance report (19:00 KST, weekdays)
    scheduler.add_job(
        _run_evening_report,
        CronTrigger(minute="0", hour="19", day_of_week="mon-fri", timezone=tz),
        id="evening_report",
        name="Evening Performance Report",
    )
    logger.info("Scheduled evening report: 19:00 KST (weekdays)")

    # Research refresh job (weekly)
    research_cfg = sched_cfg.get("research_refresh", {})
    if research_cfg.get("cron") and research_cfg.get("enabled", True) is not False:
        parts = research_cfg["cron"].split()
        tz_r = research_cfg.get("timezone", tz)
        trigger = CronTrigger(
            minute=parts[0], hour=parts[1], day=parts[2],
            month=parts[3], day_of_week=parts[4], timezone=tz_r
        )
        scheduler.add_job(_run_research_refresh, trigger, id="research_refresh", name="Research Refresh")
        logger.info(f"Scheduled research refresh: {research_cfg['cron']} ({tz_r})")

    # Market intelligence scans (4 times daily)
    intel_cfg = config.get("market_intel", {})
    if intel_cfg.get("enabled", True) is not False:
        for scan_type, hour in [("pre_market", "9"), ("midday", "13"),
                                 ("post_market", "17"), ("overnight", "23")]:
            scheduler.add_job(
                _run_intel_scan,
                CronTrigger(minute="0", hour=hour, day_of_week="mon-fri", timezone=tz),
                id=f"intel_{scan_type}",
                name=f"Intel Scan ({scan_type})",
                kwargs={"scan_type": scan_type},
            )
        logger.info("Scheduled intel scans: 09/13/17/23 KST (weekdays)")

        # Price tracker (16:00 KST weekdays, after market close)
        scheduler.add_job(
            _run_intel_price_tracker,
            CronTrigger(minute="0", hour="16", day_of_week="mon-fri", timezone=tz),
            id="intel_price_tracker",
            name="Intel Price Tracker",
        )
        logger.info("Scheduled intel price tracker: 16:00 KST (weekdays)")

        # Correlation logger (18:00 KST weekdays)
        scheduler.add_job(
            _run_correlation_logger,
            CronTrigger(minute="0", hour="18", day_of_week="mon-fri", timezone=tz),
            id="correlation_logger",
            name="Intel-Signal Correlation Logger",
        )
        logger.info("Scheduled correlation logger: 18:00 KST (weekdays)")

    logger.info("Starting scheduler... (Ctrl+C to stop)")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")
