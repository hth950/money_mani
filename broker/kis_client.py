"""KIS (Korea Investment & Securities) API client wrapper."""

import logging
from decimal import Decimal

from pykis import PyKis

from utils.config_loader import get_env

logger = logging.getLogger("money_mani.broker.kis")


class KISClient:
    """Wrapper around python-kis for price quotes and portfolio queries."""

    def __init__(self, hts_id: str = None, api_key: str = None,
                 api_secret: str = None, account_number: str = None):
        self._hts_id = hts_id or get_env("KIS_HTS_ID")
        self._api_key = api_key or get_env("KIS_API_KEY")
        self._api_secret = api_secret or get_env("KIS_API_SECRET")
        self._account_number = account_number or get_env("KIS_ACCOUNT_NUMBER")

        if not self._hts_id:
            raise ValueError("KIS_HTS_ID must be set in .env (your HTS login ID)")
        if not self._api_key or not self._api_secret:
            raise ValueError("KIS_API_KEY and KIS_API_SECRET must be set in .env")

        logger.info("Initializing KIS API client...")
        self._kis = PyKis(
            id=self._hts_id,
            appkey=self._api_key,
            secretkey=self._api_secret,
            account=self._account_number,
            use_websocket=False,
            keep_token=True,
        )
        logger.info("KIS API client initialized.")

    def get_domestic_price(self, ticker: str) -> dict | None:
        """Get current price for a KRX stock.

        Returns:
            Dict with open, high, low, close, volume keys, or None on error.
        """
        try:
            quote = self._kis.stock(ticker).quote()
            return {
                "Open": float(quote.open),
                "High": float(quote.high),
                "Low": float(quote.low),
                "Close": float(quote.price),
                "Volume": int(quote.volume),
            }
        except Exception as e:
            logger.warning(f"Failed to get domestic price for {ticker}: {e}")
            return None

    def get_overseas_price(self, ticker: str, market: str = "NYSE") -> dict | None:
        """Get current price for a US/overseas stock.

        Args:
            ticker: Stock symbol (e.g. 'AAPL')
            market: Exchange ('NYSE', 'NASDAQ', 'AMEX')

        Returns:
            Dict with open, high, low, close, volume keys, or None on error.
        """
        try:
            quote = self._kis.stock(ticker, market=market).quote()
            return {
                "Open": float(quote.open),
                "High": float(quote.high),
                "Low": float(quote.low),
                "Close": float(quote.price),
                "Volume": int(quote.volume),
            }
        except Exception as e:
            logger.warning(f"Failed to get overseas price for {ticker} ({market}): {e}")
            return None

    def get_domestic_holdings(self) -> list[dict]:
        """Get domestic (KRX) stock holdings.

        Returns:
            List of dicts with ticker, name, quantity, avg_price, current_price, pnl_pct, market.
        """
        try:
            balance = self._kis.account().balance(country="KR")
            holdings = []
            for stock in balance.stocks:
                qty = int(stock.quantity)
                if qty <= 0:
                    continue
                purchase = float(stock.purchase_amount)
                current = float(stock.current_price)
                avg_price = purchase / qty if qty > 0 else 0
                current_total = current * qty
                pnl_pct = ((current_total - purchase) / purchase * 100) if purchase > 0 else 0.0
                holdings.append({
                    "ticker": stock.symbol,
                    "name": getattr(stock, "name", stock.symbol),
                    "quantity": qty,
                    "avg_price": avg_price,
                    "current_price": current,
                    "pnl_pct": pnl_pct,
                    "market": "KRX",
                })
            logger.info(f"Fetched {len(holdings)} domestic holdings")
            return holdings
        except Exception as e:
            logger.error(f"Failed to get domestic holdings: {e}")
            return []

    def get_overseas_holdings(self) -> list[dict]:
        """Get overseas (US) stock holdings.

        Returns:
            Same format as get_domestic_holdings, with market='US'.
        """
        try:
            balance = self._kis.account().balance(country="US")
            holdings = []
            for stock in balance.stocks:
                qty = int(stock.quantity)
                if qty <= 0:
                    continue
                purchase = float(stock.purchase_amount)
                current = float(stock.current_price)
                avg_price = purchase / qty if qty > 0 else 0
                current_total = current * qty
                pnl_pct = ((current_total - purchase) / purchase * 100) if purchase > 0 else 0.0
                holdings.append({
                    "ticker": stock.symbol,
                    "name": getattr(stock, "name", stock.symbol),
                    "quantity": qty,
                    "avg_price": avg_price,
                    "current_price": current,
                    "pnl_pct": pnl_pct,
                    "market": "US",
                })
            logger.info(f"Fetched {len(holdings)} overseas holdings")
            return holdings
        except Exception as e:
            logger.error(f"Failed to get overseas holdings: {e}")
            return []

    def close(self):
        """Close the KIS API connection."""
        try:
            self._kis.close()
        except Exception:
            pass
