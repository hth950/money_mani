"""Signal state tracker: detect transitions and prevent duplicate alerts."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

logger = logging.getLogger("money_mani.monitor.signal_tracker")


@dataclass
class SignalEvent:
    ticker: str
    strategy_name: str
    previous_signal: int
    current_signal: int


class SignalTracker:
    """Track per-(ticker, strategy) signal state to detect transitions.

    Only fires alerts on state transitions (0->1, 0->-1, 1->-1, -1->1).
    Repeated same-signal (1->1, -1->-1) and signal-end (1->0, -1->0) are silent.
    Cooldown prevents rapid-fire alerts from noisy crossover oscillations.
    """

    def __init__(self, cooldown_minutes: int = 30):
        self._states: dict[tuple[str, str], int] = {}
        self._last_alert: dict[tuple[str, str], datetime] = {}
        self._cooldown = timedelta(minutes=cooldown_minutes)

    def update(self, ticker: str, strategy_name: str, signal: int) -> SignalEvent | None:
        """Update signal state and return SignalEvent if a transition occurred.

        Args:
            ticker: Stock ticker.
            strategy_name: Strategy name.
            signal: Current signal value (1=BUY, -1=SELL, 0=HOLD).

        Returns:
            SignalEvent if alert should fire, None otherwise.
        """
        key = (ticker, strategy_name)
        prev = self._states.get(key, 0)
        self._states[key] = signal

        # No alert if signal is 0 (hold) or same as previous
        if signal == 0 or signal == prev:
            return None

        # Check cooldown
        last = self._last_alert.get(key)
        if last and (datetime.now(KST) - last) < self._cooldown:
            logger.debug(f"Cooldown active for {key}, suppressing alert")
            return None

        # Transition detected
        self._last_alert[key] = datetime.now(KST)
        logger.info(f"Signal transition: {ticker}/{strategy_name} {prev} -> {signal}")
        return SignalEvent(
            ticker=ticker,
            strategy_name=strategy_name,
            previous_signal=prev,
            current_signal=signal,
        )

    def reset(self, ticker: str = None) -> None:
        """Clear state for a specific ticker or all."""
        if ticker:
            keys_to_remove = [k for k in self._states if k[0] == ticker]
            for k in keys_to_remove:
                del self._states[k]
                self._last_alert.pop(k, None)
        else:
            self._states.clear()
            self._last_alert.clear()


    def preload_states(self, days: int = 3) -> int:
        """Load last known signal states from DB to prevent re-alerting on restart.

        Args:
            days: Look back N days for most recent signal per (ticker, strategy).

        Returns:
            Number of states loaded.
        """
        try:
            from web.db.connection import get_db
            with get_db() as conn:
                rows = conn.execute(
                    """SELECT strategy_name, ticker, signal_type
                       FROM signals
                       WHERE detected_at = (
                           SELECT MAX(s2.detected_at) FROM signals s2
                           WHERE s2.strategy_name = signals.strategy_name
                             AND s2.ticker = signals.ticker
                       )
                       AND DATE(detected_at) >= DATE('now', ?)""",
                    (f"-{days} days",),
                ).fetchall()
            for row in rows:
                sig_val = 1 if row["signal_type"] == "BUY" else -1
                self._states[(row["ticker"], row["strategy_name"])] = sig_val
            logger.info(f"Preloaded {len(rows)} signal states from DB (last {days} days)")
            return len(rows)
        except Exception as e:
            logger.warning(f"Failed to preload signal states: {e}")
            return 0

