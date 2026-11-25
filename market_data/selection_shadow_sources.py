"""Provider data for the selection experiment. No scorer or broker writes."""

from __future__ import annotations

import io
import math
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests

from market_data.kis_data_client import KisDataClient
from utils.config_loader import get_env

UTC = timezone.utc
KST = timezone(timedelta(hours=9))
SEC_MAPPING = "https://www.sec.gov/files/company_tickers.json"
SEC_FORMS = {"8-K", "8-K/A", "6-K", "6-K/A", "10-Q", "10-Q/A", "10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
NAVER_TREND = "https://m.stock.naver.com/front-api/stock/domestic/trend"
DART_SEARCH = "https://opendart.fss.or.kr/api/list.json"
DART_CORPS = "https://opendart.fss.or.kr/api/corpCode.xml"
DART_ACCOUNTS = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
NAVER_BASIC = "https://m.stock.naver.com/api/stock/{ticker}/basic"
NAVER_CHART = "https://fchart.stock.naver.com/sise.nhn"
KRX_INDICES = {"KS": ("^KS11", "KOSPI"), "KQ": ("^KQ11", "KOSDAQ")}
KIS_FLOW_PATH = "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily"
SEC_ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
SECTOR_ETF = {
    "basic materials": "XLB",
    "communication services": "XLC",
    "consumer cyclical": "XLY",
    "consumer defensive": "XLP",
    "energy": "XLE",
    "financial services": "XLF",
    "healthcare": "XLV",
    "industrials": "XLI",
    "real estate": "XLRE",
    "technology": "XLK",
    "utilities": "XLU",
}


@dataclass(frozen=True)
class Observation:
    status: str
    details: dict = field(default_factory=dict)
    request_count: int | None = None


class _CountedSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.request_count = 0

    def request(self, method, url, **kwargs):
        self.request_count += 1
        return super().request(method, url, **kwargs)


def number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def utc_datetime(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def flow_window(rows: dict, dates: list[str], *, method: str, request_count: int) -> Observation:
    """Each row is foreign net, institutional net and total traded value, all in KRW."""
    details = {"method": method, "unit": "KRW", "sessions": dates, "krx_flow_5d": None}
    missing = [session for session in dates if session not in rows]
    if len(dates) != 5 or missing:
        return Observation("missing", {**details, "reason": "missing_flow_sessions", "missing_sessions": missing}, request_count)
    foreign, institution, traded = (sum(rows[session][i] for session in dates) for i in range(3))
    if traded <= 0 or abs(foreign) > traded or abs(institution) > traded:
        return Observation("missing", {**details, "reason": "invalid_traded_value"}, request_count)
    return Observation("available", {
        **details,
        "foreign_net_krw": foreign,
        "institution_net_krw": institution,
        "traded_value_krw": traded,
        "foreign_flow_5d": foreign / traded,
        "institution_flow_5d": institution / traded,
        "krx_flow_5d": (foreign + institution) / traded,
        "krx_flow_positive_share": sum(rows[day][0] + rows[day][1] > 0 for day in dates) / len(dates),
    }, request_count)


def _kis_rows(payload: dict) -> dict:
    rows = {}
    for item in payload.get("output2", []):
        session = str(item.get("stck_bsop_date", ""))
        if len(session) != 8 or not session.isdigit():
            continue
        values = [number(item.get(key)) for key in ("frgn_ntby_tr_pbmn", "orgn_ntby_tr_pbmn", "acml_tr_pbmn")]
        close, volume = number(item.get("stck_clpr")), number(item.get("acml_vol"))
        if None in values or close is None or volume is None or close <= 0 or volume <= 0:
            continue
        # FHPTJ04160001 reports investor amounts in millions and total turnover in won.
        foreign, institution, traded = values[0] * 1_000_000, values[1] * 1_000_000, values[2]
        if not .5 <= traded / (close * volume) <= 1.5:
            continue
        day = f"{session[:4]}-{session[4:6]}-{session[6:]}"
        rows[day] = (foreign, institution, traded)
    return rows


def _naver_rows(payload: dict) -> dict:
    rows = {}
    for item in payload.get("result", {}).get("items", []):
        session, day = item.get("localTradedAt"), item.get("krx") or {}
        close, volume, foreign, institution = (number(day.get(key)) for key in (
            "regularMarketClosingPrice", "tradingVolume", "foreignNetVolume", "organizationNetVolume"
        ))
        if not session or None in (close, volume, foreign, institution) or close <= 0 or volume <= 0:
            continue
        rows[session] = (foreign * close, institution * close, volume * close)
    return rows


def _hist_close(frame: pd.DataFrame, day: str) -> float | None:
    if frame.empty or "Close" not in frame:
        return None
    series = frame["Close"]
    matches = series[[str(index.date()) == day for index in series.index]]
    if len(matches) != 1:
        return None
    close = number(matches.iloc[0])
    return close if close is not None and close > 0 else None


def _annual_sec_margin(facts: dict, accession: str, period_end: str) -> dict | None:
    def fact(tags, currency=None):
        for taxonomy in ("us-gaap", "ifrs-full"):
            for tag in tags:
                for unit, entries in facts.get(taxonomy, {}).get(tag, {}).get("units", {}).items():
                    if (currency and unit != currency) or not re.fullmatch(r"[A-Z]{3}", unit):
                        continue
                    for entry in entries:
                        if entry.get("accn") != accession or entry.get("end") != period_end:
                            continue
                        try:
                            duration = date.fromisoformat(period_end) - date.fromisoformat(entry["start"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        amount = number(entry.get("val"))
                        if 330 <= duration.days <= 400 and amount is not None:
                            return amount, unit, f"{taxonomy}:{tag}"
        return None

    income = fact(("NetIncomeLoss", "ProfitLoss", "ProfitLossAttributableToOwnersOfParent"))
    if income is None:
        return None
    revenue = fact(("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "Revenue"), income[1])
    if revenue is None or revenue[0] <= 0:
        return None
    return {"value": income[0] / revenue[0], "currency": income[1], "numerator": income[0],
            "denominator": revenue[0], "line_items": [income[2], revenue[2]]}


def _dart_annual_roe(rows: list[dict], accession: str) -> dict | None:
    tags = {
        "equity": ("ifrs-full_Equity", "ifrs_Equity"),
        "income": ("ifrs-full_ProfitLoss", "ifrs_ProfitLoss", "dart_NetIncome"),
    }
    amounts = {}
    for metric, aliases in tags.items():
        for alias in aliases:
            item = next((item for item in rows if item.get("account_id") == alias
                         and item.get("rcept_no") == accession), None)
            if item is not None:
                value = number(item.get("thstrm_amount"))
                currency = item.get("currency")
                if value is not None and currency:
                    amounts[metric] = value, currency, alias
                    break
    if not {"equity", "income"}.issubset(amounts):
        return None
    (equity, equity_currency, equity_tag), (income, income_currency, income_tag) = amounts["equity"], amounts["income"]
    if equity <= 0 or equity_currency != income_currency:
        return None
    return {"value": income / equity, "currency": equity_currency, "numerator": income,
            "denominator": equity, "line_items": [income_tag, equity_tag]}


class ShadowSources:
    """Bounded provider calls, retained in memory only within one collection job."""

    def __init__(self):
        self.http = _CountedSession()
        self.kis = None
        self.sec_tickers = None
        self.sec_submissions = {}
        self.dart_cache = {}
        self.dart_corps = None
        self.benchmark_cache = {}
        self._next_sec = 0.0
        self._next_kis = 0.0

    @property
    def request_count(self) -> int:
        return self.http.request_count

    def close(self):
        self.http.close()

    def kis_flow(self, ticker: str, dates: list[str]) -> Observation:
        if not get_env("KIS_API_KEY") or not get_env("KIS_API_SECRET"):
            return Observation("missing", {"reason": "missing_credentials"}, 0)
        if self.kis is None:
            self.kis = KisDataClient()
            self.kis._session.close()
            self.kis._session = self.http
        time.sleep(max(0, self._next_kis - time.monotonic()))
        self._next_kis = time.monotonic() + .5
        payload = self.kis._get(KIS_FLOW_PATH, "FHPTJ04160001", {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker,
            "FID_INPUT_DATE_1": dates[-1].replace("-", ""),
            "FID_ORG_ADJ_PRC": "", "FID_ETC_CLS_CODE": "",
        })
        if str(payload.get("rt_cd")) != "0":
            return Observation("error", {"reason": "kis_api_error", "code": payload.get("msg_cd")}, 1)
        return flow_window(_kis_rows(payload), dates, method="kis_reported_traded_amount", request_count=1)

    def naver_flow(self, ticker: str, dates: list[str]) -> Observation:
        response = self.http.get(NAVER_TREND, params={"code": ticker}, headers={
            "User-Agent": "Mozilla/5.0", "Referer": f"https://m.stock.naver.com/domestic/stock/{ticker}/total",
        }, timeout=8)
        response.raise_for_status()
        payload = response.json()
        if payload.get("isSuccess") is not True:
            return Observation("error", {"reason": "naver_api_error", "code": payload.get("detailCode")}, 1)
        return flow_window(_naver_rows(payload), dates, method="naver_net_shares_times_regular_close", request_count=1)

    def dart(self, ticker: str, observed_at: datetime) -> Observation:
        key = get_env("DART_API_KEY")
        if not key:
            return Observation("missing", {"reason": "missing_credentials"}, 0)
        day = observed_at.astimezone(KST).date()
        request_count = 0
        if day not in self.dart_cache:
            earliest = day - timedelta(days=6)
            rows = {}
            status, reason = "available", None
            for page in range(1, 6):
                request_count += 1
                response = self.http.get(DART_SEARCH, params={
                    "crtfc_key": key, "bgn_de": earliest.strftime("%Y%m%d"),
                    "end_de": day.strftime("%Y%m%d"), "pblntf_ty": "B",
                    "page_no": page, "page_count": 100,
                }, timeout=8)
                response.raise_for_status()
                payload = response.json()
                if payload.get("status") == "013":
                    break
                if payload.get("status") != "000" or not isinstance(payload.get("list"), list):
                    status, reason = "error", "dart_api_error"
                    break
                for item in payload["list"]:
                    # DART supplies the receipt date, not an hour of publication.
                    stamp, accession, code = (item.get(k) for k in ("rcept_dt", "rcept_no", "stock_code"))
                    if not code or not accession or not earliest.strftime("%Y%m%d") <= (stamp or "") <= day.strftime("%Y%m%d"):
                        continue
                    rows.setdefault(code, {})[accession] = {
                        "id": accession, "receipt_date": stamp, "title": item.get("report_nm"),
                    }
                if page >= int(payload.get("total_page", 1)):
                    break
            else:
                status, reason = "partial", "dart_page_limit"
            self.dart_cache[day] = status, reason, rows
        status, reason, rows = self.dart_cache[day]
        items = list(rows.get(ticker, {}).values())
        return Observation(status, {
            "event_count": len(items), "events": items, "reason": reason,
            "timestamp_precision": "receipt_date", "window_days": 7, "disclosure_type": "B",
        }, request_count)

    def sec(self, ticker: str, observed_at: datetime) -> Observation:
        user_agent = get_env("SEC_USER_AGENT").strip()
        if "@" not in user_agent:
            return Observation("missing", {"reason": "sec_contact_user_agent_unconfigured"}, 0)
        headers = {"User-Agent": user_agent, "Accept": "application/json"}
        request_count = 0
        if self.sec_tickers is None:
            request_count += 1
            self.sec_tickers = {
                row["ticker"].upper().replace(".", "-"): int(row["cik_str"])
                for row in self._sec_json(SEC_MAPPING, headers).values()
            }
        cik = self.sec_tickers.get(ticker.replace(".", "-"))
        if cik is None:
            return Observation("missing", {"reason": "no_sec_cik"}, request_count)
        recent = self._sec_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", headers)["filings"]["recent"]
        self.sec_submissions[ticker] = cik, recent
        keys = ("form", "acceptanceDateTime", "accessionNumber", "items")
        if not all(isinstance(recent.get(key), list) for key in keys) or len({len(recent[key]) for key in keys}) != 1:
            return Observation("error", {"reason": "sec_filings_schema_changed"}, request_count + 1)
        lower = observed_at - timedelta(days=7)
        events, invalid = {}, 0
        for form, stamp, accession, disclosure_items in zip(*(recent[key] for key in keys)):
            if form not in SEC_FORMS:
                continue
            try:
                accepted = utc_datetime(stamp)
            except (AttributeError, ValueError):
                invalid += 1
                continue
            if lower <= accepted <= observed_at and accession:
                events[accession] = {"id": accession, "accepted_at": accepted.isoformat(), "form": form, "items": disclosure_items}
        return Observation("partial" if invalid else "available", {
            "event_count": len(events), "events": list(events.values()), "cik": cik,
            "timestamp_precision": "accepted_at", "window_days": 7,
            "reason": "sec_invalid_acceptance" if invalid else None,
        }, request_count + 1)

    def _sec_json(self, url: str, headers: dict) -> dict:
        time.sleep(max(0, self._next_sec - time.monotonic()))
        self._next_sec = time.monotonic() + .2
        response = self.http.get(url, headers=headers, timeout=8)
        response.raise_for_status()
        return response.json()

    def compare_financial(self, provider: str, ticker: str, observed_at: datetime, incumbent: dict) -> dict:
        if provider == "dart":
            as_filed, reason = self._dart_financial(ticker, observed_at)
        else:
            as_filed, reason = self._sec_financial(ticker, observed_at)
        result = {"metric": incumbent.get("metric"), "unit": "ratio", "incumbent": incumbent,
                  "as_filed": as_filed, "difference": None}
        if as_filed is None:
            return {**result, "status": "missing", "reason": reason}
        if incumbent.get("value") is None:
            return {**result, "status": "missing", "reason": "incumbent_value_unavailable"}
        if incumbent.get("period_end") != as_filed.get("period_end") or incumbent.get("basis") != as_filed.get("basis"):
            return {**result, "status": "basis_unverified", "reason": "incumbent_period_or_basis_unverified"}
        return {**result, "status": "comparable", "reason": None,
                "difference": as_filed["value"] - incumbent["value"]}

    def _sec_financial(self, ticker: str, observed_at: datetime) -> tuple[dict | None, str | None]:
        if ticker not in self.sec_submissions:
            return None, "sec_submissions_unavailable"
        cik, recent = self.sec_submissions[ticker]
        known = []
        for form, stamp, accession, period in zip(*(recent.get(key, []) for key in (
            "form", "acceptanceDateTime", "accessionNumber", "reportDate"
        ))):
            if form not in SEC_ANNUAL_FORMS or not accession or not period:
                continue
            try:
                accepted = utc_datetime(stamp)
            except (AttributeError, ValueError):
                continue
            if accepted <= observed_at:
                known.append((period, accepted, accession, form))
        if not known:
            return None, "no_annual_filing_as_of_observation"
        headers = {"User-Agent": get_env("SEC_USER_AGENT").strip(), "Accept": "application/json"}
        payload = self._sec_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", headers)
        latest_period = max(item[0] for item in known)
        for period, accepted, accession, form in sorted(known, reverse=True):
            if period != latest_period:
                break
            amounts = _annual_sec_margin(payload.get("facts", {}), accession, period)
            if amounts is not None:
                return {**amounts, "accession": accession, "period_end": period, "form": form,
                        "available_at": accepted.isoformat(), "availability_precision": "accepted_at",
                        "age_days": (observed_at - accepted).days,
                        "basis": "annual_profit_loss_over_revenue_same_filing"}, None
        return None, "annual_fact_pair_unavailable_for_verified_accession"

    def _dart_financial(self, ticker: str, observed_at: datetime) -> tuple[dict | None, str | None]:
        key = get_env("DART_API_KEY")
        if not key:
            return None, "missing_credentials"
        if self.dart_corps is None:
            response = self.http.get(DART_CORPS, params={"crtfc_key": key}, timeout=10)
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                root = ET.fromstring(archive.read("CORPCODE.xml"))
            self.dart_corps = {item.findtext("stock_code", "").strip(): item.findtext("corp_code", "").strip()
                               for item in root.findall("list")}
        corp = self.dart_corps.get(ticker)
        if not corp:
            return None, "no_dart_corp_code"
        end = observed_at.astimezone(KST).date()
        start = end - timedelta(days=550)
        response = self.http.get(DART_SEARCH, params={"crtfc_key": key, "corp_code": corp,
            "bgn_de": start.strftime("%Y%m%d"), "end_de": end.strftime("%Y%m%d"),
            "pblntf_detail_ty": "A001", "page_count": 100,
        }, timeout=8)
        response.raise_for_status()
        reports = []
        for row in response.json().get("list", []):
            period = re.search(r"\((\d{4})\.(\d{2})\)", row.get("report_nm", ""))
            accession, filed = row.get("rcept_no"), row.get("rcept_dt")
            if period and accession and start.strftime("%Y%m%d") <= (filed or "") <= end.strftime("%Y%m%d"):
                reports.append((period[1], period[2], filed, accession))
        if not reports:
            return None, "annual_dart_filing_unavailable"
        fiscal_year, month, filed, accession = max(reports)
        for standard in ("CFS", "OFS"):
            response = self.http.get(DART_ACCOUNTS, params={"crtfc_key": key, "corp_code": corp,
                "bsns_year": fiscal_year, "reprt_code": "11011", "fs_div": standard,
            }, timeout=8)
            response.raise_for_status()
            amounts = _dart_annual_roe(response.json().get("list", []), accession)
            if amounts is not None:
                filed_date = datetime.strptime(filed, "%Y%m%d").date()
                return {**amounts, "accession": accession, "receipt_date": filed_date.isoformat(),
                        "period_end": f"{fiscal_year}-{month}", "available_at": None,
                        "availability_precision": "receipt_date", "age_days": (end - filed_date).days,
                        "statement_scope": standard, "basis": "annual_profit_loss_over_year_end_equity"}, None
        return None, "annual_account_pair_unavailable_for_verified_receipt"

    def _benchmark_return(self, symbol: str, first: str, last: str) -> tuple[float | None, int]:
        import yfinance as yf

        key = symbol, first, last
        if key in self.benchmark_cache:
            return self.benchmark_cache[key], 0
        exclusive_end = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
        frame = yf.Ticker(symbol).history(start=first, end=exclusive_end, auto_adjust=True, timeout=8)
        start_close, end_close = _hist_close(frame, first), _hist_close(frame, last)
        value = end_close / start_close - 1 if start_close and end_close else None
        self.benchmark_cache[key] = value
        # yfinance may make internal cookie or crumb requests, which this call cannot instrument.
        return value, 1

    def _krx_board(self, symbol: str) -> str | None:
        code, _, board = symbol.partition(".")
        if board in KRX_INDICES:
            return board
        if not re.fullmatch(r"[0-9]{6}", code):
            return None
        response = self.http.get(NAVER_BASIC.format(ticker=code), timeout=8)
        response.raise_for_status()
        info = response.json()
        if info.get("itemCode") == code:
            return (info.get("stockExchangeType") or {}).get("code")
        return None

    def _krx_index_return(self, index: str, first: str, last: str) -> float | None:
        key = index, first, last
        if key not in self.benchmark_cache:
            response = self.http.get(NAVER_CHART, params={
                "symbol": index, "timeframe": "day", "count": 30, "requestType": 0,
            }, timeout=8)
            response.raise_for_status()
            root = ET.fromstring(response.content.decode("euc-kr"))
            chart = root.find(".//chartdata")
            prices = {}
            if chart is not None and chart.get("symbol") == index:
                for item in chart.iter("item"):
                    fields = item.get("data", "").split("|")
                    if len(fields) >= 5:
                        prices[fields[0]] = number(fields[4])
            start, end = (prices.get(day.replace("-", "")) for day in (first, last))
            value = end / start - 1 if start and start > 0 and end and end > 0 else None
            self.benchmark_cache[key] = value
        return self.benchmark_cache[key]

    def relative_strength(self, market: str, features: dict) -> Observation:
        sessions = features.get("return_sessions", [])
        if len(sessions) != 2:
            return Observation("missing", {"reason": "insufficient_aligned_sessions"}, 0)
        benchmark_source = "yfinance"
        benchmark_fetches = 0
        if market == "US":
            sector = features.get("sector") or ""
            asset = SECTOR_ETF.get(sector.casefold())
            benchmark = "SPY"
            if not asset:
                return Observation("missing", {"reason": "sector_metadata_unavailable", "sector": sector}, 0)
            asset_return, asset_fetches = self._benchmark_return(asset, *sessions)
            benchmark_return, benchmark_fetches = self._benchmark_return(benchmark, *sessions)
        else:
            asset = features.get("symbol") or ""
            board = self._krx_board(asset)
            if board not in KRX_INDICES:
                return Observation("missing", {"asset": asset, "reason": "unverified_krx_board", "sessions": sessions})
            benchmark, index = KRX_INDICES[board]
            asset_return = features.get("stock_return_5d")
            asset_fetches = 0
            try:
                benchmark_return = self._krx_index_return(index, *sessions)
            except (requests.RequestException, ET.ParseError, UnicodeError):
                benchmark_return = None
            if benchmark_return is None:
                benchmark_return, benchmark_fetches = self._benchmark_return(benchmark, *sessions)
            else:
                benchmark_source = "naver_index_chart"
        details = {
            "asset": asset, "benchmark": benchmark, "sessions": sessions, "source": benchmark_source,
            "asset_return_5d": asset_return, "benchmark_return_5d": benchmark_return,
            "symbol_history_calls": asset_fetches + benchmark_fetches,
        }
        if asset_return is None or benchmark_return is None:
            return Observation("missing", {**details, "reason": "benchmark_bars_unavailable"})
        return Observation("available", {**details, "relative_strength": asset_return - benchmark_return})
