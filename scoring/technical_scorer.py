"""Direct indicator-based technical scoring.

Replaces consensus_count / total_strategies with transparent indicator scoring.
Indicators: RSI, MACD, MA position (20/60/200), Bollinger Bands, Volume trend.
"""

import logging

import pandas as pd

logger = logging.getLogger("money_mani.scoring.technical_scorer")


def _safe_float(value) -> float | None:
    """Convert to float, return None if NaN/error."""
    try:
        v = float(value)
        return v if v == v else None  # NaN != NaN
    except Exception:
        return None


class TechnicalScorer:
    """Compute technical score from OHLCV data using direct indicator scoring.

    All indicators produce a 0~1 score where higher = more bullish/attractive.
    The composite is a weighted average of individual indicator scores.
    """

    # Indicator weights (sum = 1.0)
    WEIGHTS = {
        "rsi": 0.25,
        "macd": 0.25,
        "ma": 0.25,
        "bb": 0.15,
        "volume": 0.10,
    }

    def score(self, ticker: str, ohlcv_df: pd.DataFrame) -> dict:
        """Calculate technical score from OHLCV history.

        Args:
            ticker: stock ticker (for logging only)
            ohlcv_df: DataFrame with OHLCV columns (any case)

        Returns:
            {"score": 0.0~1.0, "details": {indicator: value, ...}}
        """
        try:
            import pandas_ta as ta
        except ImportError:
            logger.warning("pandas_ta not available, returning neutral 0.5")
            return {"score": 0.5, "details": {"error": "pandas_ta_not_available"}}

        # Normalize column names to lowercase (handle MultiIndex from yfinance)
        df = ohlcv_df.copy()
        if hasattr(df.columns, "levels"):
            df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
        else:
            df.columns = [c[0].lower() if isinstance(c, tuple) else str(c).lower() for c in df.columns]

        if "close" not in df.columns:
            logger.warning(f"{ticker}: no close column in {list(df.columns)}")
            return {"score": 0.5, "details": {"error": "no_close_column"}}

        if len(df) < 20:
            return {"score": 0.5, "details": {"error": "insufficient_data", "rows": len(df)}}

        close = df["close"].dropna()
        details: dict = {}

        # 1. RSI score
        rsi_score = self._rsi_score(close, ta, details)

        # 2. MACD score
        macd_score = self._macd_score(close, ta, details)

        # 3. MA position score (vs MA20/MA60/MA200)
        ma_score = self._ma_score(close, details)

        # 4. Bollinger Band position score
        bb_score = self._bb_score(df, close, ta, details)

        # 5. Volume trend score
        vol_score = self._volume_score(df, details)

        # Weighted composite
        w = self.WEIGHTS
        composite = (
            rsi_score * w["rsi"]
            + macd_score * w["macd"]
            + ma_score * w["ma"]
            + bb_score * w["bb"]
            + vol_score * w["volume"]
        )
        composite = round(min(1.0, max(0.0, composite)), 4)

        details.update({
            "rsi_score": round(rsi_score, 4),
            "macd_score": round(macd_score, 4),
            "ma_score": round(ma_score, 4),
            "bb_score": round(bb_score, 4),
            "volume_score": round(vol_score, 4),
        })

        logger.debug(
            f"{ticker} technical: {composite:.3f} "
            f"(rsi={rsi_score:.2f} macd={macd_score:.2f} "
            f"ma={ma_score:.2f} bb={bb_score:.2f} vol={vol_score:.2f})"
        )
        return {"score": composite, "details": details}

    # ------------------------------------------------------------------
    # Indicator sub-scorers
    # ------------------------------------------------------------------

    def _rsi_score(self, close: pd.Series, ta, details: dict) -> float:
        """RSI < 30 → oversold (high score), RSI > 70 → overbought (low score)."""
        try:
            rsi_series = ta.rsi(close, length=14)
            rsi = _safe_float(rsi_series.iloc[-1]) if rsi_series is not None else None
        except Exception:
            rsi = None

        details["rsi"] = round(rsi, 2) if rsi is not None else None

        if rsi is None:
            return 0.5
        if rsi <= 25:
            return 0.90
        if rsi <= 30:
            return 0.80
        if rsi <= 40:
            return 0.65
        if rsi <= 50:
            return 0.55
        if rsi <= 60:
            return 0.45
        if rsi <= 70:
            return 0.35
        if rsi <= 80:
            return 0.20
        return 0.10

    def _macd_score(self, close: pd.Series, ta, details: dict) -> float:
        """MACD histogram direction + value. Positive & growing = bullish."""
        try:
            macd_df = ta.macd(close, fast=12, slow=26, signal=9)
            if macd_df is None or macd_df.empty or len(macd_df.dropna()) < 2:
                return 0.5

            # Find histogram column (contains 'h' but not 'macd' prefix)
            hist_col = next(
                (c for c in macd_df.columns if "h" in c.lower() and "macd" not in c.lower()),
                None,
            )
            if hist_col is None:
                # fallback: last column is usually histogram
                hist_col = macd_df.columns[-1]

            hist = macd_df[hist_col].dropna()
            if len(hist) < 2:
                return 0.5

            curr = _safe_float(hist.iloc[-1])
            prev = _safe_float(hist.iloc[-2])

            if curr is None:
                return 0.5

            details["macd_hist"] = round(curr, 6)

            # Positive + growing = 0.75, positive + shrinking = 0.60
            # Negative + shrinking = 0.25, negative + growing (toward 0) = 0.40
            if curr > 0:
                return 0.75 if (prev is None or curr >= prev) else 0.60
            else:
                return 0.40 if (prev is None or curr >= prev) else 0.25

        except Exception as e:
            logger.debug(f"MACD error: {e}")
            return 0.5

    def _ma_score(self, close: pd.Series, details: dict) -> float:
        """Price vs MA20/MA60/MA200. Slightly below MA = mean-reversion opportunity."""
        scores = []
        current = _safe_float(close.iloc[-1])
        if current is None or current == 0:
            return 0.5

        for period in [20, 60, 200]:
            if len(close) < period:
                continue
            ma = _safe_float(close.rolling(period).mean().iloc[-1])
            if not ma or ma == 0:
                continue

            ratio = (current - ma) / ma  # +: above MA, -: below MA
            details[f"ma{period}_ratio"] = round(ratio, 4)

            # Scoring: slightly below MA (mean reversion) = good entry
            # Deeply below or deeply above = less favorable
            if ratio < -0.15:
                s = 0.70  # significantly oversold
            elif ratio < -0.05:
                s = 0.65  # mildly below MA
            elif ratio < 0.05:
                s = 0.55  # near MA
            elif ratio < 0.15:
                s = 0.45  # moderately above
            else:
                s = 0.30  # significantly overbought

            scores.append(s)

        if not scores:
            return 0.5
        return round(sum(scores) / len(scores), 4)

    def _bb_score(self, df: pd.DataFrame, close: pd.Series, ta, details: dict) -> float:
        """Bollinger Band position: near lower band = high score, upper = low."""
        try:
            if len(df) < 20:
                return 0.5

            bb = ta.bbands(close, length=20, std=2.0)
            if bb is None or bb.empty:
                return 0.5

            cols = {c.split("_")[1].lower(): c for c in bb.columns if "_" in c}
            lower_col = cols.get("l") or next((c for c in bb.columns if "l" in c.lower()), None)
            upper_col = cols.get("u") or next((c for c in bb.columns if "u" in c.lower()), None)

            if not lower_col or not upper_col:
                return 0.5

            lower = _safe_float(bb[lower_col].iloc[-1])
            upper = _safe_float(bb[upper_col].iloc[-1])
            current = _safe_float(close.iloc[-1])

            if None in (lower, upper, current) or upper == lower:
                return 0.5

            # Position: 0.0 = at upper band, 1.0 = at lower band
            bb_pos = (upper - current) / (upper - lower)
            bb_pos = max(0.0, min(1.0, bb_pos))
            details["bb_position"] = round(bb_pos, 4)

            # Near lower band = buy signal → score 0.20~0.80
            return round(0.20 + bb_pos * 0.60, 4)

        except Exception as e:
            logger.debug(f"BB error: {e}")
            return 0.5

    def _volume_score(self, df: pd.DataFrame, details: dict) -> float:
        """Recent 5-day volume vs 20-day average. Higher = more interest."""
        try:
            if "volume" not in df.columns or len(df) < 20:
                return 0.5

            vol = df["volume"].replace(0, float("nan")).dropna()
            if len(vol) < 20:
                return 0.5

            vol5 = _safe_float(vol.tail(5).mean())
            vol20 = _safe_float(vol.tail(20).mean())

            if not vol5 or not vol20 or vol20 == 0:
                return 0.5

            ratio = vol5 / vol20
            details["volume_ratio"] = round(ratio, 3)

            if ratio >= 2.0:
                return 0.75
            if ratio >= 1.5:
                return 0.65
            if ratio >= 1.2:
                return 0.58
            if ratio >= 0.8:
                return 0.50
            if ratio >= 0.5:
                return 0.42
            return 0.35

        except Exception:
            return 0.5
