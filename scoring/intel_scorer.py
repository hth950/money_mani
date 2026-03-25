"""Intel sentiment scorer using market_intel_issues DB data."""

import logging
from datetime import datetime, timedelta, timezone
from web.db.connection import get_db
from utils.cache import TTLCache

KST = timezone(timedelta(hours=9))
logger = logging.getLogger("money_mani.scoring.intel_scorer")

# Module-level cache: persists across IntelScorer instances created per scan
_intel_accuracy_cache: TTLCache = TTLCache(ttl=3600, maxsize=8)  # 1 hour
_intel_calibration_cache: TTLCache = TTLCache(ttl=3600, maxsize=8)  # 1 hour


class IntelScorer:
    """Score ticker sentiment from market intelligence data."""

    def __init__(self):
        pass  # Uses module-level _intel_accuracy_cache

    def _get_source_accuracy(self, days: int = 30) -> dict:
        """Get avg accuracy grouped by category from recent issues."""
        cache_key = f"accuracy:{days}"
        hit, cached = _intel_accuracy_cache.get(cache_key)
        if hit:
            return cached
        try:
            cutoff = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
            with get_db() as db:
                rows = db.execute("""
                    SELECT category, AVG(accuracy_score) as avg_accuracy, COUNT(*) as cnt
                    FROM market_intel_issues
                    WHERE accuracy_score IS NOT NULL
                      AND detection_date >= ?
                    GROUP BY category
                """, (cutoff,)).fetchall()
            result = {}
            for r in rows:
                if r["avg_accuracy"] is not None and r["cnt"] >= 3:
                    result[r["category"]] = round(r["avg_accuracy"], 4)
            _intel_accuracy_cache.set(cache_key, result)
            logger.info(f"Loaded source accuracy: {result}")
            return result
        except Exception as e:
            logger.warning(f"Source accuracy query failed: {e}")
            return {}

    def _get_calibration_factors(self, days: int = 30) -> dict:
        """카테고리별 calibration factor 계산 (accuracy/confidence 비율)."""
        cache_key = f"calibration:{days}"
        hit, cached = _intel_calibration_cache.get(cache_key)
        if hit:
            return cached
        try:
            cutoff = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
            with get_db() as db:
                rows = db.execute("""
                    SELECT category,
                           AVG(accuracy_score) as avg_accuracy,
                           AVG(confidence) as avg_confidence,
                           COUNT(*) as n
                    FROM market_intel_issues
                    WHERE accuracy_score IS NOT NULL
                      AND detection_date >= ?
                    GROUP BY category
                    HAVING COUNT(*) >= 3
                """, (cutoff,)).fetchall()
            factors = {}
            all_accuracies = []
            all_confidences = []
            for r in rows:
                avg_acc = r["avg_accuracy"]
                avg_conf = r["avg_confidence"]
                if avg_acc is not None and avg_conf and avg_conf > 0:
                    factor = avg_acc / avg_conf
                    factor = max(0.3, min(1.0, factor))
                    factors[r["category"]] = round(factor, 4)
                    all_accuracies.append(avg_acc)
                    all_confidences.append(avg_conf)
            # 전체 평균 factor (데이터 부족 카테고리용 fallback)
            if all_accuracies and all_confidences:
                overall_acc = sum(all_accuracies) / len(all_accuracies)
                overall_conf = sum(all_confidences) / len(all_confidences)
                default_factor = max(0.3, min(1.0, overall_acc / overall_conf)) if overall_conf > 0 else 0.5
            else:
                default_factor = 0.5
            result = {"_factors": factors, "_default": round(default_factor, 4)}
            _intel_calibration_cache.set(cache_key, result)
            logger.info(f"Loaded calibration factors: {factors}, default={default_factor:.4f}")
            return result
        except Exception as e:
            logger.warning(f"Calibration factor query failed: {e}")
            return {"_factors": {}, "_default": 0.5}

    def score(self, ticker: str, market: str = "KRX") -> dict:
        """Calculate intel sentiment score for a ticker.

        Queries market_intel_issues from last 7 days.
        Applies temporal decay (0.85^days) and accuracy filter (>= 0.5).

        Returns: {"score": 0.0~1.0, "details": {"raw_score": -1~1, "issue_count": N, ...}}
        """
        try:
            issues = self._get_recent_issues(ticker, days=7)
            if not issues:
                return {"score": 0.5, "details": {"raw_score": 0, "issue_count": 0, "note": "no intel data"}}

            raw = 0.0
            total_weight = 0.0
            used_count = 0
            today = datetime.now(KST).date()
            source_accuracy = self._get_source_accuracy()
            calibration_data = self._get_calibration_factors()
            calibration_factors = calibration_data.get("_factors", {})
            default_calibration = calibration_data.get("_default", 0.5)

            for issue in issues:
                # Temporal decay
                detection_date = datetime.strptime(issue["detection_date"], "%Y-%m-%d").date() if issue.get("detection_date") else today
                age_days = (today - detection_date).days
                decay = 0.85 ** age_days

                # Accuracy filter: skip issues with known low accuracy
                accuracy = issue.get("accuracy_score")
                if accuracy is not None and accuracy < 0.5:
                    continue

                used_count += 1
                confidence = issue.get("confidence", 0.5)
                category = issue.get("category", "unknown")
                accuracy_weight = source_accuracy.get(category, 0.5)
                calibration = calibration_factors.get(category, default_calibration)
                calibrated_confidence = confidence * calibration
                weight = calibrated_confidence * decay * accuracy_weight
                direction = issue.get("direction", "neutral")

                if direction == "up":
                    raw += weight
                elif direction == "down":
                    raw -= weight
                total_weight += weight

            if total_weight == 0:
                return {"score": 0.5, "details": {"raw_score": 0, "issue_count": len(issues), "note": "all filtered"}}

            intel_raw = raw / total_weight  # -1 ~ 1
            intel_score = (intel_raw + 1) / 2  # 0 ~ 1

            return {
                "score": round(intel_score, 4),
                "details": {
                    "raw_score": round(intel_raw, 4),
                    "issue_count": len(issues),
                    "used_issues": used_count,
                    "total_weight": round(total_weight, 4),
                    "source_accuracy": source_accuracy,
                }
            }
        except Exception as e:
            logger.warning(f"Intel scoring failed for {ticker}: {e}")
            return {"score": 0.5, "details": {"error": str(e)}}

    def _get_recent_issues(self, ticker: str, days: int = 7) -> list[dict]:
        """Query market_intel_issues for ticker mentions in last N days."""
        cutoff = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")

        with get_db() as db:
            rows = db.execute("""
                SELECT id, title, category, sentiment, confidence,
                       affected_tickers_json, accuracy_score, detection_date
                FROM market_intel_issues
                WHERE detection_date >= ?
                ORDER BY detection_date DESC
            """, (cutoff,)).fetchall()

        # Filter issues that mention this ticker
        import json
        result = []
        for row in rows:
            try:
                tickers_data = json.loads(row["affected_tickers_json"] or "[]")
                for t in tickers_data:
                    t_code = t.get("ticker", "")
                    if t_code == ticker:
                        result.append({
                            "issue_id": row["id"],
                            "title": row["title"],
                            "sentiment": row["sentiment"],
                            "confidence": row["confidence"] or 0.5,
                            "direction": t.get("direction", "neutral"),
                            "accuracy_score": row["accuracy_score"],
                            "detection_date": row["detection_date"],
                        })
                        break
            except (json.JSONDecodeError, TypeError):
                continue

        return result
