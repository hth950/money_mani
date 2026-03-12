"""Flow Amount Scale verification script.

Fetches recent foreign/institutional net-buy amounts for sample tickers
per market-cap tier and compares against current _AMOUNT_SCALE constants.

Usage:
    python scripts/verify_flow_scale.py [--days 20]

Purpose:
    _AMOUNT_SCALE drives sigmoid normalization in FlowCollector.
    sigmoid(x / scale) == 0.5 when x == 0, but the *slope* near zero
    depends on scale: too-large scale → all scores cluster near 0.5.
    Ideally scale ≈ p50 of |net_buy| so that typical activity maps
    to the steepest part of the sigmoid.

Output:
    - Console: per-tier distribution table + recommendation
    - File:    output/analysis/flow_scale_{date}.json
"""

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# Current hardcoded scales in scoring/data_collectors.py
CURRENT_SCALES = {"large": 1e11, "mid": 1e10, "small": 1e9}

# Tiers: market-cap thresholds in KRW
TIER_THRESHOLDS = {"large": 10e12, "mid": 1e12}  # >10조, >1조, else small


def classify_tier(marcap: float) -> str:
    if marcap > TIER_THRESHOLDS["large"]:
        return "large"
    if marcap > TIER_THRESHOLDS["mid"]:
        return "mid"
    return "small"


def sigmoid(x: float, scale: float) -> float:
    return 1.0 / (1.0 + math.exp(-x / scale))


def percentile(data: list[float], p: float) -> float:
    if not data:
        return float("nan")
    data = sorted(data)
    idx = (len(data) - 1) * p / 100
    lo, hi = int(idx), min(int(idx) + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (idx - lo)


def collect_flow_data(tickers_by_tier: dict[str, list[str]], days: int) -> dict[str, list[float]]:
    """Fetch net-buy amounts per tier using pykrx."""
    from market_data.krx_fetcher import KRXFetcher

    krx = KRXFetcher(delay=1.0)
    today = datetime.now(KST).strftime("%Y-%m-%d")
    start = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    amounts: dict[str, list[float]] = {"large": [], "mid": [], "small": []}

    for tier, tickers in tickers_by_tier.items():
        for ticker in tickers:
            try:
                df = krx.get_investor_flows(ticker, start, today)
                if df is None or df.empty:
                    continue
                for col in df.columns:
                    col_str = str(col)
                    if "외국" in col_str or "기관" in col_str:
                        vals = df[col].dropna().tolist()
                        amounts[tier].extend([abs(float(v)) for v in vals if v != 0])
            except Exception as e:
                logger.warning(f"Failed {ticker}: {e}")

    return amounts


def get_sample_tickers() -> dict[str, list[str]]:
    """Load monitored tickers and classify by market cap."""
    try:
        from market_data.fdr_fetcher import FDRFetcher
        from utils.config_loader import load_config

        cfg = load_config()
        targets = cfg.get("pipeline", {}).get("targets", {}).get("custom_tickers", [])
        if not targets:
            # Fallback: KOSPI blue-chips
            targets = ["005930", "000660", "035420", "005380", "051910",
                       "090430", "003550", "032830", "086790", "015760"]

        df = FDRFetcher().get_krx_listings("KRX")
        by_tier: dict[str, list[str]] = {"large": [], "mid": [], "small": []}
        for ticker in targets:
            try:
                row = df[df.index.astype(str) == str(ticker)]
                marcap = float(row.iloc[0].get("Marcap", 0)) if not row.empty else 0
                tier = classify_tier(marcap)
                if len(by_tier[tier]) < 5:
                    by_tier[tier].append(ticker)
            except Exception:
                pass
        return by_tier
    except Exception as e:
        logger.warning(f"Could not load tickers: {e}")
        return {"large": ["005930", "000660"], "mid": ["035420", "005380"], "small": ["090430"]}


def main():
    parser = argparse.ArgumentParser(description="Verify FlowCollector _AMOUNT_SCALE")
    parser.add_argument("--days", type=int, default=20, help="Lookback window")
    args = parser.parse_args()

    print(f"\nCollecting {args.days}-day flow data by market-cap tier...")
    tickers_by_tier = get_sample_tickers()
    print(f"Sample tickers: {tickers_by_tier}")

    amounts = collect_flow_data(tickers_by_tier, args.days)

    results = {}
    print("\n" + "=" * 65)
    print(f"  Flow Amount Scale Analysis  (lookback={args.days}d)")
    print("=" * 65)
    print(f"  {'Tier':<8} {'n':>4}  {'p25':>12}  {'p50':>12}  {'p75':>12}  {'Current Scale':>14}  Sigmoid@p50")
    print("-" * 65)

    recommendations = {}
    for tier in ["large", "mid", "small"]:
        data = amounts[tier]
        n = len(data)
        if n < 5:
            print(f"  {tier:<8} {n:>4}  -- insufficient data (need >=5)")
            recommendations[tier] = None
            continue

        p25 = percentile(data, 25)
        p50 = percentile(data, 50)
        p75 = percentile(data, 75)
        cur_scale = CURRENT_SCALES[tier]
        sig_at_p50 = sigmoid(p50, cur_scale)
        rec_scale = p50  # Recommended scale ≈ median so sigmoid(p50/scale)=0.731

        print(
            f"  {tier:<8} {n:>4}  {p25:>12,.0f}  {p50:>12,.0f}  {p75:>12,.0f}  "
            f"{cur_scale:>14,.0f}  {sig_at_p50:.3f}"
        )
        results[tier] = {"n": n, "p25": p25, "p50": p50, "p75": p75,
                         "current_scale": cur_scale, "sigmoid_at_p50": round(sig_at_p50, 4)}
        recommendations[tier] = round(p50)

    print("=" * 65)
    print("\nRecommended _AMOUNT_SCALE (based on p50 of |net_buy|):")
    for tier, rec in recommendations.items():
        if rec:
            cur = CURRENT_SCALES[tier]
            change = "↑" if rec > cur else "↓" if rec < cur else "="
            print(f"  {tier:<8} current={cur:.2e}  recommended={rec:.2e}  {change}")

    print("\nNote: Update scoring/data_collectors.py _AMOUNT_SCALE if recommended")
    print("      values differ significantly (>5x) from current values.")
    print("      Alternatively, move to config/scoring.yaml flow_scoring.amount_scale")

    # Save JSON
    out_dir = PROJECT_ROOT / "output" / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(KST).strftime("%Y-%m-%d")
    out_path = out_dir / f"flow_scale_{today}.json"
    payload = {
        "generated_at": datetime.now(KST).isoformat(),
        "lookback_days": args.days,
        "tickers_sampled": tickers_by_tier,
        "current_scales": CURRENT_SCALES,
        "analysis": results,
        "recommendations": recommendations,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved → {out_path}")


if __name__ == "__main__":
    main()
