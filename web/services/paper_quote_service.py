"""Read-only market quotes used exclusively by manual paper trading."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("money_mani.web.services.paper_quote")


class PaperQuoteUnavailable(RuntimeError):
    """Raised when neither a current quote nor a recent close is available."""


class PaperQuoteService:
    """Resolve quotes without ever invoking a broker order API.

    KRX prefers the existing KIS read-only quote wrapper.  US prefers
    yfinance's last-price field.  Both markets fall back to the most recent
    daily close and mark that quote as delayed.
    """

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _valid_price(value) -> float | None:
        try:
            price = float(value)
        except (TypeError, ValueError):
            return None
        return price if math.isfinite(price) and price > 0 else None

    def get_quote(self, market: str, ticker: str) -> dict:
        market = str(market).strip().upper()
        ticker = str(ticker).strip().upper()
        if market == "KRX":
            quote = self._get_krx_current(ticker)
        elif market == "US":
            quote = self._get_us_current(ticker)
        else:
            raise ValueError(f"Unsupported market: {market}")
        if quote:
            return quote

        fallback = self._get_recent_close(market, ticker)
        if fallback:
            return fallback
        raise PaperQuoteUnavailable(
            f"{market} {ticker}의 현재가와 최근 종가를 모두 조회할 수 없습니다."
        )

    def _get_krx_current(self, ticker: str) -> dict | None:
        try:
            from broker.kis_client import KISClient

            client = KISClient()
            try:
                raw = client.get_domestic_price(ticker)
            finally:
                client.close()
            price = self._valid_price((raw or {}).get("Close"))
            if price:
                return {
                    "price": price,
                    "source": "kis_current",
                    "price_at": self._now(),
                    "is_delayed": False,
                }
        except Exception as exc:
            logger.info("KIS quote unavailable for %s: %s", ticker, exc)
        return None

    def _get_us_current(self, ticker: str) -> dict | None:
        try:
            import yfinance as yf

            fast_info = yf.Ticker(ticker).fast_info
            if hasattr(fast_info, "get"):
                raw = fast_info.get("last_price")
            else:
                raw = getattr(fast_info, "last_price", None)
            price = self._valid_price(raw)
            if price:
                return {
                    "price": price,
                    "source": "yfinance_last_price",
                    "price_at": self._now(),
                    "is_delayed": False,
                }
        except Exception as exc:
            logger.info("US last-price unavailable for %s: %s", ticker, exc)
        return None

    def _get_recent_close(self, market: str, ticker: str) -> dict | None:
        try:
            end = datetime.now(timezone.utc).date() + timedelta(days=1)
            start = end - timedelta(days=21)
            if market == "KRX":
                from market_data.krx_fetcher import KRXFetcher

                frame = KRXFetcher(delay=0).get_ohlcv(
                    ticker, start.isoformat(), end.isoformat()
                )
                source = "krx_recent_close"
            else:
                from market_data.us_fetcher import USFetcher

                frame = USFetcher().get_ohlcv(
                    ticker, start.isoformat(), end.isoformat()
                )
                source = "yfinance_recent_close"
            if frame is None or frame.empty:
                return None
            close_column = "Close" if "Close" in frame.columns else "close"
            price = self._valid_price(frame[close_column].iloc[-1])
            if not price:
                return None
            index_value = frame.index[-1]
            try:
                price_at = index_value.isoformat()
            except AttributeError:
                price_at = str(index_value)
            return {
                "price": price,
                "source": source,
                "price_at": price_at,
                "is_delayed": True,
            }
        except Exception as exc:
            logger.warning("Recent-close fallback failed for %s %s: %s", market, ticker, exc)
            return None

    def get_ohlcv(self, market: str, ticker: str, lookback_days: int = 180):
        """Fetch daily OHLCV used by the scoring refresh."""
        end = datetime.now(timezone.utc).date() + timedelta(days=1)
        start = end - timedelta(days=max(lookback_days, 60))
        if market == "KRX":
            from market_data.krx_fetcher import KRXFetcher

            return KRXFetcher(delay=0).get_ohlcv(
                ticker, start.isoformat(), end.isoformat()
            )
        if market == "US":
            from market_data.us_fetcher import USFetcher

            return USFetcher().get_ohlcv(ticker, start.isoformat(), end.isoformat())
        raise ValueError(f"Unsupported market: {market}")
