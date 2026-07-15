"""CLI script to run walk-forward validation on all validated strategies."""

import sys
import json
import logging
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy.registry import StrategyRegistry
from backtester.walk_forward import WalkForwardValidator
from market_data.krx_fetcher import KRXFetcher
from market_data.us_fetcher import USFetcher
from web.db.connection import get_db
from web.db.migrate import run_schema_migrations

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("walk_forward_validate")

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Walk-forward validation")
    parser.add_argument("--tickers", nargs="+", default=["005930", "000660", "035420"])
    parser.add_argument("--start", default="2021-01-01", help="Data start date")
    parser.add_argument("--market", default="KRX", choices=["KRX", "US"])
    parser.add_argument("--mark-overfit", action="store_true", help="Update strategy status to overfit_suspect")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Only log what would be marked (default)")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                        help="Actually mark overfit strategies")
    args = parser.parse_args()

    # Use the application's canonical additive schema.  This keeps direct CLI
    # runs compatible with web startup and preserves older evidence rows.
    run_schema_migrations()

    registry = StrategyRegistry()
    strategies = registry.get_validated()
    # Filter by market
    strategies = [s for s in strategies if s.market in (args.market, "ALL")]

    logger.info(f"Validating {len(strategies)} strategies on {len(args.tickers)} tickers")

    validator = WalkForwardValidator()
    fetcher = KRXFetcher(delay=0.5) if args.market == "KRX" else USFetcher()

    overfit_strategies = set()

    for ticker in args.tickers:
        logger.info(f"\n--- Ticker: {ticker} ---")
        df = fetcher.get_ohlcv(ticker, args.start)
        if df.empty or len(df) < 315:  # need at least train+test
            logger.warning(f"Insufficient data for {ticker}: {len(df)} rows")
            continue

        for strat in strategies:
            result = validator.validate(strat, df, ticker)

            # Save to DB
            windows_data = [
                {
                    "idx": w.window_idx,
                    "train": f"{w.train_start}~{w.train_end}",
                    "test": f"{w.test_start}~{w.test_end}",
                    "train_sharpe": round(w.train_sharpe, 3),
                    "test_sharpe": round(w.test_sharpe, 3),
                }
                for w in result.windows
            ]

            with get_db() as db:
                db.execute("""
                    INSERT INTO walk_forward_results
                    (strategy_name, ticker, market, total_windows, valid_windows,
                     avg_train_sharpe, avg_test_sharpe, sharpe_degradation,
                     is_overfit, overfit_reason, windows_json, train_days,
                     test_days, step_days, overfit_threshold, min_windows)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    result.strategy_name, ticker, args.market,
                    result.total_windows, result.valid_windows,
                    round(result.avg_train_sharpe, 4),
                    round(result.avg_test_sharpe, 4),
                    round(result.sharpe_degradation, 4),
                    1 if result.is_overfit else 0,
                    result.overfit_reason,
                    json.dumps(windows_data, ensure_ascii=False),
                    validator.train_days,
                    validator.test_days,
                    validator.step_days,
                    validator.overfit_threshold,
                    validator.min_windows,
                ))

            if result.is_overfit:
                overfit_strategies.add(strat.name)
                logger.warning(f"OVERFIT: {strat.name} on {ticker} - {result.overfit_reason}")

    # Summary
    logger.info(f"\n=== Walk-Forward Summary ===")
    logger.info(f"Strategies tested: {len(strategies)}")
    logger.info(f"Overfit suspects: {len(overfit_strategies)}")
    for name in sorted(overfit_strategies):
        logger.info(f"  - {name}")

    if args.mark_overfit and overfit_strategies:
        if args.dry_run:
            logger.info(f"[DRY-RUN] Would mark {len(overfit_strategies)} strategies as overfit: {sorted(overfit_strategies)}")
        else:
            for name in sorted(overfit_strategies):
                try:
                    with get_db() as db:
                        # Update strategy status to retired
                        db.execute(
                            "UPDATE strategies SET status = 'retired', updated_at = datetime('now') WHERE name = ?",
                            (name,),
                        )
                    logger.info(f"Marked strategy '{name}' as retired (overfit)")
                except Exception as e:
                    logger.warning(f"Failed to mark {name}: {e}")
            logger.info(f"Marked {len(overfit_strategies)} strategies as retired (overfit)")


if __name__ == "__main__":
    main()
