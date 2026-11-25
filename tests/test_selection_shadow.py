import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from market_data.selection_shadow_sources import Observation, ShadowSources, _annual_sec_margin, _dart_annual_roe, _kis_rows, _naver_rows, flow_window
from pipeline import selection_shadow as shadow
from web.db import connection
from web.services.decision_label_service import exchange_schedule


NOW = datetime(2026, 11, 25, 0, 0, tzinfo=timezone.utc)
DAY = "2026-11-25"


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "selection.db"
    monkeypatch.setattr(connection, "DB_PATH", path)
    monkeypatch.setattr(shadow, "_now", lambda: NOW)
    connection.init_db()
    return path


def add_decision(ticker, action, market="US", recommendation="SKIP", hard_block=None):
    with connection.get_db() as db:
        db.execute("""
            INSERT INTO decision_events (ticker, market, scan_date, signal_action, recommendation,
                hard_block_reason, composite_score, score_details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 0.55, ?, ?)
        """, (ticker, market, DAY, action, recommendation, hard_block,
              json.dumps({"fundamental": {"profit_margin": .12, "roe": .12}}),
              (NOW - timedelta(minutes=1)).isoformat()))


def bars(market):
    days = exchange_schedule(market).index
    latest = shadow._completed_sessions(market, NOW)[-1]
    days = days[days <= latest][-60:]
    return pd.DataFrame({"Close": range(100, 160), "Volume": [100] * 59 + [200]}, index=days)


def test_first_forward_capture_and_buy_only_cutoff(db_path, monkeypatch):
    class Sources:
        request_count = 0

        def close(self):
            pass

        def sec(self, ticker, observed_at):
            assert ticker == "ZZZ"
            return Observation("available", {"event_count": 0})

        def compare_financial(self, provider, ticker, observed_at, incumbent):
            assert incumbent["value"] == .12
            return {"status": "basis_unverified", "incumbent": incumbent}

        def relative_strength(self, market, features):
            return Observation("available", {"relative_strength": .03})

    add_decision("ZZZ", "BUY")
    add_decision("AAA", None)
    add_decision("AAA", "BUY")
    add_decision("BLOCK", "BUY", hard_block="open_position")
    monkeypatch.setattr(shadow, "ShadowSources", Sources)
    frames = {ticker: bars("US") for ticker in ("ZZZ", "AAA", "BLOCK")}
    assert shadow.capture_scan("2026-11-24", "US", frames) == 0
    assert shadow.capture_scan(DAY, "US", frames, tickers=["GAP"], sectors={"ZZZ": "Technology"}) == 4
    assert shadow.capture_scan(DAY, "US", frames, tickers=["NEW"]) == 0
    monkeypatch.setattr(shadow, "_now", lambda: NOW + timedelta(minutes=5))
    result = shadow.collect_pending({"selection_shadow": {"tickers_per_run": 1}})
    assert result == {"observations": 2, "candidates": 1}
    with connection.get_db() as db:
        snapshots = db.execute("SELECT ticker, eligible FROM selection_shadow_snapshots ORDER BY ticker").fetchall()
        assert [tuple(x) for x in snapshots] == [("AAA", 1), ("BLOCK", 1), ("GAP", 0), ("ZZZ", 1)]
        candidate = db.execute("SELECT * FROM selection_shadow_candidates").fetchone()
        assert candidate["ticker"] == "ZZZ"
        assert candidate["baseline_score"] == .55
        assert candidate["decision_at"] == (NOW + timedelta(minutes=5)).isoformat()
        features = json.loads(candidate["features_json"])
        assert features["relative_volume"] == 2
        assert features["sector_relative_strength"] == .03
        assert features["us_news_novelty"] is None
        assert db.execute("SELECT COUNT(*) FROM selection_shadow_provider_observations WHERE collected_at > ?", (candidate["decision_at"],)).fetchone()[0] == 0


def test_novelty_dedup_and_zero_coverage(db_path):
    with connection.get_db() as db:
        for days, ticker, title in ((2, "OLD", "Earnings reaction"), (0, "OLD", "EARNINGS reaction!"),
                                    (0, "FRESH", "New Filing"), (-1, "FRESH", "Future filing")):
            timestamp = (NOW - timedelta(days=days, hours=1)).isoformat()
            scan = db.execute("""INSERT INTO market_intel_scans (scan_time, scan_type, status, created_at)
                               VALUES ('00:00', 'us_post_market', 'success', ?)""", (timestamp,)).lastrowid
            db.execute("""INSERT INTO market_intel_issues (scan_id, title, confidence, affected_tickers_json, created_at)
                          VALUES (?, ?, .7, ?, ?)""", (scan, title,
                         json.dumps([{"ticker": ticker, "market": "US", "direction": "up"}]), timestamp))
        result = shadow._news(db, NOW, {"OLD", "FRESH", "NONE"})
    assert result["OLD"][0] == 0
    assert result["FRESH"][0] == .7
    assert result["NONE"][0] == 0
    assert result["NONE"][1]["status"] == "available"


def test_flow_requires_exact_sessions_and_preserves_native_vs_proxy():
    dates = [f"2026-11-{day:02}" for day in range(16, 21)]
    kis = _kis_rows({"output2": [{"stck_bsop_date": day.replace("-", ""), "stck_clpr": "10000",
                                 "acml_vol": "10000", "acml_tr_pbmn": "100000000", "frgn_ntby_tr_pbmn": "2",
                                 "orgn_ntby_tr_pbmn": "-1"} for day in dates]})
    naver = _naver_rows({"result": {"items": [{"localTradedAt": day, "krx": {
        "regularMarketClosingPrice": "10,000", "closingPrice": "9,876", "tradingVolume": "10,000",
        "foreignNetVolume": "200", "organizationNetVolume": "-50",
    }} for day in dates]}})
    assert flow_window(kis, dates, method="native", request_count=1).details["krx_flow_5d"] == .01
    assert flow_window(naver, dates, method="proxy", request_count=1).details["krx_flow_5d"] == .015
    assert flow_window(naver, dates, method="proxy", request_count=1).details["krx_flow_positive_share"] == 1
    naver.pop(dates[1])
    assert flow_window(naver, dates, method="proxy", request_count=1).status == "missing"


def test_financial_ratios_require_same_accession_period_and_currency():
    annual = {"accn": "filing", "val": 100, "end": "2025-12-31", "start": "2025-01-01"}
    facts = {"us-gaap": {"NetIncomeLoss": {"units": {"USD": [annual]}}, "Revenues": {"units": {"USD": [
        {**annual, "val": 3000, "accn": "future"}, {**annual, "val": 200, "start": "2025-10-01"},
        {**annual, "val": 500},
    ]}}}}
    assert _annual_sec_margin(facts, "filing", "2025-12-31")["value"] == .2
    report = {"rcept_no": "file", "currency": "KRW"}
    rows = [{**report, "account_id": "ifrs-full_Equity", "thstrm_amount": "1,000"},
            {**report, "account_id": "ifrs-full_ProfitLoss", "thstrm_amount": "-100"}]
    assert _dart_annual_roe(rows, "file")["value"] == -.1
    assert _dart_annual_roe([{**rows[0], "currency": "USD"}, rows[1]], "file") is None


def test_krx_relative_strength_uses_verified_board_and_only_requested_closes(monkeypatch):
    from types import SimpleNamespace

    sources = ShadowSources()
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if url.endswith("/basic"):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
                "itemCode": "247540", "stockExchangeType": {"code": "KQ"}})
        assert kwargs["params"]["symbol"] == "KOSDAQ"
        return SimpleNamespace(raise_for_status=lambda: None, content=(
            '<root><chartdata symbol="KOSDAQ"><item data="20261117|0|0|0|100"/>'
            '<item data="20261124|0|0|0|105"/><item data="20261125|0|0|0|300"/></chartdata></root>'
        ).encode("euc-kr"))

    monkeypatch.setattr(sources.http, "get", get)
    monkeypatch.setattr(sources, "_benchmark_return", lambda *args: (None, 1))
    features = {"symbol": "247540", "return_sessions": ["2026-11-17", "2026-11-24"], "stock_return_5d": .08}
    result = sources.relative_strength("KRX", features)
    assert result.details["relative_strength"] == pytest.approx(.03)
    assert result.details["benchmark"] == "^KQ11"
    assert result.details["source"] == "naver_index_chart"
    cached = sources.relative_strength("KRX", {**features, "symbol": "247540.KQ"})
    assert cached.details["relative_strength"] == result.details["relative_strength"]
    assert len(calls) == 2
    misaligned = {**features, "return_sessions": ["2026-11-18", "2026-11-24"]}
    assert sources.relative_strength("KRX", misaligned).status == "missing"
    assert sources.relative_strength("KRX", {**features, "symbol": "123456"}).details["reason"] == "unverified_krx_board"
