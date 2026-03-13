"""End-to-end pipeline: research -> analyze -> extract -> backtest."""

import logging
from datetime import datetime

from youtube_scraper import YouTubeScraper, SubtitleExtractor
from llm.video_filter import VideoFilter
from llm.strategy_refiner import StrategyRefiner
from notebooklm_analyzer.analyzer import NotebookLMAnalyzer
from strategy.extractor import StrategyExtractor
from strategy.registry import StrategyRegistry
from market_data import KRXFetcher, USFetcher
from backtester.signals import SignalGenerator
from backtester.engine import BacktestEngine
from backtester.report import format_text_report
from llm.backtest_interpreter import BacktestInterpreter
from utils.config_loader import load_config

logger = logging.getLogger("money_mani.pipeline.runner")


class PipelineRunner:
    """Orchestrates the full research-to-backtest pipeline."""

    def __init__(self, config: dict = None):
        self.config = config or load_config()
        self.registry = StrategyRegistry()

    def run_research(self, queries: list[str] = None, max_videos: int = None) -> list[dict]:
        """Stage 1: Search YouTube for stock strategy videos."""
        cfg = self.config["pipeline"]["research"]
        max_vids = max_videos or cfg.get("max_videos_per_query", 15)
        min_views = cfg.get("min_view_count", 10000)

        if not queries:
            from utils.config_loader import load_config
            q_cfg = load_config("search_queries.yaml")
            queries = [q["term"] for q in q_cfg["queries"][:3]]

        scraper = YouTubeScraper()
        all_videos = []
        for query in queries:
            logger.info(f"Searching YouTube: {query}")
            results = scraper.search(query, max_results=max_vids)
            filtered = [v for v in results if v.get("view_count", 0) >= min_views]
            all_videos.extend(filtered)
            logger.info(f"  Found {len(results)} videos, {len(filtered)} after view filter")

        # LLM quality filter
        try:
            vf = VideoFilter()
            all_videos = vf.filter_videos(all_videos)
            logger.info(f"After LLM filter: {len(all_videos)} videos")
        except Exception as e:
            logger.warning(f"LLM filter failed, using all videos: {e}")

        return all_videos

    def run_analysis(self, videos: list[dict], session_name: str = None) -> str:
        """Stage 2: Push videos to NotebookLM and get analysis."""
        name = session_name or f"StockResearch_{datetime.now().strftime('%Y%m%d_%H%M')}"
        urls = [v["url"] for v in videos if v.get("url")]

        analyzer = NotebookLMAnalyzer()
        try:
            notebook_id = analyzer.create_research_session(name)
            logger.info(f"Created notebook: {notebook_id}")
            analyzer.add_videos(notebook_id, urls)
            analysis = analyzer.extract_strategies(notebook_id)
            logger.info(f"NotebookLM analysis complete ({len(analysis)} chars)")
            return analysis
        except Exception as e:
            logger.warning(f"NotebookLM failed: {e}. Falling back to subtitle analysis.")
            return self._fallback_subtitle_analysis(videos)

    def _fallback_subtitle_analysis(self, videos: list[dict]) -> str:
        """Fallback: extract subtitles and analyze directly with LLM."""
        sub = SubtitleExtractor()
        texts = []
        for v in videos[:5]:
            try:
                text = sub.extract_text(v["url"], language="ko")
                if text:
                    texts.append(f"[{v.get('title', 'Unknown')}]\n{text[:3000]}")
            except Exception:
                continue
        combined = "\n\n---\n\n".join(texts)
        logger.info(f"Extracted subtitles from {len(texts)} videos")
        return combined

    def run_extraction(self, raw_analysis: str) -> list:
        """Stage 3: Extract strategies from analysis using LLM refinement."""
        refiner = StrategyRefiner()
        extractor = StrategyExtractor()
        strategies = extractor.extract_from_analysis(raw_analysis, refiner)
        logger.info(f"Extracted {len(strategies)} strategies")

        for strat in strategies:
            self.registry.save_strategy(strat)
            logger.info(f"  Saved: {strat.name} ({strat.category})")

        return strategies

    def run_backtest(self, strategy_name: str = None, tickers: list[str] = None,
                     market: str = "KRX") -> list:
        """Stage 4: Backtest strategies against historical data."""
        cfg = self.config["pipeline"]["backtest"]
        start_date = cfg.get("default_period", "2020-01-01")
        capital = cfg.get("initial_capital", 10_000_000)
        commission = cfg.get("commission_krx", cfg.get("commission", 0.00105))

        # Load strategies
        if strategy_name:
            strategies = [self.registry.load(strategy_name)]
        else:
            strategies = self.registry.get_validated()
            if not strategies:
                all_names = self.registry.list_strategies()
                strategies = [self.registry.load(n) for n in all_names]

        # Determine tickers
        if not tickers:
            if market == "US":
                tickers = self.config["pipeline"].get("us_targets", {}).get("custom_tickers", ["AAPL"])
            else:
                tickers = self.config["pipeline"]["targets"].get("custom_tickers", ["005930"])

        fetcher = KRXFetcher(delay=0.5) if market == "KRX" else USFetcher()
        engine = BacktestEngine(initial_capital=capital, commission=commission)
        interpreter = BacktestInterpreter()
        results = []

        for strat in strategies:
            for ticker in tickers:
                try:
                    logger.info(f"Backtesting {strat.name} on {ticker}")
                    df = fetcher.get_ohlcv(ticker, start_date)
                    if df.empty or len(df) < 100:
                        logger.warning(f"Insufficient data for {ticker}")
                        continue
                    result = engine.run(df, strat)
                    result.ticker = ticker

                    # LLM interpretation
                    try:
                        insight = interpreter.interpret(result.__dict__
                            if hasattr(result, '__dict__') else {})
                        logger.info(f"  LLM insight: {insight[:80]}...")
                    except Exception:
                        pass

                    report = format_text_report(result)
                    logger.info(f"\n{report}")
                    results.append(result)
                except Exception as e:
                    logger.error(f"Backtest failed for {strat.name}/{ticker}: {e}")

        return results

    def run_full(self, queries: list[str] = None) -> dict:
        """Run the complete pipeline: research -> analyze -> extract -> backtest."""
        logger.info("=== Starting Full Pipeline ===")

        # Stage 1
        videos = self.run_research(queries)
        if not videos:
            logger.warning("No videos found. Aborting.")
            return {"videos": 0, "strategies": 0, "results": []}

        # Stage 2
        analysis = self.run_analysis(videos)

        # Stage 3
        strategies = self.run_extraction(analysis)
        if not strategies:
            logger.warning("No strategies extracted.")
            return {"videos": len(videos), "strategies": 0, "results": []}

        # Stage 4
        results = self.run_backtest()

        logger.info(f"=== Pipeline Complete: {len(videos)} videos, {len(strategies)} strategies, {len(results)} backtests ===")
        return {
            "videos": len(videos),
            "strategies": len(strategies),
            "results": results,
        }
