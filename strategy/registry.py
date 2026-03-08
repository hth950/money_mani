"""Strategy registry: load/save/list strategies from config/strategies/."""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import Strategy

_STRATEGIES_DIR = Path(__file__).parent.parent / "config" / "strategies"


class StrategyRegistry:
    def __init__(self, strategies_dir: Path | None = None):
        self._dir = Path(strategies_dir) if strategies_dir else _STRATEGIES_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    def list_strategies(self) -> list[str]:
        """Return strategy names (yaml stems) from the strategies directory."""
        return [p.stem for p in sorted(self._dir.glob("*.yaml"))]

    def load(self, name: str) -> Strategy:
        """Load a Strategy by filename stem or internal name."""
        # Try exact filename match first
        path = self._dir / f"{name}.yaml"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return Strategy.from_yaml(data)

        # Fallback: search by internal name field
        for p in self._dir.glob("*.yaml"):
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data and data.get("name") == name:
                return Strategy.from_yaml(data)

        raise FileNotFoundError(f"Strategy not found: {name}")

    # Alias kept for compatibility
    def load_strategy(self, name: str) -> Strategy:
        return self.load(name)

    def save_strategy(self, strategy: Strategy) -> None:
        """Save a Strategy to config/strategies/{name}.yaml."""
        path = self._dir / f"{strategy.name}.yaml"
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(strategy.to_dict(), f, allow_unicode=True, sort_keys=False)

    def get_validated(self) -> list[Strategy]:
        """Return all strategies with status == 'validated'."""
        result = []
        for name in self.list_strategies():
            try:
                strat = self.load(name)
                if strat.status == "validated":
                    result.append(strat)
            except Exception:
                pass
        return result
