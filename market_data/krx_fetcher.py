"""Korean stock data fetcher: KIS REST API → yfinance → pykrx fallback."""

import time
import logging
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

import pandas as pd

logger = logging.getLogger("money_mani.market_data.krx")

_kis_client = None


def _get_kis_client():
    """Lazy-init KisDataClient (requires KIS_API_KEY env var)."""
    global _kis_client
    if _kis_client is None:
        try:
            from market_data.kis_data_client import KisDataClient
            _kis_client = KisDataClient()
        except Exception as e:
            logger.warning(f"KisDataClient init failed: {e}")
    return _kis_client


class KRXFetcher:
    """Fetch OHLCV, fundamentals, and investor flow data from KRX.

    Priority:
      OHLCV:  KIS REST API → yfinance .KS → pykrx
      Flow:   KIS REST API → Naver scraper → pykrx
    """

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

        # 1차: KIS REST API
        kis = _get_kis_client()
        if kis is not None:
            try:
                df = kis.get_daily_ohlcv(ticker, start_fmt, end_fmt)
                if not df.empty:
                    return df
                logger.warning(f"KIS returned empty OHLCV for {ticker}, trying yfinance")
            except Exception as e:
                logger.warning(f"KIS OHLCV failed for {ticker}: {e}, trying yfinance")

        # 2차: yfinance .KS
        try:
            import yfinance as yf
            start_dt = f"{start_fmt[:4]}-{start_fmt[4:6]}-{start_fmt[6:]}"
            end_dt = f"{end_fmt[:4]}-{end_fmt[4:6]}-{end_fmt[6:]}"
            yf_ticker = f"{ticker}.KS"
            df = yf.download(yf_ticker, start=start_dt, end=end_dt, progress=False)
            if hasattr(df.columns, "levels"):
                df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            if not df.empty:
                df.index.name = "Date"
                return df[["Open", "High", "Low", "Close", "Volume"]]
            logger.warning(f"yfinance also returned empty for {yf_ticker}")
        except Exception as e:
            logger.warning(f"yfinance fallback failed for {ticker}: {e}")

        # 3차: pykrx (legacy fallback)
        try:
            from pykrx import stock as krx
            df = krx.get_market_ohlcv(start_fmt, end_fmt, ticker)
            self._wait()
            if not df.empty:
                df.columns = ["Open", "High", "Low", "Close", "Volume", "Change"]
                df.index.name = "Date"
                return df[["Open", "High", "Low", "Close", "Volume"]]
        except Exception as e:
            logger.warning(f"pykrx fallback also failed for {ticker}: {e}")

        return pd.DataFrame()

    def get_fundamentals(self, ticker: str, start: str, end: str = None) -> pd.DataFrame:
        """Get fundamental data (PER, PBR, EPS, BPS, DIV)."""
        start_fmt = start.replace("-", "")
        end_fmt = (end or datetime.now(KST).strftime("%Y%m%d")).replace("-", "")
        logger.info(f"Fetching KRX fundamentals: {ticker}")
        try:
            from pykrx import stock as krx
            df = krx.get_market_fundamental(start_fmt, end_fmt, ticker)
            self._wait()
            if df.empty:
                logger.warning(f"No fundamental data for {ticker}")
            return df
        except Exception as e:
            logger.warning(f"pykrx fundamentals failed for {ticker}: {e}")
            return pd.DataFrame()

    def get_investor_flows(self, ticker: str, start: str, end: str = None) -> pd.DataFrame:
        """Get investor trading data (외국인/기관/개인 순매수).

        Priority: KIS REST API → Naver scraper → pykrx
        """
        start_fmt = start.replace("-", "")
        end_fmt = (end or datetime.now(KST).strftime("%Y%m%d")).replace("-", "")
        logger.info(f"Fetching KRX investor flows: {ticker}")

        # 1차: KIS REST API
        kis = _get_kis_client()
        if kis is not None:
            try:
                df = kis.get_investor_flow(ticker, start_fmt, end_fmt)
                if not df.empty:
                    return df
                logger.warning(f"KIS investor flow empty for {ticker}, trying Naver")
            except Exception as e:
                logger.warning(f"KIS investor flow failed for {ticker}: {e}, trying Naver")

        # 2차: Naver Finance scraper
        try:
            from market_data.naver_flow_fetcher import NaverFlowFetcher
            naver_df = NaverFlowFetcher().get_investor_flows(ticker, start, end)
            if naver_df is not None and not naver_df.empty:
                return naver_df
        except Exception as e:
            logger.warning(f"Naver flow fallback failed for {ticker}: {e}")

        # 3차: pykrx (legacy fallback)
        try:
            from pykrx import stock as krx
            df = krx.get_market_trading_value_by_date(start_fmt, end_fmt, ticker)
            self._wait()
            if not df.empty:
                return df
        except Exception as e:
            logger.warning(f"pykrx investor flow fallback also failed for {ticker}: {e}")

        return pd.DataFrame()

    def get_top_tickers(self, market: str = "KOSPI", n: int = 30) -> list[str]:
        """Get top N tickers by market cap."""
        today = datetime.now(KST).strftime("%Y%m%d")
        logger.info(f"Fetching top {n} {market} tickers")
        try:
            from pykrx import stock as krx
            df = krx.get_market_cap(today, market=market)
            self._wait()
            if df.empty:
                yesterday = (datetime.now(KST) - timedelta(days=1)).strftime("%Y%m%d")
                df = krx.get_market_cap(yesterday, market=market)
                self._wait()
            return df.head(n).index.tolist()
        except Exception as e:
            logger.warning(f"pykrx get_top_tickers failed: {e}")
            return []

    def get_ticker_name(self, ticker: str) -> str:
        """Get company name for a ticker."""
        try:
            from pykrx import stock as krx
            return krx.get_market_ticker_name(ticker)
        except Exception as e:
            logger.warning(f"pykrx get_ticker_name failed for {ticker}: {e}")
            return ticker
