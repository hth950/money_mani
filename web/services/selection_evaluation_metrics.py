"""Statistics for equal-weight, daily selection comparisons."""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping
from statistics import mean, quantiles
from typing import Any

TOP_K = (1, 3)
BOOTSTRAP_SAMPLES = 2000


def finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def pct(numerator: int, denominator: int) -> float | None:
    return round(100.0 * numerator / denominator, 4) if denominator else None


def average(values: Iterable[float]) -> float | None:
    values = list(values)
    return round(mean(values), 6) if values else None


def daily_return(ids: list[int], labels: Mapping[int, tuple[float, float]], *, complete: bool) -> tuple[float, float] | None:
    """Evaluate a frozen pick list, or average the labelled part of a baseline."""
    returns = [labels[event_id] for event_id in ids if event_id in labels]
    if not returns or (complete and len(returns) != len(ids)):
        return None
    net, excess = zip(*returns)
    return mean(net), mean(excess)


def return_summary(daily: Mapping[str, tuple[float, float]], planned_days: int) -> dict:
    return {
        "days": len(daily),
        "planned_days": planned_days,
        "day_coverage_pct": pct(len(daily), planned_days),
        "avg_net_return_pct": average(value[0] for value in daily.values()),
        "avg_net_excess_pct": average(value[1] for value in daily.values()),
        "win_rate_pct": pct(sum(value[0] > 0 for value in daily.values()), len(daily)),
    }


def _block_interval(deltas: list[float], block_size: int) -> tuple[float | None, float | None]:
    """Resample paired date blocks to retain overlapping holding-period dependence."""
    n = len(deltas)
    if n < 2 * block_size:
        return None, None
    # Circular blocks give every date equal weight. Require at least two
    # horizon-sized blocks to avoid presenting a degenerate confidence interval.
    rng = random.Random(19745)
    wrapped = deltas + deltas[:block_size]
    partial = [0.0]
    for value in wrapped:
        partial.append(partial[-1] + value)
    block_totals = [partial[i + block_size] - partial[i] for i in range(n)]
    full_blocks, remainder = divmod(n, block_size)
    estimates = []
    for _ in range(BOOTSTRAP_SAMPLES):
        total = sum(block_totals[rng.randrange(n)] for _ in range(full_blocks))
        if remainder:
            i = rng.randrange(n)
            total += partial[i + remainder] - partial[i]
        estimates.append(total / n)
    bounds = quantiles(estimates, n=40, method="inclusive")
    return round(bounds[0], 6), round(bounds[-1], 6)


def paired_summary(
    candidate: Mapping[str, tuple[float, float]],
    incumbent: Mapping[str, tuple[float, float]],
    baseline: Mapping[str, tuple[float, float]],
    horizon: int,
) -> dict:
    dates = sorted(candidate.keys() & incumbent.keys() & baseline.keys())
    candidates = [candidate[day][1] - baseline[day][1] for day in dates]
    incumbents = [incumbent[day][1] - baseline[day][1] for day in dates]
    uplifts = [candidate[day][1] - incumbent[day][1] for day in dates]
    low, high = _block_interval(uplifts, horizon)
    baseline_low, baseline_high = _block_interval(candidates, horizon)
    incumbent_low, incumbent_high = _block_interval(incumbents, horizon)
    return {
        "days": len(dates),
        "avg_uplift_pct": average(uplifts),
        "ci_low_pct": low,
        "ci_high_pct": high,
        "avg_vs_baseline_pct": average(candidates),
        "baseline_ci_low_pct": baseline_low,
        "baseline_ci_high_pct": baseline_high,
        "incumbent_avg_vs_baseline_pct": average(incumbents),
        "incumbent_baseline_ci_low_pct": incumbent_low,
        "incumbent_baseline_ci_high_pct": incumbent_high,
    }


def baseline_comparison(
    selected: Mapping[str, tuple[float, float]], baseline: Mapping[str, tuple[float, float]], horizon: int,
) -> dict:
    dates = sorted(selected.keys() & baseline.keys())
    uplift = [selected[day][1] - baseline[day][1] for day in dates]
    low, high = _block_interval(uplift, horizon)
    return {
        "days": len(dates),
        "avg_vs_baseline_pct": average(uplift),
        "same_day_baseline_avg_net_excess_pct": average(baseline[day][1] for day in dates),
        "baseline_ci_low_pct": low,
        "baseline_ci_high_pct": high,
    }
