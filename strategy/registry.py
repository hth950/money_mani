"""Strategy registry: load/save/list strategies from config/strategies/."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

from .models import Strategy


def _safe_filename(name: str) -> str:
    """Return a bounded filename component without path semantics."""
    value = str(name)
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    if sanitized in {"", ".", ".."}:
        sanitized = "strategy"
    if len(sanitized.encode("utf-8")) > 180:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        prefix = sanitized.encode("utf-8")[:140].decode("utf-8", errors="ignore")
        sanitized = prefix + "_" + digest
    return sanitized

_STRATEGIES_DIR = Path(__file__).parent.parent / "config" / "strategies"


class StrategyRegistry:
    def __init__(self, strategies_dir: Path | None = None):
        configured = Path(strategies_dir) if strategies_dir else _STRATEGIES_DIR
        self._dir = configured.expanduser().resolve()
        self._dir.mkdir(parents=True, exist_ok=True)

    def _candidate(self, name: str) -> Path:
        """Build a strategy path and prove it remains inside the registry."""
        candidate = self._dir / f"{_safe_filename(name)}.yaml"
        if candidate.parent.resolve() != self._dir:
            raise ValueError("strategy filename escapes the registry directory")
        return candidate

    def _yaml_files(self):
        """Yield regular, non-symlink YAML files contained by the registry."""
        for path in sorted(self._dir.glob("*.yaml")):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                if path.resolve().parent != self._dir:
                    continue
            except OSError:
                continue
            yield path

    def list_strategies(self) -> list[str]:
        """Return strategy names (yaml stems) from the strategies directory."""
        return [p.stem for p in self._yaml_files()]

    def load(self, name: str) -> Strategy:
        """Load a Strategy by filename stem or internal name."""
        # Filenames always pass through the same path-free normalization used
        # by save/delete. Never probe a raw external strategy name as a path.
        path = self._candidate(name)
        if path.is_file() and not path.is_symlink():
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return Strategy.from_yaml(data)

        # Fallback: search by internal name field
        for p in self._yaml_files():
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
        path = self._candidate(name)
        if path.is_file() and not path.is_symlink():
            return path
        # Search by internal name
        for p in self._yaml_files():
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data and data.get("name") == name:
                return p
        return None

    def save_strategy(self, strategy: Strategy) -> None:
        """Save a Strategy, overwriting existing file if found."""
        existing = self._find_file(strategy.name)
        path = existing or self._candidate(strategy.name)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(strategy.to_dict(), f, allow_unicode=True, sort_keys=False)

    def delete_strategy(self, name: str) -> bool:
        """Delete only the contained YAML file matching an internal name."""
        path = self._find_file(name)
        if path is None:
            return False
        if path.parent.resolve() != self._dir or path.is_symlink():
            raise ValueError("refusing to delete a strategy outside the registry")
        path.unlink()
        return True

    def get_validated(self) -> list[Strategy]:
        """Return all strategies with status 'validated' or 'validated_v2'."""
        result = []
        for name in self.list_strategies():
            try:
                strat = self.load(name)
                if strat.status in ("validated", "validated_v2"):
                    result.append(strat)
            except Exception:
                pass
        return result
