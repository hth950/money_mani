"""Nightly orchestrator: runs evening report, position cleanup, analytics, and knowledge."""

import logging

from pipeline.evening_report import EveningReport
from web.services.position_service import PositionService
from web.services.analytics_service import AnalyticsService

logger = logging.getLogger("money_mani.pipeline.nightly")


class NightlyOrchestrator:
    """Coordinate all nightly tasks without bloating EveningReport."""

    def __init__(self):
        self.evening_report = EveningReport()
        self.position_service = PositionService()
        self.analytics_service = AnalyticsService()

    def run(self, target_date: str = None) -> dict:
        """Run full nightly pipeline.

        1. Evening P&L evaluation (existing)
        2. Auto-close expired positions
        3. Refresh strategy analytics
        4. Generate knowledge insights (Phase 4)
        5. Update MEMORY.md (Phase 4)
        """
        logger.info("=== Nightly Orchestrator Started ===")

        # Step 1: Evening P&L report
        report_result = self.evening_report.run(target_date)

        # Step 2: Close expired positions
        try:
            closed = self.position_service.close_expired_positions(
                close_price_fetcher=self._fetch_close_price
            )
            if closed:
                logger.info(f"Auto-closed {len(closed)} expired positions")
        except Exception as e:
            logger.error(f"Failed to close expired positions: {e}")

        # Step 3: Refresh strategy analytics
        try:
            self.analytics_service.refresh_all_stats()
        except Exception as e:
            logger.error(f"Failed to refresh analytics: {e}")

        # Step 4 & 5: Knowledge (added in Phase 4)
        self._run_knowledge_tasks()

        logger.info("=== Nightly Orchestrator Complete ===")
        return report_result

    def _fetch_close_price(self, ticker: str, market: str) -> float | None:
        """Fetch closing price for a ticker."""
        try:
            if market == "KRX":
                from market_data import KRXFetcher
                fetcher = KRXFetcher(delay=0.5)
            else:
                from market_data import USFetcher
                fetcher = USFetcher()

            from datetime import datetime, timedelta, timezone
            today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
            df = fetcher.get_ohlcv(ticker, today)
            if not df.empty:
                return float(df.iloc[-1]["Close"])
        except Exception as e:
            logger.error(f"Close price fetch failed for {ticker}: {e}")
        return None

    def _run_knowledge_tasks(self):
        """Placeholder for Phase 4 knowledge generation."""
        try:
            from web.services.knowledge_service import KnowledgeService
            ks = KnowledgeService()
            ks.generate_strategy_insights()
            ks.update_memory_md()
        except ImportError:
            pass  # Phase 4 not yet implemented
        except Exception as e:
            logger.error(f"Knowledge tasks failed: {e}")
