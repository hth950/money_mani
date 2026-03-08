"""Stock listing and global data fetcher using FinanceDataReader."""

import logging

import pandas as pd
import FinanceDataReader as fdr

logger = logging.getLogger("money_mani.market_data.fdr")


class FDRFetcher:
    """Fetch stock listings and global market data via FinanceDataReader."""

    def get_krx_listings(self, market: str = "KRX") -> pd.DataFrame:
        """Get all stock listings for a Korean market.

        Args:
            market: 'KRX' (all), 'KOSPI', 'KOSDAQ', 'KONEX'
        """
        logger.info(f"Fetching {market} listings")
        return fdr.StockListing(market)

    def get_us_listings(self, exchange: str = "NASDAQ") -> pd.DataFrame:
        """Get US stock listings.

        Args:
            exchange: 'NASDAQ', 'NYSE', 'AMEX'
        """
        logger.info(f"Fetching {exchange} listings")
        return fdr.StockListing(exchange)

    def get_index(self, symbol: str, start: str, end: str = None) -> pd.DataFrame:
        """Get index data (e.g. 'KS11' for KOSPI, 'DJI' for Dow Jones)."""
        logger.info(f"Fetching index: {symbol}")
        return fdr.DataReader(symbol, start, end)

    def get_exchange_rate(self, pair: str = "USD/KRW", start: str = "2020-01-01") -> pd.DataFrame:
        """Get exchange rate data."""
        logger.info(f"Fetching exchange rate: {pair}")
        return fdr.DataReader(pair, start)
