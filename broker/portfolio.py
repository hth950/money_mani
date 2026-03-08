"""Portfolio manager: fetch and cache holdings from KIS API."""

import logging
from dataclasses import dataclass

from broker.kis_client import KISClient

logger = logging.getLogger("money_mani.broker.portfolio")


@dataclass
class HoldingInfo:
    ticker: str
    name: str
    quantity: int
    avg_price: float
    current_price: float
    pnl_pct: float
    market: str  # "KRX" or "US"


class PortfolioManager:
    """High-level portfolio operations over KISClient."""

    def __init__(self, kis_client: KISClient):
        self._kis = kis_client
        self._holdings: dict[str, HoldingInfo] = {}

    def fetch_all_holdings(self) -> dict[str, HoldingInfo]:
        """Fetch all holdings (KRX + US) and return as dict keyed by ticker."""
        self._holdings = {}

        for raw in self._kis.get_domestic_holdings():
            h = HoldingInfo(**raw)
            self._holdings[h.ticker] = h

        for raw in self._kis.get_overseas_holdings():
            h = HoldingInfo(**raw)
            self._holdings[h.ticker] = h

        logger.info(f"Total holdings: {len(self._holdings)} stocks")
        return self._holdings

    def get_portfolio_tickers(self, market: str = None) -> list[str]:
        """Return ticker list, optionally filtered by market."""
        if not self._holdings:
            self.fetch_all_holdings()
        if market:
            return [t for t, h in self._holdings.items() if h.market == market]
        return list(self._holdings.keys())

    def get_holding(self, ticker: str) -> HoldingInfo | None:
        """Look up a single holding by ticker."""
        return self._holdings.get(ticker)

    def refresh(self):
        """Re-fetch holdings (call on market session transitions)."""
        logger.info("Refreshing portfolio holdings...")
        self.fetch_all_holdings()
