"""Portfolio-level risk management gate."""

import logging
import json
from datetime import datetime, timedelta, timezone

import yaml
from pathlib import Path
from web.db.connection import get_db

KST = timezone(timedelta(hours=9))
logger = logging.getLogger("money_mani.scoring.risk_manager")


def _load_risk_config() -> dict:
    try:
        config_path = Path(__file__).parent.parent / "config" / "risk.yaml"
        if config_path.exists():
            with open(config_path) as f:
                return yaml.safe_load(f)
    except Exception as e:
        logger.warning(f"Failed to load risk config: {e}")
    return {
        "enabled": True,
        "max_positions": 10,
        "max_single_weight": 0.20,
        "max_sector_weight": 0.30,
        "max_daily_loss": -0.03,
    }


class PortfolioRiskManager:
    """Check portfolio-level risk constraints before allowing new positions."""

    def __init__(self):
        self.config = _load_risk_config()
        self._sector_cache = {}  # ticker -> sector

    @property
    def enabled(self) -> bool:
        return self.config.get("enabled", True)

    def check_can_buy(self, ticker: str, market: str = "KRX") -> tuple[bool, str]:
        """Check if a new BUY is allowed given current portfolio risk state.

        Returns: (allowed: bool, reason: str)
        """
        if not self.enabled:
            return True, "risk management disabled"

        try:
            # Get current open positions
            positions = self._get_open_positions()

            # Check 1: Max positions
            max_pos = self.config.get("max_positions", 10)
            if len(positions) >= max_pos:
                return False, f"포지션 한도 초과 ({len(positions)}/{max_pos})"

            # Check 2: Duplicate ticker
            open_tickers = {p["ticker"] for p in positions}
            if ticker in open_tickers:
                return False, f"이미 포지션 보유 중: {ticker}"

            # Check 3: Sector concentration
            sector = self._get_sector(ticker, market)
            if sector and sector != "Unknown":
                sector_count = sum(1 for p in positions if self._get_sector(p["ticker"], p.get("market", "KRX")) == sector)
                total = len(positions) + 1  # including new position
                sector_weight = (sector_count + 1) / total if total > 0 else 0
                max_sector = self.config.get("max_sector_weight", 0.30)
                if sector_weight > max_sector:
                    return False, f"섹터 집중도 초과: {sector} ({sector_weight:.0%} > {max_sector:.0%})"

            # Check 4: Daily loss limit
            max_loss = self.config.get("max_daily_loss", -0.03)
            daily_pnl = self._get_daily_pnl()
            if daily_pnl < max_loss:
                return False, f"일일 손실 한도 초과 ({daily_pnl:.1%} < {max_loss:.1%})"

            return True, "OK"

        except Exception as e:
            logger.error(f"Risk check error: {e}")
            return True, f"risk check error (allowing): {e}"

    def get_risk_status(self) -> dict:
        """Get current portfolio risk status summary."""
        positions = self._get_open_positions()
        max_pos = self.config.get("max_positions", 10)
        daily_pnl = self._get_daily_pnl()

        # Sector breakdown
        sectors = {}
        for p in positions:
            s = self._get_sector(p["ticker"], p.get("market", "KRX"))
            sectors[s] = sectors.get(s, 0) + 1

        return {
            "positions_count": len(positions),
            "max_positions": max_pos,
            "daily_pnl": round(daily_pnl, 4),
            "max_daily_loss": self.config.get("max_daily_loss", -0.03),
            "sector_breakdown": sectors,
            "max_sector_weight": self.config.get("max_sector_weight", 0.30),
            "risk_enabled": self.enabled,
        }

    def _get_open_positions(self) -> list[dict]:
        try:
            with get_db() as db:
                rows = db.execute("""
                    SELECT ticker, ticker_name, market, entry_price, entry_date
                    FROM positions WHERE status = 'open'
                """).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def _get_daily_pnl(self) -> float:
        """Get today's realized + unrealized P&L as a fraction."""
        try:
            today = datetime.now(KST).strftime("%Y-%m-%d")
            with get_db() as db:
                row = db.execute("""
                    SELECT COALESCE(SUM(pnl_pct), 0) / 100.0 as total_pnl
                    FROM signal_performance
                    WHERE signal_date = ?
                """, (today,)).fetchone()
            return row["total_pnl"] if row else 0.0
        except Exception:
            return 0.0

    def _get_sector(self, ticker: str, market: str) -> str:
        if ticker in self._sector_cache:
            return self._sector_cache[ticker]

        sector = "Unknown"
        try:
            if market == "KRX":
                from market_data.fdr_fetcher import FDRFetcher
                listings = FDRFetcher().get_krx_listings()
                if listings is not None and not listings.empty:
                    match = listings[listings.index == ticker]
                    if not match.empty and "Sector" in match.columns:
                        sector = match.iloc[0]["Sector"] or "Unknown"
            else:
                from market_data.us_fetcher import USFetcher
                info = USFetcher().get_fundamentals(ticker)
                sector = info.get("sector", "Unknown")
        except Exception:
            pass

        self._sector_cache[ticker] = sector
        return sector
