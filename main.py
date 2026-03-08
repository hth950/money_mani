"""money_mani CLI entry point."""

import argparse
import sys
import logging

from utils.config_loader import load_config
from utils.logging_config import setup_logging


def cmd_research(args):
    """Run YouTube research pipeline."""
    from pipeline.runner import PipelineRunner
    runner = PipelineRunner()
    queries = [args.query] if args.query else None
    videos = runner.run_research(queries, max_videos=args.count)
    print(f"\nFound {len(videos)} videos:")
    for v in videos[:10]:
        print(f"  - {v.get('title', 'N/A')} ({v.get('view_count', 0):,} views)")


def cmd_analyze(args):
    """Run NotebookLM analysis on videos."""
    from pipeline.runner import PipelineRunner
    runner = PipelineRunner()
    videos = runner.run_research([args.query] if args.query else None)
    if not videos:
        print("No videos found.")
        return
    analysis = runner.run_analysis(videos, args.name)
    print(f"\nAnalysis ({len(analysis)} chars):")
    print(analysis[:500] + "..." if len(analysis) > 500 else analysis)


def cmd_backtest(args):
    """Run backtest on a strategy."""
    from pipeline.runner import PipelineRunner
    runner = PipelineRunner()
    tickers = args.tickers.split(",") if args.tickers else None
    results = runner.run_backtest(args.strategy, tickers, args.market)
    print(f"\nBacktest complete: {len(results)} results")
    for r in results:
        from backtester.report import format_text_report
        print(format_text_report(r))


def cmd_scan(args):
    """Run daily scan."""
    from pipeline.daily_scan import DailyScan
    scan = DailyScan()
    result = scan.run()
    n = len(result.get("signals", []))
    print(f"\nScan complete: {n} signal(s) found on {result['date']}")
    for s in result.get("signals", []):
        print(f"  {s['signal_type']} {s['ticker_name']}({s['ticker']}) @ {s['price']:,.0f} [{s['strategy_name']}]")


def cmd_schedule(args):
    """Start the scheduler."""
    from pipeline.scheduler import start_scheduler
    print("Starting scheduler (Ctrl+C to stop)...")
    start_scheduler()


def cmd_full(args):
    """Run the full pipeline."""
    from pipeline.runner import PipelineRunner
    runner = PipelineRunner()
    result = runner.run_full([args.query] if args.query else None)
    print(f"\nPipeline complete:")
    print(f"  Videos: {result['videos']}")
    print(f"  Strategies: {result['strategies']}")
    print(f"  Backtests: {len(result['results'])}")


def cmd_strategies(args):
    """List all strategies."""
    from strategy.registry import StrategyRegistry
    registry = StrategyRegistry()
    names = registry.list_strategies()
    print(f"\nStrategies ({len(names)}):")
    for name in names:
        strat = registry.load(name)
        status_icon = {"validated": "V", "testing": "~", "draft": "o", "retired": "X"}.get(strat.status, "?")
        print(f"  [{status_icon}] {strat.name} ({strat.category}) - {strat.status}")


def cmd_monitor(args):
    """Start real-time monitoring."""
    from monitor.realtime_monitor import RealtimeMonitor
    market = args.market if hasattr(args, "market") and args.market else None
    monitor = RealtimeMonitor(market_filter=market)
    print("Starting real-time monitor (Ctrl+C to stop)...")
    try:
        monitor.start()
    except KeyboardInterrupt:
        monitor.stop()
        print("\nMonitor stopped.")


def cmd_discover(args):
    """Run automated strategy discovery."""
    from pipeline.discovery import StrategyDiscovery
    discovery = StrategyDiscovery()
    queries = args.queries.split(",") if args.queries else None
    market = args.market or "KRX"
    top_n = args.top or 3
    use_trends = getattr(args, "trends", False)

    mode = "trend-aware" if use_trends else "standard"
    print(f"\nStarting strategy discovery ({mode}, market={market}, top_n={top_n})...")
    report = discovery.run(queries=queries, market=market, top_n=top_n, use_trends=use_trends)

    if report.trends:
        print(f"\n  Detected trends:")
        for t in report.trends:
            print(f"    - {t.get('sector', '?')}: {t.get('reason', '')}")

    print(f"\n{'='*60}")
    print(f"  Strategy Discovery Results ({report.date})")
    print(f"{'='*60}")
    print(f"  Videos found:        {report.videos_found}")
    print(f"  Strategies extracted: {report.strategies_extracted}")
    print(f"  Strategies ranked:    {report.strategies_ranked}")
    print(f"  Strategies validated: {report.strategies_validated}")

    if report.rankings:
        print(f"\n{'  Rank':<8}{'Strategy':<30}{'Return':>10}{'Sharpe':>10}{'MDD':>10}{'WinRate':>10}{'Score':>10}")
        print(f"  {'─'*86}")
        for i, s in enumerate(report.rankings[:10], 1):
            ret_sign = "+" if s.avg_return >= 0 else ""
            ret_str = f"{ret_sign}{s.avg_return:.1%}"
            mdd_str = f"{s.avg_mdd:.1%}"
            wr_str = f"{s.avg_win_rate:.1%}"
            print(f"  {i:<8}{s.strategy_name:<30}"
                  f"{ret_str:>10}"
                  f"{s.avg_sharpe:>10.2f}"
                  f"{mdd_str:>10}"
                  f"{wr_str:>10}"
                  f"{s.composite_score:>10.3f}")
    else:
        print("\n  No strategies could be ranked.")
    print()


def cmd_portfolio(args):
    """Show current portfolio holdings."""
    from broker.kis_client import KISClient
    from broker.portfolio import PortfolioManager
    kis = KISClient()
    pm = PortfolioManager(kis)
    holdings = pm.fetch_all_holdings()
    if not holdings:
        print("No holdings found.")
        kis.close()
        return
    print(f"\nPortfolio ({len(holdings)} stocks):")
    for t, h in holdings.items():
        pnl_sign = "+" if h.pnl_pct >= 0 else ""
        currency = "원" if h.market == "KRX" else "$"
        print(f"  {h.name}({h.ticker}) x{h.quantity} | "
              f"avg:{h.avg_price:,.0f}{currency} | "
              f"now:{h.current_price:,.0f}{currency} | "
              f"{pnl_sign}{h.pnl_pct:.2f}%")
    kis.close()


def main():
    parser = argparse.ArgumentParser(
        description="money_mani - Stock Investment Research & Alert Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py research -q "주식 골든크로스 전략"
  python main.py backtest -s example_golden_cross -t 005930
  python main.py scan
  python main.py schedule
  python main.py full
  python main.py strategies
  python main.py monitor
  python main.py monitor -m KRX
  python main.py portfolio
  python main.py discover
  python main.py discover -m US -n 5
        """,
    )
    subparsers = parser.add_subparsers(dest="command")

    # research
    p_res = subparsers.add_parser("research", help="Search YouTube for stock strategies")
    p_res.add_argument("-q", "--query", help="Search query")
    p_res.add_argument("-n", "--count", type=int, default=15, help="Max videos per query")
    p_res.set_defaults(func=cmd_research)

    # analyze
    p_ana = subparsers.add_parser("analyze", help="Run NotebookLM analysis")
    p_ana.add_argument("-q", "--query", help="Search query")
    p_ana.add_argument("--name", help="Notebook session name")
    p_ana.set_defaults(func=cmd_analyze)

    # backtest
    p_bt = subparsers.add_parser("backtest", help="Backtest a strategy")
    p_bt.add_argument("-s", "--strategy", help="Strategy name (from config/strategies/)")
    p_bt.add_argument("-t", "--tickers", help="Comma-separated ticker list")
    p_bt.add_argument("-m", "--market", default="KRX", choices=["KRX", "US"])
    p_bt.set_defaults(func=cmd_backtest)

    # scan
    p_scan = subparsers.add_parser("scan", help="Run daily scan")
    p_scan.set_defaults(func=cmd_scan)

    # schedule
    p_sched = subparsers.add_parser("schedule", help="Start the scheduler")
    p_sched.set_defaults(func=cmd_schedule)

    # full
    p_full = subparsers.add_parser("full", help="Run full pipeline")
    p_full.add_argument("-q", "--query", help="Search query")
    p_full.set_defaults(func=cmd_full)

    # strategies
    p_strat = subparsers.add_parser("strategies", help="List all strategies")
    p_strat.set_defaults(func=cmd_strategies)

    # monitor
    p_mon = subparsers.add_parser("monitor", help="Start real-time monitoring")
    p_mon.add_argument("-m", "--market", choices=["KRX", "US"], help="Monitor specific market only")
    p_mon.set_defaults(func=cmd_monitor)

    # portfolio
    p_port = subparsers.add_parser("portfolio", help="Show current holdings")
    p_port.set_defaults(func=cmd_portfolio)

    # discover
    p_disc = subparsers.add_parser("discover", help="Auto-discover and rank strategies")
    p_disc.add_argument("-q", "--queries", help="Comma-separated search queries")
    p_disc.add_argument("-m", "--market", default="KRX", choices=["KRX", "US"])
    p_disc.add_argument("-n", "--top", type=int, default=3, help="Top N strategies to validate")
    p_disc.add_argument("--trends", action="store_true", help="Scan market trends and auto-generate queries")
    p_disc.set_defaults(func=cmd_discover)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Setup
    config = load_config()
    setup_logging(config.get("logging", {}))

    args.func(args)


if __name__ == "__main__":
    main()
