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


class TickerConsensusTracker:
    """Track per-ticker consensus direction across all strategies.

    Fires an event only when the ticker's net consensus direction changes
    (majority of strategies flip from BUY to SELL or vice versa).
    Includes a minimum hold time between direction flips to prevent rapid oscillation.
    Urgent override: if consensus ratio >= urgent_threshold, bypass the cooldown.
    """

    def __init__(
        self,
        threshold: float = 0.55,
        hold_threshold: float = 0.50,
        min_hold_minutes: int = 60,
        urgent_threshold: float = 0.80,
    ):
        self._strategy_signals: dict[str, dict[str, int]] = {}
        self._ticker_direction: dict[str, int] = {}
        self._last_flip: dict[str, datetime] = {}
        self._threshold = threshold
        self._hold_threshold = hold_threshold
        self._min_hold = timedelta(minutes=min_hold_minutes)
        self._urgent_threshold = urgent_threshold

    def update(
        self, ticker: str, strategy_name: str, signal: int, is_holding: bool = False
    ) -> dict | None:
        """Update strategy signal for ticker. Returns consensus event dict if direction changed.

        Returns dict with keys: ticker, signal_type, buy_count, sell_count,
        total_strategies, consensus_ratio, prev_direction, urgent (bool).
        Returns None if no direction change or conditions not met.
        """
        signals_map = self._strategy_signals.setdefault(ticker, {})
        signals_map[strategy_name] = signal

        active = [s for s in signals_map.values() if s != 0]
        if len(active) < 3:
            return None

        buy_count = sum(1 for s in active if s == 1)
        sell_count = sum(1 for s in active if s == -1)
        total = len(active)
        threshold = self._hold_threshold if is_holding else self._threshold

        if buy_count / total >= threshold:
            new_dir = 1
            consensus_ratio = buy_count / total
        elif sell_count / total >= threshold:
            new_dir = -1
            consensus_ratio = sell_count / total
        else:
            return None

        prev_dir = self._ticker_direction.get(ticker, 0)
        if new_dir == prev_dir:
            return None

        last_flip = self._last_flip.get(ticker)
        is_urgent = consensus_ratio >= self._urgent_threshold
        in_cooldown = last_flip and (datetime.now(KST) - last_flip) < self._min_hold

        if in_cooldown and not is_urgent:
            return None

        self._ticker_direction[ticker] = new_dir
        self._last_flip[ticker] = datetime.now(KST)

        return {
            "ticker": ticker,
            "signal_type": "BUY" if new_dir == 1 else "SELL",
            "buy_count": buy_count,
            "sell_count": sell_count,
            "total_strategies": total,
            "consensus_ratio": consensus_ratio,
            "prev_direction": prev_dir,
            "urgent": is_urgent and bool(in_cooldown),
        }

    def preload_directions(self, days: int = 3) -> int:
        """Load last known consensus direction from DB on restart."""
        try:
            from web.db.connection import get_db
            with get_db() as conn:
                rows = conn.execute(
                    """SELECT ticker, signal_type
                       FROM signals
                       WHERE source = 'realtime_consensus'
                         AND detected_at = (
                             SELECT MAX(s2.detected_at) FROM signals s2
                             WHERE s2.ticker = signals.ticker
                               AND s2.source = 'realtime_consensus'
                         )
                         AND DATE(detected_at) >= DATE('now', ?)""",
                    (f"-{days} days",),
                ).fetchall()
            for row in rows:
                self._ticker_direction[row["ticker"]] = (
                    1 if row["signal_type"] == "BUY" else -1
                )
            logger.info(f"Preloaded {len(rows)} consensus directions from DB")
            return len(rows)
        except Exception as e:
            logger.warning(f"Failed to preload consensus directions: {e}")
            return 0

