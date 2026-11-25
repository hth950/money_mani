"""Versioned, executable forward outcomes for recorded research decisions."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
import logging
import math
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from web.db.connection import get_db
from web.services.outcome_service import DEFAULT_BENCHMARKS, HORIZONS

logger = logging.getLogger(__name__)

LABEL_VERSION = "next_open_v1"
LABEL_TABLE = "decision_outcome_labels"
SHADOW_LABEL_TABLE = "shadow_outcome_labels"
KST = ZoneInfo("Asia/Seoul")
EXCHANGES = {"KRX": "XKRX", "US": "XNYS"}
# Fixed round-trip percentage points in this research label version.
ROUND_TRIP_TRANSACTION_COST_PCT = {"KRX": 0.21, "US": 0.0}
ROUND_TRIP_SLIPPAGE_PCT = {"KRX": 0.10, "US": 0.04}
STATUSES = ("evaluated", "pending", "unavailable", "invalid")


@dataclass(frozen=True)
class LabelTarget:
    source: str
    table: str
    foreign_key: str
    event_filter: str


DECISIONS = LabelTarget(
    "decision_events", LABEL_TABLE, "decision_event_id", "e.signal_action IN ('BUY', 'SELL')"
)
SHADOW = LabelTarget("selection_shadow_candidates", SHADOW_LABEL_TABLE, "shadow_candidate_id", "1")


def _label_ddl(target: LabelTarget) -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS {target.table} (
            {target.foreign_key} INTEGER NOT NULL REFERENCES {target.source}(id) ON DELETE CASCADE,
            horizon_days INTEGER NOT NULL CHECK (horizon_days IN (1, 5, 10, 20)),
            label_version TEXT NOT NULL,
            decision_at TEXT,
            entry_date TEXT,
            exit_date TEXT,
            entry_at TEXT,
            exit_at TEXT,
            entry_price REAL,
            exit_price REAL,
            raw_return_pct REAL,
            transaction_cost_pct REAL,
            slippage_pct REAL,
            net_return_pct REAL,
            benchmark_symbol TEXT,
            benchmark_entry_price REAL,
            benchmark_exit_price REAL,
            benchmark_return_pct REAL,
            excess_return_pct REAL,
            status TEXT NOT NULL CHECK (status IN ('evaluated', 'pending', 'unavailable', 'invalid')),
            reason TEXT,
            observed_at TEXT NOT NULL,
            retry_at TEXT,
            PRIMARY KEY ({target.foreign_key}, horizon_days, label_version)
        )
    """


OUTCOME_LABEL_DDL = _label_ddl(DECISIONS)
SHADOW_OUTCOME_LABEL_DDL = _label_ddl(SHADOW)


def _utc_iso(value) -> str:
    return value.isoformat(timespec="microseconds")


def observation_cutoff(value=None) -> datetime:
    """Interpret date-only replay inputs as the end of their KST day."""
    now = datetime.now(timezone.utc)
    if value is None:
        return now
    if isinstance(value, str) and len(value) == 10:
        value = date.fromisoformat(value)
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time.max, tzinfo=KST)
    elif isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return min(now, value.astimezone(timezone.utc))


def _parse_recorded_time(value) -> datetime:
    if not isinstance(value, str) or len(value) < 16 or value[10] not in {"T", " "}:
        raise ValueError("A recorded decision timestamp must include a time")
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # Both persisted decision event clocks use SQLite datetime('now'), in UTC.
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)


def decision_timestamp(event: dict) -> datetime:
    """Resolve the recorded decision instant, including its detection clock."""
    if "decision_at" in event:
        return _parse_recorded_time(event["decision_at"])
    created_at = _parse_recorded_time(event.get("created_at"))
    detected_at = event.get("detected_at")
    # The audit snapshot and its detection must both precede the modeled fill.
    return max(created_at, _parse_recorded_time(detected_at)) if detected_at else created_at


@lru_cache(maxsize=2)
def exchange_schedule(market: str) -> pd.DataFrame:
    import exchange_calendars

    schedule = exchange_calendars.get_calendar(
        EXCHANGES[market], start="2000-01-01", end="2050-12-31"
    ).schedule
    if market == "KRX":
        # Constitution Day was restored as a public holiday in 2026.
        # https://www.korea.kr/news/policyNewsView.do?newsId=148959060
        holidays = []
        for year in range(2026, 2051):
            constitution_day = date(year, 7, 17)
            # The substitute day after a Saturday or Sunday observance is Monday.
            if constitution_day.weekday() >= 5:
                constitution_day += timedelta(days=7 - constitution_day.weekday())
            holidays.append(pd.Timestamp(constitution_day))
        schedule = schedule.drop(holidays, errors="ignore")
        # KRX November 2026 college entrance exam hours, in UTC.
        # https://kind.krx.co.kr/external/2026/11/05/000141/20261105000216/99303.htm
        schedule.loc["2026-11-19", "open"] = pd.Timestamp("2026-11-19T01:00:00Z")
        schedule.loc["2026-11-19", "close"] = pd.Timestamp("2026-11-19T07:30:00Z")
    return schedule


def _entry_position(schedule: pd.DataFrame, decision_at: datetime) -> int:
    if decision_at.year < 2000:
        return len(schedule)
    return int(schedule["open"].searchsorted(decision_at, side="right"))


def scheduled_exit_at(market: str, decision_at: datetime | str, horizon: int) -> datetime | None:
    """Return the expected UTC exit close even when market bars are absent."""
    if market not in EXCHANGES or horizon not in HORIZONS:
        return None
    if isinstance(decision_at, datetime):
        decision_at = decision_at.isoformat()
    try:
        timestamp = _parse_recorded_time(decision_at)
    except (TypeError, ValueError):
        return None
    schedule = exchange_schedule(market)
    exit_pos = _entry_position(schedule, timestamp) + horizon - 1
    if exit_pos < len(schedule):
        return schedule["close"].iloc[exit_pos].to_pydatetime()
    return None


def fetch_daily_bars(market: str, ticker: str, start: str, end: str) -> pd.DataFrame:
    """Fetch split-adjusted, dividend-unadjusted daily open and close prices."""
    if market == "KRX" and not ticker.startswith("^"):
        from market_data.krx_fetcher import download_yahoo_ohlcv

        return download_yahoo_ohlcv(ticker, start=start, end=end, auto_adjust=False)
    import yfinance as yf

    return yf.Ticker(ticker).history(start=start, end=end, auto_adjust=False)


def _plan(event: dict, horizons: list[int], as_of: datetime) -> dict[int, dict]:
    market = event["market"]
    base = {
        "transaction_cost_pct": ROUND_TRIP_TRANSACTION_COST_PCT.get(market),
        "slippage_pct": ROUND_TRIP_SLIPPAGE_PCT.get(market),
        "benchmark_symbol": DEFAULT_BENCHMARKS.get(market),
        "observed_at": _utc_iso(as_of),
    }
    try:
        decision_at = decision_timestamp(event)
    except (TypeError, ValueError):
        return {horizon: dict(base, status="invalid", reason="invalid_decision_timestamp")
                for horizon in horizons}
    base["decision_at"] = _utc_iso(decision_at)
    if market not in EXCHANGES:
        return {horizon: dict(base, status="invalid", reason="unsupported_market")
                for horizon in horizons}

    schedule = exchange_schedule(market)
    entry_pos = _entry_position(schedule, decision_at)
    planned = {}
    for horizon in horizons:
        # The first session held counts as one day. A 1-day label exits at
        # the close of the very session whose opening price supplied the fill.
        exit_pos = entry_pos + horizon - 1
        if exit_pos >= len(schedule):
            planned[horizon] = dict(base, status="invalid", reason="calendar_out_of_range")
            continue
        entry = schedule.iloc[entry_pos]
        exit_session = schedule.iloc[exit_pos]
        exit_at = exit_session["close"]
        row = dict(
            base, entry_date=entry.name.date().isoformat(), entry_at=_utc_iso(entry["open"]),
            exit_date=exit_session.name.date().isoformat(), exit_at=_utc_iso(exit_at),
        )
        if decision_at > as_of:
            row.update(status="pending", reason="decision_after_cutoff", retry_at=_utc_iso(exit_at))
        elif exit_at > as_of:
            row.update(status="pending", reason="horizon_not_matured", retry_at=_utc_iso(exit_at))
        else:
            row.update(status="unavailable", retry_at=_utc_iso(as_of + timedelta(days=1)))
        planned[horizon] = row
    return planned


def normalise_daily_bars(frame: pd.DataFrame | None) -> pd.DataFrame:
    """Use the provider's daily session dates, retaining missing price fields."""
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame()
    frame = frame.copy()
    if not isinstance(frame.index, pd.DatetimeIndex):
        # CSV caches with exchange-local offsets across US DST still retain
        # the session date in their first ten characters.
        frame.index = frame.index.astype(str).str[:10]
    try:
        index = pd.to_datetime(frame.index, errors="coerce")
        index = index.tz_localize(None).normalize()
    except (AttributeError, TypeError, ValueError):
        return pd.DataFrame()
    frame.index = index
    frame = frame.loc[frame.index.notna() & ~frame.index.duplicated(keep="last")].sort_index()
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    frame = frame.loc[:, ~frame.columns.duplicated(keep="last")]
    return frame.reindex(columns=["open", "close", "volume"]).apply(pd.to_numeric, errors="coerce")


def _bar_returns(frame: pd.DataFrame, entry: str, exit_date: str, kind: str) -> tuple:
    if frame.empty:
        return None, None, f"{kind}_data_unavailable"
    if pd.Timestamp(entry) not in frame.index:
        return None, None, f"{kind}_entry_bar_missing"
    if pd.Timestamp(exit_date) not in frame.index:
        return None, None, f"{kind}_exit_bar_missing"
    opening, closing = float(frame.loc[entry, "open"]), float(frame.loc[exit_date, "close"])
    if not all(math.isfinite(price) for price in (opening, closing)):
        return None, None, f"{kind}_open_or_close_unavailable"
    if opening <= 0 or closing <= 0:
        return None, None, f"{kind}_invalid_price"
    if kind == "price":
        daily_volume = frame.loc[[entry, exit_date], "volume"]
        if (daily_volume <= 0).any():
            return None, None, "price_nontrading_bar"
    window_closes = frame.loc[entry:exit_date, "close"].dropna()
    jump_ratios = window_closes / window_closes.shift(1)
    if not 0.1 <= closing / opening <= 10.0 or ((jump_ratios < 0.1) | (jump_ratios > 10.0)).any():
        return None, None, f"{kind}_jump_out_of_range"
    return opening, closing, None


def _evaluate(row: dict, frame: pd.DataFrame, benchmark: pd.DataFrame, direction: int) -> None:
    entry_date, exit_date = row["entry_date"], row["exit_date"]
    opening, closing, reason = _bar_returns(frame, entry_date, exit_date, "price")
    if reason:
        row["reason"] = reason
        if reason in {"price_invalid_price", "price_jump_out_of_range", "price_nontrading_bar"}:
            row.update(status="invalid", retry_at=None)
        return

    gross = direction * (closing / opening - 1.0) * 100.0
    net = gross - row["transaction_cost_pct"] - row["slippage_pct"]
    row.update(entry_price=opening, exit_price=closing, raw_return_pct=round(gross, 6),
               net_return_pct=round(net, 6))
    benchmark_open, benchmark_close, reason = _bar_returns(benchmark, entry_date, exit_date, "benchmark")
    if reason:
        row["reason"] = reason
        return
    benchmark_return = direction * (benchmark_close / benchmark_open - 1.0) * 100.0
    row.update(
        benchmark_entry_price=benchmark_open, benchmark_exit_price=benchmark_close,
        benchmark_return_pct=round(benchmark_return, 6),
        excess_return_pct=round(net - benchmark_return, 6), status="evaluated", retry_at=None,
    )


_LABEL_COLUMNS = (
    "decision_at", "entry_date", "exit_date", "entry_at", "exit_at", "entry_price", "exit_price",
    "raw_return_pct", "transaction_cost_pct", "slippage_pct", "net_return_pct", "benchmark_symbol",
    "benchmark_entry_price", "benchmark_exit_price", "benchmark_return_pct", "excess_return_pct",
    "status", "reason", "observed_at", "retry_at",
)


class DecisionLabelService:
    """Compute labels independently of the legacy forward-outcome tables."""

    def __init__(self, price_loader: Callable | None = None, benchmark_loader: Callable | None = None,
                 db_provider: Callable | None = None):
        self.price_loader = price_loader or fetch_daily_bars
        self.benchmark_loader = benchmark_loader or fetch_daily_bars
        self.db_provider = db_provider or get_db

    def label_pending(self, limit: int = 200, as_of=None) -> dict:
        return self._label_pending(DECISIONS, limit, observation_cutoff(as_of))

    def label_shadow_pending(self, limit: int = 200, as_of=None) -> dict:
        return self._label_pending(SHADOW, limit, observation_cutoff(as_of))

    def relabel_all(self, *, batch_size: int = 200, as_of=None, shadow: bool = False) -> dict:
        """Process a fixed copy once, retrying each incomplete label on that copy."""
        target = SHADOW if shadow else DECISIONS
        as_of = observation_cutoff(as_of)
        with self.db_provider() as db:
            if not self._ensure_schema(db, target):
                return self._coverage(target, as_of)
            high_water = db.execute(f"SELECT COALESCE(MAX(id), 0) FROM {target.source}").fetchone()[0]
        after_id = 0
        while after_id < high_water:
            with self.db_provider() as db:
                events = db.execute(f"""
                    SELECT * FROM {target.source} e
                    WHERE e.id > ? AND e.id <= ? AND {target.event_filter}
                    ORDER BY e.id LIMIT ?
                """, (after_id, high_water, max(1, int(batch_size)))).fetchall()
                events = [dict(event) for event in events]
                retryable = self._retryable_horizons(db, target, events)
            if not events:
                break
            self._process(target, events, retryable, as_of)
            after_id = events[-1]["id"]
        return self._coverage(target, as_of)

    @staticmethod
    def _ensure_schema(db, target) -> bool:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                          (target.source,)).fetchone():
            return False
        db.execute(_label_ddl(target))
        return True

    def _label_pending(self, target: LabelTarget, limit: int, as_of: datetime) -> dict:
        with self.db_provider() as db:
            if not self._ensure_schema(db, target):
                return {"events": 0, **dict.fromkeys(STATUSES, 0), "reasons": {}}
            events = db.execute(f"""
                SELECT e.*, COUNT(o.horizon_days) AS labels_count
                FROM {target.source} e
                LEFT JOIN {target.table} o
                  ON o.{target.foreign_key} = e.id AND o.label_version = ?
                WHERE {target.event_filter}
                GROUP BY e.id
                HAVING labels_count < ? OR MIN(o.retry_at) <= ?
                ORDER BY (labels_count = ?) ASC, MIN(o.retry_at), e.id
                LIMIT ?
            """, (LABEL_VERSION, len(HORIZONS), _utc_iso(as_of), len(HORIZONS),
                  max(1, int(limit)))).fetchall()
            events = [dict(event) for event in events]
            retryable = self._retryable_horizons(db, target, events, as_of)
        return self._process(target, events, retryable, as_of)

    @staticmethod
    def _retryable_horizons(db, target, events, as_of=None) -> dict[int, list[int]]:
        retryable = {}
        for event in events:
            rows = db.execute(f"""
                SELECT horizon_days, retry_at FROM {target.table}
                WHERE {target.foreign_key} = ? AND label_version = ?
            """, (event["id"], LABEL_VERSION)).fetchall()
            blocked = {horizon for horizon, retry in rows
                       if retry is None or (as_of is not None and retry > _utc_iso(as_of))}
            retryable[event["id"]] = [horizon for horizon in HORIZONS if horizon not in blocked]
        return retryable

    @staticmethod
    def _frames(loader: Callable, bounds: dict) -> dict:
        frames = {}
        for (market, ticker), (start, last) in bounds.items():
            end = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
            try:
                frames[(market, ticker)] = normalise_daily_bars(loader(market, ticker, start, end))
            except Exception as exc:
                logger.warning("Could not read daily bars for %s:%s: %s", market, ticker, exc)
                frames[(market, ticker)] = pd.DataFrame()
        return frames

    def _process(self, target: LabelTarget, events: list[dict], retryable: dict, as_of: datetime) -> dict:
        prices, benchmarks = {}, {}
        planned = []
        for event in events:
            rows = _plan(event, retryable[event["id"]], as_of)
            for horizon, row in rows.items():
                planned.append((event, horizon, row))
                if row["status"] != "unavailable":
                    continue
                for bounds, symbol in ((prices, event["ticker"]), (benchmarks, row["benchmark_symbol"])):
                    pair = (event["market"], symbol)
                    start, last = bounds.get(pair, (row["entry_date"], row["exit_date"]))
                    bounds[pair] = (min(start, row["entry_date"]), max(last, row["exit_date"]))

        price_frames = self._frames(self.price_loader, prices)
        benchmark_frames = self._frames(self.benchmark_loader, benchmarks)
        counts = dict.fromkeys(STATUSES, 0)
        reasons = Counter()
        if planned:
            with self.db_provider() as db:
                for event, horizon, row in planned:
                    if row["status"] == "unavailable":
                        market = event["market"]
                        _evaluate(row, price_frames[(market, event["ticker"])],
                                  benchmark_frames[(market, row["benchmark_symbol"])],
                                  -1 if event.get("signal_action") == "SELL" else 1)
                    if self._upsert(db, target, event["id"], horizon, row):
                        counts[row["status"]] += 1
                        if row.get("reason"):
                            reasons[row["reason"]] += 1
        return {"events": len(events), **counts, "reasons": dict(reasons)}

    @staticmethod
    def _upsert(db, target, event_id, horizon, row) -> bool:
        values = (event_id, horizon, LABEL_VERSION, *(row.get(column) for column in _LABEL_COLUMNS))
        assignments = ", ".join(f"{column}=excluded.{column}" for column in _LABEL_COLUMNS)
        return db.execute(f"""
            INSERT INTO {target.table}
            ({target.foreign_key}, horizon_days, label_version, {', '.join(_LABEL_COLUMNS)})
            SELECT {', '.join('?' for _ in values)}
            WHERE EXISTS (SELECT 1 FROM {target.source} WHERE id = ?)
            ON CONFLICT ({target.foreign_key}, horizon_days, label_version) DO UPDATE SET {assignments}
            WHERE {target.table}.retry_at IS NOT NULL
              AND {target.table}.observed_at <= excluded.observed_at
        """, (*values, event_id)).rowcount > 0

    def _coverage(self, target, as_of: datetime) -> dict:
        coverage = {"events": 0, "labels": 0, "as_of": _utc_iso(as_of), "groups": []}
        with self.db_provider() as db:
            if not self._ensure_schema(db, target):
                return coverage
            coverage["events"] = db.execute(f"""
                SELECT COUNT(*) FROM {target.source} e WHERE {target.event_filter}
            """).fetchone()[0]
            rows = db.execute(f"""
                SELECT e.market, o.horizon_days, o.status, o.reason, COUNT(*) AS count
                FROM {target.table} o JOIN {target.source} e ON e.id = o.{target.foreign_key}
                WHERE o.label_version = ? AND o.observed_at <= ? AND {target.event_filter}
                GROUP BY e.market, o.horizon_days, o.status, o.reason
                ORDER BY e.market, o.horizon_days, o.status, o.reason
            """, (LABEL_VERSION, _utc_iso(as_of))).fetchall()
        coverage["groups"] = [dict(row) for row in rows]
        coverage["labels"] = sum(row["count"] for row in rows)
        return coverage
