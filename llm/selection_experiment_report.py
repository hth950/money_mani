"""Cost, extraction-quality review, and paired rank results for captured news."""

import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from statistics import mean

from llm.selection_experiment import BUDGET, CALIBRATION_PAIRS, FLASH, LITE, MODELS, dollars, read_experiment
from llm.selection_experiment_replay import normalized_text, parse_issues, replay_rank
from web.db import connection


RUBRIC = {
    "repeat": ["new", "repeat", "unclear"],
    "ticker_link": ["correct", "incorrect", "unclear"],
    "direction": ["supported", "contradicted", "insufficient"],
    "example_annotation": {"1": {"repeat": "new", "links": {"1": {
        "ticker_link": "correct", "direction": "supported", "article_numbers": [1],
    }}}},
    "instructions": [
        "Use issue and ticker-link numbers exactly as shown. Omit unreviewed fields.",
        "Place each review in annotations under its review_id, following example_annotation.",
        "Mark repeat when the same underlying development appears in earlier headlines or another issue in this output.",
        "Check whether the symbol identifies the named company and the frozen article connects it to the issue.",
        "Judge whether the frozen evidence supports the stated direction, using article_numbers when available.",
    ],
}


def _blind_id(pair, model):
    return hashlib.sha256(f"{pair['news_digest']}:{model}".encode()).hexdigest()[:16]


def _attempts(pair):
    for attempt in pair["attempts"]:
        details = json.loads(attempt["detail_json"] or "{}")
        parsed = None
        if attempt["state"] == "settled" and details.get("finish_reason") == "stop":
            parsed = parse_issues(details.get("raw"))
        yield attempt, details, parsed


def review_worksheet():
    packets = []
    annotations = {}
    for pair in read_experiment()["pairs"]:
        outputs = []
        for attempt, _, parsed in _attempts(pair):
            if parsed is None:
                continue
            review_id = _blind_id(pair, attempt["model"])
            outputs.append({"review_id": review_id, "issues": [
                {**issue, "issue_number": index, "affected_tickers": [
                    {**link, "link_number": number}
                    for number, link in enumerate(issue["affected_tickers"], 1)
                ]} for index, issue in enumerate(parsed, 1)
            ]})
            annotations[review_id] = {}
        if outputs:
            packets.append({
                "packet_id": pair["id"], "stage": pair["stage"], "market": pair["market"],
                "captured_at": pair["captured_at"], "prior_headlines": pair["prior_titles"],
                "articles": [{**article, "article_number": index} for index, article in enumerate(pair["articles"], 1)],
                "outputs": sorted(outputs, key=lambda item: item["review_id"]),
            })
    return {"rubric": RUBRIC, "packets": packets, "annotations": annotations}


def _quality(pair, issues, annotations):
    counts = Counter()
    prior = {normalized_text(title) for title in pair["prior_titles"]}
    seen = set()
    for index, issue in enumerate(issues, 1):
        counts["issues"] += 1
        title = normalized_text(issue["title"])
        if title and (title in seen or title in prior):
            counts["exact_headline_repeats"] += 1
        seen.add(title)
        issue_annotation = annotations.get(str(index), {})
        repeat = issue_annotation.get("repeat")
        if repeat is not None:
            if repeat not in RUBRIC["repeat"]:
                raise ValueError(f"Unknown repeat annotation for issue {index}.")
            counts[f"repeat_{repeat}"] += 1
            counts["repeat_reviewed"] += 1
        for number, link in enumerate(issue["affected_tickers"], 1):
            counts["ticker_links"] += 1
            pattern = r"[0-9]{6}" if pair["market"] == "KRX" else r"[A-Z]{1,6}(?:[.\-][A-Z0-9]{1,4})?"
            if not re.fullmatch(pattern, link["ticker"].strip()):
                counts["invalid_or_missing_symbol_format"] += 1
            link_annotation = issue_annotation.get("links", {}).get(str(number), {})
            for field in ("ticker_link", "direction"):
                value = link_annotation.get(field)
                if value is not None:
                    if value not in RUBRIC[field]:
                        raise ValueError(f"Unknown {field} annotation for issue {index}, link {number}.")
                    counts[f"{field}_{value}"] += 1
                    counts[f"{field}_reviewed"] += 1
            evidence = link_annotation.get("article_numbers")
            if evidence is not None:
                if not isinstance(evidence, list) or any(type(item) is not int or not 1 <= item <= len(pair["articles"])
                                                        for item in evidence):
                    raise ValueError(f"Invalid frozen article reference for issue {index}, link {number}.")
                if evidence:
                    counts["links_with_article_references"] += 1
    return counts


def monthly_cost_summary(db, as_of):
    """Use the caller's read-only database snapshot and point-in-time report cutoff."""
    cutoff = as_of.isoformat() if isinstance(as_of, datetime) else as_of
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    traffic = {}
    if "market_intel_scans" in names:
        traffic = dict(db.execute(
            """SELECT CASE WHEN scan_type GLOB 'us_*' THEN 'US' ELSE 'KRX' END AS market, COUNT(*) AS scans
               FROM market_intel_scans WHERE model_used = ?
                 AND julianday(created_at) BETWEEN julianday(?) - 30 AND julianday(?)
                 AND COALESCE(error_message, '') <> 'No search results'
               GROUP BY market""", (LITE, cutoff, cutoff),
        ).fetchall())
    costs = defaultdict(list)
    if {"selection_intel_attempts", "selection_intel_packets"}.issubset(names):
        for market, lite, flash in db.execute(
            """SELECT p.market, l.billed_nanos, f.billed_nanos
               FROM selection_intel_packets p
               JOIN selection_intel_attempts l ON l.packet_id = p.id AND l.model = ? AND l.state = 'settled'
               JOIN selection_intel_attempts f ON f.packet_id = p.id AND f.model = ? AND f.state = 'settled'
               WHERE julianday(l.finished_at) <= julianday(?) AND julianday(f.finished_at) <= julianday(?)""",
            (LITE, FLASH, cutoff, cutoff),
        ):
            costs[market].append({LITE: lite, FLASH: flash})
    return _monthly(costs, traffic)


def _monthly(costs, traffic):
    result = {}
    for market in ("KRX", "US"):
        scans = traffic.get(market, 0)
        pairs = costs[market]
        average = {model: round(dollars(mean(pair[model] for pair in pairs)), 9) for model in MODELS} if pairs else None
        monthly = {model: round(scans * average[model], 6) for model in MODELS} if scans and average else None
        result[market] = {
            "observed_scans_last_30_days": scans, "settled_paired_samples": len(pairs),
            "mean_usd_per_call": average, "projected_usd_per_30_days": monthly,
            "projected_flash_increment_usd": round(monthly[FLASH] - monthly[LITE], 6) if monthly else None,
        }
    covered = [row for row in result.values() if row["observed_scans_last_30_days"]]
    total = (round(sum(row["projected_flash_increment_usd"] for row in covered), 6)
             if covered and all(row["projected_flash_increment_usd"] is not None for row in covered) else None)
    return {"markets": result, "projected_total_flash_increment_usd": total}


def build_report(annotations=None, *, now=None):
    data = read_experiment()
    annotations = (annotations or {}).get("annotations", annotations or {})
    if not isinstance(annotations, dict):
        raise ValueError("Annotations must be keyed by worksheet review ID.")
    summaries = defaultdict(lambda: defaultdict(Counter))
    rank_summaries = defaultdict(Counter)
    reported_pairs = []
    for pair in data["pairs"]:
        cohort = (pair["stage"], pair["market"])
        parsed_by_model = {}
        attempts = {}
        for attempt, details, parsed in _attempts(pair):
            model = attempt["model"]
            parsed_by_model[model] = parsed
            counters = summaries[cohort][model]
            counters[attempt["state"]] += 1
            if attempt["state"] == "settled":
                counters["parseable_outputs" if parsed is not None else "unusable_outputs"] += 1
            if parsed is not None:
                key = _blind_id(pair, model)
                counters.update(_quality(pair, parsed, annotations.get(key, {})))
            attempts[model] = {
                **{key: value for key, value in details.items() if key != "raw"},
                "state": attempt["state"], "review_id": _blind_id(pair, model),
                "issues_parsed": len(parsed) if parsed is not None else None,
                "cost_usd": dollars(attempt["billed_nanos"]) if attempt["billed_nanos"] is not None else None,
            }
        replay = replay_rank(pair, parsed_by_model)
        if replay["status"] == "available":
            for kind, results in replay["ranks"].items():
                if results is not None:
                    rank_summaries[cohort][f"{kind}_comparable_pairs"] += 1
                    rank_summaries[cohort][f"{kind}_top1_changes"] += int(results["changed"])
        else:
            rank_summaries[cohort][replay["reason"]] += 1
        reported_pairs.append({
            **{key: pair[key] for key in ("id", "news_digest", "captured_at", "market", "scan_type", "stage", "production_scan_id")},
            "attempts": attempts, "counterfactual_rank": replay,
        })
    timestamp = now or datetime.now(timezone.utc)
    ledger = data["ledger"]
    monthly = _monthly(defaultdict(list), {})
    if connection.DB_PATH.is_file():
        with closing(sqlite3.connect(connection.DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            monthly = monthly_cost_summary(db, timestamp)
    return {
        "as_of": timestamp.isoformat(), "models": {"lite": LITE, "flash": FLASH},
        "budget": {"cap_usd": dollars(BUDGET), "settled_usd": dollars(ledger["billed"]),
                   "bound_provider_key_cap_usd": dollars(data["key_limit"]) if data["key_limit"] is not None else None,
                   "held_usd": dollars(ledger["held"]),
                   "remaining_usd": dollars(max(0, (data["key_limit"] or BUDGET) - ledger["billed"] - ledger["held"])),
                   "reservation_breaches": ledger["breaches"]},
        "calibration": {"target_pairs": CALIBRATION_PAIRS, **data["calibration"]}, "packets": data["packet_counts"],
        "quality_and_rank": {
            stage: {market: {"models": {model: dict(summaries[stage, market][model]) for model in MODELS},
                             "rank": dict(rank_summaries[stage, market])} for market in ("KRX", "US")}
            for stage in ("calibration", "holdout")
        },
        "monthly_cost": monthly,
        "pairs": reported_pairs,
        "method": [
            "Settled calls report OpenRouter usage.cost. In-flight or uncertain calls retain their full reservation.",
            "Monthly projections use paired observed per-call cost and the recorded Lite scan volume in the preceding 30 days. They assume one completion per scan and exclude unknown retries and fees.",
            "Symbol format and exact repeats are automatic. True ticker-link errors, semantic repeats and supported direction require worksheet annotations; the reviewed counts are the corresponding denominators.",
            "Rank replay uses the same frozen candidate set and non-news inputs for both models. It reports top-1 changes. KRX replay uses emitted ticker codes with numeric padding, so production name repair may differ.",
        ],
    }
