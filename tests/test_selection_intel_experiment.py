import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from llm.selection_experiment import (
    BUDGET, FLASH, LITE, MODELS, ExperimentBlocked, ProviderBudget, capture_packet, cost_nanos,
    finish_attempt, read_experiment, reserve_pair,
)
from llm.selection_experiment_replay import replay_rank
from llm.selection_experiment_report import build_report, review_worksheet
from llm.selection_experiment_runner import ExperimentClient, run_comparison
from web.db import connection


@pytest.fixture(autouse=True)
def experiment_db(tmp_path, monkeypatch):
    monkeypatch.setattr(connection, "DB_PATH", tmp_path / "experiment.db")
    connection.init_db()


def _capture(index, *, title=None):
    captured_at = datetime.now(timezone.utc) + timedelta(milliseconds=index)
    return capture_packet(
        captured_at=captured_at.isoformat(), market="US", scan_type="us_midday",
        articles=[{"url": f"https://example.com/{title or index}", "title": title or f"news {index}",
                   "snippet": "earnings rose", "source": "wire", "date": "2026-11-24"}],
        messages=[{"role": "user", "content": f"Frozen prompt {index}"}],
        temperature=0.2, max_tokens=4096, production_model=LITE,
    )


def _issue(ticker, title="Earnings", direction="up"):
    return {"title": title, "category": "earnings", "confidence": .8, "affected_tickers": [
        {"ticker": ticker, "direction": direction, "name": ticker},
    ]}


class _Client:
    def __init__(self, *, uncertain=False):
        self.uncertain = uncertain
        self.posts = []

    def verify_pricing(self):
        pass

    def budget(self):
        return ProviderBudget("key", BUDGET, BUDGET)

    def reservations(self, _):
        return {model: cost_nanos("0.10") for model in MODELS}

    def complete(self, packet, model):
        self.posts.append((packet["id"], model, packet["messages"]))
        if self.uncertain:
            return None, {"reason": "timeout"}
        cost = "0.01" if model == LITE else "0.03"
        return cost_nanos(cost), {"finish_reason": "stop", "raw": json.dumps([_issue("NVDA")])}


def test_stage_pairs_use_identical_inputs_and_next_distinct_holdout():
    for index in range(1, 24):
        _capture(index, title="duplicate" if index in (1, 2) else None)
    client = _Client()
    assert run_comparison("holdout", 1, client=client)["stopped"] == "calibration_incomplete"
    assert run_comparison("calibration", 40, client=client)["pairs_started"] == 20
    assert run_comparison("holdout", 10, client=client)["pairs_started"] == 2
    assert len(client.posts) == 44
    assert all(first[0] == second[0] and first[1] != second[1] and first[2] == second[2]
               for first, second in zip(client.posts[::2], client.posts[1::2]))
    assert all(packet_id != 2 for packet_id, _, _ in client.posts)


def test_uncertain_attempt_keeps_reservation_and_never_resends():
    _capture(1)
    client = _Client(uncertain=True)
    assert run_comparison("calibration", 5, client=client)["stopped"] == "charge_uncertain_reservation_retained"
    assert read_experiment()["ledger"]["held"] == cost_nanos("0.10")
    assert run_comparison("calibration", 1, client=client)["pairs_started"] == 0
    assert len(client.posts) == 1
    attempts = read_experiment()["pairs"][0]["attempts"]
    assert {attempt["state"] for attempt in attempts} == {"uncertain", "skipped"}


def test_concurrent_reservation_cannot_spend_the_same_budget():
    _capture(1)
    _capture(2)
    ceilings = lambda _: {model: cost_nanos(3) for model in MODELS}
    budget = ProviderBudget("key", BUDGET, BUDGET)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: reserve_pair("calibration", ceilings, budget), range(2)))
    assert sum(packet is not None for packet, _ in results) == 1
    assert read_experiment()["ledger"]["held"] == cost_nanos(6)


def test_stale_provider_balance_cannot_replenish_bound_key():
    _capture(1)
    _capture(2)
    budget = ProviderBudget("key", cost_nanos(7), cost_nanos(7))
    ceilings = lambda _: {model: cost_nanos(3) for model in MODELS}
    packet, _ = reserve_pair("calibration", ceilings, budget)
    for model in MODELS:
        finish_attempt(packet["id"], model, "settled", {}, cost_nanos(3))
    assert reserve_pair("calibration", ceilings, budget) == (None, "budget_remaining_below_pair_reservation")
    with pytest.raises(ExperimentBlocked):
        reserve_pair("calibration", ceilings, ProviderBudget("other-key", BUDGET, BUDGET))


def test_key_must_be_distinct_and_check_nonresetting_8_dollar_cap(monkeypatch):
    monkeypatch.setattr("llm.selection_experiment_runner.load_config", lambda: {"llm": {"api_key": "live"}})
    with pytest.raises(ExperimentBlocked):
        ExperimentClient("live")
    client = ExperimentClient("isolated")
    key_metadata = {"limit": 8, "limit_remaining": 7.505, "limit_reset": None,
                    "is_management_key": False, "include_byok_in_limit": True, "byok_usage": 0}
    monkeypatch.setattr(client, "_metadata", lambda *_args, **_kwargs: key_metadata)
    assert client.budget().remaining == cost_nanos("7.505")
    for key, value in (("limit", 8.000000001), ("limit_reset", "monthly"), ("byok_usage", .1),
                       ("include_byok_in_limit", False), ("limit_remaining", float("nan"))):
        with monkeypatch.context() as context:
            context.setitem(key_metadata, key, value)
            with pytest.raises(ExperimentBlocked):
                client.budget()


def test_model_quality_annotations_and_incremental_cost_use_observed_calls():
    _capture(1)
    run_comparison("calibration", 1, client=_Client())
    with connection.get_db() as db:
        db.executemany(
            """INSERT INTO market_intel_scans(scan_time, scan_type, model_used, status, error_message)
               VALUES ('10:00', 'us_midday', ?, ?, ?)""",
            [(LITE, "success", None), (LITE, "success", None), (LITE, "failed", "No search results")],
        )
    worksheet = review_worksheet()
    review_id = worksheet["packets"][0]["outputs"][0]["review_id"]
    assert "model" not in worksheet["packets"][0]["outputs"][0]
    worksheet["annotations"][review_id] = {"1": {"repeat": "new", "links": {"1": {
        "ticker_link": "correct", "direction": "supported", "article_numbers": [1],
    }}}}
    report = build_report(worksheet)
    assert report["monthly_cost"]["projected_total_flash_increment_usd"] == .04
    model_quality = report["quality_and_rank"]["calibration"]["US"]["models"]
    assert sum(counts.get("direction_supported", 0) for counts in model_quality.values()) == 1
    assert report["pairs"][0]["counterfactual_rank"]["reason"] == "no_recent_frozen_candidate_set"


def test_rank_replays_only_news_over_frozen_candidate_set():
    rank = {"scan_date": "2026-11-24", "detection_date": "2026-11-24", "candidates": [
        {"ticker": ticker, "name": ticker, "baseline": .6, "axes": {"intel": .5}, "weights": {"intel": .25},
         "features": {"us_news_novelty": 0, "relative_volume": 1, "sector_relative_strength": 0}}
        for ticker in ("AMD", "NVDA")
    ], "prior_issues": [], "prior_successful_us_scans": 0, "accuracy": {}, "calibration": {},
        "default_calibration": .5, "captured_at": "2026-11-24T16:00:00+00:00"}
    result = replay_rank({"rank": rank, "market": "US"}, {LITE: [_issue("AMD")], FLASH: [_issue("NVDA")]})
    for method in ("incumbent", "prospective"):
        assert result["ranks"][method] == {"changed": True, "top1": {LITE: "AMD", FLASH: "NVDA"}}


def test_live_scanner_freezes_exact_prompt_and_candidate_axes_before_inference(monkeypatch):
    from pipeline.market_intel import KST, MarketIntelScanner
    from pipeline.selection_shadow import ensure_shadow_schema

    now = datetime.now(timezone.utc)
    scan_day = now.astimezone(KST).date().isoformat()
    observed_at = (now - timedelta(minutes=1)).isoformat()
    with connection.get_db() as db:
        ensure_shadow_schema(db)
        for ticker in ("AMD", "NVDA"):
            event_id = db.execute(
                """INSERT INTO decision_events(ticker, market, scan_date, composite_score, score_breakdown_json, weights_used_json)
                   VALUES (?, 'US', ?, .6, '{"intel": 0.5}', '{"intel": 0.25}')""", (ticker, scan_day),
            ).lastrowid
            db.execute(
                """INSERT INTO selection_shadow_snapshots
                   (scan_date, market, ticker, captured_at, eligible, features_json, sources_json, baseline_score, decision_event_id)
                   VALUES (?, 'US', ?, ?, 1, '{}', '{}', .6, ?)""", (scan_day, ticker, observed_at, event_id),
            )
            db.execute(
                """INSERT INTO selection_shadow_candidates
                   (scan_date, market, ticker, decision_at, baseline_score, decision_event_id, features_json)
                   VALUES (?, 'US', ?, ?, .6, ?, '{"sector_relative_strength": 0.01}')""",
                (scan_day, ticker, observed_at, event_id),
            )
    article = {"title": "Earnings", "source": "wire", "date": "2026-11-24", "snippet": "AMD reports growth",
               "url": "https://example.com/earnings"}

    def chat(**kwargs):
        with connection.get_db() as db:
            row = db.execute("SELECT packet_json, production_scan_id FROM selection_intel_packets").fetchone()
            frozen = json.loads(row["packet_json"])
            assert row["production_scan_id"] is None
            assert frozen["messages"] == kwargs["messages"]
            assert frozen["articles"] == [article]
            assert [candidate["ticker"] for candidate in frozen["rank"]["candidates"]] == ["AMD", "NVDA"]
            assert frozen["rank"]["candidates"][0]["features"]["sector_relative_strength"] == .01
        return json.dumps([{**_issue("AMD"), "sentiment": "positive"}])

    scanner = MarketIntelScanner.__new__(MarketIntelScanner)
    scanner.llm = SimpleNamespace(chat=chat, _resolve_model=lambda _: LITE)
    scanner.searcher = SimpleNamespace(multi_search=lambda *args, **kwargs: [article])
    monkeypatch.setattr(scanner, "_send_discord_alert", lambda *args: None)
    result = scanner.scan("us_midday")
    assert result["status"] == "success"
    with connection.get_db() as db:
        assert db.execute("SELECT production_scan_id FROM selection_intel_packets").fetchone()[0] == result["scan_id"]
