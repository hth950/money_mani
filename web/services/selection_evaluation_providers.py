"""Summaries of source coverage and charges recorded during shadow runs."""

import json
from collections import Counter, defaultdict
from datetime import timezone, timedelta

from web.services.selection_evaluation_metrics import average, finite, pct

KST = timezone(timedelta(hours=9))


def _details(row):
    try:
        value = json.loads(row.get("details_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def matched_krx_flows(rows, cutoffs=None):
    """Pair observed 5-session KIS amounts and Naver proxies by ticker/date."""
    observations = defaultdict(dict)
    for row in rows:
        name = row["provider"]
        if name not in {"kis", "naver"} or row["status"] != "available":
            continue
        key = row["scan_date"], row["ticker"].strip().upper()
        if cutoffs is not None:
            cutoff = cutoffs.get(key)
            if cutoff is None or row["_collected_at"] > cutoff:
                continue
        observations[key][name] = _details(row)
    pairs = {}
    for key, observations_by_name in observations.items():
        kis = observations_by_name.get("kis", {})
        naver = observations_by_name.get("naver", {})
        sessions = kis.get("sessions")
        if not isinstance(sessions, list) or len(sessions) != 5 or sessions != naver.get("sessions"):
            continue
        if len({str(session) for session in sessions}) != 5:
            continue
        if finite(kis.get("krx_flow_5d")) is None or finite(naver.get("krx_flow_5d")) is None:
            continue
        pairs[key] = observations_by_name
    return pairs


def provider_evidence(rows, decisions):
    providers = {}
    for row in rows:
        name = row["provider"]
        provider = providers.setdefault(name, {
            "observations": 0, "statuses": Counter(), "reasons": Counter(),
            "before_shadow_cutoff": 0, "after_shadow_cutoff": 0,
            "financial_comparison_statuses": Counter(), "financial_differences": defaultdict(list),
        })
        provider["observations"] += 1
        provider["statuses"][row["status"]] += 1
        details = _details(row)
        if details.get("reason"):
            provider["reasons"][str(details["reason"])] += 1
        key = row["scan_date"], row["ticker"].upper()
        cutoff = decisions.get(key)
        if cutoff is not None:
            timing = "before_shadow_cutoff" if row["_collected_at"] <= cutoff else "after_shadow_cutoff"
            provider[timing] += 1
        comparison = details.get("financial_comparison")
        if isinstance(comparison, dict):
            status = comparison.get("status") or "unknown"
            provider["financial_comparison_statuses"][status] += 1
            difference = finite(comparison.get("difference"))
            if status == "comparable" and difference is not None:
                unit = comparison.get("unit") or "unknown"
                metric = comparison.get("metric") or "unknown"
                provider["financial_differences"][metric, unit].append(difference)
    for provider in providers.values():
        stats = provider["financial_differences"]
        provider["financial_differences"] = [
            {"metric": metric, "unit": unit, "pairs": len(values), "avg_as_filed_minus_incumbent": average(values)}
            for (metric, unit), values in sorted(stats.items())
        ]
    differences = [
        (float(pair["kis"]["krx_flow_5d"]), float(pair["naver"]["krx_flow_5d"]))
        for pair in matched_krx_flows(rows).values()
    ]
    return {
        "providers": providers,
        "krx_flow_comparison": {
            "direct_provider": "kis",
            "proxy_provider": "naver",
            "matched_ticker_dates": len(differences),
            "mean_absolute_difference": average(abs(a - b) for a, b in differences),
            "direction_agreement_pct": pct(
                sum((a > 0) - (a < 0) == (b > 0) - (b < 0) for a, b in differences), len(differences),
            ),
        },
    }


def provider_costs(rows: list[dict]) -> dict:
    providers = {}
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["provider"]].append(row)
    for provider, entries in sorted(grouped.items()):
        months = defaultdict(lambda: defaultdict(list))
        totals = defaultdict(list)
        calls, costed_calls, counted = 0, 0, 0
        for row in entries:
            request_count = finite(row.get("request_count"))
            if request_count is None or request_count < 0 or not request_count.is_integer():
                continue
            request_count = int(request_count)
            counted += 1
            calls += request_count
            actual_cost = finite(row.get("actual_cost"))
            currency = str(row.get("cost_currency") or "").strip().upper()
            if actual_cost is None or actual_cost < 0 or not currency:
                continue
            costed_calls += request_count
            totals[currency].append(actual_cost)
            month = row["_collected_at"].astimezone(KST).strftime("%Y-%m")
            months[month][currency].append(actual_cost)
        dates = [entry["_collected_at"].astimezone(KST).date() for entry in entries]
        calendar_days = (max(dates) - min(dates)).days + 1
        observed = {currency: round(sum(values), 8) for currency, values in sorted(totals.items())}
        complete = counted == len(entries) and costed_calls == calls and bool(observed)
        providers[provider] = {
            "observations": len(entries),
            "statuses": dict(sorted(Counter(row.get("status") or "unknown" for row in entries).items())),
            "first_observed_on": min(dates).isoformat(),
            "last_observed_on": max(dates).isoformat(),
            "calendar_days": calendar_days,
            "request_count": calls if counted else None,
            "costed_request_count": costed_calls if counted else None,
            "cost_coverage_pct": pct(costed_calls, calls),
            "observed_cost": observed,
            "observed_months": {
                month: {currency: round(sum(values), 8) for currency, values in sorted(costs.items())}
                for month, costs in sorted(months.items())
            },
            "estimated_monthly_cost": (
                {currency: round(value * (365.25 / 12.0) / calendar_days, 8) for currency, value in observed.items()}
                if complete else None
            ),
        }
    return {"observed_providers": len(providers), "providers": providers}
