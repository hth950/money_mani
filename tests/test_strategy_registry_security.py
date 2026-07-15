"""Filesystem boundary tests for externally generated strategy names."""

from __future__ import annotations

from pathlib import Path

import pytest

from strategy.models import Strategy
from strategy.registry import StrategyRegistry


def _strategy(name: str) -> Strategy:
    return Strategy(
        name=name,
        description="test",
        source="external transcript",
        category="test",
        status="draft",
        rules={},
        indicators=[],
        parameters={},
    )


@pytest.mark.parametrize(
    "malicious_name",
    ("../settings", "../../outside", "/tmp/money-mani-escape", "..\\settings"),
)
def test_strategy_names_cannot_read_write_or_delete_outside_registry(
    tmp_path: Path, malicious_name: str
):
    config_dir = tmp_path / "config"
    strategies_dir = config_dir / "strategies"
    strategies_dir.mkdir(parents=True)
    settings = config_dir / "settings.yaml"
    settings.write_text("sentinel: unchanged\n", encoding="utf-8")

    registry = StrategyRegistry(strategies_dir)
    with pytest.raises(FileNotFoundError):
        registry.load(malicious_name)

    registry.save_strategy(_strategy(malicious_name))
    assert settings.read_text(encoding="utf-8") == "sentinel: unchanged\n"
    saved_files = list(strategies_dir.glob("*.yaml"))
    assert len(saved_files) == 1
    assert saved_files[0].resolve().parent == strategies_dir.resolve()
    assert registry.load(malicious_name).name == malicious_name

    assert registry.delete_strategy(malicious_name) is True
    assert settings.read_text(encoding="utf-8") == "sentinel: unchanged\n"
    assert not list(strategies_dir.glob("*.yaml"))


def test_registry_ignores_symlinks_that_leave_strategy_directory(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text("name: outside\n", encoding="utf-8")
    link = strategies_dir / "outside.yaml"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not available")

    registry = StrategyRegistry(strategies_dir)
    assert registry.list_strategies() == []
    with pytest.raises(FileNotFoundError):
        registry.load("outside")
