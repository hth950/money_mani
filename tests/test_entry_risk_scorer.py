from scoring.entry_risk_scorer import EntryRiskScorer


def _scorer():
    return EntryRiskScorer(config={"model_version": "test", "mode": "shadow"})


def _inputs(**overrides):
    result = {
        "opportunity_score": .70,
        "component_scores": {"technical": .85, "fundamental": .80, "flow": .75, "intel": .80, "macro": .80},
        "positions": [],
        "sector": "Technology",
        "ticker": "NEW",
        "market": "US",
        "volatility": {"atr_pct": .01, "volatility_percentile": .20, "gap_pct": 0},
        "macro_event": {"macro_score": .80, "intel_confidence": .80},
        "data_quality": {"price_available": True, "fundamentals_available": True, "flow_available": True, "intel_available": True},
    }
    result.update(overrides)
    return result


def test_first_sector_entry_uses_twenty_percent_not_fifty_percent():
    scorer = _scorer()
    result = scorer.assess(**_inputs(positions=[{"ticker": "UUP", "market": "US", "sector": "Currency"}]))
    portfolio = result["risk_breakdown"]["portfolio"]
    assert portfolio["inputs"]["same_sector_positions"] == 0
    assert portfolio["inputs"]["expected_sector_weight"] == .20
    assert portfolio["score"] == 0


def test_second_same_sector_entry_is_forty_percent_and_riskier():
    scorer = _scorer()
    first = scorer.assess(**_inputs())
    second = scorer.assess(**_inputs(positions=[{"ticker": "AAPL", "market": "US", "sector": "Technology"}]))
    assert second["risk_breakdown"]["portfolio"]["inputs"]["expected_sector_weight"] == .40
    assert second["risk_breakdown"]["portfolio"]["score"] > first["risk_breakdown"]["portfolio"]["score"]


def test_boundaries_and_conditional_tier():
    scorer = _scorer()
    ready = scorer.assess(**_inputs(opportunity_score=65))
    assert ready["recommendation_tier"] == "BUY_READY"
    # An imminent event moves an otherwise sound candidate to conditional.
    conditional = scorer.assess(**_inputs(macro_event={"macro_score": .2, "intel_confidence": .2, "event_imminent": True}))
    assert conditional["recommendation_tier"] == "BUY_CONDITIONAL"
    assert conditional["risk_level"] in {"MEDIUM", "HIGH"}


def test_hard_data_failure_is_unavailable_while_soft_duplicate_is_conditional():
    scorer = _scorer()
    hard = scorer.assess(**_inputs(data_quality={"all_price_sources_failed": True}))
    assert hard["recommendation_tier"] == "UNAVAILABLE"
    assert hard["hard_block_reason"]
    soft = scorer.assess(**_inputs(positions=[{"ticker": "NEW", "market": "US", "sector": "Technology"}]))
    assert soft["recommendation_tier"] == "BUY_CONDITIONAL"
    assert soft["hard_block_reason"] is None


def test_missing_optional_inputs_never_crash_and_hash_is_stable():
    scorer = _scorer()
    first = scorer.assess(opportunity_score=.65, component_scores=None, positions=None, data_quality=None)
    second = scorer.assess(opportunity_score=65, component_scores={}, positions=[], data_quality={})
    assert 0 <= first["risk_score"] <= 100
    assert first["risk_breakdown"]["data_quality"]["score"] >= 25
    assert first["risk_snapshot_hash"] == second["risk_snapshot_hash"]
    assert len(first["risk_snapshot_hash"]) == 64


def test_legacy_portfolio_warning_is_preserved_as_soft_risk():
    result = _scorer().assess(
        **_inputs(legacy_soft_reason="당일 손실 한도에 근접했습니다.")
    )

    portfolio = result["risk_breakdown"]["portfolio"]
    assert "당일 손실 한도에 근접했습니다." in portfolio["reasons"]
    assert result["hard_block_reason"] is None
    assert result["recommendation_tier"] == "BUY_CONDITIONAL"


def test_invalid_market_and_price_integrity_are_hard_blocks():
    scorer = _scorer()
    invalid_market = scorer.assess(**_inputs(market="CRYPTO"))
    missing_price = scorer.assess(
        **_inputs(data_quality={"price_available": False})
    )

    assert invalid_market["recommendation_tier"] == "UNAVAILABLE"
    assert missing_price["recommendation_tier"] == "UNAVAILABLE"
