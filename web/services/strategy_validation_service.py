"""Read-only strategy validation centre.

This service combines immutable decision outcomes with persisted backtest and
walk-forward evidence.  It intentionally does not change strategy status,
weights, or scanner behaviour: the initial rollout is a shadow evaluation.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from typing import Any

import pandas as pd

from utils.config_loader import load_config
from web.db.connection import get_db
from web.services.backtest_evidence_service import BacktestEvidenceService
from web.services.decision_label_service import (
    EXCHANGES, KST, LABEL_TABLE, LABEL_VERSION, SHADOW_LABEL_TABLE, exchange_schedule,
)


def _finite_average(values: list[Any]) -> float | None:
    cleaned = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            cleaned.append(number)
    return round(sum(cleaned) / len(cleaned), 4) if cleaned else None


class StrategyValidationService:
    """Build a market-aware, non-mutating validation overview."""

    def __init__(self, evidence_service: BacktestEvidenceService | None = None):
        self.evidence_service = evidence_service

    @staticmethod
    def _policy() -> dict:
        try:
            return load_config("validation.yaml") or {}
        except (FileNotFoundError, ValueError):
            return {}

    @staticmethod
    def _normalise_market(market: str | None) -> str | None:
        if market is None:
            return None
        value = str(market).strip().upper()
        if value and value not in {"KRX", "US"}:
            raise ValueError("market must be KRX or US")
        return value or None

    @staticmethod
    def _normalise_horizon(horizon: int) -> int:
        try:
            horizon = int(horizon)
        except (TypeError, ValueError) as exc:
            raise ValueError("horizon must be one of 5, 10, 20") from exc
        if horizon not in {5, 10, 20}:
            raise ValueError("horizon must be one of 5, 10, 20")
        return horizon

    @staticmethod
    def _has_consensus(row: dict) -> bool:
        if (row["consensus_count"] or 0) > 1:
            return True
        try:
            members = json.loads(row["consensus_strategies_json"] or "null")
        except (TypeError, ValueError):
            return True  # Unknown membership cannot substantiate an individual claim.
        return members not in (None, [])

    @staticmethod
    def _price_windows(rows: list[dict]) -> dict:
        windows = {}
        conflicts = set()
        status_rank = {"evaluated": 3, "invalid": 2, "unavailable": 1, "pending": 0}
        for row in rows:
            if (row.get("signal_action") != "BUY" or not row["entry_date"]
                    or not row["exit_date"] or row["status"] not in status_rank):
                continue
            key = (row["market"], row["ticker"], row["entry_date"], row["exit_date"])
            previous = windows.get(key)
            if previous and previous["status"] == row["status"] == "evaluated":
                measures = ("net_return_pct", "excess_return_pct", "benchmark_source")
                if any(previous[field] != row[field] for field in measures):
                    conflicts.add(key)
            if previous is None or status_rank[row["status"]] > status_rank[previous["status"]]:
                windows[key] = row
        for key in conflicts:
            windows[key] = {"status": "unavailable"}
        return windows

    @staticmethod
    def _own_signal_outcomes(signals: list[dict], windows: dict, horizon: int) -> dict:
        decisions = defaultdict(list)
        by_market = defaultdict(list)
        for row in signals:
            if row["market"] in EXCHANGES:
                by_market[row["market"]].append(row)
        for market, rows in by_market.items():
            frame = pd.DataFrame(rows)
            timestamps = frame["detected_at"].astype("string")
            timestamps = timestamps.where(timestamps.str[10].isin({" ", "T"}))
            frame["observed_at"] = pd.to_datetime(timestamps, utc=True, format="mixed", errors="coerce")
            frame["session_day"] = frame["observed_at"].dt.tz_convert(KST).dt.date
            frame = frame.sort_values("observed_at", kind="stable").drop_duplicates(
                ["strategy_name", "ticker", "session_day"], keep="first",
            )
            schedule = exchange_schedule(market)
            opens = schedule["open"].searchsorted(frame["observed_at"], side="right")
            session_dates = schedule.index.strftime("%Y-%m-%d")
            for row, entry in zip(frame.itertuples(index=False), opens):
                exit_pos = entry + horizon - 1
                outcome = {"status": "unlabelled"}
                if pd.isna(row.observed_at) or row.observed_at.year < 2000 or exit_pos >= len(schedule):
                    outcome = {"status": "invalid"}
                else:
                    key = (market, row.ticker, session_dates[entry], session_dates[exit_pos])
                    outcome = windows.get(key, outcome)
                decisions[(row.strategy_name, market)].append(outcome)
        return decisions

    def _forward_cohorts(self, horizon: int, market: str | None) -> tuple[dict, dict]:
        ensemble = defaultdict(list)
        market_params = (market,) if market else ()
        market_filter = "AND UPPER(e.market) = ?" if market else ""
        outcome_columns = """
            o.status, o.entry_date, o.exit_date, o.net_return_pct, o.excess_return_pct,
            o.label_version AS price_source, o.benchmark_symbol AS benchmark_source
        """
        with get_db() as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?)",
                (LABEL_TABLE, SHADOW_LABEL_TABLE),
            )}
            if LABEL_TABLE in tables:
                decision_rows = [dict(row) for row in db.execute(f"""
                    SELECT e.ticker, UPPER(e.market) AS market, e.signal_action,
                           e.consensus_count, e.consensus_strategies_json, {outcome_columns}
                      FROM decision_events e
                      LEFT JOIN {LABEL_TABLE} o ON o.decision_event_id = e.id
                           AND o.horizon_days = ? AND o.label_version = ?
                     WHERE e.signal_action IN ('BUY', 'SELL') {market_filter}
                     ORDER BY e.id
                """, (horizon, LABEL_VERSION, *market_params))]
            else:
                decision_rows = [dict(row) for row in db.execute(f"""
                    SELECT e.ticker, UPPER(e.market) AS market, e.signal_action,
                           e.consensus_count, e.consensus_strategies_json,
                           NULL AS status, NULL AS entry_date, NULL AS exit_date
                      FROM decision_events e
                     WHERE e.signal_action IN ('BUY', 'SELL') {market_filter}
                     ORDER BY e.id
                """, market_params)]
            shadow_rows = []
            if SHADOW_LABEL_TABLE in tables:
                shadow_rows = [dict(row) for row in db.execute(f"""
                    SELECT e.ticker, UPPER(e.market) AS market, 'BUY' AS signal_action,
                           {outcome_columns}
                      FROM selection_shadow_candidates e
                      JOIN {SHADOW_LABEL_TABLE} o ON o.shadow_candidate_id = e.id
                     WHERE o.horizon_days = ? AND o.label_version = ? {market_filter}
                     ORDER BY e.id
                """, (horizon, LABEL_VERSION, *market_params))]
            signal_rows = [dict(row) for row in db.execute(f"""
                SELECT e.strategy_name, e.ticker, UPPER(e.market) AS market, e.detected_at
                  FROM signals e
                 WHERE e.signal_type = 'BUY' AND e.strategy_name IS NOT NULL {market_filter}
                 ORDER BY e.id
            """, market_params)]

        for row in decision_rows:
            if self._has_consensus(row):
                ensemble[row["market"]].append(row)
        windows = self._price_windows(decision_rows + shadow_rows)
        return self._own_signal_outcomes(signal_rows, windows, horizon), ensemble

    @staticmethod
    def _forward_summary(rows: list[dict], requirements: dict) -> dict:
        status = Counter(str(row.get("status") or "unlabelled") for row in rows)
        evaluated = [row for row in rows if row.get("status") == "evaluated"]
        comparable = [
            row for row in evaluated
            if _finite_average([row.get("net_return_pct")]) is not None
            and _finite_average([row.get("excess_return_pct")]) is not None
        ]
        evaluated_count = len(comparable)
        total = len(rows)
        coverage = round(evaluated_count * 100.0 / total, 4) if total else 0.0
        avg_net = _finite_average([row.get("net_return_pct") for row in comparable])
        avg_excess = _finite_average([row.get("excess_return_pct") for row in comparable])
        min_n = int(requirements.get("min_evaluated_decisions", 30))
        min_coverage = float(requirements.get("min_label_coverage_pct", 80.0))
        meets = (
            evaluated_count >= min_n
            and coverage >= min_coverage
            and avg_net is not None
            and avg_net >= float(requirements.get("min_avg_net_return_pct", 0.0))
            and avg_excess is not None
            and avg_excess >= float(requirements.get("min_avg_excess_return_pct", 0.0))
        )
        return {
            "total": total,
            "evaluated": evaluated_count,
            "coverage_pct": coverage,
            "avg_net_return_pct": avg_net,
            "avg_excess_return_pct": avg_excess,
            "status_counts": dict(sorted(status.items())),
            "price_sources": dict(sorted(Counter(
                str(row.get("price_source") or "unknown") for row in rows
            ).items())),
            "benchmark_sources": dict(sorted(Counter(
                str(row.get("benchmark_source") or "unknown") for row in rows
            ).items())),
            "evidence_state": (
                "insufficient_independent_evidence" if evaluated_count < min_n
                else "ready" if meets else "below_forward_threshold"
            ),
            "meets_forward_gate": meets,
        }

    def build_overview(self, *, horizon: int = 10, market: str | None = None) -> dict:
        horizon = self._normalise_horizon(horizon)
        market = self._normalise_market(market)
        policy = self._policy()
        forward_requirements = policy.get("forward_evaluation", {})
        evidence_policy = policy.get("evidence", {})
        evidence_service = self.evidence_service or BacktestEvidenceService(
            min_tested_tickers=evidence_policy.get("min_tested_tickers", 5),
            min_ticker_pass_rate=evidence_policy.get("min_ticker_pass_rate", 0.20),
            min_walk_forward_tickers=evidence_policy.get("min_walk_forward_tickers", 1),
            min_walk_forward_windows=evidence_policy.get("min_walk_forward_windows", 3),
        )
        evidence = evidence_service.build_report(include_unclaimed=True, market=market)
        forward_by_market, ensemble_by_market = self._forward_cohorts(horizon, market)

        strategies = []
        for item in evidence["strategies"]:
            strategy_market = str(item.get("market") or "ALL").upper()
            venue_results = {}
            for venue, venue_evidence in item["evidence_by_market"].items():
                forward = self._forward_summary(
                    forward_by_market.get((item["name"], venue), []), forward_requirements,
                )
                evidence_ready = bool(venue_evidence["evidence_ready"])
                forward_ready = bool(forward["meets_forward_gate"])
                reasons = list(venue_evidence["reasons"])
                if forward["evidence_state"] == "insufficient_independent_evidence":
                    reasons.append(f"insufficient independent {venue} strategy outcome labels")
                elif not forward_ready:
                    reasons.append(f"{venue} forward outcome gate is below threshold")
                venue_results[venue] = {
                    "evidence_state": venue_evidence["evidence_state"],
                    "evidence_ready": evidence_ready,
                    "forward": forward,
                    "activation_ready": evidence_ready and forward_ready,
                    "reasons": reasons,
                }
            display_market = max(venue_results, key=lambda venue: (
                venue_results[venue]["activation_ready"],
                venue_results[venue]["evidence_ready"],
                venue_results[venue]["forward"]["meets_forward_gate"],
                venue_results[venue]["forward"]["evaluated"],
                venue == item["evidence_market"],
            ))
            strategies.append({
                "name": item["name"],
                "market": strategy_market,
                "category": item.get("category"),
                "strategy_type": item.get("strategy_type"),
                "yaml_status": item.get("yaml_status"),
                "evaluation_market": display_market,
                "by_market": venue_results,
                **venue_results[display_market],
            })

        strategies.sort(key=lambda row: (not row["activation_ready"], row["name"]))
        rollout = policy.get("rollout", {})
        return {
            "policy": {
                "version": policy.get("version", "unversioned"),
                "rollout_mode": rollout.get("mode", "shadow"),
                "shadow_days": int(rollout.get("shadow_days", 28)),
                "horizon_days": horizon,
                "outcome_label_version": LABEL_VERSION,
                "forward_requirements": forward_requirements,
                "evidence_requirements": evidence_policy,
            },
            "summary": {
                "strategies": len(strategies),
                "activation_ready": sum(row["activation_ready"] for row in strategies),
                "evidence_ready": sum(row["evidence_ready"] for row in strategies),
                "forward_ready": sum(
                    any(venue["forward"]["meets_forward_gate"] for venue in row["by_market"].values())
                    for row in strategies
                ),
                "shadow_only": rollout.get("mode", "shadow") == "shadow",
            },
            "ensemble_forward_by_market": {
                venue: {
                    key: value for key, value in self._forward_summary(rows, forward_requirements).items()
                    if key not in {"evidence_state", "meets_forward_gate"}
                }
                for venue, rows in sorted(ensemble_by_market.items())
            },
            "strategies": strategies,
        }
