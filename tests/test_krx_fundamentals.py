"""Network-free regression tests for KRX fundamental collection."""

import pandas as pd
import pytest
import yfinance as yf
import sys
import types

from market_data import krx_fetcher
from market_data.krx_fetcher import (
    KRXFetcher,
    download_yahoo_ohlcv,
    resolve_yahoo_symbols,
)
from scoring import data_collectors
from scoring.dart_fundamental import DARTFundamentalFetcher
from scoring.data_collectors import FundamentalCollector


@pytest.fixture(autouse=True)
def clear_fundamental_caches():
    data_collectors._fundamental_cache.clear()
    data_collectors._fundamental_failure_cache.clear()
    data_collectors._sector_yf_cache.clear()
    krx_fetcher._yahoo_symbol_cache.clear()
    krx_fetcher._ticker_name_cache.clear()
    krx_fetcher._ticker_name_failures.clear()
    yield
    data_collectors._fundamental_cache.clear()
    data_collectors._fundamental_failure_cache.clear()
    data_collectors._sector_yf_cache.clear()
    krx_fetcher._yahoo_symbol_cache.clear()
    krx_fetcher._ticker_name_cache.clear()
    krx_fetcher._ticker_name_failures.clear()


def _frame(values: dict, *, source: str = "yfinance") -> pd.DataFrame:
    frame = pd.DataFrame([values])
    frame.attrs.update({"source": source, "status": "available"})
    return frame


def _ohlcv(rows: int) -> pd.DataFrame:
    index = pd.bdate_range("2026-07-01", periods=rows)
    close = pd.Series(range(100, 100 + rows), index=index, dtype=float)
    return pd.DataFrame({
        "Open": close,
        "High": close + 1,
        "Low": close - 1,
        "Close": close,
        "Volume": 1000,
    })


@pytest.mark.parametrize(
    ("ticker", "quote_types", "expected"),
    [
        ("005930", {"005930.KS": "EQUITY"}, ("005930.KS",)),
        (
            "000250",
            {"000250.KS": "MUTUALFUND", "000250.KQ": "EQUITY"},
            ("000250.KQ",),
        ),
    ],
)
def test_yahoo_symbol_resolver_selects_confirmed_equity_and_caches(
    monkeypatch, ticker, quote_types, expected
):
    calls = []

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def get_info(self):
            calls.append(self.symbol)
            return {"quoteType": quote_types.get(self.symbol, "MUTUALFUND")}

    monkeypatch.setattr(yf, "Ticker", FakeTicker)
    assert resolve_yahoo_symbols(ticker, yf) == expected
    first_call_count = len(calls)
    assert resolve_yahoo_symbols(ticker, yf) == expected
    assert len(calls) == first_call_count


def test_yahoo_symbol_resolver_excludes_explicit_non_equities(monkeypatch):
    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def get_info(self):
            return {
                "quoteType": "MUTUALFUND" if self.symbol.endswith(".KS") else "ETF"
            }

    monkeypatch.setattr(yf, "Ticker", FakeTicker)
    assert resolve_yahoo_symbols("000250", yf) == ()


def test_metadata_failure_compares_both_suffix_data_instead_of_stopping_at_ks(
    monkeypatch,
):
    class FailingMetadataTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def get_info(self):
            raise TimeoutError(self.symbol)

    downloads = []

    def fake_download(symbol, **kwargs):
        downloads.append(symbol)
        return _ohlcv(2 if symbol.endswith(".KS") else 10)

    monkeypatch.setattr(yf, "Ticker", FailingMetadataTicker)
    monkeypatch.setattr(yf, "download", fake_download)
    result = download_yahoo_ohlcv("000250", period="6mo", yf_module=yf)

    assert downloads == ["000250.KS", "000250.KQ"]
    assert len(result) == 10
    assert result.attrs["symbol"] == "000250.KQ"


@pytest.mark.parametrize(
    ("ticker", "quote_types", "expected_symbol"),
    [
        ("005930", {"005930.KS": "EQUITY"}, "005930.KS"),
        (
            "000250",
            {"000250.KS": "MUTUALFUND", "000250.KQ": "EQUITY"},
            "000250.KQ",
        ),
    ],
)
def test_krx_ohlcv_uses_resolved_equity_symbol(
    monkeypatch, ticker, quote_types, expected_symbol
):
    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def get_info(self):
            return {"quoteType": quote_types.get(self.symbol, "MUTUALFUND")}

    downloads = []
    monkeypatch.setattr(krx_fetcher, "_get_kis_client", lambda: None)
    monkeypatch.setattr(yf, "Ticker", FakeTicker)
    monkeypatch.setattr(
        yf,
        "download",
        lambda symbol, **kwargs: downloads.append(symbol) or _ohlcv(10),
    )

    result = KRXFetcher(delay=0).get_ohlcv(
        ticker, "2026-07-01", "2026-07-14"
    )
    assert downloads == [expected_symbol]
    assert result.attrs["symbol"] == expected_symbol


def test_krx_fetcher_uses_yfinance_values_and_normalises_dividend(monkeypatch):
    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def get_info(self):
            if self.symbol.endswith(".KS"):
                return {
                    "quoteType": "EQUITY",
                    "trailingPE": 18.0,
                    "priceToBook": 1.2,
                    "returnOnEquity": 0.16,
                    "dividendYield": 0.58,
                    "dividendRate": 1_500.0,
                    "regularMarketPrice": 60_000.0,
                    "sector": "Technology",
                }
            raise AssertionError("KQ fallback should not be called after valid KS data")

    monkeypatch.setattr(yf, "Ticker", FakeTicker)
    result = KRXFetcher(delay=0).get_fundamentals(
        "005930", "2026-07-01", "2026-07-14"
    )

    assert result.attrs["source"] == "yfinance"
    assert result.attrs["symbol"] == "005930.KS"
    assert result.iloc[0]["DIV"] == pytest.approx(2.5)
    assert result.iloc[0]["ROE"] == pytest.approx(0.16)


def test_krx_fetcher_falls_back_to_kosdaq_symbol(monkeypatch):
    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def get_info(self):
            if self.symbol.endswith(".KS"):
                return {"quoteType": "MUTUALFUND", "trailingPE": 1.0}
            return {
                "quoteType": "EQUITY",
                "trailingPE": 12.0,
                "sector": "Technology",
            }

    monkeypatch.setattr(yf, "Ticker", FakeTicker)
    result = KRXFetcher(delay=0).get_fundamentals(
        "000250", "2026-07-01", "2026-07-14"
    )

    assert result.attrs["symbol"] == "000250.KQ"
    assert result.iloc[0]["PER"] == pytest.approx(12.0)


def test_sector_and_dart_market_cap_reuse_resolved_kosdaq_symbol(monkeypatch):
    calls = []

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def get_info(self):
            calls.append(self.symbol)
            if self.symbol.endswith(".KS"):
                return {
                    "quoteType": "MUTUALFUND",
                    "sector": "Wrong Fund Sector",
                    "marketCap": 1,
                }
            return {
                "quoteType": "EQUITY",
                "sector": "Healthcare",
                "marketCap": 123_000_000,
            }

    monkeypatch.setattr(yf, "Ticker", FakeTicker)
    monkeypatch.setattr(data_collectors, "_get_sector_map", lambda: {})

    assert data_collectors._get_ticker_sector_eng("000250") == "Healthcare"
    assert DARTFundamentalFetcher()._get_market_cap_yfinance("000250") == 123_000_000
    # The resolver cache ensures downstream callers cannot drift back to the
    # explicitly rejected same-code .KS fund.
    assert calls.count("000250.KS") == 1
    assert all(symbol == "000250.KQ" for symbol in calls[1:])


def test_ticker_name_fallback_ignores_non_equity_and_is_cached(monkeypatch):
    fake_pykrx = types.SimpleNamespace(
        stock=types.SimpleNamespace(get_market_ticker_name=lambda ticker: ticker)
    )
    monkeypatch.setitem(sys.modules, "pykrx", fake_pykrx)
    calls = []

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def get_info(self):
            calls.append(self.symbol)
            if self.symbol.endswith(".KS"):
                return {
                    "quoteType": "MUTUALFUND",
                    "shortName": "잘못된 동명 펀드",
                }
            return {"quoteType": "EQUITY", "shortName": "SK이터닉스"}

    monkeypatch.setattr(yf, "Ticker", FakeTicker)
    fetcher = KRXFetcher(delay=0)
    assert fetcher.get_ticker_name("475150") == "SK이터닉스"
    first_call_count = len(calls)
    assert fetcher.get_ticker_name("475150") == "SK이터닉스"
    assert len(calls) == first_call_count
    assert "475150.KS" in calls
    assert calls[-1] == "475150.KQ"


def test_collector_scores_observed_yfinance_metrics(monkeypatch):
    frame = _frame({
        "PER": 20.0,
        "PBR": 2.0,
        "ROE": 0.20,
        "DIV": 2.0,
        "sector": "Technology",
    })
    monkeypatch.setattr(KRXFetcher, "get_fundamentals", lambda *args, **kwargs: frame)

    result = FundamentalCollector().score("005930", "KRX")

    assert result["score"] == pytest.approx(0.7933, abs=0.0001)
    assert result["score"] != 0.5
    assert result["details"]["source"] == "yfinance"
    assert result["details"]["data_status"] == "available"
    assert result["details"]["available_metrics"] == ["per", "pbr", "roe", "div"]


def test_collector_marks_partial_data_and_keeps_missing_metrics_neutral(monkeypatch):
    frame = _frame({
        "PER": 10.0,
        "PBR": None,
        "ROE": None,
        "DIV": None,
        "sector": "Financial Services",
    })
    monkeypatch.setattr(KRXFetcher, "get_fundamentals", lambda *args, **kwargs: frame)

    result = FundamentalCollector().score("000000", "KRX")

    assert result["score"] != 0.5
    assert result["details"]["data_status"] == "partial"
    assert result["details"]["available_metrics"] == ["per"]
    assert result["details"]["missing_metrics"] == ["pbr", "roe", "div"]
    assert result["details"]["pbr_score"] == 0.5


def test_collector_scores_dart_fallback_including_roe(monkeypatch):
    empty = pd.DataFrame()
    empty.attrs.update({
        "source": "none",
        "status": "unavailable",
        "reason": "no_fundamental_data",
    })
    monkeypatch.setattr(KRXFetcher, "get_fundamentals", lambda *args, **kwargs: empty)
    monkeypatch.setattr(
        data_collectors,
        "_get_ticker_sector_eng",
        lambda ticker: "Technology",
    )

    def fake_dart(self, ticker):
        self.last_status = "available"
        self.last_error = None
        return {"per": 20.0, "pbr": 2.0, "roe": 0.20, "div_yield": 0.02}

    monkeypatch.setattr(DARTFundamentalFetcher, "get_financial_data", fake_dart)

    result = FundamentalCollector().score("005930", "KRX")

    assert result["score"] == pytest.approx(0.7933, abs=0.0001)
    assert result["details"]["source"] == "dart"
    assert result["details"]["roe"] == pytest.approx(0.20)
    assert result["details"]["div"] == pytest.approx(2.0)


def test_collector_distinguishes_source_error_from_missing_data(monkeypatch):
    empty = pd.DataFrame()
    empty.attrs.update({
        "source": "none",
        "status": "error",
        "reason": "yfinance:Timeout;pykrx:ImportError",
    })
    monkeypatch.setattr(KRXFetcher, "get_fundamentals", lambda *args, **kwargs: empty)

    def failed_dart(self, ticker):
        self.last_status = "error"
        self.last_error = "corp_code_download_failed:BadZipFile"
        return {}

    monkeypatch.setattr(DARTFundamentalFetcher, "get_financial_data", failed_dart)

    result = FundamentalCollector().score("005930", "KRX")

    assert result["score"] == 0.5
    assert result["details"]["source"] == "none"
    assert result["details"]["data_status"] == "error"
    assert result["details"]["reason"] == "all_fundamental_sources_failed"
    assert [attempt["status"] for attempt in result["details"]["attempts"]] == [
        "error",
        "error",
    ]
