"""LLM-based strategy refiner: raw text -> structured strategy dicts."""

import json
from llm.client import BaseLLMClient, create_llm_client
from llm.prompts import STRATEGY_REFINE_PROMPT, STRATEGY_VALIDATE_PROMPT


class StrategyRefiner:
    """Parse and validate strategy dicts from raw analysis text using LLM."""

    def __init__(self, client: BaseLLMClient | None = None):
        self._client = client or create_llm_client()

    def _parse_json(self, raw: str) -> object:
        """Parse JSON from LLM response, stripping markdown fences."""
        raw = raw.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            # parts[1] is inside the first fence
            raw = parts[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())

    def refine(self, raw_analysis: str) -> list[dict]:
        """Parse NotebookLM analysis text into a list of structured strategy dicts.

        Args:
            raw_analysis: Free-form text from NotebookLM or subtitle extraction.

        Returns:
            List of strategy dicts matching the YAML schema, or empty list on failure.
        """
        prompt = STRATEGY_REFINE_PROMPT.format(raw_analysis=raw_analysis)
        try:
            raw = self._client.chat(
                [{"role": "user", "content": prompt}],
                model="default",
                max_tokens=4096,
            )
            result = self._parse_json(raw)
            if isinstance(result, list):
                return result
            return []
        except (json.JSONDecodeError, KeyError, ValueError):
            return []

    def validate(self, strategy_dict: dict) -> dict:
        """Check if a strategy dict is codeable; refine rules if LLM suggests fixes.

        Args:
            strategy_dict: A single strategy dict from refine().

        Returns:
            Dict with keys: is_codeable (bool), issues (list), refined_rules (dict).
            On parse failure returns is_codeable=False with the original strategy unchanged.
        """
        prompt = STRATEGY_VALIDATE_PROMPT.format(strategy_dict=json.dumps(strategy_dict, ensure_ascii=False, indent=2))
        try:
            raw = self._client.chat(
                [{"role": "user", "content": prompt}],
                model="default",
                max_tokens=1024,
            )
            result = self._parse_json(raw)
            return {
                "is_codeable": bool(result.get("is_codeable", False)),
                "issues": result.get("issues", []),
                "refined_rules": result.get("refined_rules", {}),
            }
        except (json.JSONDecodeError, KeyError, ValueError):
            return {"is_codeable": False, "issues": ["LLM response parse error"], "refined_rules": {}}
