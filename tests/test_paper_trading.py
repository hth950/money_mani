"""Regression tests for the isolated manual paper-trading ledger."""

from __future__ import annotations

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.db import connection
from web.models.schemas import PaperOrderPreviewRequest
from web.routers import paper_trading as paper_router
from web.services.paper_quote_service import PaperQuoteUnavailable
from web.services.paper_trading_service import (
    PaperTradingConflict,
    PaperTradingService,
)
from web.services.signal_service import SignalService


class FakeQuotes:
    def __init__(self, prices=None, failures=None):
        self.prices = {key: list(values) for key, values in (prices or {}).items()}
        self.failures = set(failures or [])

    def get_quote(self, market, ticker):
        if ticker in self.failures:
            raise PaperQuoteUnavailable(f"no quote: {ticker}")
        values = self.prices[(market, ticker)]
        price = values.pop(0) if len(values) > 1 else values[0]
        return {
            "price": float(price),
            "source": "fake_quote",
            "price_at": "2026-07-14T01:00:00+00:00",
            "is_delayed": False,
        }

    def get_ohlcv(self, market, ticker, lookback_days=180):
        if ticker in self.failures:
            raise RuntimeError(f"no bars: {ticker}")
        close = self.prices[(market, ticker)][-1]
        index = pd.date_range("2026-05-01", periods=60, freq="B")
        return pd.DataFrame(
            {
                "Open": [close] * 60,
                "High": [close + 1] * 60,
                "Low": [close - 1] * 60,
                "Close": [close] * 60,
                "Volume": [1000] * 60,
            },
            index=index,
        )


class FakeScorer:
    def score(self, ticker, market, ohlcv_df=None):
        return {
            "composite_score": 0.72,
            "decision": "EXECUTE",
            "scores": {
                "technical": 0.8,
                "fundamental": 0.6,
                "flow": 0.7,
                "intel": 0.5,
                "macro": 0.4,
            },
            "details": {},
            "weights": {},
        }


class CapturingExitScorer:
    def __init__(self):
        self.last_close = None

    def evaluate(self, **kwargs):
        self.last_close = float(kwargs["ohlcv_df"]["Close"].iloc[-1])
        return {
            "exit_score": 0.2,
            "decision": "SELL_EXECUTE",
            "reason": "test",
            "scores": {},
            "details": {},
        }


@pytest.fixture
def paper_db(tmp_path, monkeypatch):
    db_path = tmp_path / "paper.db"
    monkeypatch.setattr(connection, "DB_PATH", db_path)
    connection.init_db()
    return db_path


def seed_recommendation(
    ticker="AAA", market="KRX", decision="EXECUTE", scan_date="2026-07-14",
    composite_score=0.72,
):
    with connection.get_db() as db:
        db.execute(
            """
            INSERT INTO scoring_results
              (ticker, ticker_name, market, scan_date, technical_score,
               fundamental_score, flow_score, intel_score, macro_score,
               composite_score, decision, source)
            VALUES (?, ?, ?, ?, .8, .6, .7, .5, .4, ?, ?, 'live')
            """,
            (
                ticker, f"Name-{ticker}", market, scan_date,
                composite_score, decision,
            ),
        )


def order(side, ticker="AAA", market="KRX", quantity=1, key="unique-key-0001"):
    return {
        "side": side,
        "market": market,
        "ticker": ticker,
        "quantity": quantity,
        "idempotency_key": key,
    }


def acknowledged_additional_buy(
    ticker="AAA", market="KRX", quantity=1, key="additional-buy-001"
):
    action = next(
        item for item in SignalService().get_actions()
        if item["ticker"] == ticker and item["market"] == market
    )
    return {
        **order("BUY", ticker=ticker, market=market, quantity=quantity, key=key),
        "risk_acknowledged": True,
        "risk_snapshot_hash": action["risk_snapshot_hash"],
    }


def previewed_conditional_order(service, payload):
    """Refresh the conditional hash with the same quote snapshot as the UI."""
    preview = service.preview_order(payload)
    snapshot = preview["recommendation_snapshot"]
    return {
        **payload,
        "risk_acknowledged": True,
        "risk_snapshot_hash": snapshot["risk_snapshot_hash"],
        "preview_price": preview["estimated_price"],
    }


def seed_conditional_recommendation(
    ticker="COND", market="US", scan_date="2026-07-14", risk_score=52.0,
):
    breakdown = (
        '{"portfolio":{"score":70,"label":"포트폴리오 집중",'
        '"reasons":["동일 섹터 집중도가 높습니다."]}}'
    )
    with connection.get_db() as db:
        db.execute(
            """
            INSERT INTO scoring_results
              (ticker, ticker_name, market, scan_date, technical_score,
               fundamental_score, flow_score, intel_score, macro_score,
               composite_score, decision, opportunity_decision, risk_score,
               risk_level, risk_breakdown_json, recommendation_tier,
               risk_model_version, source)
            VALUES (?, ?, ?, ?, .7, .6, .5, .6, .5, .68, 'BLOCKED',
                    'EXECUTE', ?, NULL, ?, 'BUY_CONDITIONAL', 'v1', 'live')
            """,
            (ticker, f"Name-{ticker}", market, scan_date, risk_score, breakdown),
        )


def test_first_and_additional_buy_use_weighted_fill_and_fee_cost(paper_db):
    seed_recommendation()
    service = PaperTradingService(
        quote_service=FakeQuotes({("KRX", "AAA"): [100, 120]})
    )

    first = service.place_order(order("BUY", quantity=10, key="buy-first-001"))
    second_payload = acknowledged_additional_buy(
        quantity=10, key="buy-second-01"
    )
    second = service.place_order(
        previewed_conditional_order(service, second_payload)
    )

    assert first["position"]["quantity"] == 10
    assert first["position"]["remaining_cost"] == pytest.approx(1000.15)
    assert first["position"]["unrealized_pnl"] == pytest.approx(-2.10)
    assert first["position"]["paid_fees"] == pytest.approx(0.15)
    assert first["position"]["estimated_exit_fee"] == pytest.approx(1.95)
    assert first["position"]["exit_decision"] in {
        "HOLD", "SELL_WATCH", "SELL_EXECUTE"
    }
    assert second["position"]["quantity"] == 20
    assert second["position"]["avg_price"] == pytest.approx(110.0)
    assert second["position"]["remaining_cost"] == pytest.approx(2200.33)
    assert second["position"]["total_buy_fees"] == pytest.approx(0.33)
    with connection.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 2


def test_partial_full_sell_and_rebuy_create_a_new_cycle(paper_db):
    seed_recommendation()
    service = PaperTradingService(
        quote_service=FakeQuotes(
            {("KRX", "AAA"): [100, 120, 120, 130, 130, 90]}
        )
    )
    service.place_order(order("BUY", quantity=10, key="cycle-buy-0001"))
    additional_payload = acknowledged_additional_buy(
        quantity=10, key="cycle-buy-0002"
    )
    bought = service.place_order(
        previewed_conditional_order(service, additional_payload)
    )
    position_id = bought["position"]["id"]

    partial = service.place_order(order("SELL", quantity=5, key="partial-sell-01"))
    assert partial["trade"]["allocated_cost"] == pytest.approx(550.0825)
    assert partial["trade"]["fee"] == pytest.approx(1.2675)
    assert partial["trade"]["realized_pnl"] == pytest.approx(98.65)
    assert partial["position"]["quantity"] == 15
    assert partial["position"]["remaining_cost"] == pytest.approx(1650.2475)

    closed = service.place_order(order("SELL", quantity=15, key="full-sell-00001"))
    assert closed["position"]["status"] == "closed"
    assert closed["position"]["quantity"] == 0
    assert closed["position"]["remaining_cost"] == 0
    assert closed["trade"]["realized_pnl"] == pytest.approx(295.95)

    rebought = service.place_order(order("BUY", quantity=2, key="rebuy-new-00001"))
    assert rebought["position"]["status"] == "open"
    assert rebought["position"]["id"] != position_id
    assert rebought["position"]["quantity"] == 2


def test_oversell_and_short_sale_are_rejected(paper_db):
    seed_recommendation()
    service = PaperTradingService(
        quote_service=FakeQuotes({("KRX", "AAA"): [100]})
    )
    with pytest.raises(PaperTradingConflict):
        service.place_order(order("SELL", quantity=1, key="no-short-000001"))
    service.place_order(order("BUY", quantity=2, key="small-buy-00001"))
    with pytest.raises(PaperTradingConflict):
        service.place_order(order("SELL", quantity=3, key="over-sell-00001"))
    with connection.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 1


def test_idempotency_replays_once_and_rejects_key_reuse(paper_db):
    seed_recommendation()
    service = PaperTradingService(
        quote_service=FakeQuotes({("KRX", "AAA"): [100]})
    )
    payload = {
        **order("BUY", quantity=3, key="idempotent-key-01"),
        "estimated_price": 99,
    }
    first = service.place_order(payload)
    replay = service.place_order(payload)

    assert replay["idempotent_replay"] is True
    assert first["quote_changed"] is True
    assert replay["trade"]["id"] == first["trade"]["id"]
    assert replay["position"]["quantity"] == 3
    with pytest.raises(PaperTradingConflict):
        service.place_order({**payload, "quantity": 4})
    with connection.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 1


def test_conditional_buy_requires_current_acknowledged_risk_snapshot(paper_db):
    seed_conditional_recommendation()
    service = PaperTradingService(
        quote_service=FakeQuotes({("US", "COND"): [100]})
    )
    action = SignalService().get_actions()[0]
    base = {"side": "BUY", "market": "US", "ticker": "COND", "quantity": 2}

    with pytest.raises(PaperTradingConflict, match="위험 내용을 확인"):
        service.preview_order(base)
    with pytest.raises(PaperTradingConflict, match="위험 정보가 변경"):
        service.preview_order({
            **base,
            "risk_acknowledged": True,
            "risk_snapshot_hash": "0" * 64,
        })

    acknowledged = {
        **base,
        "risk_acknowledged": True,
        "risk_snapshot_hash": action["risk_snapshot_hash"],
        "risk_acknowledged_reasons": ["동일 섹터 집중도가 높습니다."],
    }
    preview = service.preview_order(acknowledged)
    assert preview["recommendation_snapshot"]["recommendation_tier"] == "BUY_CONDITIONAL"
    result = service.place_order({
        **acknowledged,
        "idempotency_key": "conditional-buy-001",
        "preview_price": preview["estimated_price"],
    })
    assert result["trade"]["risk_score"] == pytest.approx(52.0)
    assert result["trade"]["risk_snapshot_hash"] == action["risk_snapshot_hash"]
    assert result["trade"]["risk_acknowledged_at"]
    assert result["trade"]["risk_acknowledgement_version"] == "v1"
    assert "동일 섹터 집중도가 높습니다." in result["trade"]["risk_snapshot_json"]


def test_buy_ready_does_not_record_conditional_acknowledgement(paper_db):
    seed_recommendation()
    service = PaperTradingService(
        quote_service=FakeQuotes({("KRX", "AAA"): [100]})
    )

    result = service.place_order({
        **order("BUY", key="ready-with-fake-ack"),
        "risk_acknowledged": True,
        "risk_snapshot_hash": "a" * 64,
    })

    assert result["trade"]["risk_acknowledged_at"] is None
    assert result["trade"]["risk_acknowledgement_version"] is None
    snapshot = result["trade"]["risk_snapshot"]
    assert snapshot["acknowledged_reasons"] == []


def test_preview_recalculates_current_model_volatility_with_latest_quote(paper_db):
    seed_recommendation()
    service = PaperTradingService(
        quote_service=FakeQuotes({("KRX", "AAA"): [100]})
    )

    preview = service.preview_order({
        "side": "BUY", "market": "KRX", "ticker": "AAA", "quantity": 1
    })

    snapshot = preview["recommendation_snapshot"]
    assert snapshot["risk_model_version"] == "entry-risk-v1"
    volatility = snapshot["risk_breakdown"]["volatility"]["inputs"]
    assert volatility["atr_pct"] == pytest.approx(0.02)
    assert volatility["gap_pct"] == pytest.approx(0.0)


def test_conditional_buy_rejects_changed_snapshot_without_mutation(paper_db):
    seed_conditional_recommendation()
    service = PaperTradingService(
        quote_service=FakeQuotes({("US", "COND"): [100]})
    )
    old_hash = SignalService().get_actions()[0]["risk_snapshot_hash"]
    with connection.get_db() as db:
        db.execute(
            "UPDATE scoring_results SET risk_score=61, risk_level='HIGH' WHERE ticker='COND'"
        )
    with pytest.raises(PaperTradingConflict, match="위험 정보가 변경"):
        service.place_order({
            "side": "BUY", "market": "US", "ticker": "COND", "quantity": 1,
            "risk_acknowledged": True, "risk_snapshot_hash": old_hash,
            "idempotency_key": "conditional-stale-01", "preview_price": 100,
        })
    with connection.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 0


def test_hard_block_reason_always_overrides_buy_tier(paper_db):
    seed_recommendation()
    with connection.get_db() as db:
        db.execute(
            """UPDATE scoring_results
               SET recommendation_tier='BUY_READY',
                   hard_block_reason='시세 없음'
               WHERE ticker='AAA'"""
        )
    service = PaperTradingService(
        quote_service=FakeQuotes({("KRX", "AAA"): [100]})
    )

    with pytest.raises(PaperTradingConflict, match="시세 없음"):
        service.preview_order({
            "side": "BUY", "market": "KRX", "ticker": "AAA", "quantity": 1
        })
    with connection.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 0


def test_portfolio_change_invalidates_conditional_snapshot_hash(
    paper_db, monkeypatch
):
    seed_conditional_recommendation()
    service = PaperTradingService(
        quote_service=FakeQuotes({("US", "COND"): [100]})
    )
    old_hash = SignalService().get_actions()[0]["risk_snapshot_hash"]
    with connection.get_db() as db:
        db.execute(
            """INSERT INTO paper_positions
               (market, ticker, ticker_name, status, quantity, avg_price,
                remaining_cost)
               VALUES ('US', 'HELD', 'Held', 'open', 1, 10, 10)"""
        )

    with pytest.raises(PaperTradingConflict, match="위험 정보가 변경"):
        service.preview_order({
            "side": "BUY", "market": "US", "ticker": "COND", "quantity": 1,
            "risk_acknowledged": True, "risk_snapshot_hash": old_hash,
        })


def test_stale_production_recommendation_is_rejected(paper_db, monkeypatch):
    seed_recommendation(scan_date="2020-01-02")
    monkeypatch.setenv("MONEY_MANI_ENV", "production")
    service = PaperTradingService(
        quote_service=FakeQuotes({("KRX", "AAA"): [100]})
    )

    with pytest.raises(PaperTradingConflict, match="만료"):
        service.preview_order({
            "side": "BUY", "market": "KRX", "ticker": "AAA", "quantity": 1
        })


def test_buy_expires_but_held_stock_can_always_be_sold(paper_db):
    seed_recommendation()
    service = PaperTradingService(
        quote_service=FakeQuotes({("KRX", "AAA"): [100, 105]})
    )
    service.place_order(order("BUY", quantity=2, key="active-buy-00001"))
    seed_recommendation(
        decision="WATCH", scan_date="2026-07-15", composite_score=0.50
    )

    with pytest.raises(PaperTradingConflict):
        service.preview_order({"side": "BUY", "market": "KRX", "ticker": "AAA", "quantity": 1})
    sold = service.place_order(order("SELL", quantity=1, key="sell-after-expiry"))
    assert sold["position"]["quantity"] == 1


def test_missing_latest_recommendation_preserves_name_scores_and_refresh(paper_db):
    seed_recommendation()
    quotes = FakeQuotes({("KRX", "AAA"): [100, 105], ("KRX", "OTHER"): [10]})
    setup = PaperTradingService(quote_service=quotes)
    bought = setup.place_order(order("BUY", quantity=2, key="vanish-buy-00001"))
    seed_recommendation("OTHER", scan_date="2026-07-15")

    refresh_service = PaperTradingService(
        quote_service=quotes, scorer=FakeScorer(), exit_scorer=CapturingExitScorer()
    )
    refreshed = refresh_service.refresh_open_positions(refresh_source="scheduled")
    assert refreshed == {"total": 1, "succeeded": 1, "failed": 0, "errors": []}

    sold = refresh_service.place_order(
        order("SELL", quantity=1, key="vanish-sell-0001")
    )
    assert sold["trade"]["ticker_name"] == "Name-AAA"
    assert sold["trade"]["composite_score"] == pytest.approx(0.72)
    assert sold["position"]["composite_score"] == pytest.approx(0.72)
    assert sold["position"]["scores"]["technical"] == pytest.approx(0.8)
    assert sold["position"]["id"] == bought["position"]["id"]


def test_us_zero_fee_and_krx_unrealized_pnl_are_net_of_exit_fee(paper_db):
    seed_recommendation("USA", market="US")
    service = PaperTradingService(
        quote_service=FakeQuotes({("US", "USA"): [50]})
    )
    result = service.place_order(
        order("BUY", ticker="USA", market="US", quantity=2, key="us-zero-fee-001")
    )
    assert result["trade"]["fee"] == 0
    assert result["position"]["remaining_cost"] == 100
    assert result["position"]["estimated_sell_fee"] == 0
    assert result["position"]["unrealized_pnl"] == 0
    overview = service.get_overview()
    assert overview["summaries"]["US"]["paid_fees"] == 0
    assert overview["summaries"]["US"]["estimated_sell_fees"] == 0


def test_quote_failure_never_mutates_the_ledger(paper_db):
    seed_recommendation()
    service = PaperTradingService(
        quote_service=FakeQuotes({("KRX", "AAA"): [100]}, failures={"AAA"})
    )
    with pytest.raises(PaperQuoteUnavailable):
        service.place_order(order("BUY", key="quote-fail-0001"))
    with connection.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM paper_position_marks").fetchone()[0] == 0


def test_sell_recommendations_sort_zero_hard_stop_first(paper_db):
    seed_recommendation("ZERO")
    seed_recommendation("WEAK")
    service = PaperTradingService(
        quote_service=FakeQuotes({("KRX", "ZERO"): [100], ("KRX", "WEAK"): [100]})
    )
    zero = service.place_order(order("BUY", "ZERO", key="sort-zero-buy01"))
    weak = service.place_order(order("BUY", "WEAK", key="sort-weak-buy01"))
    with connection.get_db() as db:
        for position_id, score in (
            (zero["position"]["id"], 0.0),
            (weak["position"]["id"], 0.2),
        ):
            db.execute(
                """
                INSERT INTO paper_position_marks
                  (position_id, current_price, market_value, unrealized_pnl,
                   exit_score, exit_decision, price_source, price_at,
                   refresh_source)
                VALUES (?, 100, 100, 0, ?, 'SELL_EXECUTE',
                        'test', '2026-07-14', 'manual')
                """,
                (position_id, score),
            )
    recommendations = service.get_overview()["sell_recommendations"]
    assert [item["ticker"] for item in recommendations] == ["ZERO", "WEAK"]


def test_refresh_is_partial_preserves_last_mark_and_uses_live_quote_for_exit(paper_db):
    seed_recommendation("AAA")
    seed_recommendation("BBB")
    quotes = FakeQuotes({("KRX", "AAA"): [100], ("KRX", "BBB"): [200]})
    setup = PaperTradingService(quote_service=quotes)
    a = setup.place_order(order("BUY", "AAA", quantity=1, key="refresh-buy-aaa"))
    b = setup.place_order(order("BUY", "BBB", quantity=1, key="refresh-buy-bbb"))
    with connection.get_db() as db:
        before_a = db.execute(
            "SELECT COUNT(*) FROM paper_position_marks WHERE position_id=?", (a["position"]["id"],)
        ).fetchone()[0]
        before_b = db.execute(
            "SELECT COUNT(*) FROM paper_position_marks WHERE position_id=?", (b["position"]["id"],)
        ).fetchone()[0]

    quotes.prices[("KRX", "AAA")] = [80]
    quotes.failures.add("BBB")
    exit_scorer = CapturingExitScorer()
    service = PaperTradingService(
        quote_service=quotes, scorer=FakeScorer(), exit_scorer=exit_scorer
    )
    result = service.refresh_open_positions(refresh_source="manual")

    assert result["total"] == 2
    assert result["succeeded"] == 1
    assert result["failed"] == 1
    assert result["errors"][0]["ticker"] == "BBB"
    assert exit_scorer.last_close == 80
    with connection.get_db() as db:
        after_a = db.execute(
            "SELECT COUNT(*) FROM paper_position_marks WHERE position_id=?", (a["position"]["id"],)
        ).fetchone()[0]
        after_b = db.execute(
            "SELECT COUNT(*) FROM paper_position_marks WHERE position_id=?", (b["position"]["id"],)
        ).fetchone()[0]
        b_error = db.execute(
            "SELECT last_refresh_error FROM paper_positions WHERE id=?", (b["position"]["id"],)
        ).fetchone()[0]
    assert after_a == before_a + 1
    assert after_b == before_b
    assert "PaperQuoteUnavailable" in b_error
    overview = service.get_overview()
    assert overview["sell_recommendations"][0]["ticker"] == "AAA"


def test_schemas_are_idempotent_isolated_and_validate_strict_quantity(paper_db):
    connection.init_db()
    with connection.get_db() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        names = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'paper_%'"
            )
        }
        assert names == {"paper_positions", "paper_trades", "paper_position_marks"}
        db.execute(
            """INSERT INTO positions
               (strategy_name, ticker, entry_date, entry_price)
               VALUES ('automatic', 'AUTO', '2026-07-14', 10)"""
        )
        db.execute(
            """INSERT INTO portfolio_snapshots
               (ticker, name, market, quantity, avg_price, current_price)
               VALUES ('LIVE', 'Live', 'KRX', 3, 10, 11)"""
        )
        legacy_counts = (
            db.execute("SELECT COUNT(*) FROM positions").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM portfolio_snapshots").fetchone()[0],
        )
        assert db.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 0
        index_rows = db.execute("PRAGMA index_list(paper_positions)").fetchall()
        indexes = {row["name"]: row for row in index_rows}
        assert indexes["idx_paper_positions_unique_open"]["unique"] == 1
        assert indexes["idx_paper_positions_unique_open"]["partial"] == 1
        trade_fks = {
            (row["table"], row["on_delete"])
            for row in db.execute("PRAGMA foreign_key_list(paper_trades)")
        }
        mark_fks = {
            (row["table"], row["on_delete"])
            for row in db.execute("PRAGMA foreign_key_list(paper_position_marks)")
        }
        assert ("paper_positions", "RESTRICT") in trade_fks
        assert ("paper_positions", "CASCADE") in mark_fks

    seed_recommendation()
    PaperTradingService(
        quote_service=FakeQuotes({("KRX", "AAA"): [100]})
    ).place_order(order("BUY", key="isolation-buy-001"))
    with connection.get_db() as db:
        assert legacy_counts == (
            db.execute("SELECT COUNT(*) FROM positions").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM portfolio_snapshots").fetchone()[0],
        )


@pytest.mark.parametrize("quantity", [0, -1, 1.5, True])
def test_quantity_validation_rejects_non_positive_fractional_and_bool(quantity):
    with pytest.raises(Exception):
        PaperOrderPreviewRequest(
            side="BUY", market="KRX", ticker="AAA", quantity=quantity
        )


def test_api_returns_409_503_and_paginates(paper_db, monkeypatch):
    seed_recommendation()
    app = FastAPI()
    app.include_router(paper_router.router)
    service = PaperTradingService(
        quote_service=FakeQuotes({("KRX", "AAA"): [100]})
    )
    monkeypatch.setattr(paper_router, "service", service)
    client = TestClient(app)

    invalid = client.post(
        "/api/paper-trading/orders/preview",
        json={"side": "BUY", "market": "KRX", "ticker": "AAA", "quantity": True},
    )
    assert invalid.status_code == 422
    placed = client.post(
        "/api/paper-trading/orders",
        json={
            **order("BUY", quantity=1, key="api-order-key-01"),
            "preview_price": 100,
        },
    )
    assert placed.status_code == 200
    trades = client.get("/api/paper-trading/trades?limit=1&offset=0")
    assert trades.status_code == 200
    assert trades.json()["total"] == 1

    seed_recommendation(
        decision="WATCH", scan_date="2026-07-15", composite_score=0.50
    )
    stale = client.post(
        "/api/paper-trading/orders/preview",
        json={"side": "BUY", "market": "KRX", "ticker": "AAA", "quantity": 1},
    )
    assert stale.status_code == 409

    seed_recommendation("CCC", decision="EXECUTE", scan_date="2026-07-15")
    monkeypatch.setattr(
        paper_router,
        "service",
        PaperTradingService(
            quote_service=FakeQuotes({("KRX", "CCC"): [100]}, failures={"CCC"})
        ),
    )
    unavailable = client.post(
        "/api/paper-trading/orders/preview",
        json={"side": "BUY", "market": "KRX", "ticker": "CCC", "quantity": 1},
    )
    assert unavailable.status_code == 503

    missing_preview = client.post(
        "/api/paper-trading/orders",
        json=order("BUY", ticker="CCC", quantity=1, key="no-preview-key-01"),
    )
    assert missing_preview.status_code == 422


def test_job_endpoint_exposes_partial_result(paper_db, monkeypatch):
    app = FastAPI()
    app.include_router(paper_router.router)
    monkeypatch.setattr(
        paper_router.job_service,
        "get_job",
        lambda job_id: {
            "id": job_id,
            "job_name": "paper_trading_refresh",
            "status": "success",
            "result_summary": (
                "{'total': 2, 'succeeded': 1, 'failed': 1, "
                "'errors': [{'ticker': 'BBB'}], 'errors_truncated': 0}"
            ),
        },
    )
    response = TestClient(app).get("/api/paper-trading/jobs/77")
    assert response.status_code == 200
    payload = response.json()
    assert payload["job_status"] == "success"
    assert payload["status"] == "partial"
    assert payload["result"]["failed"] == 1


def test_refresh_job_summary_stays_parseable_under_job_service_limit(
    paper_db, monkeypatch,
):
    errors = [
        {"position_id": i, "ticker": f"T{i}", "error": "x" * 500}
        for i in range(10)
    ]
    monkeypatch.setattr(
        paper_router.service,
        "refresh_open_positions",
        lambda refresh_source: {
            "total": 10, "succeeded": 0, "failed": 10, "errors": errors,
        },
    )
    summary = paper_router._run_refresh_job()
    assert summary["failed"] == 10
    assert len(summary["errors"]) == 2
    assert summary["errors_truncated"] == 8
    assert len(str(summary)) < 500
