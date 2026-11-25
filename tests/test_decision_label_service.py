"""Executable label timing, benchmarks, coverage, and copy isolation."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pandas as pd
import pytest

from web.db import connection
from web.services.decision_label_service import DecisionLabelService, scheduled_exit_at


@pytest.fixture
def event_db(tmp_path, monkeypatch):
    path = tmp_path / "events.db"
    monkeypatch.setattr(connection, "DB_PATH", path)
    connection.init_db()
    return path


def event(market, created_at, *, detected_at=None, action="BUY"):
    with connection.get_db() as db:
        return db.execute("""
            INSERT INTO decision_events (market, ticker, scan_date, signal_action, created_at, detected_at)
            VALUES (?, 'TEST', '2024-01-02', ?, ?, ?)
        """, (market, action, created_at, detected_at)).lastrowid


def bars(values):
    return pd.DataFrame.from_dict(values, orient="index", columns=["Open", "Close"])


def stored(event_id, horizon):
    with connection.get_db() as db:
        return dict(db.execute("""
            SELECT * FROM decision_outcome_labels WHERE decision_event_id=? AND horizon_days=?
        """, (event_id, horizon)).fetchone())


def test_krx_strict_open_uses_both_recorded_clocks_and_actual_session(event_db):
    before = event("KRX", "2026-07-16 23:59:59")
    on_open = event("KRX", "2026-07-19 23:59:59", detected_at="2026-07-20T09:00:00+09:00")
    decision_bars = bars({"2026-07-20": (100, 110), "2026-07-21": (110, 132)})
    benchmark = bars({"2026-07-20": (50, 51), "2026-07-21": (60, 63)})
    service = DecisionLabelService(lambda *_: decision_bars, lambda *_: benchmark)

    report = service.label_pending(as_of="2026-07-21T07:00:00Z")
    assert report == {"events": 2, "evaluated": 2, "pending": 6, "unavailable": 0, "invalid": 0,
                      "reasons": {"horizon_not_matured": 6}}
    same = stored(before, 1)
    assert same["entry_date"] == same["exit_date"] == "2026-07-20"  # July 17 holiday
    assert same["benchmark_entry_price"] == 50
    assert same["benchmark_exit_price"] == 51
    assert same["raw_return_pct"] == 10
    assert same["transaction_cost_pct"] == 0.21
    assert same["slippage_pct"] == 0.10
    assert same["net_return_pct"] == 9.69
    assert same["excess_return_pct"] == 7.69
    rolled = stored(on_open, 1)
    assert rolled["decision_at"] == "2026-07-20T00:00:00.000000+00:00"
    assert rolled["entry_at"] == "2026-07-21T00:00:00.000000+00:00"
    assert rolled["exit_at"] == "2026-07-21T06:30:00.000000+00:00"


def test_us_dst_maturity_and_benchmark_dates_do_not_shift(event_db):
    sell = event("US", "2026-07-02T15:00:00-04:00", action="SELL")
    asset_prices = bars({"2026-07-06": (100, 90), "2026-07-10": (90, 80)})
    index_prices = bars({"2026-07-07": (50, 55), "2026-07-10": (60, 66)})
    bar_calls = []

    def price_loader(*args):
        bar_calls.append(args)
        return asset_prices

    service = DecisionLabelService(price_loader, lambda *_: index_prices)
    assert service.label_pending(as_of="2026-07-06T19:59:59Z")["pending"] == 4
    assert not bar_calls
    report = service.label_pending(as_of="2026-07-10T20:00:00Z")
    assert report["reasons"] == {"benchmark_entry_bar_missing": 2}
    unavailable = stored(sell, 5)
    assert unavailable["status"] == "unavailable"
    assert unavailable["entry_at"] == "2026-07-06T13:30:00.000000+00:00"
    assert unavailable["exit_at"] == "2026-07-10T20:00:00.000000+00:00"
    assert unavailable["net_return_pct"] == 19.96
    assert unavailable["benchmark_return_pct"] is None
    assert bar_calls == [("US", "TEST", "2026-07-06", "2026-07-11")]

    index_prices.loc["2026-07-06"] = (50, 52)
    assert service.label_pending(as_of="2026-07-11T20:00:00Z")["evaluated"] == 2
    assert stored(sell, 5)["excess_return_pct"] == 51.96
    assert service.label_pending(as_of="2026-07-11T20:00:00Z")["events"] == 0


def test_special_exchange_session_closes():
    assert scheduled_exit_at("KRX", "2026-11-19T09:30:00+09:00", 1) == datetime(
        2026, 11, 19, 7, 30, tzinfo=timezone.utc
    )
    assert scheduled_exit_at("US", "2025-11-27T08:00:00-05:00", 1) == datetime(
        2025, 11, 28, 18, 0, tzinfo=timezone.utc
    )
    assert scheduled_exit_at("US", "2025-11-27", 1) is None


def test_date_only_decision_is_invalid_and_future_decision_is_pending(event_db):
    invalid = event("US", "2025-11-27")
    future = event("US", "2025-11-28T18:00:01Z")

    def no_market_data(*_):
        pytest.fail("Invalid or future decisions should trigger zero requests")

    service = DecisionLabelService(no_market_data, no_market_data)
    report = service.label_pending(as_of="2025-11-28T18:00:00Z")
    assert report["reasons"] == {"invalid_decision_timestamp": 4, "decision_after_cutoff": 4}
    assert stored(invalid, 1)["entry_at"] is None
    assert stored(future, 1)["entry_date"] == "2025-12-01"


def test_missing_open_stays_unavailable_and_shadow_candidates_get_their_own_labels(event_db):
    event_id = event("KRX", "2026-09-02 23:00:00")
    with connection.get_db() as db:
        db.execute("""
            CREATE TABLE selection_shadow_candidates (
                id INTEGER PRIMARY KEY, market TEXT, ticker TEXT, decision_at TEXT
            )
        """)
        db.execute("INSERT INTO selection_shadow_candidates VALUES (1, 'US', 'TEST', '2026-09-03T12:00:00Z')")

    open_missing = pd.DataFrame({"Close": [100]}, index=["2026-09-03"])
    benchmark = bars({"2026-09-03": (50, 55)})
    service = DecisionLabelService(lambda *_: open_missing, lambda *_: benchmark)
    assert service.label_pending(as_of="2026-09-03T22:00:00Z")["unavailable"] == 1
    assert service.label_shadow_pending(as_of="2026-09-03T22:00:00Z")["unavailable"] == 1
    assert stored(event_id, 1)["reason"] == "price_open_or_close_unavailable"
    assert stored(event_id, 1)["entry_price"] is None
    with connection.get_db() as db:
        shadow = db.execute("SELECT * FROM shadow_outcome_labels WHERE horizon_days=1").fetchone()
        assert shadow["shadow_candidate_id"] == 1
        assert shadow["entry_at"] == "2026-09-03T13:30:00.000000+00:00"


def test_offline_relabel_copies_backup_preserving_legacy_rows(event_db, tmp_path):
    event_id = event("US", "2026-07-06 12:00:00")
    with connection.get_db() as db:
        db.execute("""
            INSERT INTO decision_outcomes (decision_event_id, horizon_days, status, raw_return_pct)
            VALUES (?, 1, 'evaluated', -99)
        """, (event_id,))
    original = event_db.read_bytes()
    cache = tmp_path / "cache"
    cache.mkdir()
    bars({"2026-07-06": (100, 110)}).to_csv(cache / "US_TEST_20260706_20260707.csv")
    bars({"2026-07-06": (50, 52)}).to_csv(cache / "US_^GSPC.csv")
    output = tmp_path / "outcome-copy.db"
    cli = Path(__file__).resolve().parents[1] / "scripts" / "relabel_decisions.py"
    command = [sys.executable, str(cli), "--input-db", str(event_db), "--output-db", str(output),
               "--cache-dir", str(cache), "--as-of", "2026-07-06T20:00:00Z", "--batch-size", "1"]
    run = subprocess.run(command, check=True, capture_output=True, text=True)
    assert json.loads(run.stdout)["decision_outcomes"]["labels"] == 4
    with sqlite3.connect(output) as db:
        assert db.execute("SELECT raw_return_pct FROM decision_outcomes").fetchone()[0] == -99
        assert db.execute("SELECT COUNT(*) FROM decision_outcome_labels WHERE status='evaluated'").fetchone()[0] == 1
    assert event_db.read_bytes() == original
    assert "Output already exists" in subprocess.run(command, capture_output=True, text=True).stderr
