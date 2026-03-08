"""Evening report: fetch closing prices for today's signals and send P&L summary."""

import gc
import logging
from datetime import datetime, timedelta, timezone

from market_data import KRXFetcher, USFetcher
from alerts.discord_webhook import DiscordNotifier
from alerts.formatter import AlertFormatter
from web.services.performance_service import PerformanceService

logger = logging.getLogger("money_mani.pipeline.evening_report")

KST = timezone(timedelta(hours=9))


def _today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


class EveningReport:
    """Fetch closing prices for today's signals and generate P&L report."""

    def __init__(self):
        self.perf_service = PerformanceService()
        self.discord = DiscordNotifier()

    def run(self, target_date: str = None) -> dict:
        """Run evening evaluation for a given date.

        1. Get all unevaluated signals for the date
        2. Fetch closing prices
        3. Calculate P&L
        4. Send Discord report
        5. Save report to DB
        """
        date_str = target_date or _today_kst()
        logger.info(f"=== Evening Report for {date_str} ===")

        # 1. Get unevaluated signals
        pending = self.perf_service.get_unevaluated(date_str)
        if not pending:
            logger.info(f"{date_str}: No signals to evaluate.")
            self.discord.send(content=f"📊 {date_str} 저녁 리포트: 오늘 발생한 시그널이 없습니다.")
            return {"date": date_str, "evaluated": 0}

        # 2. Fetch closing prices and update P&L
        evaluated = 0
        krx_fetcher = None
        us_fetcher = None

        for sig in pending:
            try:
                ticker = sig["ticker"]
                market = sig.get("market", "KRX")

                if market == "KRX":
                    if krx_fetcher is None:
                        krx_fetcher = KRXFetcher(delay=0.5)
                    df = krx_fetcher.get_ohlcv(ticker, date_str)
                else:
                    if us_fetcher is None:
                        us_fetcher = USFetcher()
                    df = us_fetcher.get_ohlcv(ticker, date_str)

                if df.empty:
                    logger.warning(f"No data for {ticker} on {date_str}")
                    continue

                close_price = float(df.iloc[-1]["Close"])
                self.perf_service.update_close_price(sig["id"], close_price)
                evaluated += 1
                logger.info(
                    f"Evaluated {ticker}: signal={sig['signal_price']:.0f} "
                    f"close={close_price:.0f}"
                )

            except Exception as e:
                logger.error(f"Error evaluating {sig['ticker']}: {e}")

        gc.collect()

        # 3. Generate summary
        daily_summary = self.perf_service.get_performance_summary(date_str)

        # 4. Save report
        report_id = self.perf_service.save_report(daily_summary, "daily")

        # 5. Send Discord report
        if daily_summary["total_signals"] > 0:
            embed = AlertFormatter.format_performance_report(daily_summary, "daily")
            sent = self.discord.send(embed=embed)
            if sent:
                self.perf_service.mark_report_sent(report_id)

        # 6. Weekly report on Friday
        now = datetime.now(KST)
        if now.weekday() == 4:  # Friday
            self._send_weekly_report(date_str)

        logger.info(f"Evening report done: {evaluated}/{len(pending)} signals evaluated")
        return {"date": date_str, "evaluated": evaluated, "summary": daily_summary}

    def _send_weekly_report(self, end_date: str):
        """Generate and send weekly performance report."""
        try:
            weekly_summary = self.perf_service.get_weekly_summary(end_date)
            if weekly_summary["total_signals"] > 0:
                report_id = self.perf_service.save_report(weekly_summary, "weekly")
                embed = AlertFormatter.format_performance_report(weekly_summary, "weekly")
                sent = self.discord.send(embed=embed)
                if sent:
                    self.perf_service.mark_report_sent(report_id)
                logger.info("Weekly report sent.")
        except Exception as e:
            logger.error(f"Weekly report failed: {e}")
