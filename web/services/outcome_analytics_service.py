"""Read-only analytics for immutable decision outcome labels.

``DecisionOutcomeService`` creates point-in-time labels.  This module only
reads those labels and the decision snapshot that produced them; it never
changes scoring weights, thresholds, or any database row.  Keeping this
boundary explicit prevents a dashboard request from becoming an accidental
online-learning loop.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any, Iterable

from web.db.connection import get_db

KST = timezone(timedelta(hours=9))
HORIZONS = (1, 5, 10, 20)

# Composite scores are stored as 0..1.  A few historical/manual rows use a
# 0..100 representation, so _score_percent normalises both forms before
# assigning a bucket.  The API labels deliberately use percentages because
# that is what the scoring dashboard displays to users.
SCORE_BUCKETS = (
    ("0-40", 0.0, 40.0, False),
    ("40-65", 40.0, 65.0, False),
    ("65-80", 65.0, 80.0, False),
    ("80-100", 80.0, 100.0, True),
)


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


def _score_percent(value: Any) -> float | None:
    """Normalise a composite score to percentage points (0..100)."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score):
        return None
    if 0.0 <= score <= 1.0:
        score *= 100.0
    if score < 0.0 or score > 100.0:
        return None
    return score


def _today_kst() -> date:
    return datetime.now(KST).date()


class OutcomeAnalyticsService:
    """Aggregate labelled outcomes without mutating model configuration."""

    def __init__(self, today: date | datetime | str | None = None):
        # ``today`` is injectable for deterministic reports/tests.  It is not
        # exposed by the HTTP API and does not alter any persisted data.
        self._today_override = self._coerce_date(today) if today is not None else None

    @property
    def today(self) -> date:
        """Return current KST date unless a deterministic test date is set."""
        return self._today_override or _today_kst()

    @staticmethod
    def _coerce_date(value: date | datetime | str) -> date:
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(KST)
            return value.date()
        if isinstance(value, date):
            return value
        return datetime.fromisoformat(str(value)[:10]).date()

    @staticmethod
    def _validate_horizon(horizon: int = 5, horizon_days: int | None = None) -> int:
        if horizon_days is not None:
            horizon = horizon_days
        try:
            horizon = int(horizon)
        except (TypeError, ValueError) as exc:
            raise ValueError("horizon must be one of 1, 5, 10, 20") from exc
        if horizon not in HORIZONS:
            raise ValueError("horizon must be one of 1, 5, 10, 20")
        return horizon

    @staticmethod
    def _validate_days(days: int | None) -> int | None:
        if days is None:
            return None
        try:
            days = int(days)
        except (TypeError, ValueError) as exc:
            raise ValueError("days must be a positive integer") from exc
        if days < 1:
            raise ValueError("days must be a positive integer")
        return days

    @staticmethod
    def _normalise_market(market: str | None) -> str | None:
        if market is None:
            return None
        value = str(market).strip().upper()
        return value or None

    def _date_window(self, days: int | None) -> tuple[str | None, str]:
        end = self.today.isoformat()
        if days is None:
            return None, end
        # ``days=1`` means today's cohort; larger windows include today and
        # the preceding ``days - 1`` calendar dates.
        start = (self.today - timedelta(days=days - 1)).isoformat()
        return start, end

    def _fetch_rows(
        self,
        *,
        horizon: int,
        days: int | None,
        market: str | None,
        signal_action: str | None = None,
        recommendation: str | None = None,
        execution_state: str | None = None,
        label_source: str | None = None,
        provenance: str | None = None,
    ) -> list[dict]:
        """Fetch one outcome row per event for the requested cohort.

        The date predicate is intentionally on ``decision_events.scan_date``
        (the decision cohort), never on the observed exit date.  This avoids
        survivorship-style drift when a later report is requested.
        """
        start_date, end_date = self._date_window(days)
        clauses = [
            "e.scan_date <= ?",
        ]
        # The horizon predicate belongs in the LEFT JOIN.  That way a
        # decision event with no label yet is still counted as ``unlabelled``
        # and capture coverage cannot look better merely because the row is
        # missing from decision_outcomes.
        params: list[Any] = [horizon, end_date]
        if start_date is not None:
            clauses.append("e.scan_date >= ?")
            params.append(start_date)
        if market:
            clauses.append("UPPER(e.market) = ?")
            params.append(market)
        if signal_action:
            clauses.append("UPPER(COALESCE(e.signal_action, '')) = ?")
            params.append(signal_action.upper())
        if recommendation:
            clauses.append("UPPER(COALESCE(e.recommendation, '')) = ?")
            params.append(recommendation.upper())
        if execution_state:
            clauses.append("UPPER(COALESCE(e.execution_state, '')) = ?")
            params.append(execution_state.upper())
        if label_source:
            clauses.append("COALESCE(o.label_source, '') = ?")
            params.append(label_source)
        if provenance:
            clauses.append("COALESCE(e.provenance_json, '') LIKE ?")
            params.append(f"%{provenance}%")

        query = f"""
            SELECT
                o.id AS outcome_id,
                o.decision_event_id,
                o.horizon_days,
                o.status,
                o.entry_date,
                o.exit_date,
                o.raw_return_pct,
                o.benchmark_return_pct,
                o.excess_return_pct,
                o.transaction_cost_pct,
                o.net_return_pct,
                o.mfe_pct,
                o.mae_pct,
                o.price_source,
                o.benchmark_source,
                o.label_source,
                o.reason,
                e.ticker,
                e.ticker_name,
                e.market,
                e.signal_action,
                e.recommendation,
                e.execution_state,
                e.scan_date,
                e.composite_score,
                e.provenance_json,
                e.data_quality_json
            FROM decision_events e
            LEFT JOIN decision_outcomes o
              ON e.id = o.decision_event_id AND o.horizon_days = ?
            WHERE {' AND '.join(clauses)}
            ORDER BY e.scan_date ASC, e.id ASC
        """
        with get_db() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _status_counts(rows: Iterable[dict]) -> dict[str, int]:
        counts = Counter(str(row.get("status") or "unlabelled") for row in rows)
        # Stable keys make the API easier for chart consumers and avoid
        # missing-key branching when a cohort has no invalid rows.
        return {
            "total": int(sum(counts.values())),
            "evaluated": int(counts.get("evaluated", 0)),
            "pending": int(counts.get("pending", 0)),
            "unavailable": int(counts.get("unavailable", 0)),
            "invalid": int(counts.get("invalid", 0)),
            "unlabelled": int(counts.get("unlabelled", 0)),
        }

    @staticmethod
    def _coverage(status_counts: dict[str, int]) -> float:
        total = status_counts.get("total", 0)
        if not total:
            return 0.0
        return round(status_counts.get("evaluated", 0) * 100.0 / total, 4)

    @staticmethod
    def _evaluated_stats(rows: list[dict]) -> dict:
        evaluated = [row for row in rows if row.get("status") == "evaluated"]
        net = [float(row["net_return_pct"]) for row in evaluated
               if row.get("net_return_pct") is not None]
        raw = [float(row["raw_return_pct"]) for row in evaluated
               if row.get("raw_return_pct") is not None]
        excess = [float(row["excess_return_pct"]) for row in evaluated
                  if row.get("excess_return_pct") is not None]
        mfe = [float(row["mfe_pct"]) for row in evaluated if row.get("mfe_pct") is not None]
        mae = [float(row["mae_pct"]) for row in evaluated if row.get("mae_pct") is not None]
        wins = sum(1 for value in net if value > 0.0)
        losses = sum(1 for value in net if value <= 0.0)
        n = len(net)
        return {
            "n": n,
            "evaluated_count": len(evaluated),
            "win_count": wins,
            "loss_count": losses,
            "win_rate_pct": _round(wins * 100.0 / n if n else 0.0),
            "avg_raw_return_pct": _round(sum(raw) / len(raw) if raw else None),
            "avg_net_return_pct": _round(sum(net) / len(net) if net else None),
            "median_net_return_pct": _round(median(net) if net else None),
            "avg_excess_return_pct": _round(sum(excess) / len(excess) if excess else None),
            # Arithmetic event-return sum only; this is intentionally not
            # presented as portfolio/cumulative return because cohorts can
            # overlap in both ticker and horizon.
            "sum_net_return_pct": _round(sum(net) if net else None),
            "avg_mfe_pct": _round(sum(mfe) / len(mfe) if mfe else None),
            "avg_mae_pct": _round(sum(mae) / len(mae) if mae else None),
            # This is a descriptive reliability flag only.  It is not a
            # statistical significance claim; overlapping horizons are not
            # independent observations.
            "reliable": n >= 30,
        }

    @staticmethod
    def _group_key(row: dict) -> tuple[str, str]:
        return (
            str(row.get("market") or "UNKNOWN").upper(),
            str(row.get("signal_action") or "UNKNOWN").upper(),
        )

    def _market_action_breakdown(self, rows: list[dict]) -> list[dict]:
        groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in rows:
            groups[self._group_key(row)].append(row)
        result = []
        for (market, action), group in sorted(groups.items()):
            status = self._status_counts(group)
            stats = self._evaluated_stats(group)
            stats.update({
                "market": market,
                "action": action,
                "status_counts": status,
                "coverage_pct": self._coverage(status),
                "pending_count": status["pending"],
                "invalid_count": status["invalid"],
                "unavailable_count": status["unavailable"],
                "unlabelled_count": status["unlabelled"],
            })
            result.append(stats)
        return result

    @staticmethod
    def _data_lineage(rows: Iterable[dict]) -> dict:
        """Expose label/provider provenance alongside descriptive aggregates."""
        rows = list(rows)

        def count_values(key: str) -> dict[str, int]:
            return dict(sorted(Counter(
                str(row.get(key) or "unknown") for row in rows
            ).items()))

        present = sum(1 for row in rows if row.get("provenance_json"))
        return {
            "label_source_counts": count_values("label_source"),
            "price_source_counts": count_values("price_source"),
            "benchmark_source_counts": count_values("benchmark_source"),
            "provenance_present_count": present,
            "provenance_missing_count": max(0, len(rows) - present),
        }

    def _base_filters(
        self,
        *,
        horizon: int,
        days: int | None,
        market: str | None,
        signal_action: str | None,
        recommendation: str | None,
        execution_state: str | None,
        label_source: str | None = None,
        provenance: str | None = None,
    ) -> dict:
        return {
            "horizon_days": horizon,
            "days": days,
            "market": market,
            "signal_action": signal_action.upper() if signal_action else None,
            "recommendation": recommendation.upper() if recommendation else None,
            "execution_state": execution_state.upper() if execution_state else None,
            "label_source": label_source,
            "provenance": provenance,
            "cohort_start": self._date_window(days)[0],
            "cohort_end": self._date_window(days)[1],
        }

    def get_summary(
        self,
        horizon: int = 5,
        days: int | None = 30,
        market: str | None = None,
        *,
        horizon_days: int | None = None,
        signal_action: str | None = None,
        recommendation: str | None = None,
        execution_state: str | None = None,
        label_source: str | None = None,
        provenance: str | None = None,
    ) -> dict:
        """Return outcome totals, returns, coverage, and market/action groups."""
        horizon = self._validate_horizon(horizon, horizon_days)
        days = self._validate_days(days)
        market = self._normalise_market(market)
        rows = self._fetch_rows(
            horizon=horizon,
            days=days,
            market=market,
            signal_action=signal_action,
            recommendation=recommendation,
            execution_state=execution_state,
            label_source=label_source,
            provenance=provenance,
        )
        status = self._status_counts(rows)
        stats = self._evaluated_stats(rows)
        benchmark_rows = [row for row in rows if row.get("status") == "evaluated"]
        benchmark_observed = sum(
            1 for row in benchmark_rows if row.get("benchmark_return_pct") is not None
        )
        stats.update({
            "status_counts": status,
            "coverage_pct": self._coverage(status),
            "pending_count": status["pending"],
            "invalid_count": status["invalid"],
            "unavailable_count": status["unavailable"],
            "unlabelled_count": status["unlabelled"],
            "benchmark_observed_count": benchmark_observed,
            "benchmark_coverage_pct": _round(
                benchmark_observed * 100.0 / len(benchmark_rows)
                if benchmark_rows else 0.0
            ),
            "data_lineage": self._data_lineage(rows),
        })
        breakdown = self._market_action_breakdown(rows)
        return {
            "filters": self._base_filters(
                horizon=horizon,
                days=days,
                market=market,
                signal_action=signal_action,
                recommendation=recommendation,
                execution_state=execution_state,
                label_source=label_source,
                provenance=provenance,
            ),
            "summary": stats,
            "market_action_breakdown": breakdown,
            # Alias retained for small clients that used the shorter name in
            # early P2 development builds.
            "breakdown": breakdown,
            "pending_counts": status,
        }

    @staticmethod
    def _bucket_for(score_percent: float | None) -> tuple[str, float, float, bool] | None:
        if score_percent is None:
            return None
        for bucket in SCORE_BUCKETS:
            label, lower, upper, inclusive_upper = bucket
            if lower <= score_percent < upper or (
                inclusive_upper and lower <= score_percent <= upper
            ):
                return bucket
        return None

    def get_calibration(
        self,
        horizon: int = 5,
        days: int | None = 30,
        market: str | None = None,
        *,
        horizon_days: int | None = None,
        signal_action: str | None = None,
        recommendation: str | None = None,
        execution_state: str | None = None,
        label_source: str | None = None,
        provenance: str | None = None,
    ) -> dict:
        """Return descriptive score-response buckets and label coverage.

        The endpoint is named ``calibration`` for dashboard compatibility,
        but composite scores are ranking scores, not probabilities.  The
        response therefore reports hit-rate/returns and a simple ``reliable``
        sample-size flag instead of claiming statistical calibration.
        """
        horizon = self._validate_horizon(horizon, horizon_days)
        days = self._validate_days(days)
        market = self._normalise_market(market)
        rows = self._fetch_rows(
            horizon=horizon,
            days=days,
            market=market,
            signal_action=signal_action,
            recommendation=recommendation,
            execution_state=execution_state,
            label_source=label_source,
            provenance=provenance,
        )
        status = self._status_counts(rows)
        buckets: list[dict] = []
        for label, lower, upper, inclusive_upper in SCORE_BUCKETS:
            group = []
            for row in rows:
                score = _score_percent(row.get("composite_score"))
                bucket = self._bucket_for(score)
                if bucket and bucket[0] == label:
                    group.append(row)
            group_status = self._status_counts(group)
            item = self._evaluated_stats(group)
            item.update({
                "bucket": label,
                "score_min_pct": lower,
                "score_max_pct": upper,
                "score_max_inclusive": inclusive_upper,
                "status_counts": group_status,
                "coverage_pct": self._coverage(group_status),
                "pending_count": group_status["pending"],
                "invalid_count": group_status["invalid"],
                "unavailable_count": group_status["unavailable"],
                "unlabelled_count": group_status["unlabelled"],
            })
            buckets.append(item)

        unscored = sum(
            1 for row in rows if self._bucket_for(_score_percent(row.get("composite_score"))) is None
        )
        return {
            "filters": self._base_filters(
                horizon=horizon,
                days=days,
                market=market,
                signal_action=signal_action,
                recommendation=recommendation,
                execution_state=execution_state,
                label_source=label_source,
                provenance=provenance,
            ),
            "buckets": buckets,
            "score_buckets": buckets,
            "data_lineage": self._data_lineage(rows),
            "unscored_count": unscored,
            "pending_counts": status,
            "coverage_pct": self._coverage(status),
            "evaluated_count": status["evaluated"],
            "pending_count": status["pending"],
            "invalid_count": status["invalid"],
            "unavailable_count": status["unavailable"],
            "unlabelled_count": status["unlabelled"],
        }

    # Friendly aliases for callers that prefer noun-style methods.
    def summary(self, *args, **kwargs) -> dict:
        return self.get_summary(*args, **kwargs)

    def calibration(self, *args, **kwargs) -> dict:
        return self.get_calibration(*args, **kwargs)
