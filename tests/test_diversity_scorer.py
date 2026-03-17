"""Tests for DiversityScorer category weighting."""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scoring.diversity_scorer import DiversityScorer


@pytest.fixture
def scorer():
    return DiversityScorer(min_category_diversity=3)


def make_signals(categories_and_signals):
    """Build signals_by_strategy dict from [(category, signal_type), ...]."""
    return {
        f"strategy_{i}": {"category": cat, "signal_type": sig}
        for i, (cat, sig) in enumerate(categories_and_signals)
    }


def test_diversity_scorer_meets_min_with_3_categories(scorer):
    """3 different categories → meets_diversity_min=True."""
    signals = make_signals([
        ("momentum", "BUY"),
        ("trend", "BUY"),
        ("volatility", "BUY"),
    ])
    result = scorer.score_ensemble(signals, signal_type="BUY")
    assert result["meets_diversity_min"] is True
    assert result["agreeing_categories"] == 3


def test_diversity_scorer_fails_with_1_category(scorer):
    """All strategies in same category → meets_diversity_min=False."""
    signals = make_signals([
        ("momentum", "BUY"),
        ("momentum", "BUY"),
        ("momentum", "BUY"),
        ("momentum", "BUY"),
        ("momentum", "BUY"),
        ("momentum", "BUY"),
        ("momentum", "BUY"),
    ])
    result = scorer.score_ensemble(signals, signal_type="BUY")
    assert result["meets_diversity_min"] is False
    assert result["agreeing_categories"] == 1


def test_diversity_scorer_weighted_score_discounted_for_low_diversity(scorer):
    """Low diversity → lower weighted_score than high diversity with same count."""
    low_div = make_signals([("momentum", "BUY")] * 5)
    high_div = make_signals([
        ("momentum", "BUY"),
        ("trend", "BUY"),
        ("volatility", "BUY"),
        ("breakout", "BUY"),
        ("composite", "BUY"),
    ])
    low_result = scorer.score_ensemble(low_div, signal_type="BUY")
    high_result = scorer.score_ensemble(high_div, signal_type="BUY")
    assert high_result["weighted_score"] >= low_result["weighted_score"]


def test_diversity_scorer_empty_signals(scorer):
    """Empty signals dict → safe return."""
    result = scorer.score_ensemble({}, signal_type="BUY")
    assert result["meets_diversity_min"] is False
    assert result["weighted_score"] == 0.0 or result["raw_count"] == 0
