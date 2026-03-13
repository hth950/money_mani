"""Composite score vs pnl_pct correlation analysis.

Usage:
    python scripts/correlation_analysis.py [--days 30]

Purpose:
    Measures whether composite_score (and each axis) actually predicts
    future returns. If Spearman r < 0.1, weighting parameters should
    be reconsidered before further tuning.

Output:
    - Console: ranked correlation table per axis
    - File: output/analysis/correlation_{date}.json
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
MIN_SAMPLES = 10  # Minimum joined rows required to report


def load_data(days: int) -> list[dict]:
    """Join scoring_results and signal_performance on (ticker, date).

    signal_performance has one row per strategy signal, so we aggregate
    pnl_pct to ticker-date level (AVG) before joining.
    """
    from web.db.connection import get_db

    cutoff = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")

    with get_db() as db:
        rows = db.execute("""
            SELECT
                sr.scan_date,
                sr.ticker,
                sr.market,
                sr.technical_score,
                sr.fundamental_score,
                sr.flow_score,
                sr.intel_score,
                sr.macro_score,
                sr.composite_score,
                sr.decision,
                AVG(sp.pnl_pct) AS avg_pnl_pct,
                COUNT(sp.id)    AS signal_count
            FROM scoring_results sr
            JOIN signal_performance sp
              ON sp.ticker = sr.ticker
             AND sp.signal_date = sr.scan_date
             AND sp.close_price IS NOT NULL
            WHERE sr.scan_date >= ?
              AND sr.composite_score IS NOT NULL
            GROUP BY sr.scan_date, sr.ticker
            ORDER BY sr.scan_date
        """, (cutoff,)).fetchall()

    return [dict(r) for r in rows]


def spearman_r(x: list[float], y: list[float]) -> float:
    """Pure-Python Spearman rank correlation (no scipy required)."""
    n = len(x)
    if n < 3:
        return float("nan")

    def rank(lst):
        sorted_idx = sorted(range(n), key=lambda i: lst[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n and lst[sorted_idx[j]] == lst[sorted_idx[i]]:
                j += 1
            avg = (i + j - 1) / 2.0 + 1
            for k in range(i, j):
                ranks[sorted_idx[k]] = avg
            i = j
        return ranks

    rx, ry = rank(x), rank(y)
    mean_rx = sum(rx) / n
    mean_ry = sum(ry) / n
    num = sum((rx[i] - mean_rx) * (ry[i] - mean_ry) for i in range(n))
    den_x = sum((rx[i] - mean_rx) ** 2 for i in range(n)) ** 0.5
    den_y = sum((ry[i] - mean_ry) ** 2 for i in range(n)) ** 0.5
    if den_x == 0 or den_y == 0:
        return float("nan")
    return num / (den_x * den_y)


def analyze(rows: list[dict]) -> dict:
    """Calculate Spearman r for composite and each axis."""
    pnl = [r["avg_pnl_pct"] for r in rows if r["avg_pnl_pct"] is not None]

    axes = {
        "composite": "composite_score",
        "technical": "technical_score",
        "fundamental": "fundamental_score",
        "flow": "flow_score",
        "intel": "intel_score",
        "macro": "macro_score",
    }

    results = {}
    for label, col in axes.items():
        scores = [r[col] for r in rows if r[col] is not None and r["avg_pnl_pct"] is not None]
        paired_pnl = [r["avg_pnl_pct"] for r in rows if r[col] is not None and r["avg_pnl_pct"] is not None]
        if len(scores) < MIN_SAMPLES:
            results[label] = {"r": None, "n": len(scores), "note": "insufficient data"}
        else:
            r = spearman_r(scores, paired_pnl)
            interpretation = (
                "strong" if abs(r) >= 0.3 else
                "moderate" if abs(r) >= 0.1 else
                "weak/none"
            )
            results[label] = {"r": round(r, 4), "n": len(scores), "interpretation": interpretation}

    return results


def run_analysis(days: int = 90) -> dict:
    """weight_optimizer와 correlation_report에서 호출하는 통합 진입점."""
    data = load_data(days=days)
    if not data:
        return {"sample_count": 0, "correlations": {}}
    result = analyze(data)
    result["sample_count"] = len(data)
    return result


def main():
    parser = argparse.ArgumentParser(description="Composite score vs pnl_pct correlation")
    parser.add_argument("--days", type=int, default=30, help="Lookback window in days")
    args = parser.parse_args()

    logger.info(f"Loading data for last {args.days} days...")
    rows = load_data(args.days)

    if len(rows) < MIN_SAMPLES:
        print(f"\n[!] Only {len(rows)} joined rows found (need >= {MIN_SAMPLES}).")
        print("    Run the system for more days before interpreting results.")
        print("    Tip: ensure signal_performance.close_price is being updated.")
        return

    logger.info(f"Joined {len(rows)} ticker-date pairs")
    results = analyze(rows)

    print("\n" + "=" * 55)
    print(f"  Spearman Correlation: Score vs pnl_pct  (n={len(rows)})")
    print("=" * 55)
    def _r_sort_key(kv):
        r = kv[1].get("r")
        if r is None:
            return -1.0
        try:
            return abs(float(r)) if r == r else -1.0  # NaN check
        except (TypeError, ValueError):
            return -1.0

    sorted_axes = sorted(results.items(), key=_r_sort_key, reverse=True)
    for label, info in sorted_axes:
        r = info.get("r")
        is_valid = r is not None and r == r  # NaN check: NaN != NaN
        if not is_valid:
            print(f"  {label:<14} n={info['n']:>4}  -- {info.get('note', 'insufficient data')}")
        else:
            bar = "#" * int(abs(r) * 20)
            print(f"  {label:<14} r={r:+.4f}  n={info['n']:>4}  [{bar:<20}]  {info['interpretation']}")
    print("=" * 55)

    composite_r = results.get("composite", {}).get("r")
    if composite_r is not None:
        if abs(composite_r) >= 0.1:
            print(f"\n[OK] composite r={composite_r:+.4f} >= 0.1 → proceed with tuning")
        else:
            print(f"\n[!!] composite r={composite_r:+.4f} < 0.1 → revisit axis weights before tuning")

    # Save JSON
    out_dir = PROJECT_ROOT / "output" / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(KST).strftime("%Y-%m-%d")
    out_path = out_dir / f"correlation_{today}.json"
    payload = {
        "generated_at": datetime.now(KST).isoformat(),
        "lookback_days": args.days,
        "total_rows": len(rows),
        "correlations": results,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved → {out_path}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
