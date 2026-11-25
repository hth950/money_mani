"""Point-in-time candidate and news context for paired counterfactual rankings."""

import hashlib
import json
import math
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone

from web.services.selection_evaluation_scoring import incumbent_score_with_intel, prospective_score


KST = timezone(timedelta(hours=9))


def _decoded(value, expected):
    try:
        data = json.loads(value or "null")
    except (TypeError, ValueError):
        return expected()
    return data if isinstance(data, expected) else expected()


def _table_exists(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name = ? AND type = 'table'", (table,)).fetchone() is not None


def _frozen_candidates(db, market, captured_at):
    if not all(_table_exists(db, table) for table in (
        "selection_shadow_candidates", "selection_shadow_snapshots", "decision_events",
    )):
        return None
    scan_day = db.execute(
        """SELECT MAX(scan_date) FROM selection_shadow_candidates
           WHERE market = ? AND julianday(decision_at) <= julianday(?)
           AND julianday(decision_at) >= julianday(?) - 1""", (market, captured_at, captured_at),
    ).fetchone()[0]
    if not scan_day:
        return None
    latest_snapshot = db.execute(
        """SELECT MAX(scan_date) FROM selection_shadow_snapshots
           WHERE market = ? AND julianday(captured_at) <= julianday(?)""", (market, captured_at),
    ).fetchone()[0]
    if latest_snapshot != scan_day:
        return None
    rows = db.execute(
        """SELECT c.ticker, c.decision_event_id, c.baseline_score, c.decision_at,
                  d.ticker_name, d.score_breakdown_json, d.weights_used_json,
                  c.features_json, s.captured_at
           FROM selection_shadow_candidates c
           LEFT JOIN decision_events d ON d.id = c.decision_event_id
           LEFT JOIN selection_shadow_snapshots s
             ON s.scan_date = c.scan_date AND s.market = c.market AND s.ticker = c.ticker
           WHERE c.market = ? AND c.scan_date = ? AND julianday(c.decision_at) <= julianday(?)
           ORDER BY c.ticker""", (market, scan_day, captured_at),
    ).fetchall()
    if len(rows) < 2 or len({row["ticker"] for row in rows}) != len(rows):
        return None
    return {
        "scan_date": scan_day,
        "candidates": [
            {
                "ticker": row["ticker"], "name": row["ticker_name"], "decision_event_id": row["decision_event_id"],
                "baseline": row["baseline_score"], "axes": _decoded(row["score_breakdown_json"], dict),
                "weights": _decoded(row["weights_used_json"], dict),
                "features": _decoded(row["features_json"], dict),
            }
            for row in rows
        ],
    }


def freeze_replay_context(db, market, captured_at, detection_date):
    cutoff = datetime.fromisoformat(captured_at)
    today = detection_date or cutoff.astimezone(KST).date()
    rank = _frozen_candidates(db, market, captured_at)
    if not _table_exists(db, "market_intel_issues"):
        return {"rank": None, "prior_titles": []}
    recent = db.execute(
        """SELECT i.id, i.title, i.category, i.confidence, i.content_hash, i.detection_date,
                  i.affected_tickers_json, i.accuracy_score, i.created_at, s.scan_type, s.status AS scan_status
           FROM market_intel_issues i LEFT JOIN market_intel_scans s ON s.id = i.scan_id
           WHERE i.detection_date BETWEEN ? AND ? AND julianday(i.created_at) <= julianday(?)
           ORDER BY i.detection_date DESC, i.id""",
        ((today - timedelta(days=7)).isoformat(), today.isoformat(), captured_at),
    ).fetchall()
    prior_titles = [row["title"] for row in recent if row["scan_type"] and (
        row["scan_type"].startswith("us_") == (market == "US")
    )]
    if rank is not None:
        tickers = {candidate["ticker"] for candidate in rank["candidates"]}
        rank["prior_issues"] = []
        for row in recent:
            affected = _decoded(row["affected_tickers_json"], list)
            if any(isinstance(item, dict) and item.get("ticker") in tickers for item in affected):
                rank["prior_issues"].append({**dict(row), "affected_tickers": affected})
            # Today's hashes are needed for exact production dedup even if the earlier issue
            # mentioned a different ticker to the generated replacement.
            elif row["detection_date"] == today.isoformat() and row["content_hash"]:
                rank["prior_issues"].append({**dict(row), "affected_tickers": []})
        categories = db.execute(
            """SELECT category, AVG(accuracy_score) AS accuracy, AVG(confidence) AS confidence
               FROM market_intel_issues WHERE accuracy_score IS NOT NULL
               AND detection_date BETWEEN ? AND ? AND julianday(created_at) <= julianday(?)
               GROUP BY category HAVING COUNT(*) >= 3""",
            ((today - timedelta(days=30)).isoformat(), today.isoformat(), captured_at),
        ).fetchall()
        rank["accuracy"] = {row["category"] or "": round(row["accuracy"], 4) for row in categories}
        reliable = [row for row in categories if row["confidence"] and row["confidence"] > 0]
        rank["calibration"] = {
            row["category"] or "": round(max(0.3, min(1.0, row["accuracy"] / row["confidence"])), 4)
            for row in reliable
        }
        overall = sum(row["accuracy"] for row in reliable) / sum(row["confidence"] for row in reliable) if reliable else 0.5
        rank["default_calibration"] = round(max(0.3, min(1.0, overall)), 4)
        rank["detection_date"] = today.isoformat()
        rank["captured_at"] = captured_at
        if market == "US":
            rank["prior_successful_us_scans"] = db.execute(
                """SELECT COUNT(*) FROM market_intel_scans WHERE scan_type GLOB 'us_*' AND status = 'success'
                   AND julianday(created_at) BETWEEN julianday(?) - 1 AND julianday(?)""",
                (captured_at, captured_at),
            ).fetchone()[0]
    return {"rank": rank, "prior_titles": prior_titles}


def parse_issues(raw):
    """Validate the fields needed for quality counts and replay."""
    if not isinstance(raw, str):
        return None
    clean = raw.strip()
    if clean.startswith("```") and clean.endswith("```") and "\n" in clean:
        clean = clean.split("\n", 1)[1][:-3].strip()
    try:
        issues = json.loads(clean)
    except ValueError:
        return None
    if not isinstance(issues, list):
        return None
    for issue in issues:
        if not isinstance(issue, dict) or not all(isinstance(issue.get(key), str) for key in ("title", "category")):
            return None
        confidence = issue.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            return None
        links = issue.get("affected_tickers")
        if not isinstance(links, list) or any(not isinstance(link, dict) or not isinstance(link.get("ticker"), str)
                                               or not isinstance(link.get("direction"), str) for link in links):
            return None
    return issues


def normalized_text(value):
    return "".join(ch for ch in unicodedata.normalize("NFKC", str(value or "")).casefold() if ch.isalnum())


def _current_issues(model_issues, rank, market):
    old = {row["content_hash"] or f"id:{row['id']}": row for row in rank["prior_issues"]}
    next_id = max((row["id"] for row in old.values()), default=0) + 1
    for offset, issue in enumerate(model_issues):
        detection_date = rank["detection_date"]
        material = f"{issue['title'].strip().lower()}|{issue['category']}|{detection_date}"
        identity = hashlib.sha256(material.encode()).hexdigest()[:16]
        previous = old.get(identity)
        if previous and issue["confidence"] <= (previous.get("confidence") or 0):
            continue
        affected = []
        for link in issue["affected_tickers"]:
            ticker = link["ticker"]
            if market == "KRX":
                ticker = ticker.strip().zfill(6) if ticker.strip() else ""
            affected.append({**link, "ticker": ticker})
        old[identity] = {
            **issue, "affected_tickers": affected, "detection_date": detection_date,
            "accuracy_score": previous.get("accuracy_score") if previous else None,
            "id": previous["id"] if previous else next_id + offset,
            "created_at": previous["created_at"] if previous else rank["captured_at"],
            "scan_type": previous["scan_type"] if previous else "us_comparison" if market == "US" else "comparison",
            "scan_status": previous["scan_status"] if previous else "success",
        }
    return list(old.values())


def _intel_axis(ticker, issues, rank):
    total = 0.0
    directional = 0.0
    today = date.fromisoformat(rank["detection_date"])
    for issue in issues:
        link = next((item for item in issue["affected_tickers"] if isinstance(item, dict) and item.get("ticker") == ticker), None)
        if link is None or (issue["accuracy_score"] is not None and issue["accuracy_score"] < .5):
            continue
        age = (today - date.fromisoformat(issue["detection_date"])).days
        category = issue["category"] or ""
        weight = ((issue["confidence"] or .5) * .85 ** age * rank["accuracy"].get(category, .5)
                  * rank["calibration"].get(category, rank["default_calibration"]))
        directional += weight * {"up": 1, "down": -1}.get(link.get("direction"), 0)
        total += weight
    return round((1 + directional / total) / 2.0, 4) if total else .5


def _datetime(raw):
    result = datetime.fromisoformat(raw)
    return result if result.tzinfo is not None else result.replace(tzinfo=timezone.utc)


def _novelty_axis(ticker, issues, rank, has_model_issues):
    # Same 24-hour signed-confidence and 7-day headline dedup policy as the
    # production shadow input. Replace only this hypothetical scan's issues.
    if not (rank["prior_successful_us_scans"] or has_model_issues):
        return None
    as_of = _datetime(rank["captured_at"])
    seen = set()
    result = 0.0
    eligible = [issue for issue in issues if (issue["scan_type"] or "").startswith("us_")
                and issue["scan_status"] == "success" and issue["created_at"]]
    for issue in sorted(eligible, key=lambda row: (_datetime(row["created_at"]), row["id"])):
        created_at = _datetime(issue["created_at"])
        if created_at < as_of - timedelta(days=7):
            continue
        headline = " ".join(re.findall(r"\w+", issue["title"].casefold()))
        link = next((item for item in issue["affected_tickers"] if isinstance(item, dict)
                     and item.get("market", "US") == "US" and str(item.get("ticker", "")).upper() == ticker), None)
        if not headline or headline in seen or link is None:
            continue
        seen.add(headline)
        conf = issue["confidence"]
        if created_at >= as_of - timedelta(hours=24) and conf is not None and 0 <= conf <= 1:
            result += conf * {"up": 1, "down": -1}.get(link.get("direction"), 0)
    return result


def replay_rank(packet, outputs):
    """Compare only over the identical frozen candidate set with all required input scores."""
    rank = packet.get("rank")
    if not rank:
        return {"status": "unavailable", "reason": packet.get("rank_unavailable_reason", "no_recent_frozen_candidate_set")}
    if len(outputs) != 2 or any(issues is None for issues in outputs.values()):
        return {"status": "unavailable", "reason": "incomplete_model_outputs"}
    market = packet["market"]
    candidates = rank["candidates"]
    incumbent = {}
    prospective = {}
    for model, output in outputs.items():
        model_history = _current_issues(output, rank, market)
        incumbent_scores = {}
        prospective_scores = {}
        for candidate in candidates:
            ticker = candidate["ticker"]
            score = _intel_axis(ticker, model_history, rank)
            incumbent_scores[ticker] = incumbent_score_with_intel(
                candidate["baseline"], candidate["axes"], candidate["weights"], score,
            )
            features = candidate["features"]
            if market == "US":
                features = {**features, "us_news_novelty": _novelty_axis(ticker, model_history, rank, bool(output))}
            prospective_scores[ticker] = prospective_score(
                market, candidate["baseline"], candidate["axes"], features,
                intel_score=score, weights=candidate["weights"],
            )
        for kind, scores in ((incumbent, incumbent_scores), (prospective, prospective_scores)):
            if all(score is not None and math.isfinite(score) for score in scores.values()):
                kind[model] = min(scores, key=lambda ticker: (-scores[ticker], ticker))
    return {
        "status": "available" if len(incumbent) == 2 or len(prospective) == 2 else "unavailable",
        "reason": None if len(incumbent) == 2 or len(prospective) == 2 else "incomplete_frozen_candidate_scores",
        "candidate_scan_date": rank["scan_date"], "candidate_count": len(candidates),
        "ranks": {
            name: {"top1": winners, "changed": len(set(winners.values())) > 1} if len(winners) == 2 else None
            for name, winners in (("incumbent", incumbent), ("prospective", prospective))
        },
    }
