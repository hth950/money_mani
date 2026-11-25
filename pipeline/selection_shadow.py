"""Freeze forward selection research inputs without changing the live scan."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta

import pandas as pd

from market_data.selection_shadow_sources import KST, UTC, Observation, ShadowSources, number, utc_datetime
from web.db.connection import get_db
from web.services.decision_label_service import exchange_schedule

logger = logging.getLogger(__name__)
SNAPSHOT_TABLE = "selection_shadow_snapshots"
PROVIDER_TABLE = "selection_shadow_provider_observations"
CANDIDATE_TABLE = "selection_shadow_candidates"
PROVIDERS = {
    "KRX": ("kis", "naver", "dart", "market_relative_strength"),
    "US": ("sec", "sector_relative_strength"),
}
SELECTION_SHADOW_DDL = (
    """CREATE TABLE IF NOT EXISTS selection_shadow_snapshots (
        scan_date TEXT NOT NULL, market TEXT NOT NULL, ticker TEXT NOT NULL,
        captured_at TEXT NOT NULL, as_of_session TEXT, eligible INTEGER NOT NULL CHECK (eligible IN (0, 1)),
        features_json TEXT NOT NULL, sources_json TEXT NOT NULL,
        baseline_score REAL, decision_event_id INTEGER REFERENCES decision_events(id),
        PRIMARY KEY (scan_date, market, ticker)
    )""",
    """CREATE TABLE IF NOT EXISTS selection_shadow_provider_observations (
        scan_date TEXT NOT NULL, market TEXT NOT NULL, ticker TEXT NOT NULL, provider TEXT NOT NULL,
        collected_at TEXT NOT NULL, as_of_session TEXT,
        status TEXT NOT NULL CHECK (status IN ('available', 'partial', 'missing', 'error')),
        details_json TEXT NOT NULL, request_count INTEGER, actual_cost REAL, cost_currency TEXT,
        PRIMARY KEY (scan_date, market, ticker, provider),
        FOREIGN KEY (scan_date, market, ticker) REFERENCES selection_shadow_snapshots(scan_date, market, ticker)
    )""",
    """CREATE TABLE IF NOT EXISTS selection_shadow_candidates (
        id INTEGER PRIMARY KEY, scan_date TEXT NOT NULL, market TEXT NOT NULL, ticker TEXT NOT NULL,
        decision_at TEXT NOT NULL, baseline_score REAL NOT NULL,
        decision_event_id INTEGER NOT NULL REFERENCES decision_events(id), features_json TEXT NOT NULL,
        UNIQUE (scan_date, market, ticker),
        FOREIGN KEY (scan_date, market, ticker) REFERENCES selection_shadow_snapshots(scan_date, market, ticker)
    )""",
)
_collect_lock = threading.Lock()


def ensure_shadow_schema(db):
    for statement in SELECTION_SHADOW_DDL:
        db.execute(statement)


def _now() -> datetime:
    return datetime.now(UTC)


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _decode(text, default):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _completed_sessions(market: str, captured_at: datetime) -> list[str]:
    schedule = exchange_schedule(market)
    stop = int(schedule["close"].searchsorted(captured_at, side="right"))
    return [stamp.date().isoformat() for stamp in schedule.index[stop - 21:stop]]


def _bar_features(frame, sessions: list[str]) -> tuple[str | None, dict, dict]:
    features = {"relative_volume": None, "stock_return_5d": None, "flow_sessions": sessions[-5:]}
    details = {"status": "missing", "reason": "scan_ohlcv_unavailable", "expected_latest_session": sessions[-1]}
    if frame is None or frame.empty or not {"Close", "Volume"}.issubset(frame.columns):
        return None, features, details
    features["symbol"] = frame.attrs.get("symbol")
    allowed = set(sessions)
    rows, duplicate_sessions = {}, set()
    for index, row in frame[["Close", "Volume"]].tail(35).iterrows():
        day = pd.Timestamp(index).date().isoformat()
        if day in allowed:
            if day in rows:
                duplicate_sessions.add(day)
            rows[day] = (number(row["Close"]), number(row["Volume"]))
    for day in duplicate_sessions:
        rows.pop(day)
    last_seen = max(rows, default=None)
    if last_seen != sessions[-1]:
        return last_seen, features, {**details, "reason": "latest_completed_bar_unavailable"}
    if all(day in rows for day in sessions[-6:]):
        first_close, last_close = rows[sessions[-6]][0], rows[last_seen][0]
        if first_close is not None and first_close > 0 and last_close is not None and last_close > 0:
            features["stock_return_5d"] = last_close / first_close - 1
            features["return_sessions"] = [sessions[-6], last_seen]
    if all(day in rows for day in sessions):
        volumes = [rows[day][1] for day in sessions]
        if all(vol is not None and vol >= 0 for vol in volumes):
            mean_volume = sum(volumes[:-1]) / 20
            if mean_volume > 0:
                features["relative_volume"] = volumes[-1] / mean_volume
    details = {
        **details, "status": "available", "reason": None, "source": frame.attrs.get("source", "daily_scan_ohlcv"),
        "return_sessions": features.get("return_sessions"), "volume_sessions": sessions,
        "relative_volume_status": "available" if features["relative_volume"] is not None else "missing_aligned_volume",
    }
    return last_seen, features, details


def _news(db, captured_at: datetime, tickers: set[str]) -> dict:
    recent = captured_at - timedelta(hours=24)
    prior = captured_at - timedelta(days=7)
    window = (recent.isoformat(), captured_at.isoformat())
    scan_count = db.execute("""
        SELECT COUNT(*) FROM market_intel_scans
        WHERE scan_type GLOB 'us_*' AND status = 'success'
          AND julianday(created_at) BETWEEN julianday(?) AND julianday(?)
    """, window).fetchone()[0]
    rows = db.execute("""
        SELECT i.id, i.title, i.confidence, i.source_info, i.affected_tickers_json, i.created_at
        FROM market_intel_issues i JOIN market_intel_scans s ON s.id = i.scan_id
        WHERE s.scan_type GLOB 'us_*' AND s.status = 'success'
          AND julianday(i.created_at) BETWEEN julianday(?) AND julianday(?)
          AND julianday(s.created_at) <= julianday(?)
        ORDER BY julianday(i.created_at), i.id
    """, (prior.isoformat(), captured_at.isoformat(), captured_at.isoformat())).fetchall()
    first_seen = set()
    news = {ticker: [] for ticker in tickers}
    for row in rows:
        headline = " ".join(re.findall(r"\w+", row["title"].casefold()))
        if not headline:
            continue
        created_at = utc_datetime(row["created_at"])
        for item in _decode(row["affected_tickers_json"], []):
            if not isinstance(item, dict) or item.get("market", "US") != "US":
                continue
            ticker = str(item.get("ticker", "")).upper()
            key = ticker, headline
            if ticker not in tickers or key in first_seen:
                continue
            first_seen.add(key)
            conf = number(row["confidence"])
            if created_at >= recent and conf is not None and 0 <= conf <= 1:
                direction = {"up": 1, "down": -1}.get(item.get("direction"), 0)
                news[ticker].append({
                    "id": row["id"], "observed_at": created_at.isoformat(), "direction": direction,
                    "confidence": conf, "source": row["source_info"],
                })
    return {
        ticker: (sum(item["direction"] * item["confidence"] for item in news[ticker]) if scan_count else None, {
            "source": "market_intel_issues", "status": "available" if scan_count else "missing",
            "reason": None if scan_count else "no_successful_us_intel_scan_in_window",
            "window_hours": 24, "dedup_lookback_days": 7, "successful_scans": scan_count,
            "events": news[ticker],
        }) for ticker in tickers
    }


def _baseline(db, market: str, scan_date: str, captured_at: datetime) -> dict:
    rows = db.execute("""
        SELECT id, ticker, created_at, signal_action, recommendation, execution_state, hard_block_reason,
               composite_score, score_details_json
        FROM decision_events
        WHERE scan_date = ? AND market = ? AND julianday(created_at) <= julianday(?)
        ORDER BY julianday(created_at), id
    """, (scan_date, market, captured_at.isoformat()))
    first = {}
    for row in rows:
        first.setdefault(row["ticker"], row)
    return first


def _baseline_score(row) -> float | None:
    if row is None:
        return None
    score = number(row["composite_score"])
    if score is not None and 0 <= score <= 100:
        return score
    return None


def _explicit_buy(row) -> bool:
    return row is not None and (
        (row["signal_action"] or "").strip().upper() == "BUY"
        and not (row["hard_block_reason"] or "").strip()
        and (row["recommendation"] or "").strip().upper() != "BLOCKED"
        and (row["execution_state"] or "").strip().upper() != "BLOCKED"
        and _baseline_score(row) is not None
    )


def _incumbent_financial(event, market: str) -> dict:
    metric = "return_on_equity" if market == "KRX" else "profit_margin"
    raw = _decode(event["score_details_json"], {}).get("fundamental") or {}
    value = number(raw.get("roe" if market == "KRX" else "profit_margin"))
    # USCollector substitutes zero for missing fundamentals, so this history
    # alone cannot establish that a recorded zero was actually reported.
    if market == "US" and value == 0:
        value = None
    return {
        "metric": metric, "value": value, "unit": "ratio", "decision_at": event["created_at"],
        "source": raw.get("source") or ("yfinance" if market == "US" else None),
        "period_end": None, "basis": "provider_ratio_period_unrecorded",
    }


def capture_scan(scan_date: str | date, market: str, ohlcv_by_ticker: dict, *,
                 tickers=(), sectors=None, captured_at: datetime | None = None) -> int:
    """Store the first observed same-day scan, including eligible non-consensus tickers."""
    scan_date = str(scan_date)
    now = _now()
    captured_at = captured_at or now
    if market not in PROVIDERS or captured_at.tzinfo is None or captured_at > now:
        return 0
    if captured_at.astimezone(KST).date().isoformat() != scan_date or now.astimezone(KST).date().isoformat() != scan_date:
        return 0
    # A caller-supplied time can only scope the in-memory bars. The live DB
    # read and immutable snapshot are stamped at their actual observation time.
    sectors = sectors or {}
    sessions = _completed_sessions(market, captured_at)
    if len(sessions) < 21 or pd.Timestamp(scan_date) not in exchange_schedule(market).index:
        return 0
    with get_db() as db:
        ensure_shadow_schema(db)
        db.execute("BEGIN IMMEDIATE")
        if db.execute(f"SELECT 1 FROM {SNAPSHOT_TABLE} WHERE scan_date=? AND market=? LIMIT 1", (scan_date, market)).fetchone():
            return 0
        observed_at = _now()
        baseline = _baseline(db, market, scan_date, observed_at)
        universe = set(tickers) | set(ohlcv_by_ticker) | set(baseline)
        news = _news(db, observed_at, universe) if market == "US" else {}
        records = []
        for ticker in sorted(universe):
            frame = ohlcv_by_ticker.get(ticker)
            eligible = frame is not None and len(frame) >= 60
            last_session, features, bars = _bar_features(frame, sessions)
            if bars.get("source") == "daily_scan_ohlcv" and (market == "US" or features.get("symbol")):
                bars["source"] = "yfinance"
            sources = {"ohlcv": bars, "bars_cutoff": captured_at.isoformat()}
            if market == "US":
                features["us_news_novelty"], sources["news_novelty"] = news[ticker]
                features["sector"] = sectors.get(ticker) or None
                sources["sector"] = {"source": "yfinance_scan_metadata", "status": "available" if features["sector"] else "missing"}
            event = baseline.get(ticker)
            sources["baseline"] = {
                "source": "decision_events", "candidate": _explicit_buy(event),
                "decision_at": event["created_at"] if event else None,
                "action": event["signal_action"] if event else None,
            }
            if event is not None:
                sources["incumbent_financial"] = _incumbent_financial(event, market)
            records.append((scan_date, market, ticker, observed_at.isoformat(), last_session, int(eligible),
                            _json(features), _json(sources), _baseline_score(event), event["id"] if event else None))
        db.executemany(f"""
            INSERT INTO {SNAPSHOT_TABLE} (scan_date, market, ticker, captured_at, as_of_session, eligible,
                features_json, sources_json, baseline_score, decision_event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, records)
    return len(records)


def _observe(sources: ShadowSources, row, provider: str, started_at: datetime) -> Observation:
    market, ticker = row["market"], row["ticker"]
    features = _decode(row["features_json"], {})
    if provider in ("dart", "sec"):
        try:
            events = getattr(sources, provider)(ticker, started_at)
        except Exception as exc:
            events = Observation("error", {"reason": type(exc).__name__})
        metadata = _decode(row["sources_json"], {})
        if not metadata.get("baseline", {}).get("candidate"):
            return events
        incumbent = metadata.get("incumbent_financial", {})
        try:
            comparison = sources.compare_financial(provider, ticker, started_at, incumbent)
        except Exception as exc:
            comparison = {"status": "error", "reason": type(exc).__name__, "incumbent": incumbent}
        return Observation(events.status, {**events.details, "financial_comparison": comparison}, events.request_count)
    if provider in ("kis", "naver"):
        dates = features.get("flow_sessions", [])
        if len(dates) != 5:
            return Observation("missing", {"reason": "insufficient_aligned_sessions"}, 0)
        return getattr(sources, f"{provider}_flow")(ticker, dates)
    return sources.relative_strength(market, features)


def _finalize_market(db, scan_date: str, market: str) -> int:
    if db.execute(f"SELECT 1 FROM {CANDIDATE_TABLE} WHERE scan_date=? AND market=? LIMIT 1", (scan_date, market)).fetchone():
        return 0
    rows = db.execute(f"SELECT * FROM {SNAPSHOT_TABLE} WHERE scan_date=? AND market=?", (scan_date, market)).fetchall()
    source_rows = db.execute(f"SELECT * FROM {PROVIDER_TABLE} WHERE scan_date=? AND market=?", (scan_date, market))
    observed = {(row["ticker"], row["provider"]): row for row in source_rows}
    required = PROVIDERS[market]
    baseline_rows = [row for row in rows if _decode(row["sources_json"], {}).get("baseline", {}).get("candidate")]
    if any((row["ticker"], provider) not in observed for row in baseline_rows if row["eligible"] for provider in required):
        return 0
    decision_at = _now()
    if decision_at.astimezone(KST).date().isoformat() != scan_date:
        return 0
    decisions = []
    for row in baseline_rows:
        ticker = row["ticker"]
        features = _decode(row["features_json"], {})
        for provider in required:
            observation = observed.get((ticker, provider))
            if not observation or observation["status"] != "available":
                continue
            details = _decode(observation["details_json"], {})
            if provider in ("kis", "naver") and features.get("krx_flow_5d") is None:
                features.update({key: details.get(key) for key in ("krx_flow_5d", "krx_flow_positive_share")})
                features["krx_flow_provider"] = provider
            elif provider.endswith("relative_strength"):
                value = details.get("relative_strength")
                features["relative_strength"] = value
                features[provider] = value
        decisions.append((scan_date, market, ticker, decision_at.isoformat(), row["baseline_score"],
                          row["decision_event_id"], _json(features)))
    before = db.total_changes
    db.executemany(f"""
        INSERT INTO {CANDIDATE_TABLE} (scan_date, market, ticker, decision_at, baseline_score, decision_event_id, features_json)
        VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(scan_date, market, ticker) DO NOTHING
    """, decisions)
    return db.total_changes - before


def collect_pending(config=None) -> dict:
    """Observe only today's scan. A bounded standalone job can catch up during the day."""
    summary = {"observations": 0, "candidates": 0}
    if not _collect_lock.acquire(blocking=False):
        return summary
    sources = None
    try:
        options = (config or {}).get("selection_shadow", {})
        # Budgets count tickers, not endpoint requests. Provider loops are independently capped.
        budget = max(1, min(20, int(options.get("tickers_per_run", 8))))
        scan_date = _now().astimezone(KST).date().isoformat()
        with get_db() as db:
            ensure_shadow_schema(db)
            rows = db.execute(f"""
                SELECT * FROM {SNAPSHOT_TABLE} WHERE scan_date=? AND eligible=1
                ORDER BY json_extract(sources_json, '$.baseline.candidate') DESC, market, ticker
            """, (scan_date,)).fetchall()
            done = {
                (row["market"], row["ticker"], row["provider"])
                for row in db.execute(f"SELECT market, ticker, provider FROM {PROVIDER_TABLE} WHERE scan_date=?", (scan_date,))
            }
        sources = ShadowSources()
        deadline = time.monotonic() + 90
        for row in rows:
            market, ticker = row["market"], row["ticker"]
            pending = [provider for provider in PROVIDERS[market] if (market, ticker, provider) not in done]
            if not pending:
                continue
            if budget <= 0 or time.monotonic() >= deadline:
                break
            budget -= 1
            for provider in pending:
                started_at = _now()
                if started_at.astimezone(KST).date().isoformat() != scan_date:
                    return summary
                previous_request_count = sources.request_count
                try:
                    result = _observe(sources, row, provider, started_at)
                except Exception as exc:
                    # HTTP errors can contain credentials or provider request URLs.
                    logger.info("Shadow %s %s provider %s failed (%s)", market, ticker, provider, type(exc).__name__)
                    result = Observation("error", {"reason": type(exc).__name__})
                collected_at = _now()
                if collected_at.astimezone(KST).date().isoformat() != scan_date:
                    return summary
                details = {**result.details, "started_at": started_at.isoformat()}
                request_count = sources.request_count - previous_request_count
                if provider.endswith("relative_strength") and (
                    details.get("symbol_history_calls", 0) or result.status == "error"
                ):
                    # yfinance does not expose its internal HTTP request count.
                    request_count = None
                sessions = details.get("sessions") or []
                as_of_session = sessions[-1] if sessions else None
                with get_db() as db:
                    cursor = db.execute(f"""
                        INSERT INTO {PROVIDER_TABLE} (scan_date, market, ticker, provider, collected_at, as_of_session,
                            status, details_json, request_count)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(scan_date, market, ticker, provider) DO NOTHING
                    """, (scan_date, market, ticker, provider, collected_at.isoformat(), as_of_session,
                          result.status, _json(details), request_count))
                    summary["observations"] += cursor.rowcount
            if _decode(row["sources_json"], {}).get("baseline", {}).get("candidate"):
                with get_db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    summary["candidates"] += _finalize_market(db, scan_date, market)
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            for market in PROVIDERS:
                summary["candidates"] += _finalize_market(db, scan_date, market)
        return summary
    finally:
        if sources is not None:
            sources.close()
        _collect_lock.release()
