"""Re-validate all 'validated' strategies against stricter criteria.

Usage:
    python scripts/revalidate_strategies.py [--dry-run] [--apply] [--workers N]

Modes:
    --dry-run  (default) Output report to output/revalidation_report.json, no YAML changes
    --apply    Apply results: set failed strategies to status=rejected in YAML
"""

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import yaml

from utils.config_loader import load_config
from strategy.models import Strategy
from strategy.registry import StrategyRegistry
from backtester.engine import BacktestEngine
from backtester.metrics import BacktestResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("output/revalidation.log"),
    ],
)
logger = logging.getLogger("revalidate")

CACHE_DIR = Path("output/data_cache")
OUTPUT_DIR = Path("output")

# S&P 500 top 50 (same as prefetch_ohlcv.py)
US_TOP_50 = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "BRK-B", "LLY", "AVGO", "TSLA",
    "WMT", "JPM", "UNH", "V", "XOM", "MA", "ORCL", "COST", "HD", "PG",
    "JNJ", "ABBV", "BAC", "NFLX", "CRM", "AMD", "KO", "CVX", "MRK", "ADBE",
    "PEP", "TMO", "ACN", "LIN", "MCD", "CSCO", "QCOM", "GE", "ABT", "DHR",
    "VZ", "TXN", "AMGN", "NOW", "PM", "IBM", "CAT", "INTU", "SPGI", "GS",
]


def load_ohlcv(market: str, ticker: str, start: str) -> pd.DataFrame:
    """Load OHLCV from cache or fetch live."""
    cache_path = CACHE_DIR / f"{market}_{ticker}_{start}.parquet"
    if cache_path.exists():
        df = pd.read_parquet(cache_path)
        return df

    # Live fetch (slower path)
    if market == "KRX":
        try:
            import pykrx.stock as krx
            import time
            time.sleep(1.0)
            df = krx.get_market_ohlcv(start.replace("-", ""), "20251231", ticker)
            if not df.empty:
                df.index = pd.to_datetime(df.index)
                df = df.iloc[:, :5]
                df.columns = ["Open", "High", "Low", "Close", "Volume"]
                return df.dropna()
        except Exception:
            pass
        try:
            import yfinance as yf
            df = yf.download(f"{ticker}.KS", start=start, progress=False, auto_adjust=True)
            if not df.empty:
                return df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        except Exception:
            pass
    else:
        try:
            import yfinance as yf
            df = yf.download(ticker, start=start, progress=False, auto_adjust=True)
            if not df.empty:
                return df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        except Exception:
            pass
    return pd.DataFrame()


def backtest_strategy_on_ticker(
    strategy: Strategy,
    market: str,
    ticker: str,
    start: str,
    commission: float,
) -> dict:
    """Run single backtest and return result summary."""
    try:
        df = load_ohlcv(market, ticker, start)
        if df.empty or len(df) < 200:
            return {"ticker": ticker, "skipped": True, "reason": "insufficient_data"}

        engine = BacktestEngine(
            initial_capital=10_000_000 if market == "KRX" else 100_000,
            commission=commission,
        )
        result = engine.run(df, strategy, ticker=ticker, market=market)
        return {
            "ticker": ticker,
            "skipped": False,
            "is_valid": result.is_valid,
            "sharpe": round(result.sharpe_ratio, 3),
            "mdd": round(result.max_drawdown, 3),
            "total_return": round(result.total_return, 3),
            "win_rate": round(result.win_rate, 3),
            "num_trades": result.num_trades,
            "annual_trade_rate": round(result.annual_trade_rate, 2),
            "avg_holding_days": round(result.avg_holding_days, 1),
        }
    except Exception as e:
        return {"ticker": ticker, "skipped": True, "reason": str(e)}


def validate_strategy(
    strategy: Strategy,
    krx_tickers: list[str],
    us_tickers: list[str],
    cfg: dict,
    workers: int = 4,
) -> dict:
    """Run multi-ticker backtest for one strategy and return validation result."""
    start = cfg["backtest"].get("default_period", "2013-01-01")
    commission_krx = cfg["backtest"].get("commission_krx", 0.00105)
    commission_us = cfg["backtest"].get("commission_us", 0.0)
    min_pass_rate = cfg["backtest"]["validation_thresholds"].get("min_ticker_pass_rate", 0.50)

    # Determine which markets to test
    mkt = (strategy.market or "ALL").upper()
    tasks = []
    if mkt in ("KRX", "ALL"):
        tasks += [("KRX", t, commission_krx) for t in krx_tickers]
    if mkt in ("US", "ALL"):
        tasks += [("US", t, commission_us) for t in us_tickers]

    ticker_results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(backtest_strategy_on_ticker, strategy, mkt_, ticker, start, comm): (mkt_, ticker)
            for mkt_, ticker, comm in tasks
        }
        for future in as_completed(futures):
            ticker_results.append(future.result())

    tested = [r for r in ticker_results if not r.get("skipped")]
    valid = [r for r in tested if r.get("is_valid")]
    pass_rate = len(valid) / max(len(tested), 1)

    avg_sharpe = sum(r["sharpe"] for r in tested) / max(len(tested), 1) if tested else 0
    avg_mdd = sum(r["mdd"] for r in tested) / max(len(tested), 1) if tested else 0
    avg_annual_trades = sum(r["annual_trade_rate"] for r in tested) / max(len(tested), 1) if tested else 0
    avg_holding = sum(r["avg_holding_days"] for r in tested) / max(len(tested), 1) if tested else 0

    result_label = "validated_v2" if pass_rate >= min_pass_rate else "rejected_v2"

    return {
        "name": strategy.name,
        "category": strategy.category,
        "market": strategy.market,
        "result": result_label,
        "pass_rate": round(pass_rate, 3),
        "tested_count": len(tested),
        "valid_count": len(valid),
        "avg_sharpe": round(avg_sharpe, 3),
        "avg_mdd": round(avg_mdd, 3),
        "avg_annual_trades": round(avg_annual_trades, 2),
        "avg_holding_days": round(avg_holding, 1),
        "ticker_results": ticker_results,
    }


def get_krx_tickers(n: int) -> list[str]:
    """Get KOSPI top-N tickers. Tries pykrx (with delay/retry) → FinanceDataReader → parquet cache."""
    import time

    cache_path = CACHE_DIR / "krx_tickers.json"
    if cache_path.exists():
        try:
            tickers = json.loads(cache_path.read_text())
            if tickers:
                return tickers[:n]
        except Exception:
            pass

    # 1) pykrx — rate-limit 회피를 위해 딜레이 후 재시도 (최대 3회)
    for attempt in range(1, 4):
        try:
            import pykrx.stock as krx
            time.sleep(3 * attempt)  # 3s → 6s → 9s
            tickers = krx.get_market_ticker_list(market="KOSPI")
            tickers = [str(t).zfill(6) for t in tickers if t]
            if tickers:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(tickers))
                logger.info(f"Loaded {len(tickers)} KOSPI tickers via pykrx (attempt {attempt})")
                return tickers[:n]
        except Exception as e:
            logger.warning(f"pykrx attempt {attempt} failed: {e}")

    # 2) FinanceDataReader
    try:
        import FinanceDataReader as fdr
        df = fdr.StockListing("KOSPI")
        cap_col = next((c for c in df.columns if "Mkt" in c or "Cap" in c or "시가총액" in c), None)
        if cap_col:
            df = df.sort_values(cap_col, ascending=False)
        tickers = df["Code"].dropna().astype(str).str.zfill(6).tolist()[:n]
        if tickers:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(tickers))
            logger.info(f"Loaded {len(tickers)} KOSPI tickers via FinanceDataReader")
            return tickers
    except Exception as e:
        logger.warning(f"FinanceDataReader failed: {e}")

    # 3) Fallback: prefetched parquet cache
    cached = sorted({p.stem.split("_")[1] for p in CACHE_DIR.glob("KRX_*.parquet")})
    if cached:
        logger.info(f"Using {len(cached)} tickers from parquet cache")
        return cached[:n]

    # 4) Static config file (config/krx_revalidation_tickers.json)
    static_path = Path(__file__).parent.parent / "config" / "krx_revalidation_tickers.json"
    if static_path.exists():
        tickers = json.loads(static_path.read_text())
        logger.info(f"Using {len(tickers)} tickers from config/krx_revalidation_tickers.json")
        return tickers[:n]

    logger.error("Could not load KRX tickers — no data source available")
    return []


def main():
    parser = argparse.ArgumentParser(description="Re-validate trading strategies")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Report only, no YAML changes (default)")
    parser.add_argument("--apply", action="store_true", help="Apply results to YAML files")
    parser.add_argument("--workers", type=int, default=4, help="Parallel backtest workers")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of strategies (for testing)")
    args = parser.parse_args()

    if args.apply:
        args.dry_run = False

    cfg = load_config()
    krx_n = cfg["backtest"]["revalidation_tickers"].get("krx_top", 100)
    us_n = cfg["backtest"]["revalidation_tickers"].get("us_top", 50)

    logger.info(f"Mode: {'DRY-RUN' if args.dry_run else 'APPLY'}")
    logger.info(f"Tickers: KRX top {krx_n}, US top {us_n}")

    # Load tickers
    krx_tickers = get_krx_tickers(krx_n)
    us_tickers = US_TOP_50[:us_n]
    logger.info(f"Loaded {len(krx_tickers)} KRX tickers, {len(us_tickers)} US tickers")

    # Load validated strategies
    registry = StrategyRegistry()
    all_strategies = [registry.load(name) for name in registry.list_strategies()]
    validated = [s for s in all_strategies if s and s.status == "validated"]
    if args.limit:
        validated = validated[:args.limit]
    logger.info(f"Strategies to re-validate: {len(validated)}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    for i, strategy in enumerate(validated):
        logger.info(f"[{i+1}/{len(validated)}] Validating: {strategy.name}")
        try:
            result = validate_strategy(strategy, krx_tickers, us_tickers, cfg, workers=args.workers)
            results.append(result)
            status = "PASS" if result["result"] == "validated_v2" else "FAIL"
            logger.info(
                f"  {status} {result['result']} | pass_rate={result['pass_rate']:.0%} "
                f"| sharpe={result['avg_sharpe']:.2f} | mdd={result['avg_mdd']:.1%} "
                f"| trades/yr={result['avg_annual_trades']:.1f}"
            )
        except Exception as e:
            logger.error(f"  ERROR: {e}")
            results.append({"name": strategy.name, "result": "error", "error": str(e)})

    # Summary
    passed = [r for r in results if r.get("result") == "validated_v2"]
    failed = [r for r in results if r.get("result") == "rejected_v2"]
    errors = [r for r in results if r.get("result") == "error"]

    summary = {
        "total": len(results),
        "passed": len(passed),
        "failed": len(failed),
        "errors": len(errors),
        "pass_rate_overall": round(len(passed) / max(len(results), 1), 3),
    }

    report = {
        "config": {
            "period": cfg["backtest"].get("default_period"),
            "commission_krx": cfg["backtest"].get("commission_krx"),
            "thresholds": cfg["backtest"].get("validation_thresholds"),
            "krx_tickers_tested": len(krx_tickers),
            "us_tickers_tested": len(us_tickers),
        },
        "summary": summary,
        "strategies": results,
    }

    report_path = OUTPUT_DIR / "revalidation_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    logger.info(f"\nReport saved: {report_path}")
    logger.info(f"Summary: {summary['passed']} passed / {summary['failed']} failed / {summary['errors']} errors out of {summary['total']}")

    # Print top survivors
    if passed:
        logger.info("\nTop 10 survivors by Sharpe ratio:")
        top = sorted(passed, key=lambda r: r.get("avg_sharpe", 0), reverse=True)[:10]
        for r in top:
            logger.info(f"  {r['name']}: sharpe={r.get('avg_sharpe', 0):.2f}, pass={r.get('pass_rate', 0):.0%}")

    # Apply mode: update YAML files
    if not args.dry_run:
        strategies_dir = Path("config/strategies")
        updated = 0
        for result in results:
            if result.get("result") != "rejected_v2":
                continue
            # Find YAML file for this strategy
            for yaml_file in strategies_dir.glob("*.yaml"):
                with open(yaml_file) as f:
                    content = f.read()
                if f"name: {result['name']}" in content or f'name: "{result["name"]}"' in content:
                    content = content.replace("status: validated", "status: rejected")
                    content = content.replace('status: "validated"', 'status: "rejected"')
                    with open(yaml_file, "w") as f:
                        f.write(content)
                    updated += 1
                    break
        logger.info(f"Applied: {updated} strategies set to rejected")


if __name__ == "__main__":
    main()
