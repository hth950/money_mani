"""Signal state tracker: detect transitions and prevent duplicate alerts."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

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
        if last and (datetime.now() - last) < self._cooldown:
            logger.debug(f"Cooldown active for {key}, suppressing alert")
            return None

        # Transition detected
        self._last_alert[key] = datetime.now()
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
