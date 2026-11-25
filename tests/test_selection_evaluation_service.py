"""Selection ranks are fixed before labels and market decisions use paired days."""

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from pipeline.selection_shadow import SELECTION_SHADOW_DDL
from web.services.decision_label_service import OUTCOME_LABEL_DDL, SHADOW_OUTCOME_LABEL_DDL
from web.services.selection_evaluation_scoring import neutralize_axis, prospective_score
from web.services.selection_evaluation_service import SelectionEvaluationService

WEIGHTS = {"technical": .5, "fundamental": .1, "flow": 0, "intel": .25, "macro": .15}
AS_OF = "2026-07-01"


@pytest.fixture
def selection_db(tmp_path):
    path = tmp_path / "selection.db"
    db = sqlite3.connect(path)
    schema = Path(__file__).resolve().parents[1] / "web/db/schema.sql"
    db.executescript(schema.read_text())
    for ddl in (*SELECTION_SHADOW_DDL, OUTCOME_LABEL_DDL, SHADOW_OUTCOME_LABEL_DDL):
        db.execute(ddl)
    yield path, db
    db.close()


def _event(db, day, ticker, score, *, market="US", action="BUY", tier="WATCH", hour="00", axis_score=.5,
           intel=.5, hard_block=None):
    clock = f"{day}T{hour}:00:00+00:00"
    return db.execute(
        """INSERT INTO decision_events
           (ticker, market, scan_date, signal_action, composite_score, created_at, detected_at,
            recommendation_tier, hard_block_reason, score_breakdown_json, weights_used_json, score_details_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticker, market, day, action, score, clock, clock, tier, hard_block,
         json.dumps({"technical": axis_score, "intel": intel, "flow": .5}), json.dumps(WEIGHTS),
         json.dumps({"flow": {"foreign_streak_score": .5, "inst_streak_score": .5}})),
    ).lastrowid


def _label(db, event, net, *, benchmark=1, shadow_day=None, exit_at="2026-06-01T08:00:00+00:00"):
    table = "shadow_outcome_labels" if shadow_day else "decision_outcome_labels"
    key = "shadow_candidate_id" if shadow_day else "decision_event_id"
    clock = f"{shadow_day}T02:00:00+00:00" if shadow_day else None
    db.execute(
        f"""INSERT INTO {table}
              ({key}, horizon_days, label_version, status, observed_at, exit_at,
               decision_at, net_return_pct, benchmark_return_pct)
            VALUES (?, 10, 'next_open_v1', 'evaluated', ?, ?, ?, ?, ?)""",
        (event, exit_at, exit_at, clock, net, benchmark),
    )


def _shadow(db, event, day, ticker, score, *, novelty, sector_strength, late_capture=False):
    features = json.dumps({
        "us_news_novelty": novelty, "relative_volume": 1.5, "sector_relative_strength": sector_strength,
    })
    db.execute(
        """INSERT INTO selection_shadow_snapshots
           (scan_date, market, ticker, captured_at, eligible, features_json, sources_json, decision_event_id)
           VALUES (?, 'US', ?, ?, 1, ?, '{}', ?)""",
        (day, ticker, f"{day}T0{3 if late_capture else 1}:00:00+00:00", features, event),
    )
    return db.execute(
        """INSERT INTO selection_shadow_candidates
           (scan_date, market, ticker, decision_at, baseline_score, decision_event_id, features_json)
           VALUES (?, 'US', ?, ?, ?, ?, ?)""",
        (day, ticker, f"{day}T02:00:00+00:00", score, event, features),
    ).lastrowid


def test_historical_first_buy_per_day_missing_top_and_costed_excess(selection_db):
    path, db = selection_db
    day, other = "2026-04-01", "2026-04-02"
    top = _event(db, day, "A", .9, market="KRX", axis_score=1)
    buy = _event(db, day, "B", .8, market="KRX", axis_score=.4, tier="BUY_READY")
    duplicate = _event(db, day, "B", 1, market="KRX", hour="02")
    _event(db, day, "C", .99, market="KRX", action=None, tier="BUY_READY")
    _event(db, day, "D", .99, market="KRX", action="SELL", tier="BUY_READY")
    _event(db, day, "E", .99, market="KRX", hard_block="data")
    first_unknown = _event(db, day, "F", .3, market="KRX", action=None)
    _event(db, day, "F", .999, market="KRX", hour="03", tier="BUY_READY")
    stale = _event(db, other, "C", .7, market="KRX")
    _event(db, other, "C", .99, market="US", action="SELL")
    _label(db, buy, 4)
    _label(db, duplicate, 100)
    _label(db, first_unknown, 50)
    _label(db, stale, -1)
    db.execute(
        """INSERT INTO decision_outcomes(decision_event_id, horizon_days, status, net_return_pct)
           VALUES (?, 10, 'evaluated', 20)""", (top,),
    )
    db.commit()

    report = SelectionEvaluationService(path).build_report(as_of=AS_OF)
    krx = report["markets"]["KRX"]
    assert krx["missingness"]["exclusions"] == {
        "ambiguous_action": 2, "duplicate_decisions": 2, "hard_blocked": 1, "sell": 1,
    }
    assert krx["missingness"]["axes"]["flow"] == {"unavailable": 3}
    horizon = krx["horizons"][10]
    assert horizon["eligible_count"] == 3
    assert horizon["mature_eligible_count"] == 3
    assert horizon["valid_price_count"] == 2
    assert horizon["recommendation_day_coverage_pct"] == 50
    assert horizon["incumbent"]["top1"]["avg_net_excess_pct"] == -2
    assert horizon["incumbent"]["top3"]["days"] == 1
    assert horizon["baseline"]["avg_net_excess_pct"] == .5
    assert horizon["ablations"]["technical"]["top1"]["avg_net_excess_pct"] == .5
    assert horizon["ablations"]["flow"]["top1"]["avg_net_excess_pct"] == -2
    assert horizon["incumbent"]["top1"]["vs_baseline"]["avg_vs_baseline_pct"] == 0
    assert report["markets"]["US"]["horizons"][10]["eligible_count"] == 0


@pytest.mark.parametrize(
    ("returns", "unavailable", "expected"),
    [
        ((.5, 3, 1), None, "promote"),
        ((-3, -2, 10), None, "reduce"),
        ((3, 3, 1), None, "hold"),
        ((.5, 3, 1), "missing_inputs", "pending"),
        ((.5, 3, 1), "ambiguous_actions", "pending"),
    ],
)
def test_gate_uses_mature_pairs_and_opportunity_coverage(selection_db, returns, unavailable, expected):
    path, db = selection_db
    start = date(2026, 4, 1)
    days = [(start + timedelta(days=i)).isoformat() for i in range(46) if (start + timedelta(days=i)).weekday() < 5]
    for day in days:
        for (ticker, score, intel, novelty, relative_strength), value in zip(
            (("A", .7, .8, -1, -.05), ("B", .68, .2, 2, .05), ("C", .3, .5, 0, 0)), returns,
        ):
            event = _event(db, day, ticker, score, intel=intel)
            candidate = _shadow(
                db, event, day, ticker, score, novelty=novelty, sector_strength=relative_strength,
                late_capture=ticker == "C" and unavailable == "missing_inputs",
            )
            _label(db, candidate, value, benchmark=0, shadow_day=day)
        if unavailable == "ambiguous_actions":
            _event(db, day, "D", .99, action=None, tier="BUY_READY")
    db.commit()

    market = SelectionEvaluationService(path).build_report(as_of=AS_OF, market="US")["markets"]["US"]
    assert market["decision"]["status"] == expected
    assert market["decision"]["common_mature_days"] == len(days)
    assert market["horizons"][10]["incumbent"]["top1"]["days"] == 0
    paired = market["horizons"][10]["paired"]["top1"]
    assert paired["avg_uplift_pct"] == pytest.approx(round(returns[1] - returns[0], 6))
    if unavailable == "missing_inputs":
        assert market["decision"]["coverage_pct"] == pytest.approx(66.6667)
        assert market["missingness"]["shadow_exclusions"] == {"invalid_capture_provenance": len(days)}
    elif unavailable == "ambiguous_actions":
        assert market["decision"]["action_coverage_pct"] == 75
    else:
        assert market["decision"]["coverage_pct"] == 100


def test_future_prices_and_capture_are_ineligible_at_report_cutoff(selection_db):
    path, db = selection_db
    day = "2026-04-01"
    event = _event(db, day, "A", .7)
    _label(db, event, 2, exit_at="2026-04-16T20:00:00+00:00")
    choice = _shadow(db, event, day, "A", .7, novelty=0, sector_strength=.05)
    _label(db, choice, 2, shadow_day=day, exit_at="2026-04-16T20:00:00+00:00")
    db.commit()

    service = SelectionEvaluationService(path)
    early = service.build_report(market="US", as_of="2026-04-01T01:30:00Z")["markets"]["US"]
    assert early["decision"]["shadow_started_at"] is None
    mid = service.build_report(market="US", as_of="2026-04-16T19:59:59Z")["markets"]["US"]
    assert mid["horizons"][10]["shadow_valid_price_count"] == 0
    assert mid["horizons"][10]["valid_price_count"] == 0
    assert mid["horizons"][10]["shadow_label_statuses"] == {"pending": 1}


def test_krx_persistence_is_required_and_neutralization_retains_other_axes():
    axes = {"technical": .8, "flow": .9}
    weights = {"technical": .5, "flow": .2}
    assert neutralize_axis(.7, axes, weights, "technical") == pytest.approx(.55)
    features = {"krx_flow_5d": 0, "relative_strength": 0}
    assert prospective_score("KRX", .7, axes, features, weights=weights) is None
    weak = prospective_score("KRX", .7, axes, {**features, "krx_flow_positive_share": 0}, weights=weights)
    strong = prospective_score("KRX", .7, axes, {**features, "krx_flow_positive_share": 1}, weights=weights)
    assert strong > weak


def test_recommendation_coverage_counts_scans_with_no_recorded_decisions(selection_db):
    path, db = selection_db
    _event(db, "2026-04-01", "A", .8, tier="BUY_CONDITIONAL")
    _event(db, "2026-04-03", "B", .8, tier="BUY_READY")
    _event(db, "2026-04-03", "C", .9, tier="BUY_CONDITIONAL")
    _event(db, "2026-04-04", "D", .8, tier="BUY_READY", action=None)
    for day, markets in (("2026-03-31", "US"), ("2026-04-02", "KRX,US"), ("2026-04-03", "US")):
        db.execute(
            "INSERT INTO scan_history(scan_date, signals_count, markets_open, created_at) VALUES (?, 0, ?, ?)",
            (day, markets, f"{day}T03:00:00+00:00"),
        )
    db.commit()

    service = SelectionEvaluationService(path)
    coverage = service.build_report(market="US", as_of=AS_OF)["markets"]["US"]["recommendation_coverage"]
    assert coverage == {
        "observable_decisions_from": "2026-04-01",
        "denominator_days": 4,
        "scan_history_days": 2,
        "decision_days": 3,
        "scan_days_without_decisions": 1,
        "decision_days_without_scan_history": 2,
        "pre_audit_scan_history_days": 1,
        "recommendation_days": 2,
        "recommendation_day_coverage_pct": 50,
        "no_recorded_recommendation_days": 2,
        "ready_days": 1,
        "ready_day_coverage_pct": 25,
        "conditional_days": 2,
        "legacy_execute_days": 0,
    }
    later = service.build_report(market="US", start_date="2026-04-02", as_of=AS_OF)
    assert later["markets"]["US"]["recommendation_coverage"]["denominator_days"] == 3


def test_provider_evidence_and_costs_report_recorded_costs_only(selection_db):
    path, db = selection_db
    rows = [
        ("2026-04-01", "kis", "available", .001, 2, 4., "USD"),
        ("2026-04-01", "naver", "available", .002, 2, None, None),
        ("2026-04-30", "kis", "missing", None, 3, 6., "USD"),
    ]
    for day, provider, status, amount, count, cost, currency in rows:
        db.execute(
            """INSERT INTO selection_shadow_provider_observations
               (scan_date, market, ticker, provider, collected_at, status, details_json,
                request_count, actual_cost, cost_currency) VALUES (?, 'KRX', 'A', ?, ?, ?, ?, ?, ?, ?)""",
            (day, provider, f"{day}T03:00:00+00:00", status,
             json.dumps({"sessions": [f"2026-03-{day}" for day in range(23, 28)], "krx_flow_5d": amount}), count, cost, currency),
        )
    db.commit()
    report = SelectionEvaluationService(path).build_report(market="KRX", as_of=AS_OF)
    evidence = report["markets"]["KRX"]["missingness"]["provider_evidence"]
    assert evidence["krx_flow_comparison"]["matched_ticker_dates"] == 1
    assert evidence["krx_flow_comparison"]["mean_absolute_difference"] == .001
    assert evidence["krx_flow_comparison"]["direction_agreement_pct"] == 100
    costs = report["markets"]["KRX"]["costs"]["providers"]
    assert costs["kis"]["observed_months"] == {"2026-04": {"USD": 10}}
    assert costs["kis"]["estimated_monthly_cost"]["USD"] == pytest.approx(10 * 365.25 / 12 / 30)
    assert costs["naver"]["estimated_monthly_cost"] is None
    assert report["model_cost"]["markets"]["US"]["projected_flash_increment_usd"] is None


def test_krx_flow_rank_sensitivity_uses_two_before_cutoff_feeds_and_same_labels(selection_db):
    path, db = selection_db
    for index, day in enumerate(("2026-04-01", "2026-04-02", "2026-04-03")):
        for ticker, score, direction, net in (("A", .7, 1, 3), ("B", .69, -1, 1)):
            event = _event(db, day, ticker, score, market="KRX")
            features = json.dumps({"krx_flow_5d": .05 * direction, "krx_flow_positive_share": .5, "relative_strength": 0})
            db.execute(
                """INSERT INTO selection_shadow_snapshots
                   (scan_date, market, ticker, captured_at, eligible, features_json, sources_json, decision_event_id)
                   VALUES (?, 'KRX', ?, ?, 1, ?, '{}', ?)""",
                (day, ticker, f"{day}T01:00:00+00:00", features, event),
            )
            candidate = db.execute(
                """INSERT INTO selection_shadow_candidates
                   (scan_date, market, ticker, decision_at, baseline_score, decision_event_id, features_json)
                   VALUES (?, 'KRX', ?, ?, ?, ?, ?)""",
                (day, ticker, f"{day}T02:00:00+00:00", score, event, features),
            ).lastrowid
            _label(db, candidate, net, shadow_day=day)
            for provider, sign in (("kis", 1), ("naver", -1)):
                late = index == 1 and ticker == "A" and provider == "naver"
                offset = int(index == 2 and ticker == "B" and provider == "naver")
                details = json.dumps({
                    "sessions": [f"2026-03-{number + offset}" for number in range(23, 28)],
                    "krx_flow_5d": .05 * direction * sign,
                    "krx_flow_positive_share": float(direction * sign == 1),
                })
                db.execute(
                    """INSERT INTO selection_shadow_provider_observations
                       (scan_date, market, ticker, provider, collected_at, status, details_json)
                       VALUES (?, 'KRX', ?, ?, ?, 'available', ?)""",
                    (day, ticker, provider, f"{day}T0{3 if late else 1}:30:00+00:00", details),
                )
    db.commit()

    report = SelectionEvaluationService(path).build_report(market="KRX", as_of=AS_OF)
    result = report["markets"]["KRX"]["missingness"]["provider_evidence"]["krx_flow_comparison"]
    assert result["eligible_comparison_days"] == 1
    assert result["top1_changed_days"] == 1
    assert result["changed_dates"] == ["2026-04-01"]
    assert result["daily_top1"]["2026-04-01"] == {"kis": "A", "naver": "B", "matched_tickers": 2, "changed": True}
    assert result["paired_mature_by_horizon"][10]["avg_uplift_pct"] == 2
    assert result["paired_mature_by_horizon"][10]["days"] == 1
