"""Read-only reconciliation of strategy claims and stored validation evidence.

This service deliberately never promotes a strategy or rewrites historical
results.  It reports whether the current YAML/DB status claim is supported by
the latest persisted backtest and walk-forward rows.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone

from backtester.engine import EXECUTION_MODEL, WARMUP_BARS
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

    @staticmethod
    def _execution_payload(raw: str | None, market: str) -> dict:
        try:
            payload = json.loads(raw or "null")
        except (TypeError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        execution = payload.get("execution")
        if (
            not isinstance(execution, dict)
            or execution.get("model") != EXECUTION_MODEL
            or execution.get("market") != market
            or not isinstance(execution.get("warmup_bars"), int)
            or execution["warmup_bars"] < WARMUP_BARS
        ):
            return {}
        return payload

    def _valid_walk_forward_count(self, row: dict, market: str) -> int:
        windows = self._execution_payload(row.get("windows_json"), market).get("windows")
        if not isinstance(windows, list) or len(windows) != row["total_windows"]:
            return 0
        closed_trade_windows = sum(
            isinstance(window, dict)
            and window.get("valid") is True
            and isinstance(window.get("train_trades"), int) and window["train_trades"] > 0
            and isinstance(window.get("test_trades"), int) and window["test_trades"] > 0
            for window in windows
        )
        return closed_trade_windows if closed_trade_windows == row["valid_windows"] else 0

    def _assess_market(self, strategy, db_status, market, bt_rows, wf_rows, bt_history, wf_history):
        tested_count = len(bt_rows)
        valid_count = sum(
            row["is_valid"] == 1 and (row["num_trades"] or 0) > 0 for row in bt_rows
        )
        audited_bt = [
            row for row in bt_rows
            if row["avg_holding_days"] is not None
            and row["annual_trade_rate"] is not None
            and self._execution_payload(row["validation_policy_json"], market)
        ]
        pass_rate = valid_count / tested_count if tested_count else 0.0
        policy_auditable = len(audited_bt) == tested_count and tested_count > 0
        backtest_supported = (
            tested_count >= self.min_tested_tickers
            and pass_rate >= self.min_ticker_pass_rate
            and valid_count > 0
            and policy_auditable
        )
        usable_wf = [
            row for row in wf_rows
            if self._valid_walk_forward_count(row, market) >= max(
                self.min_walk_forward_windows, row["min_windows"] or 0,
            )
        ]
        overfit_wf = [row for row in usable_wf if row["is_overfit"] == 1]
        passing_wf = [row for row in usable_wf if row["is_overfit"] == 0]
        walk_forward_supported = len(passing_wf) >= self.min_walk_forward_tickers and not overfit_wf

        reason = ""
        if strategy.status not in VALIDATED_STATUSES:
            state = "not_claimed"
            reason = f"YAML status {strategy.status!r} does not claim validation"
        elif db_status != strategy.status:
            state = "status_mismatch"
            reason = f"YAML status {strategy.status!r} differs from DB status {db_status!r}"
        elif not bt_rows:
            state = "missing_backtest"
            reason = f"no persisted {market} backtest result exists"
        elif tested_count < self.min_tested_tickers:
            state = "insufficient_backtest_coverage"
            reason = f"{market} backtests cover {tested_count} tickers, minimum {self.min_tested_tickers}"
        elif pass_rate < self.min_ticker_pass_rate or not valid_count:
            state = "backtest_below_threshold"
            reason = f"{market} pass rate is {pass_rate:.1%}, minimum {self.min_ticker_pass_rate:.1%} with a passing ticker"
        elif not policy_auditable:
            state = "legacy_backtest_evidence"
            reason = f"current {market} execution policy exists for {len(audited_bt)}/{tested_count} latest backtests"
        elif not wf_rows:
            state = "missing_walk_forward"
            reason = f"no persisted {market} walk-forward result exists"
        elif overfit_wf:
            state = "walk_forward_overfit"
            reason = f"{len(overfit_wf)} usable {market} walk-forward result(s) flag overfit"
        elif len(passing_wf) < self.min_walk_forward_tickers:
            state = "insufficient_walk_forward"
            reason = (f"usable {market} walk-forward coverage is {len(passing_wf)}, "
                      f"minimum {self.min_walk_forward_tickers}")
        else:
            state = "ready"

        return {
            "evidence_state": state,
            "evidence_ready": state == "ready",
            "reasons": [reason] if reason else [],
            "backtest": {
                "history_rows": bt_history,
                "latest_results": tested_count,
                "valid_latest_results": valid_count,
                "invalid_latest_results": tested_count - valid_count,
                "pass_rate": round(pass_rate, 6),
                "policy_auditable_latest_results": len(audited_bt),
                "supported": backtest_supported,
                "latest_at": max((row["created_at"] or "" for row in bt_rows), default=None),
            },
            "walk_forward": {
                "history_rows": wf_history,
                "latest_results": len(wf_rows),
                "usable_latest_results": len(usable_wf),
                "passing_latest_results": len(passing_wf),
                "overfit_latest_results": len(overfit_wf),
                "supported": walk_forward_supported,
                "latest_at": max((row["created_at"] or "" for row in wf_rows), default=None),
            },
        }

    def build_report(self, *, include_unclaimed: bool = True, market: str | None = None) -> dict:
        """Return YAML/DB/backtest/walk-forward evidence without changing state."""
        if market is not None and market not in {"KRX", "US"}:
            raise ValueError("market must be KRX or US")
        strategies, load_errors = self._load_strategies()

        with get_db() as db:
            db_statuses = {
                row["name"]: row["status"]
                for row in db.execute("SELECT name, status FROM strategies").fetchall()
            }
            backtest_rows = [
                dict(row)
                for row in db.execute(
                    """SELECT id, strategy_name, ticker, UPPER(market) AS market, is_valid, num_trades,
                              avg_holding_days, annual_trade_rate,
                              validation_policy_json, created_at
                       FROM backtest_results
                       ORDER BY created_at, id"""
                ).fetchall()
            ]
            walk_forward_rows = [
                dict(row)
                for row in db.execute(
                    """SELECT id, strategy_name, ticker, UPPER(market) AS market, total_windows, windows_json,
                              valid_windows, is_overfit, overfit_reason,
                              min_windows, created_at
                       FROM walk_forward_results
                       ORDER BY created_at, id"""
                ).fetchall()
            ]

        bt_history = Counter((row["strategy_name"], row["market"]) for row in backtest_rows)
        wf_history = Counter((row["strategy_name"], row["market"]) for row in walk_forward_rows)
        latest_backtests = self._latest_by_key(backtest_rows, ("market", "ticker"))
        latest_walk_forward = self._latest_by_key(
            walk_forward_rows, ("market", "ticker")
        )

        report_rows = []
        for strategy in strategies:
            claims_validation = strategy.status in VALIDATED_STATUSES
            if not include_unclaimed and not claims_validation:
                continue

            strategy_market = strategy.market.upper()
            allowed_markets = (strategy_market,) if strategy_market in {"KRX", "US"} else ("KRX", "US")
            markets = [venue for venue in allowed_markets if market is None or venue == market]
            if not markets:
                continue

            db_status = db_statuses.get(strategy.name)
            by_market = {}
            for venue in markets:
                bt_rows = [row for key, row in latest_backtests.get(strategy.name, {}).items()
                           if key[0] == venue]
                wf_rows = [row for key, row in latest_walk_forward.get(strategy.name, {}).items()
                           if key[0] == venue]
                by_market[venue] = self._assess_market(
                    strategy, db_status, venue, bt_rows, wf_rows,
                    bt_history[(strategy.name, venue)], wf_history[(strategy.name, venue)],
                )
            evidence_market = max(markets, key=lambda venue: (
                by_market[venue]["evidence_ready"],
                by_market[venue]["backtest"]["supported"],
                by_market[venue]["backtest"]["latest_results"],
                by_market[venue]["walk_forward"]["usable_latest_results"],
            ))
            report_rows.append({
                "name": strategy.name,
                "market": strategy.market,
                "category": strategy.category,
                "strategy_type": strategy.strategy_type,
                "yaml_status": strategy.status,
                "db_status": db_status,
                "status_match": db_status == strategy.status,
                "claims_validation": claims_validation,
                "evidence_market": evidence_market,
                "evidence_by_market": by_market,
                **by_market[evidence_market],
            })

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
                "readiness requires current execution evidence within a single market",
            ],
            "strategies": sorted(report_rows, key=lambda row: row["name"]),
        }
