"""Automated multi-strategy discovery: research, extract, backtest, rank."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from pipeline.runner import PipelineRunner
from pipeline.ranking import StrategyRanker, StrategyScore
from strategy.registry import StrategyRegistry
from alerts.discord_webhook import DiscordNotifier
from alerts.formatter import AlertFormatter
from utils.config_loader import load_config

logger = logging.getLogger("money_mani.pipeline.discovery")


@dataclass
class DiscoveryReport:
    """Result of a discovery run."""
    date: str
    queries_used: list[str]
    videos_found: int
    strategies_extracted: int
    strategies_ranked: int
    strategies_validated: int
    rankings: list[StrategyScore]
    market: str
    trends: list[dict] = field(default_factory=list)


class StrategyDiscovery:
    """Orchestrates automated strategy discovery across multiple search queries."""

    def __init__(self, config: dict = None):
        self.config = config or load_config()
        self.runner = PipelineRunner(self.config)
        self.registry = StrategyRegistry()
        self.ranker = StrategyRanker()
        self.discord = DiscordNotifier()

    def run(self, queries: list[str] = None, market: str = "KRX",
            top_n: int = 3, use_trends: bool = False) -> DiscoveryReport:
        """Run full discovery pipeline.

        Args:
            queries: Search queries. If None, loads from search_queries.yaml.
            market: "KRX" or "US" for backtest targets.
            top_n: Number of top strategies to auto-validate.
            use_trends: If True, scan market trends first and auto-generate queries.

        Returns:
            DiscoveryReport with rankings and summary.
        """
        logger.info("=== Starting Strategy Discovery ===")

        # Trend-aware mode: scan market trends and generate queries
        trends = []
        if use_trends and not queries:
            from pipeline.trend_scanner import TrendScanner
            scanner = TrendScanner(self.config)
            trends = scanner.scan_trends()
            if trends:
                queries = scanner.generate_queries(trends)
                logger.info(f"Trend mode: detected {len(trends)} sectors, generated {len(queries)} queries")

        # Load queries
        if not queries:
            queries = self._load_default_queries()
        logger.info(f"Using {len(queries)} search queries")

        # Stage 1: Research all queries
        all_videos = self.runner.run_research(queries)
        logger.info(f"Total videos found: {len(all_videos)}")

        if not all_videos:
            logger.warning("No videos found. Discovery aborted.")
            return self._empty_report(queries, market, trends=trends)

        # Stage 2: Analyze
        analysis = self.runner.run_analysis(all_videos)

        # Stage 3: Extract strategies
        strategies = self.runner.run_extraction(analysis)
        logger.info(f"Extracted {len(strategies)} strategies")

        if not strategies:
            logger.warning("No strategies extracted.")
            return self._empty_report(queries, market, len(all_videos), trends=trends)

        # Stage 4: Backtest all extracted strategies
        results = self._backtest_all(market)

        # Stage 5: Rank
        rankings = self.ranker.rank(results)

        # Stage 6: Auto-validate top N
        validated_count = self._auto_validate(rankings, top_n)

        # Build report
        report = DiscoveryReport(
            date=datetime.now().strftime("%Y-%m-%d %H:%M"),
            queries_used=queries,
            videos_found=len(all_videos),
            strategies_extracted=len(strategies),
            strategies_ranked=len(rankings),
            strategies_validated=validated_count,
            rankings=rankings,
            market=market,
            trends=trends,
        )

        # Send Discord notification
        self._send_discord_report(report)

        logger.info(f"=== Discovery Complete: {len(strategies)} found, "
                    f"{len(rankings)} ranked, {validated_count} validated ===")
        return report

    def _load_default_queries(self) -> list[str]:
        """Load search queries from search_queries.yaml."""
        try:
            q_cfg = load_config("search_queries.yaml")
            queries = [q["term"] for q in q_cfg.get("queries", [])]
            return queries
        except Exception as e:
            logger.warning(f"Failed to load search_queries.yaml: {e}")
            return [
                "주식 기술적 분석 매매 전략",
                "주식 RSI MACD 매매법",
                "주식 골든크로스 데드크로스",
            ]

    def _backtest_all(self, market: str) -> list:
        """Backtest all strategies in registry against configured tickers."""
        cfg = self.config["pipeline"]["backtest"]
        start_date = cfg.get("default_period", "2020-01-01")
        capital = cfg.get("initial_capital", 10_000_000)
        commission = cfg.get("commission", 0.00015)

        # Get tickers
        if market == "US":
            tickers = self.config["pipeline"].get("us_targets", {}).get(
                "custom_tickers", ["AAPL", "MSFT", "GOOGL"])
        else:
            tickers = self.config["pipeline"]["targets"].get(
                "custom_tickers", ["005930", "000660", "035420"])

        # Load all strategies (including newly created drafts)
        all_names = self.registry.list_strategies()
        strategies = [self.registry.load(n) for n in all_names]

        from market_data import KRXFetcher, USFetcher
        from backtester.engine import BacktestEngine

        fetcher = KRXFetcher(delay=0.5) if market == "KRX" else USFetcher()
        engine = BacktestEngine(initial_capital=capital, commission=commission)
        results = []

        for strat in strategies:
            for ticker in tickers:
                try:
                    logger.info(f"Backtesting {strat.name} on {ticker}")
                    df = fetcher.get_ohlcv(ticker, start_date)
                    if df.empty or len(df) < 100:
                        logger.warning(f"Insufficient data for {ticker}")
                        continue
                    result = engine.run(df, strat, ticker)
                    results.append(result)
                except Exception as e:
                    logger.error(f"Backtest failed {strat.name}/{ticker}: {e}")

        logger.info(f"Completed {len(results)} backtests")
        return results

    def _auto_validate(self, rankings: list[StrategyScore],
                       top_n: int) -> int:
        """Set top N strategies to 'validated' status."""
        validated = 0
        for score in rankings[:top_n]:
            if score.valid_count == 0:
                logger.info(f"Skipping {score.strategy_name}: no valid backtest results")
                continue
            try:
                strat = self.registry.load(score.strategy_name)
                strat.status = "validated"
                self.registry.save_strategy(strat)
                logger.info(f"Validated: {score.strategy_name} "
                            f"(score={score.composite_score:.3f})")
                validated += 1
            except Exception as e:
                logger.warning(f"Failed to validate {score.strategy_name}: {e}")
        return validated

    def _send_discord_report(self, report: DiscoveryReport):
        """Send discovery summary to Discord."""
        try:
            embed = AlertFormatter.format_discovery_report(report)
            self.discord.send(embed=embed)
        except Exception as e:
            logger.warning(f"Failed to send Discord report: {e}")

    def _empty_report(self, queries, market, videos=0, trends=None):
        return DiscoveryReport(
            date=datetime.now().strftime("%Y-%m-%d %H:%M"),
            queries_used=queries,
            videos_found=videos,
            strategies_extracted=0,
            strategies_ranked=0,
            strategies_validated=0,
            rankings=[],
            market=market,
            trends=trends or [],
        )
