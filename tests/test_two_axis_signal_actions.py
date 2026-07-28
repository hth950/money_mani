"""Contract tests for the opportunity/risk signal dashboard."""

from __future__ import annotations

import json

import pytest

from web.db import connection
from web.services.signal_service import SignalService


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "two-axis.db"
    monkeypatch.setattr(connection, "DB_PATH", db_path)
    connection.init_db()
    return db_path


def _seed(
    ticker: str,
    score: float,
    risk: float | None,
    tier: str,
    *,
    hard_block_reason: str | None = None,
):
    breakdown = {
        "components": {
            "timing": {
                "score": 62,
                "reasons": [f"{ticker} 기술적 진입 타이밍 주의"],
            }
        }
    }
    with connection.get_db() as db:
        db.execute(
            """
            INSERT INTO scoring_results
              (ticker, ticker_name, market, scan_date, composite_score,
               decision, opportunity_decision, risk_score, risk_level,
               risk_breakdown_json, recommendation_tier, hard_block_reason,
               risk_model_version, source)
            VALUES (?, ?, 'US', '2026-07-28', ?, ?, ?, ?, ?, ?, ?, ?, 'v1', 'live')
            """,
            (
                ticker,
                f"Name-{ticker}",
                score,
                "BLOCKED" if tier in {"BUY_CONDITIONAL", "UNAVAILABLE"} else "EXECUTE",
                "EXECUTE" if score >= 0.65 else "WATCH",
                risk,
                "UNKNOWN" if risk is None else "LOW" if risk <= 35 else "MEDIUM",
                json.dumps(breakdown, ensure_ascii=False),
                tier,
                hard_block_reason,
            ),
        )


def test_actions_expose_two_axis_contract_and_upgrade_conditions(isolated_db):
    _seed("READY", 0.69, 30, "BUY_READY")
    _seed("COND", 0.68, 52, "BUY_CONDITIONAL")
    _seed("EARLY", 0.59, 40, "EARLY_WATCH")
    _seed("NOQUOTE", 0.71, None, "UNAVAILABLE", hard_block_reason="시세 없음")

    actions = {item["ticker"]: item for item in SignalService().get_actions()}

    assert actions["READY"]["action"] == "BUY"
    assert actions["COND"]["action"] == "BUY_CONDITIONAL"
    assert actions["COND"]["opportunity_score"] == pytest.approx(68)
    assert actions["COND"]["risk_score"] == pytest.approx(52)
    assert actions["COND"]["risk_breakdown"]["components"]["timing"]["score"] == 62
    assert len(actions["COND"]["risk_snapshot_hash"]) == 64
    assert any("17.0점 이상 감소" in value for value in actions["COND"]["upgrade_conditions"])
    assert actions["EARLY"]["recommendation_tier"] == "EARLY_WATCH"
    assert actions["NOQUOTE"]["hard_block_reason"] == "시세 없음"
    assert actions["NOQUOTE"]["upgrade_conditions"] == ["판단 불가 사유 해소: 시세 없음"]


def test_signal_template_requires_conditional_risk_acknowledgement():
    template = (
        __import__("pathlib").Path(__file__).parents[1]
        / "web" / "templates" / "signals" / "index.html"
    ).read_text(encoding="utf-8")

    assert "조건부 매수 후보" in template
    assert "매수 매력도" in template
    assert "진입 위험도" in template
    assert "위 위험을 이해했습니다" in template
    assert "risk_snapshot_hash" in template
    assert "risk_acknowledged" in template

    paper_template = (
        __import__("pathlib").Path(__file__).parents[1]
        / "web" / "templates" / "paper_trading" / "index.html"
    ).read_text(encoding="utf-8")
    assert "paper-order-risk-ack" in paper_template
    assert "위 위험을 이해했습니다" in paper_template
    assert "function canPreviewCurrentOrder()" in paper_template
    assert "state.order.recommendation_tier = 'UNAVAILABLE'" in paper_template
    assert "조건부 후보가 최신 추천에서 사라졌습니다" in paper_template
    assert "button.disabled = !state.preview" in paper_template
    assert "} finally {\n      syncPreviewButtonState();" in paper_template


def test_legacy_watch_at_55_to_64_is_promoted_to_early_watch(isolated_db):
    with connection.get_db() as db:
        db.execute(
            """
            INSERT INTO scoring_results
              (ticker, ticker_name, market, scan_date, composite_score,
               decision, source)
            VALUES ('LEGACY', 'Legacy', 'US', '2026-07-28', .60,
                    'WATCH', 'live')
            """
        )

    action = SignalService().get_actions()[0]
    assert action["recommendation_tier"] == "EARLY_WATCH"
    assert action["action"] == "WATCH"
    assert len(action["risk_snapshot_hash"]) == 64
    assert set(action["risk_snapshot_hash"]) <= set("0123456789abcdef")


def test_live_recalculation_preserves_stored_volatility_inputs(isolated_db):
    risk_breakdown = {
        "portfolio": {"inputs": {"sector": "Technology"}},
        "volatility": {
            "inputs": {
                "atr_pct": 0.08,
                "volatility_percentile": 0.90,
                "gap_pct": 0.02,
            }
        },
    }
    score_breakdown = {
        "technical": 0.72,
        "fundamental": 0.65,
        "flow": 0.60,
        "intel": 0.55,
        "macro": 0.50,
    }
    with connection.get_db() as db:
        db.execute(
            """
            INSERT INTO scoring_results
              (ticker, ticker_name, market, scan_date, composite_score,
               decision, opportunity_decision, risk_score, risk_breakdown_json,
               score_breakdown_json, recommendation_tier,
               risk_model_version, source)
            VALUES ('VOL', 'Volatility', 'US', '2026-07-28', .68,
                    'EXECUTE', 'EXECUTE', 50, ?, ?, 'BUY_CONDITIONAL',
                    'entry-risk-v1', 'live')
            """,
            (
                json.dumps(risk_breakdown),
                json.dumps(score_breakdown),
            ),
        )

    action = SignalService().get_actions()[0]
    inputs = action["risk_breakdown"]["volatility"]["inputs"]
    assert inputs["atr_pct"] == pytest.approx(0.08)
    assert inputs["volatility_percentile"] == pytest.approx(90.0)
    assert action["risk_breakdown"]["volatility"]["score"] > 45
