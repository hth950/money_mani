"""Trading calendar for KRX and NYSE."""

import logging
from datetime import datetime, date, timedelta, timezone

KST = timezone(timedelta(hours=9))

logger = logging.getLogger("money_mani.market_data.calendar")

# KRX 2026 공휴일 (수동 관리)
KRX_HOLIDAYS_2026 = {
    date(2026, 1, 1),   # 신정
    date(2026, 2, 16),  # 설날 연휴
    date(2026, 2, 17),  # 설날
    date(2026, 2, 18),  # 설날 연휴
    date(2026, 3, 1),   # 삼일절
    date(2026, 5, 5),   # 어린이날
    date(2026, 5, 24),  # 부처님오신날
    date(2026, 6, 6),   # 현충일
    date(2026, 8, 15),  # 광복절
    date(2026, 9, 24),  # 추석 연휴
    date(2026, 9, 25),  # 추석
    date(2026, 9, 26),  # 추석 연휴
    date(2026, 10, 3),  # 개천절
    date(2026, 10, 9),  # 한글날
    date(2026, 12, 25), # 크리스마스
}

# NYSE 2026 공휴일
NYSE_HOLIDAYS_2026 = {
    date(2026, 1, 1),   # New Year
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),   # Independence Day (observed)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
}


class KRXCalendar:
    """Korean Exchange trading calendar."""

    def is_trading_day(self, d: date = None) -> bool:
        d = d or datetime.now(KST).date()
        if d.weekday() >= 5:
            return False
        if d in KRX_HOLIDAYS_2026:
            return False
        return True

    def next_trading_day(self, d: date = None) -> date:
        d = d or datetime.now(KST).date()
        d = d + timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def last_n_trading_days(self, n: int, from_date: date = None) -> list[date]:
        d = from_date or datetime.now(KST).date()
        days = []
        while len(days) < n:
            if self.is_trading_day(d):
                days.append(d)
            d -= timedelta(days=1)
        return list(reversed(days))

    def is_market_open(self) -> bool:
        """Check if KRX is currently open (09:00-15:30 KST)."""
        now = datetime.now(KST)
        if not self.is_trading_day(now.date()):
            return False
        market_open = now.replace(hour=9, minute=0, second=0)
        market_close = now.replace(hour=15, minute=30, second=0)
        return market_open <= now <= market_close


class NYSECalendar:
    """NYSE trading calendar."""

    def is_trading_day(self, d: date = None) -> bool:
        d = d or datetime.now(KST).date()
        if d.weekday() >= 5:
            return False
        if d in NYSE_HOLIDAYS_2026:
            return False
        return True
