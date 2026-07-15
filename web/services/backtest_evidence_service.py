"""Read-only reconciliation of strategy claims and stored validation evidence.

This service deliberately never promotes a strategy or rewrites historical
results.  It reports whether the current YAML/DB status claim is supported by
the latest persisted backtest and walk-forward rows.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone

from strategy.registry import StrategyRegistry
from utils.config_loader import load_config
from web.db.connection import get_db


VALIDATED_STATUSES = frozenset({"validated", "validated_v2"})


class BacktestEvidenceService:
    """Build a deterministic, non-mutating validation-readiness report."""

    def __init__(
        self,
        registry: StrategyRegistry | None = None,
        *,
        min_tested_tickers: int = 5,
        min_walk_forward_tickers: int = 1,
        min_walk_forward_windows: int = 3,
        min_ticker_pass_rate: float | None = None,
    ):
        self.registry = registry or StrategyRegistry()
        self.min_tested_tickers = max(int(min_tested_tickers), 1)
        self.min_walk_forward_tickers = max(int(min_walk_forward_tickers), 1)
        self.min_walk_forward_windows = max(int(min_walk_forward_windows), 1)
        if min_ticker_pass_rate is None:
            try:
                thresholds = load_config().get("backtest", {}).get(
                    "validation_thresholds", {}
                )
                min_ticker_pass_rate = float(
                    thresholds.get("min_ticker_pass_rate", 0.20)
                )
            except Exception:
                min_ticker_pass_rate = 0.20
        self.min_ticker_pass_rate = min(max(float(min_ticker_pass_rate), 0.0), 1.0)

    @staticmethod
    def _latest_by_key(rows: list[dict], key_fields: tuple[str, ...]) -> dict[str, dict]:
        """Keep the newest row for each strategy/evidence key.

        Queries are ordered oldest-first, so assignment naturally replaces a
        stale append-only record with the latest one.
        """
        latest: dict[str, dict] = defaultdict(dict)
        for row in rows:
            name = row.get("strategy_name")
            if not name:
                continue
            key = tuple((row.get(field) or "") for field in key_fields)
            latest[name][key] = row
        return dict(latest)

    def _load_strategies(self) -> tuple[list, list[dict]]:
        strategies = []
        errors = []
        seen = set()
        for registry_name in self.registry.list_strategies():
            try:
                strategy = self.registry.load(registry_name)
            except Exception as exc:
                errors.append({"registry_name": registry_name, "error": str(exc)})
                continue
            if strategy.name in seen:
                continue
            seen.add(strategy.name)
            strategies.append(strategy)
        return strategies, errors

    def build_report(self, *, include_unclaimed: bool = True) -> dict:
        """Return YAML/DB/backtest/walk-forward evidence without changing state."""
        strategies, load_errors = self._load_strategies()

        with get_db() as db:
            db_statuses = {
                row["name"]: row["status"]
                for row in db.execute("SELECT name, status FROM strategies").fetchall()
            }
            backtest_rows = [
                dict(row)
                for row in db.execute(
                    """SELECT id, strategy_name, ticker, market, is_valid,
                              avg_holding_days, annual_trade_rate,
                              validation_policy_json, created_at
                       FROM backtest_results
                       ORDER BY created_at, id"""
                ).fetchall()
            ]
            walk_forward_rows = [
                dict(row)
                for row in db.execute(
                    """SELECT id, strategy_name, ticker, market, total_windows,
                              valid_windows, is_overfit, overfit_reason,
                              min_windows, created_at
                       FROM walk_forward_results
                       ORDER BY created_at, id"""
                ).fetchall()
            ]

        bt_history = Counter(row.get("strategy_name") for row in backtest_rows)
        wf_history = Counter(row.get("strategy_name") for row in walk_forward_rows)
        latest_backtests = self._latest_by_key(backtest_rows, ("market", "ticker"))
        latest_walk_forward = self._latest_by_key(
            walk_forward_rows, ("market", "ticker")
        )

        report_rows = []
        for strategy in strategies:
            claims_validation = strategy.status in VALIDATED_STATUSES
            if not include_unclaimed and not claims_validation:
                continue

            bt_rows = list(latest_backtests.get(strategy.name, {}).values())
            valid_bt = [row for row in bt_rows if int(row.get("is_valid") or 0) == 1]
            audited_bt = [
                row
                for row in bt_rows
                if row.get("avg_holding_days") is not None
                and row.get("annual_trade_rate") is not None
                and bool(row.get("validation_policy_json"))
            ]
            tested_count = len(bt_rows)
            valid_count = len(valid_bt)
            pass_rate = valid_count / tested_count if tested_count else 0.0
            policy_auditable = len(audited_bt) == tested_count and tested_count > 0
            backtest_supported = (
                tested_count >= self.min_tested_tickers
                and pass_rate >= self.min_ticker_pass_rate
                and policy_auditable
            )

            wf_rows = list(latest_walk_forward.get(strategy.name, {}).values())
            usable_wf = []
            for row in wf_rows:
                required_windows = int(
                    row.get("min_windows") or self.min_walk_forward_windows
                )
                if int(row.get("valid_windows") or 0) >= required_windows:
                    usable_wf.append(row)
            overfit_wf = [row for row in usable_wf if int(row.get("is_overfit") or 0) == 1]
            passing_wf = [row for row in usable_wf if int(row.get("is_overfit") or 0) == 0]
            walk_forward_supported = (
                len(passing_wf) >= self.min_walk_forward_tickers
                and not overfit_wf
            )

            db_status = db_statuses.get(strategy.name)
            status_match = db_status == strategy.status
            reasons = []
            if not claims_validation:
                evidence_state = "not_claimed"
                reasons.append(f"YAML status is {strategy.status!r}, not a validation claim")
            elif not status_match:
                evidence_state = "status_mismatch"
                reasons.append(
                    f"YAML status {strategy.status!r} differs from DB status {db_status!r}"
                )
            elif not bt_rows:
                evidence_state = "missing_backtest"
                reasons.append("no persisted backtest result exists")
            elif tested_count < self.min_tested_tickers:
                evidence_state = "insufficient_backtest_coverage"
                reasons.append(
                    f"latest backtests cover {tested_count} tickers; "
                    f"minimum is {self.min_tested_tickers}"
                )
            elif pass_rate < self.min_ticker_pass_rate:
                evidence_state = "backtest_below_threshold"
                reasons.append(
                    f"recorded pass rate {pass_rate:.1%} is below "
                    f"{self.min_ticker_pass_rate:.1%}"
                )
            elif not policy_auditable:
                evidence_state = "legacy_backtest_evidence"
                reasons.append(
                    f"validation policy metadata exists for {len(audited_bt)}/"
                    f"{tested_count} latest backtest result(s)"
                )
            elif not wf_rows:
                evidence_state = "missing_walk_forward"
                reasons.append("no persisted walk-forward result exists")
            elif overfit_wf:
                evidence_state = "walk_forward_overfit"
                reasons.append(
                    f"{len(overfit_wf)} usable latest walk-forward result(s) flag overfit"
                )
            elif len(passing_wf) < self.min_walk_forward_tickers:
                evidence_state = "insufficient_walk_forward"
                reasons.append(
                    f"usable non-overfit walk-forward coverage is {len(passing_wf)}; "
                    f"minimum is {self.min_walk_forward_tickers}"
                )
            else:
                evidence_state = "ready"

            report_rows.append(
                {
                    "name": strategy.name,
                    "market": strategy.market,
                    "category": strategy.category,
                    "strategy_type": strategy.strategy_type,
                    "yaml_status": strategy.status,
                    "db_status": db_status,
                    "status_match": status_match,
                    "claims_validation": claims_validation,
                    "evidence_state": evidence_state,
                    "evidence_ready": evidence_state == "ready",
                    "reasons": reasons,
                    "backtest": {
                        "history_rows": bt_history.get(strategy.name, 0),
                        "latest_results": tested_count,
                        "valid_latest_results": valid_count,
                        "invalid_latest_results": tested_count - valid_count,
                        "pass_rate": round(pass_rate, 6),
                        "policy_auditable_latest_results": len(audited_bt),
                        "supported": backtest_supported,
                        "latest_at": max(
                            (row.get("created_at") or "" for row in bt_rows),
                            default=None,
                        ),
                    },
                    "walk_forward": {
                        "history_rows": wf_history.get(strategy.name, 0),
                        "latest_results": len(wf_rows),
                        "usable_latest_results": len(usable_wf),
                        "passing_latest_results": len(passing_wf),
                        "overfit_latest_results": len(overfit_wf),
                        "supported": walk_forward_supported,
                        "latest_at": max(
                            (row.get("created_at") or "" for row in wf_rows),
                            default=None,
                        ),
                    },
                }
            )

        state_counts = Counter(row["evidence_state"] for row in report_rows)
        validation_rows = [row for row in report_rows if row["claims_validation"]]
        yaml_names = {strategy.name for strategy in strategies}

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "policy": {
                "min_tested_tickers": self.min_tested_tickers,
                "min_ticker_pass_rate": self.min_ticker_pass_rate,
                "min_walk_forward_tickers": self.min_walk_forward_tickers,
                "default_min_walk_forward_windows": self.min_walk_forward_windows,
                "latest_result_per_market_ticker": True,
            },
            "summary": {
                "yaml_strategies": len(strategies),
                "reported_strategies": len(report_rows),
                "validation_claims": len(validation_rows),
                "evidence_ready": sum(row["evidence_ready"] for row in validation_rows),
                "attention_required": sum(
                    not row["evidence_ready"] for row in validation_rows
                ),
                "status_mismatches": sum(not row["status_match"] for row in report_rows),
                "factor_drafts_without_backtests": sum(
                    row["strategy_type"] == "factor"
                    and row["yaml_status"] == "draft"
                    and row["backtest"]["latest_results"] == 0
                    for row in report_rows
                ),
                "states": dict(sorted(state_counts.items())),
                "orphaned_backtest_strategy_names": sorted(
                    set(latest_backtests) - yaml_names
                ),
                "orphaned_walk_forward_strategy_names": sorted(
                    set(latest_walk_forward) - yaml_names
                ),
                "strategy_load_errors": load_errors,
            },
            "limitations": [
                "readiness is evidence reporting only and never changes a strategy status",
                "legacy backtest rows without validation_policy_json are counted as recorded but not policy-auditable",
                "a walk-forward row is usable only when valid_windows reaches its recorded min_windows policy",
            ],
            "strategies": sorted(report_rows, key=lambda row: row["name"]),
        }
