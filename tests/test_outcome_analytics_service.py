"""Synthetic read-only analytics tests for decision outcomes."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.db import connection
from web.db.migrate import run_schema_migrations
from web.routers.scoring import router as scoring_router
from web.services.outcome_analytics_service import OutcomeAnalyticsService


@pytest.fixture
def analytics_db(tmp_path, monkeypatch):
    db_path = tmp_path / "money_mani.db"
    monkeypatch.setattr(connection, "DB_PATH", db_path)
    connection.init_db()
    run_schema_migrations()
    return db_path


def _insert_event(
    *,
    ticker: str,
    market: str,
    action: str,
    score: float,
    scan_date: str = "2026-03-10",
    recommendation: str = "EXECUTE",
    execution_state: str = "EXECUTED",
) -> int:
    with connection.get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO decision_events
              (ticker, ticker_name, market, signal_action, recommendation,
               execution_state, scan_date, composite_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ticker, ticker, market, action, recommendation,
             execution_state, scan_date, score),
        )
        return int(cursor.lastrowid)


def _insert_outcome(
    event_id: int,
    *,
    status: str = "evaluated",
    net: float | None = 1.0,
    raw: float | None = 1.2,
    excess: float | None = 0.7,
    benchmark: float | None = 0.5,
    horizon: int = 5,
) -> None:
    with connection.get_db() as db:
        db.execute(
            """
            INSERT INTO decision_outcomes
              (decision_event_id, horizon_days, status, raw_return_pct,
               benchmark_return_pct, excess_return_pct, net_return_pct,
               entry_date, exit_date, label_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, horizon, status, raw, benchmark, excess, net,
             "2026-03-10", "2026-03-17", "synthetic-test"),
        )


def _seed_rows() -> None:
    # Primary score values use the production 0..1 representation.  The
    # bucket boundaries are [0,.40), [.40,.65), [.65,.80), [.80,1].
    rows = [
        ("US_POS", "US", "BUY", 0.35, 2.0, 1.0, 0.8),
        ("US_NEG", "US", "BUY", 0.45, -1.0, -1.5, 0.5),
        ("KR_POS", "KRX", "SELL", 0.65, 3.0, 2.0, -1.0),
        ("KR_NEG", "KRX", "SELL", 0.85, -2.0, -2.5, -0.5),
    ]
    for ticker, market, action, score, net, excess, benchmark in rows:
        event_id = _insert_event(
            ticker=ticker, market=market, action=action, score=score,
        )
        _insert_outcome(
            event_id, net=net, raw=net + 0.2, excess=excess,
            benchmark=benchmark,
        )

    pending_id = _insert_event(
        ticker="US_PENDING", market="US", action="BUY", score=0.55,
    )
    _insert_outcome(pending_id, status="pending", net=None, raw=None,
                    excess=None, benchmark=None)
    invalid_id = _insert_event(
        ticker="KR_INVALID", market="KRX", action="BUY", score=0.95,
    )
    _insert_outcome(invalid_id, status="invalid", net=None, raw=None,
                    excess=None, benchmark=None)


def test_summary_uses_evaluated_rows_and_exposes_health(analytics_db):
    _seed_rows()
    service = OutcomeAnalyticsService(today="2026-03-15")

    result = service.get_summary(horizon=5, days=30)
    summary = result["summary"]

    assert summary["n"] == 4
    assert summary["evaluated_count"] == 4
    assert summary["win_count"] == 2
    assert summary["loss_count"] == 2
    assert summary["win_rate_pct"] == pytest.approx(50.0)
    assert summary["avg_net_return_pct"] == pytest.approx(0.5)
    assert summary["avg_excess_return_pct"] == pytest.approx(-0.25)
    assert summary["reliable"] is False
    assert summary["pending_count"] == 1
    assert summary["invalid_count"] == 1
    assert summary["status_counts"]["total"] == 6
    assert summary["coverage_pct"] == pytest.approx(4 / 6 * 100)
    assert {item["market"] for item in result["market_action_breakdown"]} == {"KRX", "US"}

    krx = service.get_summary(horizon_days=5, days=30, market="krx")
    assert krx["summary"]["n"] == 2
    assert krx["summary"]["status_counts"]["total"] == 3
    assert all(item["market"] == "KRX" for item in krx["market_action_breakdown"])


def test_calibration_buckets_and_filter(analytics_db):
    _seed_rows()
    service = OutcomeAnalyticsService(today="2026-03-15")

    result = service.get_calibration(horizon=5, days=30)
    buckets = {item["bucket"]: item for item in result["buckets"]}
    assert list(buckets) == ["0-40", "40-65", "65-80", "80-100"]
    assert buckets["0-40"]["n"] == 1
    assert buckets["40-65"]["n"] == 1
    assert buckets["65-80"]["n"] == 1
    assert buckets["80-100"]["n"] == 1
    assert buckets["0-40"]["win_rate_pct"] == pytest.approx(100.0)
    # Pending/invalid rows remain visible in health counts but do not enter
    # evaluated return statistics.
    assert result["pending_count"] == 1
    assert result["invalid_count"] == 1
    assert result["unscored_count"] == 0

    sell = service.get_calibration(horizon=5, days=30, signal_action="sell")
    assert sum(item["n"] for item in sell["buckets"]) == 2
    assert sell["filters"]["signal_action"] == "SELL"


def test_days_cutoff_and_invalid_filter_are_deterministic(analytics_db):
    event_id = _insert_event(
        ticker="OLD", market="US", action="BUY", score=0.5,
        scan_date="2026-02-01",
    )
    _insert_outcome(event_id)
    service = OutcomeAnalyticsService(today="2026-03-15")

    assert service.get_summary(horizon=5, days=10)["summary"]["n"] == 0
    with pytest.raises(ValueError):
        service.get_summary(horizon=3, days=10)


def test_zero_rows_and_unlabelled_capture_are_explicit(analytics_db):
    service = OutcomeAnalyticsService(today="2026-03-15")
    empty = service.get_summary(horizon=1, days=30)
    assert empty["summary"]["n"] == 0
    assert empty["summary"]["reliable"] is False
    assert empty["summary"]["status_counts"]["total"] == 0
    assert empty["summary"]["coverage_pct"] == 0.0
    assert all(bucket["reliable"] is False for bucket in
               service.get_calibration(horizon=1, days=30)["buckets"])

    _insert_event(
        ticker="UNLABELLED", market="US", action="BUY", score=0.55,
    )
    unlabelled = service.get_summary(horizon=1, days=30)
    assert unlabelled["summary"]["n"] == 0
    assert unlabelled["summary"]["unlabelled_count"] == 1
    assert unlabelled["summary"]["coverage_pct"] == 0.0


def test_outcome_api_accepts_http_integer_horizon(analytics_db):
    app = FastAPI()
    app.include_router(scoring_router)
    client = TestClient(app)

    response = client.get("/api/scoring/outcomes/summary", params={"horizon": "5", "days": 30})
    assert response.status_code == 200
    assert response.json()["filters"]["horizon_days"] == 5

    invalid = client.get("/api/scoring/outcomes/summary", params={"horizon": "3"})
    assert invalid.status_code == 422
