"""Multi-layer decision scorer combining 4 scoring axes."""

import logging

logger = logging.getLogger("money_mani.scoring.multi_layer_scorer")


def _load_scoring_config() -> dict:
    """Load scoring configuration from config/scoring.yaml."""
    try:
        import yaml
        from pathlib import Path
        config_path = Path(__file__).parent.parent / "config" / "scoring.yaml"
        if config_path.exists():
            with open(config_path) as f:
                return yaml.safe_load(f)
    except Exception as e:
        logger.warning(f"Failed to load scoring config: {e}")
    # Default config
    return {
        "enabled": True,
        "fallback_on_error": True,
        "weights": {
            "KRX": {"technical": 0.35, "fundamental": 0.25, "flow": 0.25, "intel": 0.15},
            "US": {"technical": 0.50, "fundamental": 0.20, "flow": 0.0, "intel": 0.30},
        },
        "thresholds": {"execute": 0.60, "watch": 0.40},
    }


class MultiLayerScorer:
    """Combine technical, fundamental, flow, and intel scores."""

    def __init__(self):
        self.config = _load_scoring_config()
        self._collectors = None

    @property
    def enabled(self) -> bool:
        return self.config.get("enabled", True)

    def _get_collectors(self):
        """Lazy-load collectors to avoid circular imports."""
        if self._collectors is None:
            from scoring.data_collectors import FundamentalCollector, FlowCollector, MacroCollector
            from scoring.intel_scorer import IntelScorer
            self._collectors = {
                "fundamental": FundamentalCollector(),
                "flow": FlowCollector(),
                "macro": MacroCollector(),
                "intel": IntelScorer(),
            }
        return self._collectors

    def score(self, ticker: str, market: str, consensus_count: int, total_strategies: int) -> dict:
        """Calculate composite score from all 4 axes.

        Args:
            ticker: Stock ticker
            market: "KRX" or "US"
            consensus_count: Number of strategies agreeing
            total_strategies: Total strategies evaluated

        Returns: {
            "composite_score": 0.0~1.0,
            "decision": "EXECUTE" | "WATCH" | "SKIP",
            "scores": {"technical": ..., "fundamental": ..., "flow": ..., "intel": ...},
            "details": {axis: details_dict for each axis},
            "weights": {axis: weight for each axis},
        }
        """
        weights = self.config.get("weights", {}).get(market, self.config["weights"]["KRX"])
        thresholds = self.config.get("thresholds", {"execute": 0.60, "watch": 0.40})

        collectors = self._get_collectors()

        # 1. Technical score
        technical_score = consensus_count / max(total_strategies, 1)
        tech_details = {"consensus_count": consensus_count, "total_strategies": total_strategies}

        # 2. Fundamental score
        fund_result = collectors["fundamental"].score(ticker, market)
        fundamental_score = fund_result["score"]
        fund_details = fund_result["details"]

        # 3. Flow score
        flow_result = collectors["flow"].score(ticker, market)
        flow_score = flow_result["score"]
        flow_details = flow_result["details"]

        # 4. Intel score
        intel_result = collectors["intel"].score(ticker, market)
        intel_score = intel_result["score"]
        intel_details = intel_result["details"]

        # Weighted sum
        composite = (
            technical_score * weights.get("technical", 0.35) +
            fundamental_score * weights.get("fundamental", 0.25) +
            flow_score * weights.get("flow", 0.25) +
            intel_score * weights.get("intel", 0.15)
        )
        composite = round(min(1.0, max(0.0, composite)), 4)

        # Decision
        if composite >= thresholds["execute"]:
            decision = "EXECUTE"
            # Transaction cost filter: downgrade EXECUTE→WATCH if margin too thin
            decision = self._apply_transaction_cost_filter(composite, market, decision, thresholds)
        elif composite >= thresholds["watch"]:
            decision = "WATCH"
        else:
            decision = "SKIP"

        result = {
            "composite_score": composite,
            "decision": decision,
            "scores": {
                "technical": round(technical_score, 4),
                "fundamental": round(fundamental_score, 4),
                "flow": round(flow_score, 4),
                "intel": round(intel_score, 4),
            },
            "details": {
                "technical": tech_details,
                "fundamental": fund_details,
                "flow": flow_details,
                "intel": intel_details,
            },
            "weights": weights,
        }

        logger.info(
            f"SCORE {ticker}({market}): composite={composite:.2%} [{decision}] "
            f"tech={technical_score:.2f} fund={fundamental_score:.2f} "
            f"flow={flow_score:.2f} intel={intel_score:.2f}"
        )

        return result

    def _apply_transaction_cost_filter(self, composite_score: float, market: str,
                                        decision: str, thresholds: dict) -> str:
        """Downgrade EXECUTE → WATCH if score margin doesn't cover transaction costs."""
        if decision != "EXECUTE":
            return decision

        costs = self.config.get("transaction_costs", {}).get(market, {})
        min_return = costs.get("min_expected_return", 0) / 100  # 0.5% → 0.005
        if min_return <= 0:
            return decision

        execute_threshold = thresholds.get("execute", 0.60)
        score_margin = composite_score - execute_threshold

        if score_margin < min_return:
            logger.info(
                f"Transaction cost filter: margin {score_margin:.4f} < min_return {min_return:.4f}, "
                f"downgrade EXECUTE→WATCH"
            )
            return "WATCH"
        return "EXECUTE"
