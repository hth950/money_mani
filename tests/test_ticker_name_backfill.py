"""Tests for lazy KRX ticker-name recovery in the actions API."""

import pytest

from market_data.krx_fetcher import KRXFetcher
from web.db import connection
from web.db.migrate import run_schema_migrations
from web.services import signal_service
from web.services.signal_service import SignalService


@pytest.fixture
def name_db(tmp_path, monkeypatch):
    db_path = tmp_path / "names.db"
    monkeypatch.setattr(connection, "DB_PATH", db_path)
    connection.init_db()
    run_schema_migrations()
    signal_service._ticker_name_cache.clear()
    signal_service._ticker_name_failures.clear()
    yield db_path
    signal_service._ticker_name_cache.clear()
    signal_service._ticker_name_failures.clear()


def _insert_score(ticker, name, market="KRX"):
    with connection.get_db() as db:
        db.execute(
            """INSERT INTO scoring_results
               (ticker, ticker_name, market, scan_date, composite_score,
                decision, source)
               VALUES (?, ?, ?, '2026-07-14', 0.55, 'WATCH', 'live')""",
            (ticker, name, market),
        )


def test_actions_resolve_unique_krx_name_and_backfill_allowlisted_tables(
    name_db, monkeypatch
):
    _insert_score("475150", "475150")
    _insert_score("005930", "삼성전자")
    _insert_score("USNUM", "USNUM", market="US")
    with connection.get_db() as db:
        db.execute(
            """INSERT INTO signals
               (strategy_name, ticker, ticker_name, market, signal_type, price)
               VALUES ('s', '475150', '475150', 'KRX', 'BUY', 100)"""
        )
        db.execute(
            """INSERT INTO positions
               (strategy_name, ticker, ticker_name, market, entry_date, entry_price)
               VALUES ('s', '475150', '', 'KRX', '2026-07-14', 100)"""
        )
        db.execute(
            """INSERT INTO signal_performance
               (strategy_name, ticker, ticker_name, market, signal_type)
               VALUES ('s', '475150', NULL, 'KRX', 'BUY')"""
        )
        db.execute(
            """INSERT INTO decision_events
               (ticker, ticker_name, market, scan_date, signal_action)
               VALUES ('475150', '475150', 'KRX', '2026-07-14', 'BUY')"""
        )
        db.execute(
            """INSERT INTO portfolio_snapshots(ticker, name, market)
               VALUES ('475150', '475150', 'KRX')"""
        )

    calls = []

    def fake_name(self, ticker):
        calls.append(ticker)
        return "SK이터닉스"

    monkeypatch.setattr(KRXFetcher, "get_ticker_name", fake_name)
    actions = SignalService().get_actions()
    by_ticker = {row["ticker"]: row for row in actions}

    assert by_ticker["475150"]["ticker_name"] == "SK이터닉스"
    assert by_ticker["005930"]["ticker_name"] == "삼성전자"
    assert by_ticker["USNUM"]["ticker_name"] == "USNUM"
    assert calls == ["475150"]

    with connection.get_db() as db:
        names = {
            "scoring_results": db.execute(
                "SELECT ticker_name FROM scoring_results WHERE ticker='475150'"
            ).fetchone()[0],
            "signals": db.execute(
                "SELECT ticker_name FROM signals WHERE ticker='475150'"
            ).fetchone()[0],
            "positions": db.execute(
                "SELECT ticker_name FROM positions WHERE ticker='475150'"
            ).fetchone()[0],
            "signal_performance": db.execute(
                "SELECT ticker_name FROM signal_performance WHERE ticker='475150'"
            ).fetchone()[0],
            "decision_events": db.execute(
                "SELECT ticker_name FROM decision_events WHERE ticker='475150'"
            ).fetchone()[0],
            "portfolio_snapshots": db.execute(
                "SELECT name FROM portfolio_snapshots WHERE ticker='475150'"
            ).fetchone()[0],
        }
    assert set(names.values()) == {"SK이터닉스"}


def test_failed_krx_name_resolution_is_cached_and_keeps_numeric_name(
    name_db, monkeypatch
):
    _insert_score("185750", "185750")
    calls = []

    def unresolved(self, ticker):
        calls.append(ticker)
        return ticker

    monkeypatch.setattr(KRXFetcher, "get_ticker_name", unresolved)
    first = SignalService().get_actions()
    second = SignalService().get_actions()

    assert first[0]["ticker_name"] == "185750"
    assert second[0]["ticker_name"] == "185750"
    assert calls == ["185750"]
