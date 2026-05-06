"""Forward-return analysis for all historical signals.

Fetches OHLCV for every unique (market, ticker) seen in the signals table,
computes 5/10/20-day forward returns for each signal, aggregates by
(strategy_name, signal_type), and ranks strategies.

Output:
    output/strategy_forward_eval_<YYYYMMDD-HHMMSS>.json   (full data)
    output/strategy_forward_eval_<YYYYMMDD-HHMMSS>.md     (human report)

Run: python scripts/strategy_forward_eval.py
"""

import json
import logging
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

from market_data.krx_fetcher import KRXFetcher
from market_data.us_fetcher import USFetcher
from web.db.connection import get_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("strategy_eval")

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)
CACHE_DIR = OUTPUT_DIR / "data_cache"
CACHE_DIR.mkdir(exist_ok=True)

HORIZONS = [5, 10, 20]  # trading-day horizons


def load_signals() -> pd.DataFrame:
    with get_db() as db:
        rows = db.execute(
            "SELECT id, strategy_name, ticker, market, signal_type, price, "
            "DATE(detected_at) AS signal_date FROM signals "
            "WHERE price IS NOT NULL AND price > 0"
        ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    logger.info(f"Loaded {len(df)} signals from DB")
    return df


def fetch_ohlcv(market: str, ticker: str, start: str) -> pd.DataFrame:
    """Fetch OHLCV with parquet caching."""
    cache = CACHE_DIR / f"{market}_{ticker}.parquet"
    if cache.exists():
        df = pd.read_parquet(cache)
        if not df.empty and df.index.max() >= pd.Timestamp(datetime.now().date()) - pd.Timedelta(days=3):
            return df
    if market == "KRX":
        df = KRXFetcher().get_ohlcv(ticker, start)
    else:
        df = USFetcher().get_ohlcv(ticker, start)
    if not df.empty:
        try:
            df.to_parquet(cache)
        except Exception as e:
            logger.warning(f"Cache write failed for {market}:{ticker}: {e}")
    return df


def compute_forward_returns(signal_df: pd.DataFrame, ohlcv_map: dict) -> pd.DataFrame:
    """Compute forward returns for each signal at each horizon."""
    records = []
    skipped = 0
    for _, sig in signal_df.iterrows():
        key = (sig["market"], sig["ticker"])
        ohlcv = ohlcv_map.get(key)
        if ohlcv is None or ohlcv.empty:
            skipped += 1
            continue

        signal_date = pd.Timestamp(sig["signal_date"])
        # Find the trading day on or after the signal date
        future_idx = ohlcv.index[ohlcv.index >= signal_date]
        if len(future_idx) == 0:
            skipped += 1
            continue
        entry_date = future_idx[0]
        entry_price = ohlcv.loc[entry_date, "Close"]
        if entry_price is None or entry_price <= 0:
            skipped += 1
            continue

        rec = {
            "strategy_name": sig["strategy_name"],
            "ticker": sig["ticker"],
            "market": sig["market"],
            "signal_type": sig["signal_type"],
            "signal_date": str(sig["signal_date"]),
            "entry_price": float(entry_price),
        }
        # Forward returns at each horizon
        for h in HORIZONS:
            fut_idx_pos = ohlcv.index.searchsorted(entry_date) + h
            if fut_idx_pos >= len(ohlcv):
                rec[f"ret_{h}d"] = None
                continue
            fut_price = ohlcv.iloc[fut_idx_pos]["Close"]
            if fut_price is None or fut_price <= 0:
                rec[f"ret_{h}d"] = None
                continue
            raw = (fut_price / entry_price - 1) * 100
            # For SELL signals, invert: positive means price dropped (good)
            if sig["signal_type"] == "SELL":
                raw = -raw
            rec[f"ret_{h}d"] = round(raw, 4)
        records.append(rec)
    logger.info(f"Computed forward returns for {len(records)} signals (skipped {skipped})")
    return pd.DataFrame(records)


def aggregate_by_strategy(returns_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate by (strategy_name, signal_type)."""
    groups = []
    for (strategy, sig_type), grp in returns_df.groupby(["strategy_name", "signal_type"]):
        n = len(grp)
        rec = {
            "strategy_name": strategy,
            "signal_type": sig_type,
            "count": n,
        }
        for h in HORIZONS:
            col = f"ret_{h}d"
            valid = grp[col].dropna()
            if len(valid) == 0:
                rec[f"avg_ret_{h}d"] = None
                rec[f"hit_rate_{h}d"] = None
                rec[f"std_{h}d"] = None
                continue
            avg = valid.mean()
            std = valid.std()
            hit = (valid > 0).sum() / len(valid) * 100
            rec[f"avg_ret_{h}d"] = round(avg, 4)
            rec[f"hit_rate_{h}d"] = round(hit, 2)
            rec[f"std_{h}d"] = round(std, 4) if std is not None and not pd.isna(std) else None
            # Risk-adjusted: avg / std (Sharpe-like)
            if std and std > 0:
                rec[f"sharpe_{h}d"] = round(avg / std, 4)
            else:
                rec[f"sharpe_{h}d"] = None
        groups.append(rec)
    return pd.DataFrame(groups)


def write_report(stats_df: pd.DataFrame, returns_df: pd.DataFrame, out_path: Path):
    """Write markdown report ranked by 10d hit rate × avg_ret."""
    md = ["# Strategy Forward-Return Evaluation\n"]
    md.append(f"Generated: {datetime.now().isoformat()}\n")
    md.append(f"Total signals evaluated: {len(returns_df)}\n")
    md.append(f"Unique strategies: {stats_df['strategy_name'].nunique()}\n\n")

    for sig_type in ["BUY", "SELL"]:
        sub = stats_df[stats_df["signal_type"] == sig_type].copy()
        if sub.empty:
            continue
        # Filter: require at least 20 samples for meaningful stats
        sub = sub[sub["count"] >= 20]
        # Composite score: avg_ret_10d × hit_rate_10d / 100
        sub["score_10d"] = sub.apply(
            lambda r: (r["avg_ret_10d"] or 0) * (r["hit_rate_10d"] or 0) / 100, axis=1
        )
        sub = sub.sort_values("score_10d", ascending=False)

        md.append(f"## {sig_type} signals (n>=20)\n\n")
        md.append("### Top 15 performers\n\n")
        md.append("| Strategy | n | hit% (10d) | avg% (10d) | sharpe (10d) | avg% (20d) |\n")
        md.append("|---|---:|---:|---:|---:|---:|\n")
        for _, r in sub.head(15).iterrows():
            md.append(
                f"| {r['strategy_name']} | {r['count']} | "
                f"{r['hit_rate_10d']:.1f} | {r['avg_ret_10d']:+.2f} | "
                f"{r.get('sharpe_10d') or 0:.3f} | {r['avg_ret_20d']:+.2f} |\n"
            )

        md.append("\n### Bottom 15 performers (replacement candidates)\n\n")
        md.append("| Strategy | n | hit% (10d) | avg% (10d) | sharpe (10d) | avg% (20d) |\n")
        md.append("|---|---:|---:|---:|---:|---:|\n")
        for _, r in sub.tail(15).iterrows():
            md.append(
                f"| {r['strategy_name']} | {r['count']} | "
                f"{r['hit_rate_10d']:.1f} | {r['avg_ret_10d']:+.2f} | "
                f"{r.get('sharpe_10d') or 0:.3f} | {r['avg_ret_20d']:+.2f} |\n"
            )
        md.append("\n")

    out_path.write_text("".join(md), encoding="utf-8")
    logger.info(f"Report written: {out_path}")


def main():
    signals = load_signals()
    if signals.empty:
        logger.error("No signals to evaluate")
        return

    earliest = signals["signal_date"].min()
    fetch_start = (pd.Timestamp(earliest) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    pairs = signals[["market", "ticker"]].drop_duplicates().itertuples(index=False)
    pairs = list(pairs)
    logger.info(f"Fetching OHLCV for {len(pairs)} unique tickers from {fetch_start}")

    ohlcv_map = {}
    for i, (market, ticker) in enumerate(pairs, 1):
        logger.info(f"[{i}/{len(pairs)}] {market}:{ticker}")
        try:
            df = fetch_ohlcv(market, ticker, fetch_start)
            if not df.empty:
                ohlcv_map[(market, ticker)] = df
        except Exception as e:
            logger.warning(f"Fetch failed for {market}:{ticker}: {e}")
    logger.info(f"Got OHLCV for {len(ohlcv_map)}/{len(pairs)} tickers")

    returns_df = compute_forward_returns(signals, ohlcv_map)
    if returns_df.empty:
        logger.error("No forward returns computed — aborting")
        return
    stats_df = aggregate_by_strategy(returns_df)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = OUTPUT_DIR / f"strategy_forward_eval_{ts}.json"
    md_path = OUTPUT_DIR / f"strategy_forward_eval_{ts}.md"

    payload = {
        "generated_at": datetime.now().isoformat(),
        "horizons": HORIZONS,
        "total_signals_evaluated": len(returns_df),
        "stats": stats_df.to_dict(orient="records"),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    logger.info(f"JSON written: {json_path}")
    write_report(stats_df, returns_df, md_path)

    print(f"\n{'='*60}\nReports:\n  {json_path}\n  {md_path}\n{'='*60}")


if __name__ == "__main__":
    main()
