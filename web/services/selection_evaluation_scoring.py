"""Fixed shadow score definitions and recorded axis availability."""

from __future__ import annotations

import math
from collections.abc import Mapping

from web.services.selection_evaluation_metrics import finite

AXES = ("technical", "fundamental", "flow", "intel", "macro")
CANDIDATE_VERSION = "selection-v1"
FEATURES = {
    "KRX": ("krx_flow_5d", "krx_flow_positive_share", "relative_strength"),
    "US": ("us_news_novelty", "relative_volume", "sector_relative_strength"),
}
CANDIDATE_AXES = {"KRX": ("flow", 0.20), "US": ("intel", 0.25)}


def score_fraction(value) -> float | None:
    value = finite(value)
    if value is None or not 0 <= value <= 100:
        return None
    return value / 100 if value > 1 else value


def _weight(weights: Mapping, axis: str) -> float | None:
    value = finite(weights.get(axis))
    return value if value is not None and 0 <= value <= 1 else None


def remove_axis(incumbent, axes: Mapping, weights: Mapping, axis: str) -> float | None:
    """Remove the actual contribution and renormalize the remaining axes."""
    score = score_fraction(incumbent)
    weight = _weight(weights, axis)
    axis_score = score_fraction(axes.get(axis))
    if score is None or weight is None or weight == 1:
        return None
    if weight == 0:
        return score
    if axis_score is None:
        return None
    return min(1.0, max(0.0, (score - axis_score * weight) / (1.0 - weight)))


def neutralize_axis(incumbent, axes: Mapping, weights: Mapping, axis: str) -> float | None:
    """Replace one historically observed axis with the neutral score of 0.5."""
    score = score_fraction(incumbent)
    weight = _weight(weights, axis)
    axis_score = score_fraction(axes.get(axis))
    if score is None or weight is None:
        return None
    if weight == 0:
        return score
    if axis_score is None:
        return None
    return min(1.0, max(0.0, score + weight * (0.5 - axis_score)))


def incumbent_score_with_intel(incumbent, axes: Mapping, weights: Mapping, intel_score) -> float | None:
    score = score_fraction(incumbent)
    weight = _weight(weights, "intel")
    original = score_fraction(axes.get("intel"))
    replacement = score_fraction(intel_score)
    if score is None or weight is None or original is None or replacement is None:
        return None
    return min(1.0, max(0.0, score + weight * (replacement - original)))


def feature_value(features: Mapping, key: str) -> float | None:
    value = features.get(key)
    if isinstance(value, Mapping):
        if value.get("available") is False or value.get("status") in {"unavailable", "failed", "stale"}:
            return None
        value = value.get("value")
    return finite(value)


def _centered(value: float, half_range: float) -> float:
    return (1.0 + max(-1.0, min(1.0, value / half_range))) / 2.0


def prospective_score(
    market: str,
    incumbent: float,
    axes: Mapping,
    features: Mapping,
    *,
    intel_score: float | None = None,
    weights: Mapping | None = None,
) -> float | None:
    """Replace one incumbent axis with a fixed market-specific shadow feature mix.

    Values and weights are frozen before evaluations. KRX adds back the nominal
    flow weight even when the original event redistributed it due to unavailable
    flow. The US replacement uses the novel-news signal supplied in ``features``.
    """
    names = FEATURES.get(market)
    if names is None or weights is None:
        return None
    values = [feature_value(features, name) for name in names]
    if any(value is None for value in values):
        return None
    axis, weight = CANDIDATE_AXES[market]
    if market == "KRX":
        flow, positive_share, relative_strength = values
        if not 0 <= positive_share <= 1:
            return None
        persistence = (_centered(flow, .05) + positive_share) / 2.0
        replacement = (persistence + _centered(relative_strength, .10)) / 2.0
        if intel_score is not None:
            incumbent = incumbent_score_with_intel(incumbent, axes, weights, intel_score)
    else:
        novelty, relative_volume, sector_strength = values
        if relative_volume < 0:
            return None
        replacement = (
            (1.0 + math.tanh(novelty)) / 2.0
            + _centered(math.log2(max(relative_volume, .5)), 1.0)
            + _centered(sector_strength, .05)
        ) / 3.0
    remainder = remove_axis(incumbent, axes, weights, axis)
    if remainder is None:
        return None
    return (1.0 - weight) * remainder + weight * replacement


def axis_availability(market: str, scores: Mapping, details: Mapping, weights: Mapping, axis: str) -> str:
    if axis == "flow" and market == "US":
        return "disabled"
    value = score_fraction(scores.get(axis))
    if value is None:
        return "missing_score"
    context = details.get(axis)
    context = context if isinstance(context, Mapping) else {}
    status = context.get("data_status")
    if status in {"available", "partial", "unavailable", "failed", "disabled"}:
        return status
    if axis == "flow" and (
        _weight(weights, "flow") == 0
        or (value == .5 and context == {"foreign_streak_score": .5, "inst_streak_score": .5})
    ):
        return "unavailable"
    if context.get("error"):
        return "failed"
    if context.get("note") in {"no intel data", "all filtered"}:
        return "no_evidence"
    if context.get("note") == "VIX unavailable":
        return "partial" if context.get("community_score") is not None else "unavailable"
    if context.get("note") == "macro disabled":
        return "disabled"
    if axis == "technical" and context.get("method") == "consensus":
        return "fallback"
    return "available" if context else "metadata_unknown"
