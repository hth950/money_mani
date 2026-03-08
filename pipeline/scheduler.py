"""APScheduler setup for periodic pipeline runs."""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from pipeline.daily_scan import DailyScan
from pipeline.runner import PipelineRunner
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


def _run_research_refresh():
    """Job: weekly research refresh."""
    try:
        logger.info("=== Research Refresh Job Started ===")
        runner = PipelineRunner()
        result = runner.run_full()
        logger.info(f"Research refresh result: {result}")
    except Exception as e:
        logger.error(f"Research refresh job failed: {e}", exc_info=True)


def start_scheduler():
    """Start the APScheduler with configured jobs."""
    config = load_config()
    sched_cfg = config.get("schedule", {})

    scheduler = BlockingScheduler()

    # Daily scan job
    daily_cfg = sched_cfg.get("daily_scan", {})
    if daily_cfg.get("cron"):
        parts = daily_cfg["cron"].split()
        tz = daily_cfg.get("timezone", "Asia/Seoul")
        trigger = CronTrigger(
            minute=parts[0], hour=parts[1], day=parts[2],
            month=parts[3], day_of_week=parts[4], timezone=tz
        )
        scheduler.add_job(_run_daily_scan, trigger, id="daily_scan", name="Daily Scan")
        logger.info(f"Scheduled daily scan: {daily_cfg['cron']} ({tz})")

    # Research refresh job
    research_cfg = sched_cfg.get("research_refresh", {})
    if research_cfg.get("cron"):
        parts = research_cfg["cron"].split()
        tz = research_cfg.get("timezone", "Asia/Seoul")
        trigger = CronTrigger(
            minute=parts[0], hour=parts[1], day=parts[2],
            month=parts[3], day_of_week=parts[4], timezone=tz
        )
        scheduler.add_job(_run_research_refresh, trigger, id="research_refresh", name="Research Refresh")
        logger.info(f"Scheduled research refresh: {research_cfg['cron']} ({tz})")

    logger.info("Starting scheduler... (Ctrl+C to stop)")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")
