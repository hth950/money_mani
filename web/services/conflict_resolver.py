"""Signal conflict resolution: aggregate conflicting signals per ticker."""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("money_mani.web.services.conflict_resolver")


@dataclass
class ConflictGroup:
    """Aggregated view of signals for a single ticker."""
    ticker: str
    ticker_name: str
    market: str
    buy_strategies: list[str] = field(default_factory=list)
    sell_strategies: list[str] = field(default_factory=list)
    consensus: str = "MIXED"  # BUY, SELL, or MIXED
    signals: list[dict] = field(default_factory=list)

    @property
    def buy_count(self) -> int:
        return len(self.buy_strategies)

    @property
    def sell_count(self) -> int:
        return len(self.sell_strategies)

    @property
    def has_conflict(self) -> bool:
        return self.buy_count > 0 and self.sell_count > 0


class ConflictResolver:
    """Group signals by ticker and detect conflicts."""

    def resolve(self, signals: list[dict]) -> dict[str, ConflictGroup]:
        """Group signals by ticker and compute consensus.

        Returns:
            dict mapping ticker -> ConflictGroup (only for tickers with 2+ signals).
        """
        groups: dict[str, ConflictGroup] = {}

        for sig in signals:
            ticker = sig["ticker"]
            if ticker not in groups:
                groups[ticker] = ConflictGroup(
                    ticker=ticker,
                    ticker_name=sig.get("ticker_name", ticker),
                    market=sig.get("market", "KRX"),
                )

            group = groups[ticker]
            group.signals.append(sig)

            if sig["signal_type"] == "BUY":
                group.buy_strategies.append(sig["strategy_name"])
            else:
                group.sell_strategies.append(sig["strategy_name"])

        # Compute consensus and filter to multi-signal tickers
        result = {}
        for ticker, group in groups.items():
            if len(group.signals) < 2:
                continue

            if group.buy_count > 0 and group.sell_count == 0:
                group.consensus = "BUY"
            elif group.sell_count > 0 and group.buy_count == 0:
                group.consensus = "SELL"
            elif group.buy_count > group.sell_count:
                group.consensus = "BUY"
            elif group.sell_count > group.buy_count:
                group.consensus = "SELL"
            else:
                group.consensus = "MIXED"

            result[ticker] = group

        if result:
            conflicts = [t for t, g in result.items() if g.has_conflict]
            if conflicts:
                logger.info(f"Conflicts detected for: {conflicts}")

        return result
