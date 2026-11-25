import json
from datetime import timedelta

import pytest

from llm.selection_experiment import FLASH, LITE, ensure_experiment_tables
from llm.selection_experiment_report import model_selection_summary
from pipeline.selection_shadow import ensure_shadow_schema
from web.db import connection
from web.services.decision_label_service import LABEL_VERSION, SHADOW_OUTCOME_LABEL_DDL, exchange_schedule


@pytest.fixture(autouse=True)
def experiment_db(tmp_path, monkeypatch):
    monkeypatch.setattr(connection, "DB_PATH", tmp_path / "outcomes.db")
    connection.init_db()
    with connection.get_db() as db:
        ensure_shadow_schema(db)
        ensure_experiment_tables(db)
        db.execute(SHADOW_OUTCOME_LABEL_DDL)


def _record_candidate_set(db, captured, entry, end, *, flash_return, shift_label=False, observed_at=None):
    scan_date = entry.name.date().isoformat()
    decided_at = (captured - timedelta(minutes=5)).isoformat()
    entry_at = entry["open"].to_pydatetime()
    exit_at = end["close"].to_pydatetime()
    observed = observed_at or exit_at + timedelta(hours=1)
    candidates = []
    for ticker, net in (("AMD", -1.0), ("NVDA", flash_return)):
        event = db.execute(
            "INSERT INTO decision_events(ticker, market, scan_date, created_at) VALUES (?, 'US', ?, ?)",
            (ticker, scan_date, decided_at),
        ).lastrowid
        db.execute(
            """INSERT INTO selection_shadow_snapshots(scan_date, market, ticker, captured_at, eligible, features_json, sources_json)
               VALUES (?, 'US', ?, ?, 1, '{}', '{}')""", (scan_date, ticker, decided_at),
        )
        shadow = db.execute(
            """INSERT INTO selection_shadow_candidates
               (scan_date, market, ticker, decision_at, baseline_score, decision_event_id, features_json)
               VALUES (?, 'US', ?, ?, 0.6, ?, '{}')""", (scan_date, ticker, decided_at, event),
        ).lastrowid
        labelled_entry = entry_at + timedelta(seconds=1) if shift_label and ticker == "AMD" else entry_at
        db.execute(
            """INSERT INTO shadow_outcome_labels
               (shadow_candidate_id, horizon_days, label_version, decision_at, entry_date, exit_date,
                entry_at, exit_at, observed_at, status, net_return_pct)
               VALUES (?, 10, ?, ?, ?, ?, ?, ?, ?, 'evaluated', ?)""",
            (shadow, LABEL_VERSION, decided_at, scan_date, end.name.date().isoformat(),
             labelled_entry.isoformat(), exit_at.isoformat(), observed.isoformat(), net),
        )
        candidates.append({"ticker": ticker, "name": ticker, "decision_event_id": event, "baseline": .6,
                           "axes": {"intel": .5}, "weights": {"intel": .25}, "features": {}})
    return {
        "scan_date": scan_date, "detection_date": scan_date, "candidates": candidates,
        "prior_issues": [], "prior_successful_us_scans": 0, "accuracy": {}, "calibration": {},
        "default_calibration": .5, "captured_at": captured.isoformat(),
    }


def _record_packet(db, captured, stage, rank, finished, index):
    packet = {"market": "US", "rank": rank, "articles": [{"title": "private packet title"}], "prior_titles": []}
    digest = f"digest-{index}"
    packet_id = db.execute(
        """INSERT INTO selection_intel_packets(captured_at, market, scan_type, news_digest, packet_json)
           VALUES (?, 'US', 'us_midday', ?, ?)""", (captured.isoformat(), digest, json.dumps(packet)),
    ).lastrowid
    db.execute("INSERT INTO selection_intel_pairs VALUES (?, ?, ?, ?)",
               (packet_id, digest, stage, (captured + timedelta(minutes=1)).isoformat()))
    for model, ticker in ((LITE, "AMD"), (FLASH, "NVDA")):
        issue = {"title": "private answer title", "category": "event", "confidence": .8,
                 "affected_tickers": [{"ticker": ticker, "direction": "up"}]}
        detail = json.dumps({"finish_reason": "stop", "raw": json.dumps([issue])})
        db.execute(
            """INSERT INTO selection_intel_attempts
               (packet_id, model, state, reserved_nanos, billed_nanos, finished_at, detail_json)
               VALUES (?, ?, 'settled', 1000000, 1000000, ?, ?)""",
            (packet_id, model, finished.isoformat(), detail),
        )


def _windows(count):
    schedule = exchange_schedule("US").loc["2025-01-02":"2025-06-01"]
    return [(schedule.iloc[i], schedule.iloc[i + 9]) for i in range(count)]


@pytest.mark.parametrize(("flash_return", "expected_status"), [(3., "switch_review"), (-2., "hold")])
def test_30_distinct_timely_dates_are_required_for_model_decision(flash_return, expected_status):
    windows = _windows(31)
    with connection.get_db() as db:
        for index, (entry, end) in enumerate(windows):
            captured = entry["open"].to_pydatetime() - timedelta(hours=1)
            rank = _record_candidate_set(db, captured, entry, end, flash_return=flash_return)
            finished = captured + timedelta(minutes=2)
            _record_packet(db, captured, "calibration" if index == 0 else "holdout", rank, finished, index)
            if index == 1:
                _record_packet(db, captured + timedelta(seconds=1), "holdout", rank, finished, "duplicate")
        cutoff = windows[-1][1]["close"].to_pydatetime() + timedelta(days=1)
        evidence = model_selection_summary(db, cutoff)
    result = evidence["markets"]["US"]
    assert result["status"] == expected_status
    assert result["coverage_pct"] == 100
    assert result["heldout_packets"] == 31
    assert result["counts"]["repeated_packet_dates"] == 1
    assert result["prospective"]["days"] == result["prospective"]["top1_changes"] == 30
    assert result["prospective"]["mean_flash_minus_lite_pct"] == flash_return + 1
    assert result["prospective"]["mean_flash_minus_baseline_pct"] == (flash_return + 1) / 2
    assert result["prospective"]["changed_selection_mean_flash_minus_lite_pct"] == flash_return + 1
    assert result["retrospective"]["days"] == 0
    assert "private" not in json.dumps(evidence)


def test_late_models_and_unmatched_or_unobserved_labels_cannot_promote():
    windows = _windows(4)
    cutoff = windows[-1][1]["close"].to_pydatetime() + timedelta(days=1)
    with connection.get_db() as db:
        for index, (entry, end) in enumerate(windows):
            entry_at = entry["open"].to_pydatetime()
            captured = entry_at - timedelta(hours=1)
            rank = _record_candidate_set(db, captured, entry, end, flash_return=3., shift_label=index == 1,
                                         observed_at=(cutoff + timedelta(days=1)) if index == 2 else None)
            finished = entry_at if index == 0 else (cutoff + timedelta(days=1)) if index == 3 else captured + timedelta(minutes=2)
            _record_packet(db, captured, "holdout", rank, finished, index)
        evidence = model_selection_summary(db, cutoff)
    result = evidence["markets"]["US"]
    assert result["status"] == "pending"
    assert result["mature_entry_dates"] == 4
    assert result["coverage_pct"] == 0
    assert result["prospective"]["days"] == 0
    assert result["retrospective"]["days"] == result["retrospective"]["top1_changes"] == 1
    assert result["counts"]["unmatched_10_session_labels"] == 2
    assert result["counts"]["incomplete_pair_outputs"] == 1
