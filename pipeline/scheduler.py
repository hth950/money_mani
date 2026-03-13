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


def _start_monitor(market_filter: str = None):
    """Job: auto-start realtime monitor via web API (force restart)."""
    import requests
    try:
        # Stop first if running
        requests.post("http://localhost:8000/api/monitor/stop", timeout=5)
        import time
        time.sleep(1)
        # Start
        params = {}
        if market_filter:
            params["market_filter"] = market_filter
        resp = requests.post("http://localhost:8000/api/monitor/start",
                             params=params, timeout=10)
        logger.info(f"Monitor auto-start: {resp.json()}")
    except Exception as e:
        logger.error(f"Monitor auto-start failed: {e}", exc_info=True)


def _stop_monitor():
    """Job: auto-stop realtime monitor via web API."""
    import requests
    try:
        resp = requests.post("http://localhost:8000/api/monitor/stop", timeout=5)
        logger.info(f"Monitor auto-stop: {resp.json()}")
    except Exception as e:
        logger.error(f"Monitor auto-stop failed: {e}", exc_info=True)


def _reset_dart_counter():
    """Job: reset DART daily API call counter at midnight."""
    try:
        from scoring.dart_fundamental import reset_dart_counter
        reset_dart_counter()
    except Exception as e:
        logger.error(f"DART counter reset failed: {e}")


def _run_correlation_report():
    """Job: weekly score-return correlation report (Sunday 09:00 KST)."""
    try:
        logger.info("=== Weekly Correlation Report Job Started ===")
        from pipeline.correlation_report import CorrelationReport
        result = CorrelationReport().run()
        logger.info(f"Correlation report result: {result}")
    except Exception as e:
        logger.error(f"Correlation report job failed: {e}", exc_info=True)
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

    # Realtime monitor auto-start/stop
    # KRX: 08:50 start -> 15:35 stop (weekdays)
    scheduler.add_job(
        _start_monitor,
        CronTrigger(minute="50", hour="8", day_of_week="mon-fri", timezone=tz),
        id="monitor_krx_start",
        name="Monitor KRX Auto-Start",
        kwargs={"market_filter": None},  # Start for all markets
    )
    scheduler.add_job(
        _stop_monitor,
        CronTrigger(minute="35", hour="15", day_of_week="mon-fri", timezone=tz),
        id="monitor_krx_stop",
        name="Monitor KRX Auto-Stop",
    )
    logger.info("Scheduled monitor auto-start: 08:50 KST / auto-stop: 15:35 KST (weekdays)")

    # US: 22:50 start -> 06:05 stop (Mon-Fri start, Tue-Sat stop)
    scheduler.add_job(
        _start_monitor,
        CronTrigger(minute="50", hour="22", day_of_week="mon-fri", timezone=tz),
        id="monitor_us_start",
        name="Monitor US Auto-Start",
        kwargs={"market_filter": None},
    )
    scheduler.add_job(
        _stop_monitor,
        CronTrigger(minute="5", hour="6", day_of_week="tue-sat", timezone=tz),
        id="monitor_us_stop",
        name="Monitor US Auto-Stop",
    )
    logger.info("Scheduled monitor auto-start: 22:50 KST / auto-stop: 06:05 KST (US hours)")

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
        # KRX intel: hourly 09:00-15:00 KST
        krx_scan_hours = {
            9: "pre_market", 10: "midday", 11: "midday",
            12: "midday", 13: "midday", 14: "post_market", 15: "post_market",
        }
        for hour, scan_type in krx_scan_hours.items():
            scheduler.add_job(
                _run_intel_scan,
                CronTrigger(minute="0", hour=str(hour), day_of_week="mon-fri", timezone=tz),
                id=f"intel_krx_{hour:02d}",
                name=f"KRX Intel ({hour:02d}:00)",
                kwargs={"scan_type": scan_type},
            )
        logger.info("Scheduled KRX intel scans: hourly 09:00-15:00 KST (weekdays)")

        # US intel: hourly during US market hours (KST)
        # US regular session: 23:30~06:00 KST -> hourly 23,0,1,2,3,4,5,6
        us_scan_hours = {
            23: ("mon-fri", "us_pre_market"),
            0: ("tue-sat", "us_midday"),
            1: ("tue-sat", "us_midday"),
            2: ("tue-sat", "us_midday"),
            3: ("tue-sat", "us_midday"),
            4: ("tue-sat", "us_midday"),
            5: ("tue-sat", "us_post_market"),
            6: ("tue-sat", "us_post_market"),
        }
        for hour, (dow, scan_type) in us_scan_hours.items():
            scheduler.add_job(
                _run_intel_scan,
                CronTrigger(minute="0", hour=str(hour), day_of_week=dow, timezone=tz),
                id=f"intel_us_{hour:02d}",
                name=f"US Intel ({hour:02d}:00)",
                kwargs={"scan_type": scan_type},
            )
        logger.info("Scheduled US intel scans: hourly 23:00-06:00 KST (US market hours)")

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

    # DART counter reset (매일 00:05 KST)
    scheduler.add_job(
        _reset_dart_counter,
        CronTrigger(minute="5", hour="0", timezone=tz),
        id="dart_counter_reset",
        name="DART Daily Counter Reset",
    )
    logger.info("Scheduled DART counter reset: 00:05 KST (daily)")

    # Weekly correlation report (매주 일요일 09:00 KST)
    scheduler.add_job(
        _run_correlation_report,
        CronTrigger(day_of_week="sun", hour="9", minute="0", timezone=tz),
        id="weekly_correlation_report",
        name="Weekly Score-Return Correlation Report",
    )
    logger.info("Scheduled weekly correlation report: Sunday 09:00 KST")

    # On startup: if currently within market hours, auto-start monitor
    _auto_start_monitor_if_market_open()

    # On startup: pre-load today's sent signals to avoid re-sending after restart
    _preload_sent_signals()

    logger.info("Starting scheduler... (Ctrl+C to stop)")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


def _auto_start_monitor_if_market_open():
    """If scheduler starts during market hours, immediately start the monitor."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Asia/Seoul"))
    hour = now.hour
    weekday = now.weekday()  # 0=Mon, 6=Sun

    should_start = False

    # KRX hours: 08:50-15:35 KST, Mon-Fri
    if weekday < 5 and ((hour == 8 and now.minute >= 50) or (9 <= hour <= 14) or (hour == 15 and now.minute <= 35)):
        should_start = True
        logger.info(f"Startup during KRX hours ({now.strftime('%H:%M')} KST)")

    # US hours: 22:50-06:05 KST
    # Mon-Fri 22:50+ or Tue-Sat 00:00-06:05
    if weekday < 5 and hour >= 22 and now.minute >= 50:
        should_start = True
        logger.info(f"Startup during US hours ({now.strftime('%H:%M')} KST)")
    elif weekday > 0 and weekday <= 5 and (hour < 6 or (hour == 6 and now.minute <= 5)):
        should_start = True
        logger.info(f"Startup during US hours ({now.strftime('%H:%M')} KST)")

    if should_start:
        import threading
        threading.Timer(5.0, _start_monitor).start()
        logger.info("Monitor auto-start scheduled in 5 seconds")


def _preload_sent_signals():
    """On restart, load today's signals from DB to prevent re-sending."""
    from datetime import datetime, timedelta, timezone
    from pipeline.daily_scan import _sent_signals_today
    from web.db.connection import get_db

    KST = timezone(timedelta(hours=9))
    today = datetime.now(KST).strftime("%Y-%m-%d")
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT strategy_name, ticker, signal_type FROM signals WHERE DATE(detected_at) = ?",
                (today,),
            ).fetchall()
        _sent_signals_today[today] = set()
        for row in rows:
            key = f"{row['strategy_name']}|{row['ticker']}|{row['signal_type']}"
            _sent_signals_today[today].add(key)
        count = len(_sent_signals_today[today])
        logger.info(f"Pre-loaded {count} sent signals for {today}")
        from alerts.discord_webhook import DiscordNotifier
        DiscordNotifier().send(
            content=f"🔄 Money Mani 스케줄러 재시작 ({today}) - 오늘 신호 {count}건 중복 발송 차단"
        )
    except Exception as e:
        logger.warning(f"Failed to preload sent signals: {e}")
