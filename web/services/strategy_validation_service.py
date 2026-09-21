"""Read-only strategy validation centre.

This service combines immutable decision outcomes with persisted backtest and
walk-forward evidence.  It intentionally does not change strategy status,
weights, or scanner behaviour: the initial rollout is a shadow evaluation.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any

from utils.config_loader import load_config
from web.db.connection import get_db
from web.services.backtest_evidence_service import BacktestEvidenceService


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

    def _forward_rows(self, horizon: int, market: str | None) -> list[dict]:
        clauses = ["o.horizon_days = ?"]
        params: list[Any] = [horizon]
        if market:
            clauses.append("UPPER(e.market) = ?")
            params.append(market)
        query = f"""
            SELECT s.strategy_name, UPPER(e.market) AS market, o.status,
                   o.net_return_pct, o.excess_return_pct, o.price_source,
                   o.benchmark_source
              FROM decision_events e
              JOIN signals s ON s.id = e.signal_id
              JOIN decision_outcomes o ON o.decision_event_id = e.id
             WHERE {' AND '.join(clauses)}
        """
        with get_db() as db:
            return [dict(row) for row in db.execute(query, params).fetchall()]

    @staticmethod
    def _forward_summary(rows: list[dict], requirements: dict) -> dict:
        status = Counter(str(row.get("status") or "unlabelled") for row in rows)
        evaluated = [row for row in rows if row.get("status") == "evaluated"]
        evaluated_count = len(evaluated)
        total = len(rows)
        coverage = round(evaluated_count * 100.0 / total, 4) if total else 0.0
        avg_net = _finite_average([row.get("net_return_pct") for row in evaluated])
        avg_excess = _finite_average([row.get("excess_return_pct") for row in evaluated])
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
        evidence = evidence_service.build_report(include_unclaimed=True)
        forward_by_name: dict[str, list[dict]] = defaultdict(list)
        for row in self._forward_rows(horizon, market):
            forward_by_name[str(row["strategy_name"] or "")].append(row)

        strategies = []
        for item in evidence["strategies"]:
            strategy_market = str(item.get("market") or "ALL").upper()
            if market and strategy_market not in {"ALL", market}:
                continue
            forward = self._forward_summary(
                forward_by_name.get(item["name"], []), forward_requirements
            )
            evidence_ready = bool(item.get("evidence_ready"))
            forward_ready = bool(forward["meets_forward_gate"])
            reasons = list(item.get("reasons") or [])
            if not forward_ready:
                reasons.append("forward outcome gate is not yet satisfied")
            strategies.append({
                "name": item["name"],
                "market": strategy_market,
                "category": item.get("category"),
                "strategy_type": item.get("strategy_type"),
                "yaml_status": item.get("yaml_status"),
                "evidence_state": item.get("evidence_state"),
                "evidence_ready": evidence_ready,
                "forward": forward,
                "activation_ready": evidence_ready and forward_ready,
                "reasons": reasons,
            })

        strategies.sort(key=lambda row: (not row["activation_ready"], row["name"]))
        rollout = policy.get("rollout", {})
        return {
            "policy": {
                "version": policy.get("version", "unversioned"),
                "rollout_mode": rollout.get("mode", "shadow"),
                "shadow_days": int(rollout.get("shadow_days", 28)),
                "horizon_days": horizon,
                "forward_requirements": forward_requirements,
                "evidence_requirements": evidence_policy,
            },
            "summary": {
                "strategies": len(strategies),
                "activation_ready": sum(row["activation_ready"] for row in strategies),
                "evidence_ready": sum(row["evidence_ready"] for row in strategies),
                "forward_ready": sum(row["forward"]["meets_forward_gate"] for row in strategies),
                "shadow_only": rollout.get("mode", "shadow") == "shadow",
            },
            "strategies": strategies,
        }
