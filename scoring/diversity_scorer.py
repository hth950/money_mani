"""Ensemble diversity scorer - penalizes parameter-variant strategy voting."""

import logging
import math
from collections import defaultdict

logger = logging.getLogger("money_mani.scoring.diversity_scorer")


class DiversityScorer:
    """Weight ensemble votes by category diversity."""

    def __init__(self, min_category_diversity: int = 3):
        self.min_category_diversity = min_category_diversity

    def score_ensemble(
        self,
        signals_by_strategy: dict[str, dict],
        signal_type: str = "BUY",
    ) -> dict:
        """Score ensemble signals with category diversity weighting.

        Args:
            signals_by_strategy: {strategy_name: {"category": str, "signal_type": str, ...}}
            signal_type: "BUY" or "SELL" - which signal to score

        Returns: {
            "weighted_score": float,
            "raw_count": int,
            "agreeing_categories": int,
            "total_categories": int,
            "category_breakdown": {category: {"count": N, "weight_per_strategy": float}},
            "diversity_bonus": float,
            "meets_diversity_min": bool,
        }
        """
        # Filter strategies agreeing on signal_type
        agreeing = {
            name: info for name, info in signals_by_strategy.items()
            if info.get("signal_type") == signal_type
        }

        if not agreeing:
            return {
                "weighted_score": 0.0,
                "raw_count": 0,
                "agreeing_categories": 0,
                "total_categories": 0,
                "category_breakdown": {},
                "diversity_bonus": 0.0,
                "meets_diversity_min": False,
            }

        # Group by category
        by_category = defaultdict(list)
        for name, info in agreeing.items():
            cat = info.get("category", "unknown")
            by_category[cat].append(name)

        # Calculate weighted votes: 1/sqrt(N) per strategy in category of N
        category_breakdown = {}
        total_weighted = 0.0

        for cat, names in by_category.items():
            n = len(names)
            weight = 1.0 / math.sqrt(n)  # Diminishing returns for same category
            category_breakdown[cat] = {
                "count": n,
                "weight_per_strategy": round(weight, 4),
                "weighted_votes": round(n * weight, 4),  # = sqrt(N)
            }
            total_weighted += n * weight

        # Diversity bonus: agreeing_categories / total possible categories
        all_categories = set()
        for info in signals_by_strategy.values():
            all_categories.add(info.get("category", "unknown"))
        total_cats = len(all_categories) if all_categories else 1
        agreeing_cats = len(by_category)
        diversity_bonus = agreeing_cats / total_cats

        # Final weighted score = weighted_votes * diversity_bonus
        weighted_score = total_weighted * diversity_bonus
        meets_min = agreeing_cats >= self.min_category_diversity

        result = {
            "weighted_score": round(weighted_score, 4),
            "raw_count": len(agreeing),
            "agreeing_categories": agreeing_cats,
            "total_categories": total_cats,
            "category_breakdown": category_breakdown,
            "diversity_bonus": round(diversity_bonus, 4),
            "meets_diversity_min": meets_min,
        }

        logger.info(
            f"Diversity {signal_type}: raw={len(agreeing)} weighted={weighted_score:.2f} "
            f"categories={agreeing_cats}/{total_cats} bonus={diversity_bonus:.2f} "
            f"min_met={meets_min}"
        )

        return result
