"""Read point-in-time BUY selections and compare market rankers in shadow."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from web.db import connection
from web.services.selection_evaluation_metrics import (
    BOOTSTRAP_SAMPLES, TOP_K, baseline_comparison, daily_return, finite, paired_summary, pct, return_summary,
)
from web.services.selection_evaluation_providers import matched_krx_flows, provider_costs, provider_evidence
from web.services.selection_evaluation_scoring import (
    AXES, CANDIDATE_AXES, CANDIDATE_VERSION, FEATURES, axis_availability,
    feature_value, incumbent_score_with_intel, neutralize_axis, prospective_score, score_fraction,
)

__all__ = ["SelectionEvaluationService", "incumbent_score_with_intel", "prospective_score"]

KST = timezone(timedelta(hours=9))
HORIZONS = (5, 10, 20)
PRIMARY_HORIZON = 10
LABEL_VERSION = "next_open_v1"
MARKETS = ("KRX", "US")
SHADOW_DAYS = 28
MIN_COMMON_MATURE_DAYS = 30
MIN_COVERAGE_PCT = 80.0


def _date(value: date | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("dates must use YYYY-MM-DD") from exc


def _timestamp(value) -> datetime | None:
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    # Original immutable events use SQLite datetime('now'), which is UTC.
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def _cutoff(value: date | datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(KST)
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time.max, KST)
    if isinstance(value, str) and len(value) == 10:
        return datetime.combine(_date(value), time.max, KST)
    result = _timestamp(value)
    if result is None:
        raise ValueError("as_of must be an ISO date or timestamp")
    return result.astimezone(KST)


def _object(value) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        value = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _upper(value) -> str:
    return str(value or "").strip().upper()


def _label_values(label: dict | None, cutoff: datetime) -> tuple[float, float] | None:
    if not label or label.get("status") != "evaluated":
        return None
    exit_at = _timestamp(label.get("exit_at"))
    if exit_at is None or exit_at > cutoff:
        return None
    net = finite(label.get("net_return_pct"))
    benchmark = finite(label.get("benchmark_return_pct"))
    if net is None or benchmark is None:
        return None
    return net, net - benchmark


def _mature(label: dict | None, cutoff: datetime) -> bool:
    exit_at = _timestamp(label.get("exit_at")) if label else None
    return exit_at is not None and exit_at <= cutoff


def _recommendation_coverage(
    first_by_date: Mapping[str, list[dict]], scan_days: set[str], observed_from: str | None,
) -> dict:
    decision_days = {
        day for day, rows in first_by_date.items()
        if any(row["exclusion"] != "decision_outside_scan_date" for row in rows)
    }
    observed_scan_days = {day for day in scan_days if observed_from is not None and day >= observed_from}
    denominator = decision_days | observed_scan_days
    ready, conditional, legacy = set(), set(), set()
    for day, rows in first_by_date.items():
        for row in rows:
            if row["exclusion"] is not None:
                continue
            tier = _upper(row.get("recommendation_tier"))
            if tier == "BUY_READY":
                ready.add(day)
            elif tier == "BUY_CONDITIONAL":
                conditional.add(day)
            elif not tier and _upper(row.get("recommendation")) == "EXECUTE":
                legacy.add(day)
    actionable = ready | conditional
    return {
        "observable_decisions_from": observed_from,
        "denominator_days": len(denominator),
        "scan_history_days": len(observed_scan_days),
        "decision_days": len(decision_days),
        "scan_days_without_decisions": len(observed_scan_days - decision_days),
        "decision_days_without_scan_history": len(decision_days - observed_scan_days),
        "pre_audit_scan_history_days": len(scan_days - observed_scan_days),
        "recommendation_days": len(actionable),
        "recommendation_day_coverage_pct": pct(len(actionable), len(denominator)),
        "no_recorded_recommendation_days": len(denominator - actionable),
        "ready_days": len(ready),
        "ready_day_coverage_pct": pct(len(ready), len(denominator)),
        "conditional_days": len(conditional),
        "legacy_execute_days": len(legacy),
    }


def _exclusion(row: dict) -> str | None:
    if row["_decision_at"].astimezone(KST).date().isoformat() != row["scan_date"]:
        return "decision_outside_scan_date"
    if not row["ticker"]:
        return "missing_ticker"
    action = _upper(row.get("signal_action"))
    if action not in {"BUY", "SELL"}:
        return "ambiguous_action"
    if action == "SELL":
        return "sell"
    if _hard_blocked(row):
        return "hard_blocked"
    if row["score"] is None:
        return "missing_score"
    return None


def _rank(rows: list[dict], score_key: str) -> list[int]:
    ranked = sorted(rows, key=lambda row: (-row[score_key], row["ticker"]))
    return [row["id"] for row in ranked]


def _rank_returns(rows_by_date: Mapping[str, list[dict]], score_key: str, labels: Mapping) -> dict:
    result = {k: {} for k in TOP_K}
    for day, rows in rows_by_date.items():
        ranked = _rank(rows, score_key)
        for k in TOP_K:
            value = daily_return(ranked[:k], labels, complete=True)
            if value is not None:
                result[k][day] = value
    return result


def _baseline(rows_by_date: Mapping[str, list[dict]], labels: Mapping) -> dict:
    answer = {}
    for day, rows in rows_by_date.items():
        value = daily_return([row["id"] for row in rows], labels, complete=False)
        if value is not None:
            answer[day] = value
    return answer


def _by_day(rows) -> dict[str, list[dict]]:
    result = defaultdict(list)
    for row in rows:
        result[row["scan_date"]].append(row)
    return dict(result)


def _pair_by_rank(rows_by_date: Mapping[str, list[dict]], labels: Mapping, horizon: int, candidate_key: str) -> tuple:
    baseline = _baseline(rows_by_date, labels)
    incumbent = _rank_returns(rows_by_date, "score", labels)
    candidate = _rank_returns(rows_by_date, candidate_key, labels)
    paired = {
        f"top{k}": paired_summary(candidate[k], incumbent[k], baseline, horizon)
        for k in TOP_K
    }
    return baseline, incumbent, candidate, paired


def _rank_summary(values: Mapping, planned_days: int) -> dict:
    return {f"top{k}": return_summary(values[k], planned_days) for k in TOP_K}


def _market_decision(primary: dict, shadow_days: int) -> dict:
    top = primary["paired"]["top1"]
    coverage = primary["paired_event_coverage_pct"]
    action_coverage = primary["shadow_action_coverage_pct"]
    reasons = []
    if shadow_days < SHADOW_DAYS:
        reasons.append("shadow_window_incomplete")
    if top["days"] < MIN_COMMON_MATURE_DAYS:
        reasons.append("insufficient_common_mature_days")
    if coverage is None or coverage < MIN_COVERAGE_PCT:
        reasons.append("insufficient_paired_event_coverage")
    if action_coverage is None or action_coverage < MIN_COVERAGE_PCT:
        reasons.append("insufficient_action_coverage")
    status = "pending" if reasons else "hold"
    if not reasons:
        if top["ci_low_pct"] is not None and top["ci_low_pct"] > 0 and top["avg_vs_baseline_pct"] > 0:
            status = "promote"
            reasons.append("candidate_uplift_and_baseline_outperformance")
        elif (
            top["baseline_ci_high_pct"] is not None and top["baseline_ci_high_pct"] < 0
            and top["incumbent_baseline_ci_high_pct"] is not None and top["incumbent_baseline_ci_high_pct"] < 0
        ):
            status = "reduce"
            reasons.append("both_rankers_underperform_same_day_baseline")
        else:
            reasons.append("paired_evidence_inconclusive")
    return {
        "status": status,
        "reasons": reasons,
        "shadow_days": shadow_days,
        "common_mature_days": top["days"],
        "coverage_pct": coverage,
        "action_coverage_pct": action_coverage,
    }


class SelectionEvaluationService:
    """Aggregate historical and prospective selections using a read-only DB connection."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path is not None else None

    def build_report(
        self,
        *,
        market: str | None = None,
        start_date: date | str | None = None,
        end_date: date | str | None = None,
        as_of: date | datetime | str | None = None,
    ) -> dict:
        cutoff = _cutoff(as_of)
        start = _date(start_date)
        end = _date(end_date) or cutoff.date()
        if start and start > end:
            raise ValueError("start_date must be on or before end_date")
        if end > cutoff.date():
            raise ValueError("end_date must be on or before as_of")
        market = _upper(market)
        if market and market not in MARKETS:
            raise ValueError("market must be KRX or US")
        chosen_markets = (market,) if market else MARKETS
        data = self._load_data(chosen_markets, start, end, cutoff)
        return {
            "as_of": cutoff.isoformat(),
            "start_date": start.isoformat() if start else None,
            "end_date": end.isoformat(),
            "policy": {
                "label_version": LABEL_VERSION,
                "candidate_version": CANDIDATE_VERSION,
                "mode": "shadow",
                "horizons": list(HORIZONS),
                "primary_horizon": PRIMARY_HORIZON,
                "primary_rank": "top1",
                "shadow_days": SHADOW_DAYS,
                "min_common_mature_days": MIN_COMMON_MATURE_DAYS,
                "min_paired_event_coverage_pct": MIN_COVERAGE_PCT,
                "min_action_coverage_pct": MIN_COVERAGE_PCT,
                "paired_confidence_pct": 95,
                "bootstrap_samples": BOOTSTRAP_SAMPLES,
                "bootstrap_block_length": "horizon_in_ordered_recommendation_dates",
                "ablation_neutral_score": 0.5,
                "top3_policy": "up to three tickers with a valid label required for each selected ticker",
                "candidate_axis_weights": {name: {axis: weight} for name, (axis, weight) in CANDIDATE_AXES.items()},
            },
            "model_cost": data["model_cost"],
            "model_comparison": data["model_comparison"],
            "markets": {
                name: self._market_report(name, data, cutoff) for name in chosen_markets
            },
        }

    def _load_data(self, markets: tuple[str, ...], start: date | None, end: date, cutoff: datetime) -> dict:
        data = {
            "events": [], "labels": {}, "shadow": [], "shadow_labels": {}, "providers": [], "snapshots": [],
            "observed_from": {}, "scan_days": defaultdict(set),
            "model_comparison": {"markets": {}},
            "model_cost": {
                "scope": "global",
                "markets": {market: {"projected_flash_increment_usd": None} for market in MARKETS},
                "projected_total_flash_increment_usd": None,
            },
        }
        path = self.db_path or connection.DB_PATH
        if not path.is_file():
            return data
        arguments = [*markets, end.isoformat()]
        placeholders = ",".join("?" for _ in markets)
        date_range = f"UPPER(r.market) IN ({placeholders}) AND r.scan_date <= ?"
        if start:
            date_range += " AND r.scan_date >= ?"
            arguments.append(start.isoformat())
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("BEGIN")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            from llm.selection_experiment_report import model_selection_summary, monthly_cost_summary

            data["model_cost"] = {"scope": "global", **monthly_cost_summary(db, cutoff)}
            data["model_comparison"] = model_selection_summary(db, cutoff)
            if "decision_events" in tables:
                data["observed_from"] = {
                    row["market"]: row["first_date"] for row in db.execute(
                        f"""SELECT UPPER(market) AS market, MIN(scan_date) AS first_date
                            FROM decision_events WHERE UPPER(market) IN ({placeholders}) AND scan_date <= ?
                              AND julianday(created_at) <= julianday(?)
                              AND date(created_at, '+9 hours') = scan_date GROUP BY UPPER(market)""",
                        [*markets, end.isoformat(), cutoff.isoformat()],
                    )
                }
            if "scan_history" in tables:
                for row in db.execute(
                    """SELECT scan_date, markets_open FROM scan_history
                       WHERE scan_date BETWEEN ? AND ? AND julianday(created_at) <= julianday(?)""",
                    [start.isoformat() if start else "0001-01-01", end.isoformat(), cutoff.isoformat()],
                ):
                    open_markets = {_upper(value) for value in (row["markets_open"] or "").split(",")}
                    for market in open_markets.intersection(markets):
                        data["scan_days"][market].add(row["scan_date"])
            for key, table, clock in (
                ("events", "decision_events", "created_at"),
                ("shadow", "selection_shadow_candidates", "decision_at"),
                ("snapshots", "selection_shadow_snapshots", "captured_at"),
                ("providers", "selection_shadow_provider_observations", "collected_at"),
            ):
                if table not in tables:
                    continue
                for row in db.execute(f"SELECT r.* FROM {table} r WHERE {date_range}", arguments):
                    row = dict(row)
                    timestamp = _timestamp(row.get(clock))
                    if timestamp is None or timestamp > cutoff:
                        continue
                    if key == "events":
                        detected = _timestamp(row.get("detected_at"))
                        if detected is not None and detected > cutoff:
                            continue
                        row["_decision_at"] = max(timestamp, detected) if detected is not None else timestamp
                    row["_" + clock] = timestamp
                    data[key].append(row)
            for key, table, base, fk in (
                ("labels", "decision_outcome_labels", "decision_events", "decision_event_id"),
                ("shadow_labels", "shadow_outcome_labels", "selection_shadow_candidates", "shadow_candidate_id"),
            ):
                if table not in tables or base not in tables:
                    continue
                labels = db.execute(
                    f"""SELECT o.* FROM {table} o JOIN {base} r ON r.id = o.{fk}
                        WHERE {date_range} AND o.label_version = ? AND o.horizon_days IN (5, 10, 20)""",
                    [*arguments, LABEL_VERSION],
                )
                data[key] = {(row[fk], row["horizon_days"]): dict(row) for row in labels}
        return data

    def _market_report(self, market: str, data: dict, cutoff: datetime) -> dict:
        first = {}
        duplicate_count = 0
        # Timestamp, ID and ticker ordering do not depend on the future label.
        for row in sorted(data["events"], key=lambda item: (item["_created_at"], item["id"])):
            if _upper(row["market"]) != market:
                continue
            ticker = _upper(row["ticker"])
            key = (row["scan_date"], ticker)
            if key in first:
                duplicate_count += 1
                continue
            row = {
                **row,
                "ticker": ticker,
                "score": score_fraction(row["composite_score"]),
                "axes": _object(row.get("score_breakdown_json")),
                "details": _object(row.get("score_details_json")),
                "weights": _object(row.get("weights_used_json")),
            }
            row["exclusion"] = _exclusion(row)
            first[key] = row
        eligible = [row for row in first.values() if row["exclusion"] is None]
        exclusions = Counter(row["exclusion"] for row in first.values() if row["exclusion"])
        exclusions["duplicate_decisions"] = duplicate_count
        feature_counts = {name: Counter() for name in FEATURES[market]}
        shadow_by_event = {}
        shadow_exclusions = Counter()
        snapshots = {
            (row["scan_date"], _upper(row["ticker"])): row
            for row in data["snapshots"] if _upper(row["market"]) == market
        }
        for shadow in sorted(data["shadow"], key=lambda item: (item["_decision_at"], item["id"])):
            if _upper(shadow["market"]) != market:
                continue
            key = (shadow["scan_date"], _upper(shadow["ticker"]))
            event = first.get(key)
            if not event or event["id"] != shadow.get("decision_event_id"):
                shadow_exclusions["not_original_decision"] += 1
                continue
            if event["exclusion"] is not None:
                shadow_exclusions[event["exclusion"]] += 1
                continue
            if shadow["_decision_at"] < event["_decision_at"]:
                shadow_exclusions["shadow_before_original_decision"] += 1
                continue
            snapshot = snapshots.get(key)
            if (snapshot is None or snapshot["_captured_at"] > shadow["_decision_at"]
                    or snapshot.get("decision_event_id") != event["id"] or snapshot["eligible"] != 1):
                shadow_exclusions["invalid_capture_provenance"] += 1
                continue
            if event["id"] in shadow_by_event:
                shadow_exclusions["duplicate_shadow_decisions"] += 1
                continue
            features = _object(shadow.get("features_json"))
            if market == "US" and "sector_relative_strength" not in features:
                features["sector_relative_strength"] = features.get("relative_strength")
            for name in FEATURES[market]:
                status = "available" if feature_value(features, name) is not None else "missing"
                feature_counts[name][status] += 1
            original = score_fraction(shadow.get("baseline_score"))
            candidate = None
            if original is None or abs(original - event["score"]) > 0.00005:
                shadow_exclusions["incumbent_snapshot_mismatch"] += 1
            else:
                candidate = prospective_score(market, original, event["axes"], features, weights=event["weights"])
                if candidate is None:
                    shadow_exclusions["candidate_inputs_unavailable"] += 1
            shadow_by_event[event["id"]] = {**shadow, "candidate_score": candidate, "features": features}

        started_at = min((row["_decision_at"] for row in shadow_by_event.values()), default=None)
        started_on = min((row["scan_date"] for row in shadow_by_event.values()), default=None)
        shadow_days = (cutoff - started_at).days if started_at is not None else 0
        ablations = {
            axis: _by_day(
                {**row, "ablated_score": score}
                for row in eligible
                if (score := neutralize_axis(row["score"], row["axes"], row["weights"], axis)) is not None
            )
            for axis in AXES
        }
        for counts in feature_counts.values():
            counts["not_captured"] = sum(
                started_on is not None and row["scan_date"] >= started_on and row["id"] not in shadow_by_event
                for row in eligible
            )
        eligible_by_date = _by_day(eligible)
        first_by_date = _by_day(first.values())
        recommendation = _recommendation_coverage(
            first_by_date, data["scan_days"][market], data["observed_from"].get(market),
        )
        horizons = {
            horizon: {
                **self._horizon_report(
                    horizon, cutoff, eligible_by_date, first_by_date, ablations, shadow_by_event,
                    data["labels"], data["shadow_labels"], started_on,
                ),
                "recommendation_days": recommendation["recommendation_days"],
                "recommendation_day_coverage_pct": recommendation["recommendation_day_coverage_pct"],
            }
            for horizon in HORIZONS
        }
        providers = [row for row in data["providers"] if _upper(row["market"]) == market]
        cutoffs = {
            (row["scan_date"], _upper(row["ticker"])): row["_decision_at"]
            for row in shadow_by_event.values()
        }
        evidence = provider_evidence(providers, cutoffs)
        if market == "KRX":
            evidence["krx_flow_comparison"].update(_krx_flow_sensitivity(
                eligible, shadow_by_event, matched_krx_flows(providers, cutoffs), data["shadow_labels"], cutoff,
            ))
        return {
            "horizons": horizons,
            "recommendation_coverage": recommendation,
            "missingness": {
                "first_decisions": len(first),
                "eligible_buy_events": len(eligible),
                "exclusions": dict(sorted(exclusions.items())),
                "axes": {
                    axis: dict(sorted(Counter(
                        axis_availability(market, row["axes"], row["details"], row["weights"], axis)
                        for row in eligible
                    ).items()))
                    for axis in AXES
                },
                "features": {name: dict(sorted(counts.items())) for name, counts in feature_counts.items()},
                "shadow_exclusions": dict(sorted(shadow_exclusions.items())),
                "provider_evidence": evidence,
            },
            "costs": provider_costs(providers),
            "decision": {
                **_market_decision(horizons[PRIMARY_HORIZON], shadow_days),
                "shadow_started_at": started_at.isoformat() if started_at else None,
            },
        }

    @staticmethod
    def _horizon_report(
        horizon: int,
        cutoff: datetime,
        eligible_by_date: Mapping[str, list[dict]],
        first_by_date: Mapping[str, list[dict]],
        ablations: Mapping,
        shadow_by_event: Mapping,
        labels: Mapping,
        shadow_labels: Mapping,
        started_on: str | None,
    ) -> dict:
        eligible = [event for events in eligible_by_date.values() for event in events]
        historical_values = {}
        historical_mature = 0
        historical_statuses = Counter()
        for event in eligible:
            label = labels.get((event["id"], horizon))
            if _event_mature(event, horizon, cutoff, label):
                historical_mature += 1
            value = _label_values(label, cutoff)
            if value is not None:
                historical_values[event["id"]] = value
            historical_statuses[_label_status(label, cutoff, value)] += 1
        incumbent = _rank_returns(eligible_by_date, "score", historical_values)
        baseline = _baseline(eligible_by_date, historical_values)
        assessed_ablations = {}
        for axis, rows in ablations.items():
            _, _, ablated, comparison = _pair_by_rank(rows, historical_values, horizon, "ablated_score")
            assessed_ablations[axis] = {
                **_rank_summary(ablated, len(rows)),
                "eligible_count": sum(map(len, rows.values())),
                "paired": comparison,
            }
        shadow_rows = defaultdict(list)
        shadow_values = {}
        shadow_statuses = Counter()
        shadow_mature = 0
        market_day_cutoffs = {
            shadow["scan_date"]: shadow["_decision_at"] for shadow in shadow_by_event.values()
        }
        if started_on is not None:
            for event in eligible:
                day = event["scan_date"]
                if day < started_on:
                    continue
                shadow = shadow_by_event.get(event["id"])
                label = shadow_labels.get((shadow["id"], horizon)) if shadow else None
                shadow_clock = shadow["_decision_at"] if shadow else market_day_cutoffs.get(day)
                if _event_mature(event, horizon, cutoff, label, shadow_clock):
                    shadow_mature += 1
                if shadow is None or shadow["candidate_score"] is None:
                    shadow_statuses["candidate_unavailable"] += 1
                    continue
                shadow_rows[day].append({**event, "candidate_score": shadow["candidate_score"]})
                label_clock = _timestamp(label.get("decision_at")) if label else None
                if label and shadow_clock != label_clock:
                    shadow_statuses["label_decision_mismatch"] += 1
                    continue
                value = _label_values(label, cutoff)
                if value is not None:
                    shadow_values[event["id"]] = value
                shadow_statuses[_label_status(label, cutoff, value)] += 1
        common_baseline, common_incumbent, candidate, paired = _pair_by_rank(
            shadow_rows, shadow_values, horizon, "candidate_score"
        )
        ambiguous = sum(
            row["exclusion"] == "ambiguous_action" and row["score"] is not None
            and not _hard_blocked(row) and _event_mature(row, horizon, cutoff, None, market_day_cutoffs.get(day))
            for day, rows in first_by_date.items() if started_on is not None and day >= started_on
            for row in rows
        )
        return {
            "eligible_count": len(eligible),
            "eligible_days": len(eligible_by_date),
            "mature_eligible_count": historical_mature,
            "valid_price_count": len(historical_values),
            "coverage_pct": pct(len(historical_values), historical_mature),
            "historical_label_statuses": dict(sorted(historical_statuses.items())),
            "baseline": return_summary(baseline, len(eligible_by_date)),
            "incumbent": {
                f"top{k}": {
                    **return_summary(incumbent[k], len(eligible_by_date)),
                    "vs_baseline": baseline_comparison(incumbent[k], baseline, horizon),
                }
                for k in TOP_K
            },
            "ablations": assessed_ablations,
            "shadow_mature_eligible_count": shadow_mature,
            "shadow_comparable_count": sum(map(len, shadow_rows.values())),
            "shadow_valid_price_count": len(shadow_values),
            "shadow_label_statuses": dict(sorted(shadow_statuses.items())),
            "shadow_action_coverage_pct": pct(shadow_mature, shadow_mature + ambiguous),
            "shadow_ambiguous_action_count": ambiguous,
            "paired_event_coverage_pct": pct(len(shadow_values), shadow_mature),
            "paired_baseline": return_summary(common_baseline, len(shadow_rows)),
            "paired_incumbent": _rank_summary(common_incumbent, len(shadow_rows)),
            "candidate": _rank_summary(candidate, len(shadow_rows)),
            "paired": paired,
        }


def _krx_flow_sensitivity(eligible, shadow_by_event, matched_flows, shadow_labels, cutoff) -> dict:
    """Change only the flow feed within an identical frozen KRX universe."""
    by_date = defaultdict(list)
    for row in eligible:
        pair = matched_flows.get((row["scan_date"], row["ticker"]))
        shadow = shadow_by_event.get(row["id"])
        if pair is None or shadow is None or shadow["candidate_score"] is None:
            continue
        scores = {}
        for provider, observation in pair.items():
            features = {
                **shadow["features"],
                "krx_flow_5d": observation.get("krx_flow_5d"),
                "krx_flow_positive_share": observation.get("krx_flow_positive_share"),
            }
            scores[provider] = prospective_score("KRX", row["score"], row["axes"], features, weights=row["weights"])
        if any(score is None for score in scores.values()):
            continue
        by_date[row["scan_date"]].append({**row, "kis_score": scores["kis"], "naver_score": scores["naver"]})
    # One ticker cannot reveal whether sourcing changes a recommendation.
    by_date = {day: rows for day, rows in by_date.items() if len(rows) >= 2}
    names = {row["id"]: row["ticker"] for rows in by_date.values() for row in rows}
    daily = {}
    for day, rows in sorted(by_date.items()):
        kis = names[_rank(rows, "kis_score")[0]]
        naver = names[_rank(rows, "naver_score")[0]]
        daily[day] = {"kis": kis, "naver": naver, "matched_tickers": len(rows), "changed": kis != naver}
    changed_dates = [day for day, picks in daily.items() if picks["changed"]]
    paired = {}
    for horizon in HORIZONS:
        labels = {}
        for event_id in names:
            shadow = shadow_by_event[event_id]
            label = shadow_labels.get((shadow["id"], horizon))
            if label is not None and _timestamp(label.get("decision_at")) == shadow["_decision_at"]:
                value = _label_values(label, cutoff)
                if value is not None:
                    labels[event_id] = value
        kis = _rank_returns(by_date, "kis_score", labels)[1]
        naver = _rank_returns(by_date, "naver_score", labels)[1]
        paired[horizon] = paired_summary(kis, naver, _baseline(by_date, labels), horizon)
    return {
        "eligible_comparison_days": len(daily),
        "top1_changed_days": len(changed_dates),
        "top1_changed_pct": pct(len(changed_dates), len(daily)),
        "changed_dates": changed_dates,
        "daily_top1": daily,
        "paired_mature_by_horizon": paired,
    }


def _hard_blocked(row: dict) -> bool:
    return bool(str(row.get("hard_block_reason") or "").strip()) or any(
        _upper(row.get(key)) == "BLOCKED"
        for key in ("recommendation", "recommendation_tier", "execution_state")
    )


def _event_mature(event: dict, horizon: int, cutoff: datetime, label: dict | None, clock: datetime | None = None) -> bool:
    # Reuse exchange sessions for schedules not yet queued or missing entirely.
    # Falling back to observed labels alone would make incomplete labelling
    # look like full coverage.
    if label and _timestamp(label.get("exit_at")) is not None:
        return _mature(label, cutoff)
    from web.services.decision_label_service import scheduled_exit_at

    exit_at = scheduled_exit_at(_upper(event["market"]), clock or event["_decision_at"], horizon)
    return exit_at is not None and exit_at <= cutoff


def _label_status(label: dict | None, cutoff: datetime, value) -> str:
    if label is None:
        return "unlabelled"
    exit_at = _timestamp(label.get("exit_at"))
    if exit_at is not None and exit_at > cutoff:
        return "pending"
    if label["status"] == "evaluated" and value is None:
        return "missing_return_or_benchmark"
    return label["status"]
