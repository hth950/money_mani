"""Read-only outcome evidence for held-out model comparisons."""

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import mean
from zoneinfo import ZoneInfo

from llm.selection_experiment import FLASH, LITE, MODELS
from llm.selection_experiment_replay import parse_issues, replay_rank
from llm.selection_experiment_report import _quality, monthly_cost_summary
from web.services.decision_label_service import LABEL_VERSION, exchange_schedule
from web.services.selection_evaluation_metrics import average, finite, paired_summary, pct


HORIZON = 10
MIN_CALENDAR_DAYS = 28
MIN_DATE_COUNT = 30
MIN_COVERAGE_PCT = 80.0
MARKET_ZONES = {"KRX": ZoneInfo("Asia/Seoul"), "US": ZoneInfo("America/New_York")}


def _timestamp(value):
    if not isinstance(value, datetime):
        if not isinstance(value, str) or len(value) < 16:
            return None
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)


def _object(raw):
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _window(market, captured_at):
    schedule = exchange_schedule(market)
    if captured_at.year < 2000:
        return None
    position = int(schedule["open"].searchsorted(captured_at, side="right"))
    if position + HORIZON > len(schedule):
        return None
    entry = schedule.iloc[position]
    exit_session = schedule.iloc[position + HORIZON - 1]
    return (entry.name.date().isoformat(), exit_session.name.date().isoformat(),
            entry["open"].to_pydatetime(), exit_session["close"].to_pydatetime())


def _matched_returns(db, packet, picks, captured_at, window, cutoff):
    entry_date, exit_date, entry_at, exit_at = window
    rank = packet["rank"]
    candidates = {item["ticker"]: item.get("decision_event_id") for item in rank["candidates"]}
    if len(candidates) < 2 or len(candidates) != len(rank["candidates"]) or not set(picks.values()).issubset(candidates):
        return None
    matched = {}
    for row in db.execute(
        """SELECT c.ticker, c.decision_at AS candidate_at, c.decision_event_id, o.*
           FROM selection_shadow_candidates c JOIN shadow_outcome_labels o ON o.shadow_candidate_id = c.id
           WHERE c.market = ? AND c.scan_date = ? AND o.horizon_days = ? AND o.label_version = ?
             AND o.status = 'evaluated' AND o.entry_date = ? AND o.exit_date = ?""",
        (packet["market"], rank.get("scan_date"), HORIZON, LABEL_VERSION, entry_date, exit_date),
    ):
        if row["ticker"] not in candidates or candidates[row["ticker"]] != row["decision_event_id"]:
            continue
        decided = _timestamp(row["candidate_at"])
        observed = _timestamp(row["observed_at"])
        net = finite(row["net_return_pct"])
        if (
            decided is not None and decided <= captured_at and _timestamp(row["decision_at"]) == decided
            and _timestamp(row["entry_at"]) == entry_at and _timestamp(row["exit_at"]) == exit_at
            and observed is not None and exit_at <= observed <= cutoff and net is not None
        ):
            matched[row["ticker"]] = net
    if matched.keys() != candidates.keys():
        return None
    return (matched[picks[LITE]], matched[picks[FLASH]], mean(matched.values()), picks[LITE] != picks[FLASH])


def _returns_summary(daily):
    # The statistics helper uses the second entry as the comparable return.
    # Here all arms use net returns on the exact same market session window.
    comparisons = paired_summary(
        {day: (row[1], row[1]) for day, row in daily.items()},
        {day: (row[0], row[0]) for day, row in daily.items()},
        {day: (row[2], row[2]) for day, row in daily.items()}, HORIZON,
    )
    changes = [row[1] - row[0] for row in daily.values() if row[3]]
    return {
        "days": comparisons["days"], "top1_changes": len(changes),
        "mean_flash_minus_lite_pct": comparisons["avg_uplift_pct"],
        "ci_low_pct": comparisons["ci_low_pct"], "ci_high_pct": comparisons["ci_high_pct"],
        "mean_flash_minus_baseline_pct": comparisons["avg_vs_baseline_pct"],
        "changed_selection_mean_flash_minus_lite_pct": average(changes),
        "changed_selection_flash_wins": sum(change > 0 for change in changes),
        "mean_lite_net_pct": average(row[0] for row in daily.values()),
        "mean_flash_net_pct": average(row[1] for row in daily.values()),
    }


def _read_pair(row, captured_at, started_at, cutoff, counts):
    outputs, completions = {}, []
    for arm, model in (("lite", LITE), ("flash", FLASH)):
        finished = _timestamp(row[f"{arm}_at"])
        if row[f"{arm}_state"] != "settled" or finished is None or not captured_at <= started_at <= finished <= cutoff:
            counts[model]["unavailable_at_cutoff"] += 1
            continue
        detail = _object(row[f"{arm}_detail"])
        issues = parse_issues(detail.get("raw")) if detail.get("finish_reason") == "stop" else None
        if issues is None:
            counts[model]["unusable_outputs"] += 1
            continue
        counts[model]["parseable_outputs"] += 1
        outputs[model] = issues
        completions.append(finished)
    return outputs, completions


def summarize_model_outcomes(db, as_of):
    cutoff = _timestamp(as_of)
    if cutoff is None:
        raise ValueError("Model evidence requires an as-of timestamp.")
    costs = monthly_cost_summary(db, cutoff)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    labels_present = {"selection_shadow_candidates", "shadow_outcome_labels"}.issubset(tables)
    attempts = defaultdict(list)
    if {"selection_intel_packets", "selection_intel_pairs", "selection_intel_attempts"}.issubset(tables):
        for row in db.execute(
            """SELECT n.id, n.market, n.captured_at, p.started_at, n.packet_json,
                      l.state AS lite_state, l.finished_at AS lite_at, l.detail_json AS lite_detail,
                      f.state AS flash_state, f.finished_at AS flash_at, f.detail_json AS flash_detail
               FROM selection_intel_pairs p JOIN selection_intel_packets n ON n.id = p.packet_id
               LEFT JOIN selection_intel_attempts l ON l.packet_id = p.packet_id AND l.model = ?
               LEFT JOIN selection_intel_attempts f ON f.packet_id = p.packet_id AND f.model = ?
               WHERE p.stage = 'holdout' AND julianday(n.captured_at) <= julianday(?)
                 AND julianday(p.started_at) <= julianday(?)
               ORDER BY julianday(n.captured_at), n.captured_at, n.id""",
            (LITE, FLASH, cutoff.isoformat(), cutoff.isoformat()),
        ):
            attempts[row["market"]].append(row)

    markets = {}
    for market, zone in MARKET_ZONES.items():
        dates, sessions = set(), set()
        outcomes = {"prospective": {}, "retrospective": {}}
        quality = {model: Counter() for model in MODELS}
        counters = Counter()
        mature = 0
        for row in attempts[market]:
            captured = _timestamp(row["captured_at"])
            started = _timestamp(row["started_at"])
            if captured is None or started is None or not captured <= started <= cutoff:
                counters["invalid_observation_timestamps"] += 1
                continue
            day = captured.astimezone(zone).date()
            if day in dates:
                counters["repeated_packet_dates"] += 1
                continue
            dates.add(day)
            window = _window(market, captured)
            if window is None:
                counters["missing_calendar_window"] += 1
                continue
            entry_date, _, entry_at, exit_at = window
            if entry_date in sessions:
                counters["repeated_entry_sessions"] += 1
                continue
            sessions.add(entry_date)
            mature += exit_at <= cutoff
            packet = {**_object(row["packet_json"]), "market": market}
            outputs, completions = _read_pair(row, captured, started, cutoff, quality)
            if isinstance(packet.get("articles"), list) and isinstance(packet.get("prior_titles"), list):
                for model, issues in outputs.items():
                    quality[model].update(_quality(packet, issues, {}))
            else:
                counters["invalid_frozen_packet"] += 1
                continue
            if len(outputs) != len(MODELS):
                counters["incomplete_pair_outputs"] += 1
                continue
            try:
                rank = replay_rank(packet, outputs).get("ranks", {}).get("incumbent")
            except (KeyError, TypeError, ValueError):
                counters["invalid_frozen_rank_context"] += 1
                continue
            if not rank:
                counters["unmatched_candidate_ranks"] += 1
                continue
            timing = "prospective" if max(completions) < entry_at else "retrospective"
            counters[f"{timing}_ranked_pairs"] += 1
            counters[f"{timing}_ranked_top1_changes"] += int(rank["changed"])
            if exit_at <= cutoff:
                result = _matched_returns(db, packet, rank["top1"], captured, window, cutoff) if labels_present else None
                if result is None:
                    counters["unmatched_10_session_labels"] += 1
                else:
                    outcomes[timing][entry_date] = result

        elapsed = (max(dates) - min(dates)).days + 1 if dates else 0
        prospective = _returns_summary(outcomes["prospective"])
        coverage = pct(prospective["days"], mature)
        reasons = []
        if elapsed < MIN_CALENDAR_DAYS:
            reasons.append("heldout_window_incomplete")
        if prospective["days"] < MIN_DATE_COUNT:
            reasons.append("insufficient_prospective_mature_dates")
        if coverage is None or coverage < MIN_COVERAGE_PCT:
            reasons.append("insufficient_prospective_coverage")
        status = "pending"
        if not reasons:
            if prospective["ci_low_pct"] is not None and prospective["ci_low_pct"] > 0 and prospective["mean_flash_minus_baseline_pct"] > 0:
                status = "switch_review"
                reasons.append("positive_heldout_uplift_and_same_day_baseline")
            else:
                status = "hold"
                reasons.append("prospective_improvement_not_established")
        markets[market] = {
            "status": status, "reasons": reasons, "coverage_pct": coverage, "window_calendar_days": elapsed,
            "heldout_packets": len(attempts[market]), "observed_dates": len(dates), "mature_entry_dates": mature,
            "prospective": prospective, "retrospective": _returns_summary(outcomes["retrospective"]),
            "counts": dict(counters), "quality": {model: dict(counts) for model, counts in quality.items()},
            "monthly_cost": costs["markets"][market],
        }
    return {"markets": markets, "method": {
        "horizon_sessions": HORIZON, "min_calendar_days": MIN_CALENDAR_DAYS,
        "min_prospective_dates": MIN_DATE_COUNT, "min_coverage_pct": MIN_COVERAGE_PCT,
        "confidence_pct": 95, "label_version": LABEL_VERSION, "ranker": "incumbent",
        "sample": "First held-out packet per market-local date and executable entry session, in capture order.",
        "prospective": "Both matching model outputs finished before the scheduled entry open. The exact 10-session label must be observable at the report cutoff for every frozen candidate.",
        "retrospective": "Model output completed at or after its originally scheduled entry. Returns are diagnostic and excluded from model-switch review.",
        "baseline": "Equal-weight net return over all candidates frozen with the packet on the same entry and exit sessions.",
        "quality": "Automated format, parse and exact repeat counts only. Human link, direction and semantic duplicate review lives in the separate worksheet.",
        "cost": "Observed per-call pair costs scaled by the preceding 30 days of Lite scan traffic. Unknown retries and fees are excluded.",
    }}
