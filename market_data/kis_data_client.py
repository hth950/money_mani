"""KIS REST API client for historical OHLCV and investor flow data.

Separate from broker/kis_client.py (pykis) — this calls KIS REST directly
for market data: daily OHLCV and investor buy/sell flow.

Rate limit: KIS API max 20 req/sec → safe margin 15 req/sec.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from utils.config_loader import get_env

logger = logging.getLogger("money_mani.market_data.kis_data")

KST = timezone(timedelta(hours=9))
KIS_BASE = "https://openapi.koreainvestment.com:9443"

# 15 req/sec safe margin
_MIN_INTERVAL = 1.0 / 15


class KisDataClient:
    """KIS REST API client for historical market data (OHLCV + investor flow)."""

    def __init__(self):
        self._app_key = get_env("KIS_API_KEY")
        self._app_secret = get_env("KIS_API_SECRET")
        self._token: str | None = None
        self._token_expires: datetime | None = None
        self._last_call: float = 0.0
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        """Get or refresh OAuth2 Bearer token (valid 24h)."""
        now = datetime.now(KST)
        if self._token and self._token_expires and now < self._token_expires:
            return self._token

        resp = self._session.post(
            f"{KIS_BASE}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self._app_key,
                "appsecret": self._app_secret,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        # Token valid for ~24h; refresh 10 min before expiry
        expires_in = int(data.get("expires_in", 86400))
        self._token_expires = now + timedelta(seconds=expires_in - 600)
        logger.info("KIS token acquired.")
        return self._token

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        self._last_call = time.monotonic()

    # ------------------------------------------------------------------
    # Common request helper
    # ------------------------------------------------------------------

    def _get(self, path: str, tr_id: str, params: dict) -> dict:
        self._rate_limit()
        headers = {
            "authorization": f"Bearer {self._get_token()}",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
            "tr_id": tr_id,
            "Content-Type": "application/json; charset=utf-8",
        }
        resp = self._session.get(
            f"{KIS_BASE}{path}", headers=headers, params=params, timeout=15
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # OHLCV: 일봉 (FHKST03010100)
    # ------------------------------------------------------------------

    def get_daily_ohlcv(self, ticker: str, start: str, end: str = None) -> pd.DataFrame:
        """Fetch daily OHLCV from KIS REST API.

        Args:
            ticker: KRX ticker code (e.g. '005930')
            start: 'YYYY-MM-DD' or 'YYYYMMDD'
            end: 'YYYY-MM-DD' or 'YYYYMMDD' (default: today)

        Returns:
            DataFrame with columns: Open, High, Low, Close, Volume, index=Date
        """
        start_fmt = start.replace("-", "")
        end_fmt = (end or datetime.now(KST).strftime("%Y%m%d")).replace("-", "")

        all_rows: list[dict] = []
        cursor = end_fmt  # KIS returns data backward: newest first, paginate by moving cursor back

        logger.info(f"KIS OHLCV fetch: {ticker} {start_fmt}~{end_fmt}")

        for _ in range(30):  # max 30 pages × 100 bars = 3000 bars (~12 years)
            try:
                data = self._get(
                    "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                    tr_id="FHKST03010100",
                    params={
                        "FID_COND_MRKT_DIV_CODE": "J",
                        "FID_INPUT_ISCD": ticker,
                        "FID_INPUT_DATE_1": start_fmt,
                        "FID_INPUT_DATE_2": cursor,
                        "FID_PERIOD_DIV_CODE": "D",
                        "FID_ORG_ADJ_PRC": "0",
                    },
                )
            except Exception as e:
                logger.warning(f"KIS OHLCV page failed for {ticker}: {e}")
                break

            output2 = data.get("output2", [])
            if not output2:
                break

            for row in output2:
                date_str = row.get("stck_bsop_date", "")
                if not date_str or date_str < start_fmt:
                    continue
                try:
                    all_rows.append({
                        "Date": pd.Timestamp(f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"),
                        "Open": float(row.get("stck_oprc", 0) or 0),
                        "High": float(row.get("stck_hgpr", 0) or 0),
                        "Low": float(row.get("stck_lwpr", 0) or 0),
                        "Close": float(row.get("stck_clpr", 0) or 0),
                        "Volume": float(row.get("acml_vol", 0) or 0),
                    })
                except (ValueError, TypeError):
                    continue

            # Check if we've fetched back far enough
            oldest_date = output2[-1].get("stck_bsop_date", "")
            if not oldest_date or oldest_date <= start_fmt:
                break

            # Move cursor back to one day before oldest
            dt = datetime.strptime(oldest_date, "%Y%m%d") - timedelta(days=1)
            cursor = dt.strftime("%Y%m%d")

        if not all_rows:
            return pd.DataFrame()

        df = pd.DataFrame(all_rows).drop_duplicates("Date").sort_values("Date")
        df = df.set_index("Date")
        df.index.name = "Date"
        return df[["Open", "High", "Low", "Close", "Volume"]]

    # ------------------------------------------------------------------
    # Investor flow: 외국인/기관 순매수 (FHKST01010900)
    # ------------------------------------------------------------------

    def get_investor_flow(self, ticker: str, start: str, end: str = None) -> pd.DataFrame:
        """Fetch daily investor buy/sell flow from KIS REST API.

        Returns:
            DataFrame with columns: 외국인합계, 기관합계, 개인합계, index=Date
        """
        start_fmt = start.replace("-", "")
        end_fmt = (end or datetime.now(KST).strftime("%Y%m%d")).replace("-", "")

        all_rows: list[dict] = []
        cursor = end_fmt

        logger.info(f"KIS investor flow fetch: {ticker} {start_fmt}~{end_fmt}")

        for _ in range(20):
            try:
                data = self._get(
                    "/uapi/domestic-stock/v1/quotations/inquire-investor",
                    tr_id="FHKST01010900",
                    params={
                        "FID_COND_MRKT_DIV_CODE": "J",
                        "FID_INPUT_ISCD": ticker,
                        "FID_INPUT_DATE_1": start_fmt,
                        "FID_INPUT_DATE_2": cursor,
                        "FID_ETC_CLS_CODE": "",
                        "FID_PERIOD_DIV_CODE": "D",
                    },
                )
            except Exception as e:
                logger.warning(f"KIS investor flow page failed for {ticker}: {e}")
                break

            output = data.get("output", [])
            if not output:
                break

            for row in output:
                date_str = row.get("stck_bsop_date", "")
                if not date_str or date_str < start_fmt:
                    continue
                try:
                    all_rows.append({
                        "Date": pd.Timestamp(f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"),
                        "외국인합계": int(row.get("frgn_ntby_qty", 0) or 0),
                        "기관합계": int(row.get("orgn_ntby_qty", 0) or 0),
                        "개인합계": int(row.get("prsn_ntby_qty", 0) or 0),
                    })
                except (ValueError, TypeError):
                    continue

            oldest_date = output[-1].get("stck_bsop_date", "")
            if not oldest_date or oldest_date <= start_fmt:
                break

            dt = datetime.strptime(oldest_date, "%Y%m%d") - timedelta(days=1)
            cursor = dt.strftime("%Y%m%d")

        if not all_rows:
            return pd.DataFrame()

        df = pd.DataFrame(all_rows).drop_duplicates("Date").sort_values("Date")
        df = df.set_index("Date")
        df.index.name = "Date"
        return df[["외국인합계", "기관합계", "개인합계"]]
