"""US stock data fetcher using yfinance."""

import logging
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

import pandas as pd
import yfinance as yf

logger = logging.getLogger("money_mani.market_data.us")


class USFetcher:
    """Fetch OHLCV data for US stocks via yfinance."""

    def get_ohlcv(self, ticker: str, start: str, end: str = None) -> pd.DataFrame:
        """Get OHLCV data for a US ticker.

        Args:
            ticker: US ticker symbol (e.g. 'AAPL', 'MSFT')
            start: Start date 'YYYY-MM-DD'
            end: End date (default: today)
        Returns:
            DataFrame with columns: Open, High, Low, Close, Volume
        """
        end = end or datetime.now(KST).strftime("%Y-%m-%d")
        logger.info(f"Fetching US OHLCV: {ticker} ({start}~{end})")
        stock = yf.Ticker(ticker)
        df = stock.history(start=start, end=end)
        if df.empty:
            logger.warning(f"No OHLCV data for {ticker}")
            return df
        df = df[["Open", "High", "Low", "Close", "Volume"]]
        df.index.name = "Date"
        df.index = df.index.tz_localize(None)
        return df

    def get_fundamentals(self, ticker: str) -> dict:
        """Get fundamental data from yfinance."""
        try:
            info = yf.Ticker(ticker).info
            return {
                "PER": info.get("trailingPE", None),
                "PBR": info.get("priceToBook", None),
                "DIV": (info.get("dividendYield") or 0) * 100,
                "sector": info.get("sector", "Unknown"),
            }
        except Exception as e:
            logger.warning(f"Failed to get fundamentals for {ticker}: {e}")
            return {"PER": None, "PBR": None, "DIV": 0, "sector": "Unknown"}

    def get_info(self, ticker: str) -> dict:
        """Get basic stock info (name, sector, market cap, etc.)."""
        stock = yf.Ticker(ticker)
        info = stock.info
        return {
            "name": info.get("shortName", ""),
            "sector": info.get("sector", ""),
            "market_cap": info.get("marketCap", 0),
            "currency": info.get("currency", "USD"),
        }
