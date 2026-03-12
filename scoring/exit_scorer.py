"""Trend-based exit scoring for open positions."""

import logging
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

KST = timezone(timedelta(hours=9))

logger = logging.getLogger("money_mani.scoring.exit_scorer")


def _load_exit_config() -> dict:
    """Load exit_strategy section from config/scoring.yaml."""
    try:
        import yaml
        from pathlib import Path
        config_path = Path(__file__).parent.parent / "config" / "scoring.yaml"
        if config_path.exists():
            with open(config_path) as f:
                full_config = yaml.safe_load(f) or {}
            return full_config.get("exit_strategy", {})
    except Exception as e:
        logger.warning(f"Failed to load exit config: {e}")
    return {}


class ExitScorer:
    """Evaluate open positions for exit signals using trend/momentum/trailing stop."""

    def __init__(self, config: dict = None):
        self.config = config or _load_exit_config()

    @property
    def enabled(self) -> bool:
        return self.config.get("enabled", True)

    def evaluate(
        self,
        ticker: str,
        market: str,
        entry_price: float,
        entry_date: str,
        ohlcv_df: pd.DataFrame,
    ) -> dict:
        """Evaluate whether to hold or sell a position.

        Args:
            ticker: Stock ticker
            market: "KRX" or "US"
            entry_price: Position entry price
            entry_date: Position entry date (YYYY-MM-DD)
            ohlcv_df: OHLCV DataFrame with columns: Open, High, Low, Close, Volume

        Returns: {
            "exit_score": 0.0~1.0,
            "decision": "SELL_EXECUTE" | "SELL_WATCH" | "HOLD",
            "reason": str,
            "scores": {"trend": float, "momentum": float, "trailing_stop": float},
            "details": { ... },
        }
        """
        weights = self.config.get("weights", {"trend": 0.35, "momentum": 0.30, "trailing_stop": 0.35})
        thresholds = self.config.get("thresholds", {"sell_execute": 0.25, "sell_watch": 0.40})

        if ohlcv_df is None or len(ohlcv_df) < 20:
            return self._hold_result("Insufficient data", ticker)

        # Check minimum holding days
        min_days = self.config.get("min_holding_days", 2)
        try:
            entry_dt = datetime.strptime(entry_date, "%Y-%m-%d")
            today = datetime.now(KST).replace(tzinfo=None)
            holding_days = (today - entry_dt).days
            if holding_days < min_days:
                return self._hold_result(f"Min holding period ({min_days}d)", ticker)
        except (ValueError, TypeError):
            holding_days = 0

        current_price = float(ohlcv_df["Close"].iloc[-1])
        pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0

        # Check hard overrides first
        override = self._check_overrides(current_price, entry_price, pnl_pct, ohlcv_df, entry_date)
        if override:
            return override

        # Score each axis
        trend_score, trend_details = self._score_trend(ohlcv_df)
        momentum_score, momentum_details = self._score_momentum(ohlcv_df)
        trailing_score, trailing_details = self._check_trailing_stop(ohlcv_df, entry_price, entry_date)

        # Weighted composite
        exit_score = (
            trend_score * weights.get("trend", 0.35)
            + momentum_score * weights.get("momentum", 0.30)
            + trailing_score * weights.get("trailing_stop", 0.35)
        )
        exit_score = round(min(1.0, max(0.0, exit_score)), 4)

        # Decision
        if exit_score < thresholds.get("sell_execute", 0.25):
            decision = "SELL_EXECUTE"
            reason = "Strong sell signal (trend + momentum bearish)"
        elif exit_score < thresholds.get("sell_watch", 0.40):
            decision = "SELL_WATCH"
            reason = "Sell watch (weakening trend)"
        else:
            decision = "HOLD"
            reason = "Hold (trend healthy)"

        result = {
            "exit_score": exit_score,
            "decision": decision,
            "reason": reason,
            "scores": {
                "trend": round(trend_score, 4),
                "momentum": round(momentum_score, 4),
                "trailing_stop": round(trailing_score, 4),
            },
            "details": {
                "current_price": current_price,
                "entry_price": entry_price,
                "pnl_pct": round(pnl_pct, 4),
                "holding_days": holding_days,
                **trend_details,
                **momentum_details,
                **trailing_details,
            },
        }

        logger.info(
            f"EXIT {ticker}: score={exit_score:.2f} [{decision}] "
            f"trend={trend_score:.2f} mom={momentum_score:.2f} trail={trailing_score:.2f} "
            f"pnl={pnl_pct:+.2%}"
        )

        return result

    def _score_trend(self, df: pd.DataFrame) -> tuple[float, dict]:
        """EMA(5) vs EMA(20) cross + MACD histogram direction."""
        closes = df["Close"].astype(float)

        # EMA cross
        ema5 = closes.ewm(span=5, adjust=False).mean()
        ema20 = closes.ewm(span=20, adjust=False).mean()

        ema5_last = float(ema5.iloc[-1])
        ema20_last = float(ema20.iloc[-1])

        # EMA cross score: 1.0 when ema5 well above ema20, 0.0 when well below
        if ema20_last > 0:
            ema_ratio = (ema5_last - ema20_last) / ema20_last
            ema_cross_score = min(1.0, max(0.0, (ema_ratio + 0.03) / 0.06))
        else:
            ema_cross_score = 0.5

        # MACD histogram
        ema12 = closes.ewm(span=12, adjust=False).mean()
        ema26 = closes.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        histogram = macd_line - signal_line

        hist_last = float(histogram.iloc[-1])
        hist_prev = float(histogram.iloc[-2]) if len(histogram) > 1 else hist_last

        # MACD trend: positive & rising = 1.0, negative & falling = 0.0
        if hist_last > 0 and hist_last > hist_prev:
            macd_score = 1.0
        elif hist_last > 0 and hist_last <= hist_prev:
            macd_score = 0.6
        elif hist_last <= 0 and hist_last > hist_prev:
            macd_score = 0.4
        else:
            macd_score = 0.0

        trend_score = ema_cross_score * 0.6 + macd_score * 0.4

        details = {
            "ema5": round(ema5_last, 2),
            "ema20": round(ema20_last, 2),
            "macd_histogram": round(hist_last, 4),
            "macd_rising": hist_last > hist_prev,
        }
        return round(trend_score, 4), details

    def _score_momentum(self, df: pd.DataFrame) -> tuple[float, dict]:
        """RSI(14) + 5-day price momentum."""
        closes = df["Close"].astype(float)

        # RSI(14)
        delta = closes.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.rolling(window=14, min_periods=14).mean()
        avg_loss = loss.rolling(window=14, min_periods=14).mean()

        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi14 = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0

        # RSI score: oversold(30) = good for holding(0.8), overbought(70) = sell pressure(0.2)
        if rsi14 <= 30:
            rsi_score = 0.8  # oversold, likely to bounce — hold
        elif rsi14 <= 50:
            rsi_score = 0.6  # neutral-bullish
        elif rsi14 <= 70:
            rsi_score = 0.4  # overbought territory
        else:
            rsi_score = 0.15  # strongly overbought — sell

        # 5-day price momentum
        if len(closes) >= 6 and float(closes.iloc[-6]) > 0:
            price_mom = (float(closes.iloc[-1]) - float(closes.iloc[-6])) / float(closes.iloc[-6])
        else:
            price_mom = 0.0

        # Momentum score: -5% -> 0.0, 0% -> 0.5, +5% -> 1.0
        mom_score = min(1.0, max(0.0, (price_mom + 0.05) / 0.10))

        momentum_score = rsi_score * 0.5 + mom_score * 0.5

        details = {
            "rsi14": round(rsi14, 2),
            "price_momentum_5d": round(price_mom, 4),
        }
        return round(momentum_score, 4), details

    def _check_trailing_stop(self, df: pd.DataFrame, entry_price: float, entry_date: str) -> tuple[float, dict]:
        """ATR(14)-based trailing stop from highest price since entry."""
        atr_multiplier = self.config.get("atr_multiplier", 2.0)

        # Filter data from entry_date onwards
        try:
            entry_dt = pd.Timestamp(entry_date)
            mask = df.index >= entry_dt
            if mask.any():
                df_since_entry = df[mask]
            else:
                df_since_entry = df
        except Exception:
            df_since_entry = df

        high_since_entry = float(df_since_entry["High"].max())
        current_price = float(df["Close"].iloc[-1])

        # ATR(14) calculation
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close_prev = df["Close"].astype(float).shift(1)

        tr1 = high - low
        tr2 = (high - close_prev).abs()
        tr3 = (low - close_prev).abs()
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr14 = float(true_range.rolling(window=14, min_periods=14).mean().iloc[-1])

        if pd.isna(atr14) or atr14 <= 0:
            atr14 = abs(current_price * 0.02)  # fallback: 2% of price

        trailing_stop_price = high_since_entry - atr_multiplier * atr14

        # Score: 1.0 if safely above stop, 0.0 if below stop
        if current_price < trailing_stop_price:
            score = 0.0
        else:
            # Distance from stop as fraction of ATR range
            distance = (current_price - trailing_stop_price) / (atr_multiplier * atr14) if atr14 > 0 else 1.0
            score = min(1.0, distance)

        details = {
            "high_since_entry": round(high_since_entry, 2),
            "atr14": round(atr14, 2),
            "trailing_stop_price": round(trailing_stop_price, 2),
            "trailing_stop_hit": current_price < trailing_stop_price,
        }
        return round(score, 4), details

    def _check_overrides(self, current_price: float, entry_price: float, pnl_pct: float,
                         df: pd.DataFrame, entry_date: str) -> dict | None:
        """Check hard stop-loss and take-profit rules."""
        stop_loss = self.config.get("stop_loss_pct", -0.05)
        take_profit = self.config.get("take_profit_pct", 0.15)

        # Stop loss
        if pnl_pct <= stop_loss:
            return {
                "exit_score": 0.0,
                "decision": "SELL_EXECUTE",
                "reason": f"STOP_LOSS: {pnl_pct:+.2%} <= {stop_loss:.0%}",
                "scores": {"trend": 0.0, "momentum": 0.0, "trailing_stop": 0.0},
                "details": {
                    "current_price": current_price,
                    "entry_price": entry_price,
                    "pnl_pct": round(pnl_pct, 4),
                    "override": "STOP_LOSS",
                },
            }

        # Take profit (only when trend is declining)
        if pnl_pct >= take_profit:
            # Check if trend is declining (EMA5 < EMA20)
            closes = df["Close"].astype(float)
            ema5 = closes.ewm(span=5, adjust=False).mean()
            ema20 = closes.ewm(span=20, adjust=False).mean()
            trend_declining = float(ema5.iloc[-1]) < float(ema20.iloc[-1])

            if trend_declining:
                return {
                    "exit_score": 0.0,
                    "decision": "SELL_EXECUTE",
                    "reason": f"TAKE_PROFIT: {pnl_pct:+.2%} >= {take_profit:.0%} (trend declining)",
                    "scores": {"trend": 0.0, "momentum": 0.0, "trailing_stop": 0.0},
                    "details": {
                        "current_price": current_price,
                        "entry_price": entry_price,
                        "pnl_pct": round(pnl_pct, 4),
                        "override": "TAKE_PROFIT",
                    },
                }

        return None

    def _hold_result(self, reason: str, ticker: str) -> dict:
        """Return a neutral HOLD result."""
        logger.debug(f"EXIT {ticker}: HOLD — {reason}")
        return {
            "exit_score": 0.5,
            "decision": "HOLD",
            "reason": reason,
            "scores": {"trend": 0.5, "momentum": 0.5, "trailing_stop": 1.0},
            "details": {},
        }
