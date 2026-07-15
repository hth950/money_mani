"""Tests for point-in-time decision outcome labels."""

import pandas as pd
import pytest

from web.db import connection
from web.db.migrate import run_schema_migrations
from web.services.outcome_service import DecisionOutcomeService


@pytest.fixture
def outcome_db(tmp_path, monkeypatch):
    db_path = tmp_path / "money_mani.db"
    monkeypatch.setattr(connection, "DB_PATH", db_path)
    connection.init_db()
    run_schema_migrations()
    return db_path


def _insert_event(action="BUY", scan_date="2026-01-01", ticker="AAA"):
    with connection.get_db() as db:
        cur = db.execute(
            """
            INSERT INTO decision_events
              (ticker, ticker_name, market, signal_action, recommendation,
               execution_state, scan_date, signal_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ticker, ticker, "US", action, "WATCH", "WATCH_ONLY", scan_date, 100.0),
        )
        return cur.lastrowid


def _frame(periods=30, jump=False):
    dates = pd.bdate_range("2026-01-01", periods=periods)
    close = pd.Series([100.0 + i for i in range(periods)], index=dates)
    if jump:
        close.iloc[1] = 2000.0
    return pd.DataFrame({
        "Open": close,
        "High": close + 2.0,
        "Low": close - 2.0,
        "Close": close,
        "Volume": 1000,
    })


def test_buy_sell_horizons_costs_excess_and_idempotence(outcome_db):
    buy_id = _insert_event("BUY")
    sell_id = _insert_event("SELL")
    prices = _frame()
    benchmark = _frame()
    benchmark[["Open", "High", "Low", "Close"]] = benchmark[["Open", "High", "Low", "Close"]] * 0.5

    service = DecisionOutcomeService(
        price_loader=lambda market, ticker, start, end: prices,
        benchmark_loader=lambda market, ticker, start, end: benchmark,
        transaction_costs={"US": 1.0},
    )
    first = service.label_pending(limit=10, as_of="2026-03-01")
    second = service.label_pending(limit=10, as_of="2026-03-01")

    assert first["events"] == 2
    # Fully evaluated events are not fetched again; upsert idempotence is
    # covered by the stable row count and unique key.
    assert second["events"] == 0
    with connection.get_db() as db:
        rows = db.execute(
            "SELECT * FROM decision_outcomes ORDER BY decision_event_id, horizon_days"
        ).fetchall()
    assert len(rows) == 8
    assert all(row["status"] == "evaluated" for row in rows)
    assert len({row["decision_event_id"] for row in rows}) == 2

    buy_rows = [row for row in rows if row["decision_event_id"] == buy_id]
    sell_rows = [row for row in rows if row["decision_event_id"] == sell_id]
    assert buy_rows[0]["raw_return_pct"] > 0
    assert sell_rows[0]["raw_return_pct"] < 0
    assert buy_rows[0]["net_return_pct"] == pytest.approx(buy_rows[0]["raw_return_pct"] - 1.0)
    assert buy_rows[0]["benchmark_return_pct"] is not None
    assert buy_rows[0]["excess_return_pct"] is not None
    assert buy_rows[0]["mfe_pct"] > 0
    assert buy_rows[0]["mae_pct"] < 0


def test_unmatured_horizon_is_pending(outcome_db):
    _insert_event("BUY")
    # The loader returns future rows, but the historical as_of cutoff must
    # still leave horizons whose exit date is not observable as pending.
    prices = _frame(periods=30)
    service = DecisionOutcomeService(
        price_loader=lambda market, ticker, start, end: prices,
        benchmark_loader=lambda market, ticker, start, end: prices,
    )
    service.label_pending(limit=10, as_of="2026-01-05")
    with connection.get_db() as db:
        statuses = {
            row["horizon_days"]: row["status"]
            for row in db.execute("SELECT horizon_days, status FROM decision_outcomes")
        }
    assert statuses[1] == "evaluated"
    assert statuses[5] == "pending"
    assert statuses[20] == "pending"


def test_provider_price_jump_is_invalid(outcome_db):
    _insert_event("BUY")
    prices = _frame(periods=25, jump=True)
    service = DecisionOutcomeService(
        price_loader=lambda market, ticker, start, end: prices,
        benchmark_loader=lambda market, ticker, start, end: prices,
    )
    result = service.label_pending(limit=10, as_of="2026-03-01")
    assert result["invalid"] == 4
    with connection.get_db() as db:
        rows = db.execute("SELECT status, reason FROM decision_outcomes").fetchall()
    assert all(row["status"] == "invalid" for row in rows)
    assert all("jump_ratio" in row["reason"] for row in rows)


def test_future_event_is_not_labelled_before_as_of(outcome_db):
    _insert_event("BUY", scan_date="2026-02-01")
    service = DecisionOutcomeService(
        price_loader=lambda market, ticker, start, end: _frame(periods=30),
        benchmark_loader=lambda market, ticker, start, end: _frame(periods=30),
    )
    result = service.label_pending(limit=10, as_of="2026-01-05")
    assert result["events"] == 0
    with connection.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM decision_outcomes").fetchone()[0] == 0


def test_unavailable_retries_after_new_events_without_starving_them(outcome_db):
    unavailable_id = _insert_event("BUY", ticker="OLD")
    unavailable_service = DecisionOutcomeService(
        price_loader=lambda market, ticker, start, end: None,
        benchmark_loader=lambda market, ticker, start, end: None,
    )
    first = unavailable_service.label_pending(limit=1, as_of="2026-03-01")
    assert first["events"] == 1
    assert first["unavailable"] == 4

    new_id = _insert_event("BUY", scan_date="2026-01-02", ticker="NEW")
    prices = _frame(periods=30)
    recovered_service = DecisionOutcomeService(
        price_loader=lambda market, ticker, start, end: prices,
        benchmark_loader=lambda market, ticker, start, end: prices,
    )

    # The new/no-outcome event wins the bounded slot even though the failed
    # unavailable event is older.
    second = recovered_service.label_pending(limit=1, as_of="2026-03-01")
    assert second["events"] == 1
    assert second["evaluated"] == 4
    with connection.get_db() as db:
        old_statuses = db.execute(
            "SELECT status FROM decision_outcomes WHERE decision_event_id=?",
            (unavailable_id,),
        ).fetchall()
        new_statuses = db.execute(
            "SELECT status FROM decision_outcomes WHERE decision_event_id=?",
            (new_id,),
        ).fetchall()
        row_count_before_retry = db.execute(
            "SELECT COUNT(*) FROM decision_outcomes"
        ).fetchone()[0]
    assert {row[0] for row in old_statuses} == {"unavailable"}
    assert {row[0] for row in new_statuses} == {"evaluated"}
    assert row_count_before_retry == 8

    # Once primary work is drained, the provider recovery upgrades the same
    # four unavailable rows through the unique-key upsert.
    third = recovered_service.label_pending(limit=1, as_of="2026-03-01")
    assert third["events"] == 1
    assert third["evaluated"] == 4
    with connection.get_db() as db:
        rows = db.execute(
            "SELECT status FROM decision_outcomes WHERE decision_event_id=?",
            (unavailable_id,),
        ).fetchall()
        assert {row[0] for row in rows} == {"evaluated"}
        assert db.execute("SELECT COUNT(*) FROM decision_outcomes").fetchone()[0] == 8
    assert recovered_service.label_pending(limit=1, as_of="2026-03-01")["events"] == 0


def test_terminal_horizons_are_not_retried_in_mixed_event(outcome_db):
    event_id = _insert_event("BUY")
    with connection.get_db() as db:
        db.executemany(
            """INSERT INTO decision_outcomes
               (decision_event_id, horizon_days, status, raw_return_pct, reason)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (event_id, 1, "evaluated", 999.0, "terminal_evaluated"),
                (event_id, 5, "invalid", None, "terminal_invalid"),
                (event_id, 10, "unavailable", None, "price_data_unavailable"),
                (event_id, 20, "pending", None, "horizon_not_matured"),
            ],
        )

    prices = _frame(periods=30)
    service = DecisionOutcomeService(
        price_loader=lambda market, ticker, start, end: prices,
        benchmark_loader=lambda market, ticker, start, end: prices,
    )
    result = service.label_pending(limit=1, as_of="2026-03-01")
    assert result["events"] == 1
    assert result["evaluated"] == 2

    with connection.get_db() as db:
        rows = {
            row["horizon_days"]: dict(row)
            for row in db.execute(
                "SELECT * FROM decision_outcomes WHERE decision_event_id=?",
                (event_id,),
            ).fetchall()
        }
    assert len(rows) == 4
    assert rows[1]["status"] == "evaluated"
    assert rows[1]["raw_return_pct"] == 999.0
    assert rows[1]["reason"] == "terminal_evaluated"
    assert rows[5]["status"] == "invalid"
    assert rows[5]["reason"] == "terminal_invalid"
    assert rows[10]["status"] == "evaluated"
    assert rows[20]["status"] == "evaluated"
