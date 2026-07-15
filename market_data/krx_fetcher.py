"""Korean stock data fetcher: KIS REST API → yfinance → pykrx fallback."""

import logging
import math
import time
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

import pandas as pd

logger = logging.getLogger("money_mani.market_data.krx")

_kis_client = None
_yahoo_symbol_cache: dict[str, str] = {}
_ticker_name_cache: dict[str, str] = {}
_ticker_name_failures: set[str] = set()


def resolve_yahoo_symbols(ticker: str, yf_module=None) -> tuple[str, ...]:
    """Return safe Yahoo candidates, preferring a confirmed EQUITY symbol.

    Korean numeric codes can exist under both Yahoo suffixes, and a same-code
    ``.KS`` instrument may be a fund rather than the requested KOSDAQ equity.
    Explicit non-equity metadata is therefore rejected.  If quote metadata is
    unavailable, both unresolved suffixes remain as fallbacks so callers can
    compare their actual data responses instead of assuming ``.KS``.
    """
    code = str(ticker).strip()
    cached = _yahoo_symbol_cache.get(code)
    if cached:
        return (cached,)
    if yf_module is None:
        import yfinance as yf_module

    unresolved = []
    for suffix in ("KS", "KQ"):
        symbol = f"{code}.{suffix}"
        try:
            stock = yf_module.Ticker(symbol)
            getter = getattr(stock, "get_info", None)
            info = getter() if callable(getter) else getattr(stock, "info", {})
            info = info if isinstance(info, dict) else {}
            quote_type = str(
                info.get("quoteType") or info.get("quote_type") or ""
            ).upper()
        except Exception as exc:
            logger.debug("Yahoo quote metadata failed for %s: %s", symbol, exc)
            unresolved.append(symbol)
            continue

        if quote_type == "EQUITY":
            _yahoo_symbol_cache[code] = symbol
            return (symbol,)
        if quote_type:
            logger.warning(
                "Ignoring Yahoo symbol %s: quoteType=%s", symbol, quote_type
            )
            continue
        unresolved.append(symbol)
    return tuple(unresolved)


def download_yahoo_ohlcv(
    ticker: str,
    *,
    start: str | None = None,
    end: str | None = None,
    period: str | None = None,
    auto_adjust: bool | None = None,
    yf_module=None,
) -> pd.DataFrame:
    """Download the best valid Yahoo OHLCV candidate for a KRX code."""
    if yf_module is None:
        import yfinance as yf_module

    downloads: list[tuple[str, pd.DataFrame]] = []
    for symbol in resolve_yahoo_symbols(ticker, yf_module):
        kwargs = {"progress": False}
        if start is not None:
            kwargs["start"] = start
        if end is not None:
            kwargs["end"] = end
        if period is not None:
            kwargs["period"] = period
        if auto_adjust is not None:
            kwargs["auto_adjust"] = auto_adjust
        try:
            frame = yf_module.download(symbol, **kwargs)
            if frame is None or frame.empty:
                continue
            frame = frame.copy()
            if hasattr(frame.columns, "levels"):
                frame.columns = [
                    column[0] if isinstance(column, tuple) else column
                    for column in frame.columns
                ]
            required = ["Open", "High", "Low", "Close", "Volume"]
            if not all(column in frame.columns for column in required):
                continue
            downloads.append((symbol, frame[required]))
        except Exception as exc:
            logger.debug("Yahoo OHLCV failed for %s: %s", symbol, exc)

    if not downloads:
        return pd.DataFrame()
    # When quote metadata was unavailable for both suffixes, the equity series
    # normally has substantially more observations than a same-code fund stub.
    symbol, best = max(downloads, key=lambda item: len(item[1]))
    best = best.copy()
    best.index.name = "Date"
    best.attrs["symbol"] = symbol
    return best


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
      OHLCV:  KIS REST API → verified yfinance .KS/.KQ → pykrx
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

        # 2차: yfinance (quoteType으로 .KS/.KQ 검증)
        try:
            start_dt = f"{start_fmt[:4]}-{start_fmt[4:6]}-{start_fmt[6:]}"
            end_dt = f"{end_fmt[:4]}-{end_fmt[4:6]}-{end_fmt[6:]}"
            df = download_yahoo_ohlcv(ticker, start=start_dt, end=end_dt)
            if not df.empty:
                return df
            logger.warning("yfinance returned no valid equity OHLCV for %s", ticker)
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
        """Get fundamental data (PER, PBR, ROE, DIV).

        yfinance is the primary source because pykrx is frequently blocked or
        unavailable in the runtime environment.  Source/status metadata is
        attached to ``DataFrame.attrs`` so callers can distinguish missing
        data from transport/dependency errors instead of silently treating all
        failures as neutral fundamentals.
        """
        start_fmt = start.replace("-", "")
        end_fmt = (end or datetime.now(KST).strftime("%Y%m%d")).replace("-", "")
        logger.info(f"Fetching KRX fundamentals: {ticker}")

        errors: list[str] = []

        # 1차: yfinance. KOSPI(.KS)와 KOSDAQ(.KQ)을 모두 시도한다.
        try:
            import yfinance as yf

            candidates: list[tuple[int, pd.DataFrame]] = []
            for symbol in resolve_yahoo_symbols(ticker, yf):
                try:
                    stock = yf.Ticker(symbol)
                    info = stock.get_info()
                    if not isinstance(info, dict):
                        info = {}

                    raw_dividend = info.get("dividendYield")
                    dividend_pct = None
                    dividend_rate = info.get("dividendRate")
                    current_price = info.get("currentPrice") or info.get("regularMarketPrice")
                    try:
                        rate = float(dividend_rate)
                        price = float(current_price)
                        if math.isfinite(rate) and math.isfinite(price) and price > 0:
                            dividend_pct = rate / price * 100.0
                    except (TypeError, ValueError):
                        pass
                    if dividend_pct is None and raw_dividend is not None:
                        try:
                            dividend_pct = float(raw_dividend)
                            if not math.isfinite(dividend_pct):
                                dividend_pct = None
                            # yfinance 1.x exposes percentage points, while
                            # older 0.x releases generally exposed a ratio.
                            elif str(getattr(yf, "__version__", "0")).split(".", 1)[0] == "0":
                                dividend_pct *= 100.0
                        except (TypeError, ValueError):
                            dividend_pct = None

                    values = {
                        "PER": info.get("trailingPE"),
                        "PBR": info.get("priceToBook"),
                        "ROE": info.get("returnOnEquity"),
                        "DIV": dividend_pct,
                        "profit_margin": info.get("profitMargins"),
                        "sector": info.get("sector"),
                        "market_cap": info.get("marketCap"),
                    }
                    def has_finite_value(key: str) -> bool:
                        try:
                            return math.isfinite(float(values.get(key)))
                        except (TypeError, ValueError):
                            return False

                    if not any(has_finite_value(key) for key in ("PER", "PBR", "ROE", "DIV")):
                        continue

                    df = pd.DataFrame([values])
                    df.attrs.update({
                        "source": "yfinance",
                        "symbol": symbol,
                        "status": "available",
                    })
                    quality = sum(
                        has_finite_value(key) for key in ("PER", "PBR", "ROE", "DIV")
                    )
                    quality += int(bool(values.get("sector")))
                    quality += int(has_finite_value("market_cap"))
                    candidates.append((quality, df))
                except Exception as e:
                    errors.append(f"yfinance:{symbol}:{type(e).__name__}")
                    logger.debug(f"yfinance fundamentals failed for {symbol}: {e}")
            if candidates:
                return max(candidates, key=lambda candidate: candidate[0])[1]
        except Exception as e:
            errors.append(f"yfinance_import:{type(e).__name__}")
            logger.warning(f"yfinance fundamentals unavailable for {ticker}: {e}")

        # 2차: pykrx (legacy fallback)
        try:
            from pykrx import stock as krx
            df = krx.get_market_fundamental(start_fmt, end_fmt, ticker)
            self._wait()
            if df.empty:
                logger.warning(f"No fundamental data for {ticker}")
            else:
                df.attrs.update({"source": "pykrx", "status": "available"})
                return df
        except Exception as e:
            errors.append(f"pykrx:{type(e).__name__}")
            logger.warning(f"pykrx fundamentals failed for {ticker}: {e}")

        empty = pd.DataFrame()
        empty.attrs.update({
            "source": "none",
            "status": "error" if errors else "unavailable",
            "reason": ";".join(errors) if errors else "no_fundamental_data",
        })
        return empty

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
        """Get and cache a KRX company name, with verified Yahoo fallback."""
        code = str(ticker).strip()
        cached = _ticker_name_cache.get(code)
        if cached:
            return cached
        if code in _ticker_name_failures:
            return code

        try:
            from pykrx import stock as krx

            name = str(krx.get_market_ticker_name(code) or "").strip()
            if name and name != code:
                _ticker_name_cache[code] = name
                return name
        except Exception as e:
            logger.warning(f"pykrx get_ticker_name failed for {code}: {e}")

        try:
            import yfinance as yf

            for symbol in resolve_yahoo_symbols(code, yf):
                try:
                    stock = yf.Ticker(symbol)
                    getter = getattr(stock, "get_info", None)
                    info = getter() if callable(getter) else getattr(stock, "info", {})
                    info = info if isinstance(info, dict) else {}
                    quote_type = str(info.get("quoteType") or "").upper()
                    if quote_type and quote_type != "EQUITY":
                        continue
                    name = str(
                        info.get("shortName") or info.get("longName") or ""
                    ).strip()
                    if name and name not in {code, symbol}:
                        _ticker_name_cache[code] = name
                        return name
                except Exception as exc:
                    logger.debug("Yahoo ticker-name lookup failed for %s: %s", symbol, exc)
        except Exception as e:
            logger.warning("Yahoo ticker-name fallback unavailable for %s: %s", code, e)

        _ticker_name_failures.add(code)
        return code
