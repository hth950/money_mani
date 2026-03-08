"""StrategyExtractor: converts raw analysis text into Strategy objects."""

from __future__ import annotations

from strategy.models import Strategy
from llm.strategy_refiner import StrategyRefiner


class StrategyExtractor:
    """Pipeline step that converts raw text into validated Strategy objects."""

    def _dict_to_strategy(self, d: dict, source: str = "llm_extracted") -> Strategy:
        """Convert a refiner-produced dict into a Strategy dataclass."""
        risk = d.get("risk_management", {})
        return Strategy(
            name=d.get("name", "Unnamed Strategy"),
            description=d.get("description", ""),
            source=source,
            category=d.get("category", "extracted"),
            status="draft",
            rules={
                "entry": d.get("entry_rules", []),
                "exit": d.get("exit_rules", []),
            },
            indicators=d.get("indicators", []),
            parameters={
                "position_size": risk.get("position_size", 1.0),
                "stop_loss": risk.get("stop_loss", None),
                "take_profit": risk.get("take_profit", None),
                "timeframe": d.get("timeframe", "daily"),
            },
        )

    def extract_from_analysis(
        self,
        raw_text: str,
        refiner: StrategyRefiner,
    ) -> list[Strategy]:
        """Pipe NotebookLM output through LLM refiner and return Strategy objects.

        Args:
            raw_text: Raw analysis text from NotebookLMAnalyzer.extract_strategies().
            refiner: StrategyRefiner instance for LLM-based parsing and validation.

        Returns:
            List of Strategy objects with status="draft". Empty list if none found.
        """
        strategy_dicts = refiner.refine(raw_text)
        strategies = []
        for d in strategy_dicts:
            validation = refiner.validate(d)
            if validation["is_codeable"]:
                # Merge any refined_rules back into the dict
                refined = validation.get("refined_rules", {})
                if refined.get("entry_rules"):
                    d["entry_rules"] = refined["entry_rules"]
                if refined.get("exit_rules"):
                    d["exit_rules"] = refined["exit_rules"]
                strategies.append(self._dict_to_strategy(d, source="notebooklm"))
        return strategies

    def extract_from_subtitles(
        self,
        subtitle_text: str,
        refiner: StrategyRefiner,
    ) -> list[Strategy]:
        """Fallback extractor: use raw subtitle text instead of NotebookLM analysis.

        Args:
            subtitle_text: Raw subtitle/transcript text from SubtitleExtractor.
            refiner: StrategyRefiner instance.

        Returns:
            List of Strategy objects with status="draft".
        """
        strategy_dicts = refiner.refine(subtitle_text)
        strategies = []
        for d in strategy_dicts:
            validation = refiner.validate(d)
            if validation["is_codeable"]:
                refined = validation.get("refined_rules", {})
                if refined.get("entry_rules"):
                    d["entry_rules"] = refined["entry_rules"]
                if refined.get("exit_rules"):
                    d["exit_rules"] = refined["exit_rules"]
                strategies.append(self._dict_to_strategy(d, source="subtitle"))
        return strategies
