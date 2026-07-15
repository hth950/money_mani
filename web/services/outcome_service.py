"""Point-in-time outcome labels for decision snapshots.

The legacy ``signal_performance`` table stores one mutable close price.  This
service keeps a separate row per immutable decision event and trading horizon,
so the research pipeline can measure both selected and observed candidates
without rewriting the original decision.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from web.db.connection import get_db

logger = logging.getLogger("money_mani.web.services.outcome")

KST = timezone(timedelta(hours=9))
HORIZONS = (1, 5, 10, 20)
DEFAULT_COSTS = {"KRX": 0.21, "US": 0.0}
DEFAULT_BENCHMARKS = {"KRX": "^KS11", "US": "^GSPC"}


def _configured_transaction_costs() -> dict[str, float]:
    """Read the same round-trip cost assumptions used by the scorer."""
    try:
        import yaml

        config_path = Path(__file__).parents[2] / "config" / "scoring.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        configured = config.get("transaction_costs", {})
        return {
            market: float(values.get("round_trip_pct", DEFAULT_COSTS.get(market, 0.0)))
            for market, values in configured.items()
            if isinstance(values, dict)
        }
    except Exception as exc:
        logger.warning("Failed to load transaction costs: %s", exc)
        return dict(DEFAULT_COSTS)


def _date_string(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _as_of_date(value=None) -> pd.Timestamp:
    if value is None:
        value = datetime.now(KST).date()
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_localize(None)
    return timestamp.normalize()


def _normalise_ohlcv(frame) -> pd.DataFrame | None:
    """Return a clean, date-indexed OHLCV frame or ``None``."""
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    df = frame.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce")
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    df = df[~df.index.isna()].sort_index()
    df = df[~df.index.duplicated(keep="last")]

    aliases = {
        "open": "Open", "high": "High", "low": "Low", "close": "Close",
        "adj close": "Close", "volume": "Volume",
    }
    rename = {}
    for column in df.columns:
        key = str(column).strip().lower()
        if key in aliases:
            rename[column] = aliases[key]
    df = df.rename(columns=rename)
    # yfinance variants may expose both ``Close`` and ``Adj Close``.  After
    # aliasing they share a name; keep the first (actual Close when present)
    # instead of allowing a duplicate-column DataFrame through the math below.
    if df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated(keep="first")]
    if "Close" not in df.columns:
        return None
    for column in ("Open", "High", "Low", "Close"):
        if column not in df.columns:
            df[column] = df["Close"]
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    if df.empty or (df[["Open", "High", "Low", "Close"]] <= 0).any().any():
        return None

    # Provider/unit mistakes can look like spectacular returns.  Refuse to
    # turn an adjacent >10x jump into a training label; the caller records the
    # reason as invalid instead.
    close = df["Close"].astype(float)
    ratios = close.iloc[1:].to_numpy() / close.iloc[:-1].to_numpy()
    if len(ratios) and (not all(math.isfinite(float(r)) for r in ratios) or
                        any(float(r) > 10.0 or float(r) < 0.1 for r in ratios)):
        df.attrs["invalid_reason"] = "adjacent_close_jump_ratio_out_of_range"
    return df


def _first_index_on_or_after(frame: pd.DataFrame, target: pd.Timestamp) -> int | None:
    dates = frame.index.normalize()
    positions = dates.searchsorted(target, side="left")
    return int(positions) if positions < len(frame) else None


class DecisionOutcomeService:
    """Create and refresh forward-return labels for decision events."""

    def __init__(
        self,
        price_loader: Callable | None = None,
        benchmark_loader: Callable | None = None,
        transaction_costs: dict[str, float] | None = None,
        benchmark_symbols: dict[str, str] | None = None,
    ):
        self.price_loader = price_loader or self._default_price_loader
        self.benchmark_loader = benchmark_loader or self._default_benchmark_loader
        self.transaction_costs = {
            **_configured_transaction_costs(),
            **(transaction_costs or {}),
        }
        self.benchmark_symbols = {**DEFAULT_BENCHMARKS, **(benchmark_symbols or {})}

    def label_pending(self, limit: int = 200, as_of=None) -> dict:
        """Label the oldest decision events in a bounded batch.

        A bounded batch prevents the nightly job from issuing an unbounded
        burst of provider requests.  Re-running is idempotent because outcomes
        are upserted on ``(decision_event_id, horizon_days)``.
        """
        as_of_ts = _as_of_date(as_of)
        with get_db() as db:
            events = db.execute(
                """
                SELECT de.*,
                       CASE
                         WHEN COUNT(outcome.id) = 0 THEN 0
                         WHEN SUM(CASE WHEN outcome.status = 'pending' THEN 1 ELSE 0 END) > 0 THEN 1
                         ELSE 2
                       END AS _retry_priority
                FROM decision_events de
                LEFT JOIN decision_outcomes outcome
                  ON outcome.decision_event_id = de.id
                WHERE de.signal_action IN ('BUY', 'SELL')
                  AND de.scan_date <= ?
                GROUP BY de.id
                HAVING COUNT(outcome.id) = 0
                    OR SUM(CASE WHEN outcome.status = 'pending' THEN 1 ELSE 0 END) > 0
                    OR SUM(CASE WHEN outcome.status = 'unavailable' THEN 1 ELSE 0 END) > 0
                ORDER BY _retry_priority ASC, de.scan_date ASC, de.id ASC
                LIMIT ?
                """,
                (as_of_ts.strftime("%Y-%m-%d"), max(1, int(limit))),
            ).fetchall()
            events = [dict(row) for row in events]

            # Preserve terminal labels horizon-by-horizon.  An event can have a
            # mixture of evaluated, invalid, pending, and unavailable rows; it
            # may be selected for one retryable horizon without reopening its
            # terminal outcomes.
            existing_statuses: dict[int, dict[int, str]] = defaultdict(dict)
            if events:
                placeholders = ",".join("?" for _ in events)
                rows = db.execute(
                    f"""SELECT decision_event_id, horizon_days, status
                         FROM decision_outcomes
                         WHERE decision_event_id IN ({placeholders})""",
                    [event["id"] for event in events],
                ).fetchall()
                for row in rows:
                    existing_statuses[row["decision_event_id"]][row["horizon_days"]] = row["status"]

        if not events:
            return {"events": 0, "evaluated": 0, "pending": 0, "unavailable": 0, "invalid": 0}

        # Fetch each instrument and benchmark once for the whole batch.
        price_frames: dict[tuple[str, str], pd.DataFrame | None] = {}
        pair_dates: dict[tuple[str, str], list[str]] = {}
        for event in events:
            pair = (event.get("market") or "KRX", event["ticker"])
            pair_dates.setdefault(pair, []).append(_date_string(event["scan_date"]))
        for (market, ticker), dates in pair_dates.items():
            start = (pd.Timestamp(min(dates)) - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
            end = (as_of_ts + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            try:
                price_frames[(market, ticker)] = _normalise_ohlcv(
                    self.price_loader(market, ticker, start, end)
                )
            except Exception as exc:
                logger.warning("Outcome price fetch failed for %s:%s: %s", market, ticker, exc)
                price_frames[(market, ticker)] = None

        benchmark_frames: dict[str, pd.DataFrame | None] = {}
        for market in sorted({event.get("market") or "KRX" for event in events}):
            dates = [_date_string(event["scan_date"]) for event in events
                     if (event.get("market") or "KRX") == market]
            start = (pd.Timestamp(min(dates)) - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
            end = (as_of_ts + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            try:
                benchmark_frames[market] = _normalise_ohlcv(
                    self.benchmark_loader(market, self.benchmark_symbols.get(market, ""), start, end)
                )
                if benchmark_frames[market] is not None and benchmark_frames[market].attrs.get("invalid_reason"):
                    benchmark_frames[market] = None
            except Exception as exc:
                logger.warning("Outcome benchmark fetch failed for %s: %s", market, exc)
                benchmark_frames[market] = None

        counts = {"events": len(events), "evaluated": 0, "pending": 0, "unavailable": 0, "invalid": 0}
        with get_db() as db:
            for event in events:
                market = event.get("market") or "KRX"
                frame = price_frames.get((market, event["ticker"]))
                benchmark = benchmark_frames.get(market)
                for horizon in HORIZONS:
                    if existing_statuses.get(event["id"], {}).get(horizon) in {
                        "evaluated", "invalid"
                    }:
                        continue
                    result = self._label_event_horizon(event, frame, benchmark, horizon, as_of_ts, market)
                    self._upsert_outcome(db, event["id"], horizon, result)
                    counts[result["status"]] += 1
        return counts

    def _label_event_horizon(self, event, frame, benchmark, horizon, as_of_ts, market) -> dict:
        base = {
            "status": "unavailable",
            "reason": None,
            "entry_date": None,
            "exit_date": None,
            "entry_price": None,
            "exit_price": None,
            "raw_return_pct": None,
            "benchmark_return_pct": None,
            "excess_return_pct": None,
            "transaction_cost_pct": float(self.transaction_costs.get(market, 0.0)),
            "net_return_pct": None,
            "mfe_pct": None,
            "mae_pct": None,
            "price_source": market,
            "benchmark_source": self.benchmark_symbols.get(market),
        }
        if frame is None:
            base["reason"] = "price_data_unavailable"
            return base
        if frame.attrs.get("invalid_reason"):
            base["status"] = "invalid"
            base["reason"] = frame.attrs["invalid_reason"]
            return base

        event_date = pd.Timestamp(_date_string(event["scan_date"]))
        entry_pos = _first_index_on_or_after(frame, event_date)
        if entry_pos is None:
            base["reason"] = "no_trading_bar_on_or_after_event"
            return base
        exit_pos = entry_pos + horizon
        entry_date = frame.index[entry_pos]
        base["entry_date"] = entry_date.strftime("%Y-%m-%d")
        base["entry_price"] = float(frame.iloc[entry_pos]["Open"])

        # A missing future bar is expected for recent events, so leave it
        # pending rather than treating it as a bad prediction.
        if exit_pos >= len(frame):
            base["status"] = "pending"
            base["reason"] = "horizon_not_matured"
            return base
        if entry_date.normalize() > as_of_ts:
            base["status"] = "pending"
            base["reason"] = "event_after_as_of"
            return base

        exit_date = frame.index[exit_pos]
        if exit_date.normalize() > as_of_ts:
            base["status"] = "pending"
            base["reason"] = "horizon_not_matured"
            return base
        entry_price = base["entry_price"]
        exit_price = float(frame.iloc[exit_pos]["Close"])
        action = (event.get("signal_action") or "BUY").upper()
        price_return = (exit_price / entry_price - 1.0) * 100.0
        if action == "SELL":
            price_return = -price_return
        base.update({
            "status": "evaluated",
            "entry_date": entry_date.strftime("%Y-%m-%d"),
            "exit_date": exit_date.strftime("%Y-%m-%d"),
            "exit_price": exit_price,
            "raw_return_pct": round(price_return, 6),
            "net_return_pct": round(price_return - base["transaction_cost_pct"], 6),
            "evaluated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        })

        window = frame.iloc[entry_pos:exit_pos + 1]
        high = float(window["High"].max())
        low = float(window["Low"].min())
        if action == "SELL":
            base["mfe_pct"] = round((entry_price / low - 1.0) * 100.0, 6)
            base["mae_pct"] = round((entry_price / high - 1.0) * 100.0, 6)
        else:
            base["mfe_pct"] = round((high / entry_price - 1.0) * 100.0, 6)
            base["mae_pct"] = round((low / entry_price - 1.0) * 100.0, 6)

        if benchmark is not None:
            benchmark_entry = _first_index_on_or_after(benchmark, entry_date.normalize())
            benchmark_exit = benchmark_entry + horizon if benchmark_entry is not None else None
            if benchmark_entry is not None and benchmark_exit is not None and benchmark_exit < len(benchmark):
                b_entry = float(benchmark.iloc[benchmark_entry]["Close"])
                b_exit = float(benchmark.iloc[benchmark_exit]["Close"])
                benchmark_return = (b_exit / b_entry - 1.0) * 100.0
                if action == "SELL":
                    benchmark_return = -benchmark_return
                base["benchmark_return_pct"] = round(benchmark_return, 6)
                base["excess_return_pct"] = round(price_return - benchmark_return, 6)
        return base

    @staticmethod
    def _upsert_outcome(db, event_id: int, horizon: int, result: dict) -> None:
        db.execute(
            """
            INSERT INTO decision_outcomes
            (decision_event_id, horizon_days, status, entry_date, exit_date,
             entry_price, exit_price, raw_return_pct, benchmark_return_pct,
             excess_return_pct, transaction_cost_pct, net_return_pct, mfe_pct,
             mae_pct, price_source, benchmark_source, label_source, reason,
             observed_at, evaluated_at, updated_at)
            VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                datetime('now')
            )
            ON CONFLICT(decision_event_id, horizon_days) DO UPDATE SET
              status=excluded.status, entry_date=excluded.entry_date,
              exit_date=excluded.exit_date, entry_price=excluded.entry_price,
              exit_price=excluded.exit_price, raw_return_pct=excluded.raw_return_pct,
              benchmark_return_pct=excluded.benchmark_return_pct,
              excess_return_pct=excluded.excess_return_pct,
              transaction_cost_pct=excluded.transaction_cost_pct,
              net_return_pct=excluded.net_return_pct, mfe_pct=excluded.mfe_pct,
              mae_pct=excluded.mae_pct, price_source=excluded.price_source,
              benchmark_source=excluded.benchmark_source, label_source=excluded.label_source,
              reason=excluded.reason, observed_at=excluded.observed_at,
              evaluated_at=excluded.evaluated_at, updated_at=datetime('now')
            """,
            (
                event_id, horizon, result["status"], result.get("entry_date"),
                result.get("exit_date"), result.get("entry_price"), result.get("exit_price"),
                result.get("raw_return_pct"), result.get("benchmark_return_pct"),
                result.get("excess_return_pct"), result.get("transaction_cost_pct", 0.0),
                result.get("net_return_pct"), result.get("mfe_pct"), result.get("mae_pct"),
                result.get("price_source"), result.get("benchmark_source"),
                "decision_outcome_labeler", result.get("reason"),
                datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
                result.get("evaluated_at"),
            ),
        )

    def list_outcomes(self, event_id=None, status=None, limit=200) -> list[dict]:
        with get_db() as db:
            clauses, params = [], []
            if event_id is not None:
                clauses.append("decision_event_id=?")
                params.append(event_id)
            if status:
                clauses.append("status=?")
                params.append(status)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            params.append(limit)
            rows = db.execute(
                f"SELECT * FROM decision_outcomes {where} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        return [dict(row) for row in rows]

    def _default_price_loader(self, market: str, ticker: str, start: str, end: str):
        if market == "KRX":
            from market_data.krx_fetcher import KRXFetcher
            return KRXFetcher(delay=0.2).get_ohlcv(ticker, start, end)
        from market_data.us_fetcher import USFetcher
        return USFetcher().get_ohlcv(ticker, start, end)

    def _default_benchmark_loader(self, market: str, ticker: str, start: str, end: str):
        try:
            import yfinance as yf
            frame = yf.Ticker(ticker).history(start=start, end=end)
            if frame.empty:
                return frame
            frame.index.name = "Date"
            return frame
        except Exception as exc:
            logger.warning("Benchmark loader failed for %s:%s: %s", market, ticker, exc)
            return pd.DataFrame()
