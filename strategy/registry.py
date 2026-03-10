"""Strategy registry: load/save/list strategies from config/strategies/."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .models import Strategy


def _safe_filename(name: str) -> str:
    """Sanitize strategy name for use as filename."""
    return re.sub(r'[<>:"/\\|?*]', '_', name)

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

        # Try sanitized filename
        safe = _safe_filename(name)
        if safe != name:
            path = self._dir / f"{safe}.yaml"
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

    def _find_file(self, name: str) -> Path | None:
        """Find the YAML file for a strategy by name."""
        path = self._dir / f"{name}.yaml"
        if path.exists():
            return path
        safe = _safe_filename(name)
        if safe != name:
            path = self._dir / f"{safe}.yaml"
            if path.exists():
                return path
        # Search by internal name
        for p in self._dir.glob("*.yaml"):
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data and data.get("name") == name:
                return p
        return None

    def save_strategy(self, strategy: Strategy) -> None:
        """Save a Strategy, overwriting existing file if found."""
        existing = self._find_file(strategy.name)
        path = existing or (self._dir / f"{_safe_filename(strategy.name)}.yaml")
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
