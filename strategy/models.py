"""Strategy dataclass model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Strategy:
    name: str
    description: str
    source: str
    category: str
    status: str
    rules: dict
    indicators: list
    parameters: dict
    backtest_results: dict | None = None
    market: str = "ALL"  # "KRX", "US", or "ALL"
    timeframe: str = "1d"
    strategy_type: str = "indicator"  # "indicator" | "factor"

    @classmethod
    def from_yaml(cls, data: dict) -> "Strategy":
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            source=data.get("source", ""),
            category=data.get("category", ""),
            status=data.get("status", "draft"),
            rules=data.get("rules", {}),
            indicators=data.get("indicators", []),
            parameters=data.get("parameters", {}),
            backtest_results=data.get("backtest_results", None),
            market=data.get("market", "ALL"),
            timeframe=data.get("timeframe", "1d"),
            strategy_type=data.get("strategy_type", "indicator"),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "category": self.category,
            "status": self.status,
            "rules": self.rules,
            "indicators": self.indicators,
            "parameters": self.parameters,
            "backtest_results": self.backtest_results,
            "market": self.market,
            "timeframe": self.timeframe,
            "strategy_type": self.strategy_type,
        }
