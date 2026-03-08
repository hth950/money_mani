"""Market session manager: determine which markets are currently open."""

import logging
from datetime import datetime, time, timedelta, date
from zoneinfo import ZoneInfo

from market_data.calendar import KRXCalendar, NYSECalendar

logger = logging.getLogger("money_mani.monitor.market_session")


class MarketSession:
    """Determine which markets are open, handle KST/ET timezone conversions."""

    KST = ZoneInfo("Asia/Seoul")
    ET = ZoneInfo("America/New_York")

    KRX_OPEN = time(9, 0)
    KRX_CLOSE = time(15, 30)
    NYSE_OPEN = time(9, 30)
    NYSE_CLOSE = time(16, 0)

    def __init__(self):
        self.krx_cal = KRXCalendar()
        self.nyse_cal = NYSECalendar()

    def is_krx_open(self) -> bool:
        """Check if KRX is currently in trading hours."""
        now_kst = datetime.now(self.KST)
        if not self.krx_cal.is_trading_day(now_kst.date()):
            return False
        return self.KRX_OPEN <= now_kst.time() <= self.KRX_CLOSE

    def is_us_open(self) -> bool:
        """Check if NYSE/NASDAQ is currently in trading hours."""
        now_et = datetime.now(self.ET)
        if not self.nyse_cal.is_trading_day(now_et.date()):
            return False
        return self.NYSE_OPEN <= now_et.time() <= self.NYSE_CLOSE

    def get_active_markets(self) -> list[str]:
        """Return list of currently open markets."""
        markets = []
        if self.is_krx_open():
            markets.append("KRX")
        if self.is_us_open():
            markets.append("US")
        return markets

    def next_session_info(self) -> dict:
        """Get info about the next market session opening.

        Returns:
            Dict with market, opens_at_kst (str), seconds_until (int).
        """
        now_kst = datetime.now(self.KST)
        candidates = []

        # Next KRX open
        krx_next = self._next_krx_open(now_kst)
        if krx_next:
            diff = (krx_next - now_kst).total_seconds()
            candidates.append(("KRX", krx_next, int(diff)))

        # Next US open (compute in ET, convert to KST)
        us_next = self._next_us_open(now_kst)
        if us_next:
            diff = (us_next - now_kst).total_seconds()
            candidates.append(("US", us_next, int(diff)))

        if not candidates:
            return {"market": "N/A", "opens_at_kst": "unknown", "seconds_until": 300}

        # Return the soonest
        candidates.sort(key=lambda x: x[2])
        market, opens_at, secs = candidates[0]
        return {
            "market": market,
            "opens_at_kst": opens_at.strftime("%Y-%m-%d %H:%M KST"),
            "seconds_until": max(secs, 0),
        }

    def _next_krx_open(self, now_kst: datetime) -> datetime | None:
        """Find the next KRX opening time from now_kst."""
        d = now_kst.date()
        # If today is a trading day and market hasn't opened yet
        if self.krx_cal.is_trading_day(d) and now_kst.time() < self.KRX_OPEN:
            return now_kst.replace(hour=9, minute=0, second=0, microsecond=0)
        # Find next trading day
        d += timedelta(days=1)
        for _ in range(10):
            if self.krx_cal.is_trading_day(d):
                return datetime(d.year, d.month, d.day, 9, 0, tzinfo=self.KST)
            d += timedelta(days=1)
        return None

    def _next_us_open(self, now_kst: datetime) -> datetime | None:
        """Find the next US market opening time, returned in KST."""
        now_et = now_kst.astimezone(self.ET)
        d = now_et.date()
        # If today is a trading day and market hasn't opened yet
        if self.nyse_cal.is_trading_day(d) and now_et.time() < self.NYSE_OPEN:
            opens_et = datetime(d.year, d.month, d.day, 9, 30, tzinfo=self.ET)
            return opens_et.astimezone(self.KST)
        # Find next trading day
        d += timedelta(days=1)
        for _ in range(10):
            if self.nyse_cal.is_trading_day(d):
                opens_et = datetime(d.year, d.month, d.day, 9, 30, tzinfo=self.ET)
                return opens_et.astimezone(self.KST)
            d += timedelta(days=1)
        return None

    def get_us_hours_kst(self) -> str:
        """Return current US market hours in KST for display."""
        now_et = datetime.now(self.ET)
        open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        open_kst = open_et.astimezone(self.KST)
        close_kst = close_et.astimezone(self.KST)
        dst = "EDT" if open_et.dst() else "EST"
        return f"{open_kst.strftime('%H:%M')}~{close_kst.strftime('%H:%M')} KST ({dst})"
