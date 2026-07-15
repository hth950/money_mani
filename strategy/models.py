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
    allowed_signal_types: list = field(default_factory=lambda: ["BUY", "SELL"])

    @classmethod
    def from_yaml(cls, data: dict) -> "Strategy":
        # Newer strategy files use ``markets`` while the dataclass historically
        # exposed a singular ``market`` field.  Normalize the two forms here so
        # a KR-only/US-only strategy cannot silently run in both universes.
        market = data.get("market")
        if not market:
            markets = data.get("markets")
            if isinstance(markets, str):
                market = markets
            elif isinstance(markets, (list, tuple, set)):
                normalized = {str(value).upper() for value in markets if value}
                if len(normalized) == 1:
                    market = next(iter(normalized))
                elif normalized:
                    market = "ALL"
        market = market or "ALL"

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
            market=market,
            timeframe=data.get("timeframe", "1d"),
            strategy_type=data.get("strategy_type", "indicator"),
            allowed_signal_types=data.get("allowed_signal_types", ["BUY", "SELL"]),
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
            "allowed_signal_types": self.allowed_signal_types,
        }
