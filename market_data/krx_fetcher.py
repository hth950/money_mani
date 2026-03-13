"""Korean stock data fetcher using pykrx."""

import time
import logging
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

import pandas as pd
from pykrx import stock as krx

logger = logging.getLogger("money_mani.market_data.krx")


class KRXFetcher:
    """Fetch OHLCV, fundamentals, and investor flow data from KRX."""

    def __init__(self, delay: float = 1.0):
        self._delay = delay

    def _wait(self):
        time.sleep(self._delay)

    def get_ohlcv(self, ticker: str, start: str, end: str = None) -> pd.DataFrame:
        """Get OHLCV data for a KRX ticker.

        Args:
            ticker: KRX ticker code (e.g. '005930' for Samsung)
            start: Start date 'YYYY-MM-DD' or 'YYYYMMDD'
            end: End date (default: today)
        Returns:
            DataFrame with columns: Open, High, Low, Close, Volume
        """
        start_fmt = start.replace("-", "")
        end_fmt = (end or datetime.now(KST).strftime("%Y%m%d")).replace("-", "")
        logger.info(f"Fetching KRX OHLCV: {ticker} ({start_fmt}~{end_fmt})")
        df = krx.get_market_ohlcv(start_fmt, end_fmt, ticker)
        self._wait()
        if df.empty:
            logger.warning(f"No OHLCV data for {ticker}")
            return df
        df.columns = ["Open", "High", "Low", "Close", "Volume", "Change"]
        df.index.name = "Date"
        return df[["Open", "High", "Low", "Close", "Volume"]]

    def get_fundamentals(self, ticker: str, start: str, end: str = None) -> pd.DataFrame:
        """Get fundamental data (PER, PBR, EPS, BPS, DIV)."""
        start_fmt = start.replace("-", "")
        end_fmt = (end or datetime.now(KST).strftime("%Y%m%d")).replace("-", "")
        logger.info(f"Fetching KRX fundamentals: {ticker}")
        df = krx.get_market_fundamental(start_fmt, end_fmt, ticker)
        self._wait()
        if df.empty:
            logger.warning(f"No fundamental data for {ticker}")
        return df

    def get_investor_flows(self, ticker: str, start: str, end: str = None) -> pd.DataFrame:
        """Get investor trading data. Falls back to Naver scraper on failure."""
        start_fmt = start.replace("-", "")
        end_fmt = (end or datetime.now(KST).strftime("%Y%m%d")).replace("-", "")
        logger.info(f"Fetching KRX investor flows: {ticker}")

        df = None
        try:
            df = krx.get_market_trading_value_by_date(start_fmt, end_fmt, ticker)
            self._wait()
        except Exception as e:
            logger.warning(f"pykrx get_investor_flows failed for {ticker}: {e}")

        if df is not None and not df.empty:
            return df

        # Fallback: Naver Finance scraper
        logger.info(f"Falling back to Naver scraper for {ticker}")
        try:
            from market_data.naver_flow_fetcher import NaverFlowFetcher
            naver_df = NaverFlowFetcher().get_investor_flows(ticker, start, end)
            if naver_df is not None and not naver_df.empty:
                return naver_df
        except Exception as e:
            logger.warning(f"Naver flow fallback also failed for {ticker}: {e}")

        if df is None:
            return pd.DataFrame()
        return df

    def get_top_tickers(self, market: str = "KOSPI", n: int = 30) -> list[str]:
        """Get top N tickers by market cap."""
        today = datetime.now(KST).strftime("%Y%m%d")
        logger.info(f"Fetching top {n} {market} tickers")
        df = krx.get_market_cap(today, market=market)
        self._wait()
        if df.empty:
            yesterday = (datetime.now(KST) - timedelta(days=1)).strftime("%Y%m%d")
            df = krx.get_market_cap(yesterday, market=market)
            self._wait()
        return df.head(n).index.tolist()

    def get_ticker_name(self, ticker: str) -> str:
        """Get company name for a ticker."""
        today = datetime.now(KST).strftime("%Y%m%d")
        return krx.get_market_ticker_name(ticker)
