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
            with open(config_path, encoding="utf-8") as f:
                return yaml.safe_load(f)
    except Exception as e:
        logger.warning(f"Failed to load scoring config: {e}")
    # Default config
    return {
        "enabled": True,
        "fallback_on_error": True,
        "weights": {
            "KRX": {"technical": 0.50, "fundamental": 0.10, "flow": 0.20, "intel": 0.10, "macro": 0.10},
            "US": {"technical": 0.50, "fundamental": 0.10, "flow": 0.0, "intel": 0.25, "macro": 0.15},
        },
        "thresholds": {"execute": 0.65, "watch": 0.40},
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

    def score(
        self,
        ticker: str,
        market: str,
        consensus_count: int = 0,
        total_strategies: int = 1,
        ohlcv_df=None,
    ) -> dict:
        """Calculate composite score from all 5 axes.

        Args:
            ticker: Stock ticker
            market: "KRX" or "US"
            ohlcv_df: OHLCV DataFrame for indicator-based technical scoring (preferred)
            consensus_count: Fallback if ohlcv_df not provided
            total_strategies: Fallback if ohlcv_df not provided

        Returns: {
            "composite_score": 0.0~1.0,
            "decision": "EXECUTE" | "WATCH" | "SKIP",
            "scores": {"technical": ..., "fundamental": ..., "flow": ..., "intel": ..., "macro": ...},
            "details": {axis: details_dict for each axis},
            "weights": {axis: weight for each axis},
        }
        """
        weights = self.config.get("weights", {}).get(market, self.config["weights"]["KRX"])
        thresholds = self.config.get("thresholds", {"execute": 0.60, "watch": 0.40})

        collectors = self._get_collectors()

        # 1. Technical score — indicator-based (preferred) or consensus fallback
        if ohlcv_df is not None and not ohlcv_df.empty:
            from scoring.technical_scorer import TechnicalScorer
            tech_result = TechnicalScorer().score(ticker, ohlcv_df)
            technical_score = tech_result["score"]
            tech_details = tech_result["details"]
            tech_details["method"] = "indicator"
        else:
            technical_score = consensus_count / max(total_strategies, 1)
            tech_details = {
                "consensus_count": consensus_count,
                "total_strategies": total_strategies,
                "method": "consensus",
            }

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

        # 5. Macro score
        macro_result = collectors["macro"].score(market=market)
        macro_score = macro_result["score"]
        macro_details = macro_result["details"]

        # Flow neutral detection: redistribute flow weight when KRX supply data unavailable
        weights = dict(weights)  # work on a copy
        flow_is_neutral = (
            market == "KRX"
            and flow_result.get("score") == 0.5
            and all(v == 0.5 for v in flow_result.get("details", {}).values())
        )
        if flow_is_neutral and weights.get("flow", 0.0) > 0.0:
            flow_w = weights["flow"]
            other_axes = [k for k in weights if k != "flow"]
            other_total = sum(weights[k] for k in other_axes)
            if other_total > 0:
                for k in other_axes:
                    weights[k] += flow_w * (weights[k] / other_total)
                weights["flow"] = 0.0
                logger.info(
                    f"Flow neutral detected for {ticker}({market}): "
                    f"redistributed flow weight {flow_w:.2f} to {other_axes} → {weights}"
                )

        # Weight-sum validation
        weight_sum = sum(weights.values())
        if abs(weight_sum - 1.0) > 0.01:
            logger.warning(f"Weights don't sum to 1.0: {weight_sum}, normalizing")
            weights = {k: v / weight_sum for k, v in weights.items()}

        # Weighted sum
        composite = (
            technical_score * weights.get("technical", 0.50) +
            fundamental_score * weights.get("fundamental", 0.10) +
            flow_score * weights.get("flow", 0.20) +
            intel_score * weights.get("intel", 0.10) +
            macro_score * weights.get("macro", 0.10)
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
                "macro": macro_score,
            },
            "details": {
                "technical": tech_details,
                "fundamental": fund_details,
                "flow": flow_details,
                "intel": intel_details,
                "macro": macro_details,
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
