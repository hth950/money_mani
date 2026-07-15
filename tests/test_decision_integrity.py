"""Regression tests for the decision ledger and action semantics."""

import json
import sqlite3

import pytest

from web.db import connection
from web.db.migrate import (
    _assert_foreign_key_integrity,
    _ensure_decision_event_foreign_keys,
    _repair_signal_foreign_keys,
)
from web.services.scoring_service import ScoringService
from web.services.signal_service import SignalService
from strategy.models import Strategy


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "money_mani.db"
    monkeypatch.setattr(connection, "DB_PATH", db_path)
    connection.init_db()
    # The additive migration creates decision_events and source on fresh DBs.
    from web.db.migrate import run_schema_migrations

    run_schema_migrations()
    return db_path


def test_signal_fk_repair_preserves_rows_and_indexes():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        PRAGMA foreign_keys=OFF;
        CREATE TABLE signals(id INTEGER PRIMARY KEY, ticker TEXT);
        CREATE TABLE positions(
            id INTEGER PRIMARY KEY,
            entry_signal_id INTEGER REFERENCES signals_old(id),
            ticker TEXT
        );
        CREATE UNIQUE INDEX idx_positions_ticker ON positions(ticker);
        CREATE TABLE signal_performance(
            id INTEGER PRIMARY KEY,
            signal_id INTEGER REFERENCES signals_old(id) ON DELETE SET NULL,
            pnl REAL
        );
        INSERT INTO signals VALUES (1, 'AAA');
        INSERT INTO positions VALUES (1, 1, 'AAA');
        INSERT INTO signal_performance VALUES (1, 1, 0.1);
        PRAGMA foreign_keys=ON;
        """
    )

    assert _repair_signal_foreign_keys(db) == 2
    _assert_foreign_key_integrity(db)
    assert db.execute("SELECT ticker FROM positions").fetchone()[0] == "AAA"
    assert db.execute("SELECT pnl FROM signal_performance").fetchone()[0] == 0.1
    assert db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_positions_ticker'"
    ).fetchone()
    assert db.execute("PRAGMA foreign_key_list(positions)").fetchone()[2] == "signals"


def test_decision_fk_migration_preserves_rows_and_nulls_legacy_dangling_ids(
    isolated_db,
):
    with connection.get_db() as db:
        score_id = db.execute(
            "INSERT INTO scoring_results(ticker, market, scan_date) "
            "VALUES ('AAA', 'KRX', '2026-07-14')"
        ).lastrowid
        signal_id = db.execute(
            "INSERT INTO signals(ticker, market, signal_type) VALUES ('AAA', 'KRX', 'BUY')"
        ).lastrowid
        valid_event = db.execute(
            """INSERT INTO decision_events
               (scoring_result_id, signal_id, ticker, market, scan_date)
               VALUES (?, ?, 'AAA', 'KRX', '2026-07-14')""",
            (score_id, signal_id),
        ).lastrowid
        dangling_event = db.execute(
            """INSERT INTO decision_events(ticker, market, scan_date)
               VALUES ('BBB', 'KRX', '2026-07-14')"""
        ).lastrowid
        db.execute(
            "INSERT INTO decision_outcomes(decision_event_id, horizon_days, status) "
            "VALUES (?, 1, 'pending')",
            (valid_event,),
        )

    # Recreate the pre-FK shape while preserving the exact legacy rows, then
    # inject ids that no longer exist in their parent ledgers.
    raw = sqlite3.connect(isolated_db)
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.executescript(
        """
        CREATE TABLE decision_events_legacy AS SELECT * FROM decision_events;
        DROP TABLE decision_events;
        ALTER TABLE decision_events_legacy RENAME TO decision_events;
        CREATE INDEX idx_legacy_decision_ticker ON decision_events(ticker);
        """
    )
    raw.execute(
        "UPDATE decision_events SET scoring_result_id=99991, signal_id=99992 WHERE id=?",
        (dangling_event,),
    )
    raw.commit()
    raw.close()

    with connection.get_db() as db:
        assert _ensure_decision_event_foreign_keys(db)
        rows = db.execute(
            "SELECT id, scoring_result_id, signal_id FROM decision_events ORDER BY id"
        ).fetchall()
        outcomes = db.execute(
            "SELECT decision_event_id, status FROM decision_outcomes"
        ).fetchall()
        event_fks = {
            (row["from"], row["table"], row["on_delete"])
            for row in db.execute("PRAGMA foreign_key_list(decision_events)")
        }
        outcome_target = db.execute(
            "PRAGMA foreign_key_list(decision_outcomes)"
        ).fetchone()["table"]
        legacy_index = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_legacy_decision_ticker'"
        ).fetchone()
        _assert_foreign_key_integrity(db)

    assert len(rows) == 2
    assert tuple(rows[0][1:]) == (score_id, signal_id)
    assert tuple(rows[1][1:]) == (None, None)
    assert [tuple(row) for row in outcomes] == [(valid_event, "pending")]
    assert {
        ("scoring_result_id", "scoring_results", "SET NULL"),
        ("signal_id", "signals", "SET NULL"),
    } <= event_fks
    assert outcome_target == "decision_events"
    assert legacy_index


def test_scoring_result_keeps_append_only_decision_events(isolated_db):
    service = ScoringService()
    with connection.get_db() as db:
        signal_id = db.execute(
            """INSERT INTO signals
               (strategy_name, ticker, ticker_name, market, signal_type, price)
               VALUES ('test', 'AAA', 'AAA', 'KRX', 'BUY', 100.0)"""
        ).lastrowid
    scores = {
        "technical": 0.6,
        "fundamental": 0.5,
        "flow": 0.5,
        "intel": 0.4,
        "macro": 0.7,
        "composite": 0.55,
    }
    first_event_id = service.save_scoring_result(
        "AAA", "KRX", "2026-07-14", scores, "WATCH",
        signal_action="BUY", recommendation="WATCH", execution_state="WATCH_ONLY",
        score_details={"rsi": 42}, consensus_count=7,
        consensus_strategies=["s1", "s2"], provenance={"source": "test"},
        signal_id=signal_id,
    )
    second_event_id = service.save_scoring_result(
        "AAA", "KRX", "2026-07-14", {**scores, "composite": 0.68}, "EXECUTE",
        signal_action="BUY", recommendation="EXECUTE", execution_state="PENDING_OPEN",
        score_details={"rsi": 55}, provenance={"source": "rescore"},
        signal_id=signal_id,
    )
    assert first_event_id and second_event_id

    with connection.get_db() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM scoring_results WHERE ticker='AAA' AND scan_date='2026-07-14'"
        ).fetchone()[0] == 1
        events = db.execute(
            "SELECT recommendation, execution_state, score_details_json, signal_id, "
            "scoring_result_id FROM decision_events "
            "WHERE ticker='AAA' ORDER BY id"
        ).fetchall()
        current_score_id = db.execute(
            "SELECT id FROM scoring_results WHERE ticker='AAA'"
        ).fetchone()[0]
    assert len(events) == 2
    assert events[0][0:2] == ("WATCH", "WATCH_ONLY")
    assert json.loads(events[0][2])["rsi"] == 42
    assert events[0][3] == signal_id
    assert events[0][4] is None
    assert events[1][0:2] == ("EXECUTE", "PENDING_OPEN")
    assert events[1][3] == signal_id
    assert events[1][4] == current_score_id

    # Both nullable references are enforced and clear rather than dangling.
    with connection.get_db() as db:
        db.execute("DELETE FROM signals WHERE id=?", (signal_id,))
        db.execute("DELETE FROM scoring_results WHERE id=?", (current_score_id,))
        refs = db.execute(
            "SELECT scoring_result_id, signal_id FROM decision_events ORDER BY id"
        ).fetchall()
        _assert_foreign_key_integrity(db)
    assert all(row[0] is None and row[1] is None for row in refs)


def test_scoring_save_does_not_return_rolled_back_event_id(isolated_db):
    with connection.get_db() as db:
        db.execute(
            """CREATE TRIGGER fail_scoring_insert
               BEFORE INSERT ON scoring_results
               BEGIN SELECT RAISE(ABORT, 'forced scoring failure'); END"""
        )

    event_id = ScoringService().save_scoring_result(
        "ROLLBACK", "KRX", "2026-07-14",
        {"technical": 0.5, "fundamental": 0.5, "flow": 0.5,
         "intel": 0.5, "macro": 0.5, "composite": 0.5},
        "WATCH",
    )
    assert event_id is None
    with connection.get_db() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM decision_events WHERE ticker='ROLLBACK'"
        ).fetchone()[0] == 0


def test_skip_is_not_sell_in_signal_actions(isolated_db):
    service = ScoringService()
    service.save_scoring_result(
        "AAA", "KRX", "2026-07-14",
        {"technical": 0.2, "fundamental": 0.5, "flow": 0.5, "intel": 0.5,
         "macro": 0.5, "composite": 0.2},
        "SKIP", signal_action="BUY", recommendation="SKIP",
    )

    action = SignalService().get_actions()[0]
    assert action["action"] == "NONE"
    assert action["recommendation"] == "SKIP"


def test_strategy_market_list_is_normalized():
    kr = Strategy.from_yaml({"name": "kr", "category": "trend", "status": "validated", "markets": ["KRX"]})
    both = Strategy.from_yaml({"name": "both", "category": "trend", "status": "validated", "markets": ["KRX", "US"]})
    assert kr.market == "KRX"
    assert both.market == "ALL"


class _FakeDiscord:
    def send_signal_alert(self, *args, **kwargs):
        pass

    def send_daily_summary(self, *args, **kwargs):
        pass


class _FakeEmail:
    def send(self, *args, **kwargs):
        pass


class _FakeSignalService:
    def save_signal(self, _sig):
        return 1


class _FakePositionService:
    def __init__(self):
        self.open_calls = []

    def open_position(self, **kwargs):
        self.open_calls.append(kwargs)


class _FakeScoringService:
    def __init__(self):
        self.marked = []

    def link_decision_event_signal(self, *args):
        return True

    def mark_decision_event(self, *args):
        self.marked.append(args)


def test_watch_buy_does_not_open_position(monkeypatch):
    # Import the module without executing pipeline/__init__.py, which pulls in
    # the optional YouTube scraper dependency not needed for this unit test.
    import importlib.util
    import pathlib
    import sys
    import types

    package = types.ModuleType("pipeline")
    package.__path__ = [str(pathlib.Path(__file__).parents[1] / "pipeline")]
    monkeypatch.setitem(sys.modules, "pipeline", package)
    spec = importlib.util.spec_from_file_location(
        "pipeline.daily_scan", pathlib.Path(__file__).parents[1] / "pipeline" / "daily_scan.py"
    )
    daily_scan = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "pipeline.daily_scan", daily_scan)
    spec.loader.exec_module(daily_scan)

    daily_scan._sent_signals_today.clear()
    scanner = daily_scan.DailyScan.__new__(daily_scan.DailyScan)
    scanner.discord = _FakeDiscord()
    scanner.email = _FakeEmail()
    scanner.signal_service = _FakeSignalService()
    scanner.position_service = _FakePositionService()
    scanner.scoring_service = _FakeScoringService()
    scanner.config = {"notifications": {"email": {"enabled": False}}}

    scanner._send_alerts([{
        "strategy_name": "test",
        "ticker": "AAA",
        "ticker_name": "AAA",
        "market": "KRX",
        "signal_type": "BUY",
        "score_decision": "WATCH",
        "price": 100.0,
        "date": "2026-07-14",
        "decision_event_id": 7,
    }], "2026-07-14")

    assert scanner.position_service.open_calls == []
    assert scanner.scoring_service.marked == [(7, "WATCH_ONLY")]
