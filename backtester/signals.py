"""Signal generation from Strategy indicator/rule definitions."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pandas_ta as ta

from strategy.models import Strategy

logger = logging.getLogger("money_mani.backtester.signals")

# Supported indicator types -> pandas_ta function names
_INDICATOR_MAP = {
    "sma": "sma",
    "ema": "ema",
    "rsi": "rsi",
    "macd": "macd",
    "bbands": "bbands",
    "stoch": "stoch",
}


class SignalGenerator:
    def __init__(self, strategy: Strategy):
        self.strategy = strategy

    # ------------------------------------------------------------------
    # Indicator computation
    # ------------------------------------------------------------------

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add indicator columns to df and return a copy."""
        out = df.copy()
        for ind in self.strategy.indicators:
            self._add_indicator(out, ind)
        # Compute derived fields (e.g. VOL_RATIO = volume / vol_sma)
        self._compute_derived(out)
        return out

    def _compute_derived(self, df: pd.DataFrame) -> None:
        """Compute derived columns that depend on other indicators."""
        vol = df.get("Volume", df.get("volume", None))
        close = df.get("Close", df.get("close", None))

        # VOL_RATIO: current volume / volume SMA
        if vol is not None:
            for col in df.columns:
                if col.startswith("VOL_SMA_") and "VOL_RATIO" not in df.columns:
                    df["VOL_RATIO"] = vol / df[col].replace(0, np.nan)

        # OBV_SLOPE: rate of change of OBV (5-period)
        if "OBV" in df.columns and "OBV_SLOPE" not in df.columns:
            df["OBV_SLOPE"] = df["OBV"].diff(5)

        # ATR_BREAKOUT: close > prev_close + ATR (positive = breakout)
        if close is not None and "ATR_14" in df.columns and "ATR_BREAKOUT" not in df.columns:
            df["ATR_BREAKOUT"] = close - (close.shift(1) + df["ATR_14"])

        # MA_SPREAD: spread between shortest and longest MA (convergence/divergence)
        sma_cols = sorted([c for c in df.columns if c.startswith("SMA_")],
                          key=lambda x: int(x.split("_")[1]) if x.split("_")[1].isdigit() else 0)
        if len(sma_cols) >= 2 and "MA_SPREAD" not in df.columns:
            df["MA_SPREAD"] = df[sma_cols[0]] - df[sma_cols[-1]]

        # VWAP_DIST: percentage distance from VWAP
        vwap_cols = [c for c in df.columns if c.upper().startswith("VWAP") and "DIST" not in c.upper()]
        if close is not None and vwap_cols and "VWAP_DIST" not in df.columns:
            vwap = df[vwap_cols[0]]
            df["VWAP_DIST"] = ((close - vwap) / vwap.replace(0, np.nan)) * 100

    def _add_indicator(self, df: pd.DataFrame, ind: dict) -> None:
        itype = ind["type"].lower()
        output_name = ind["output_name"]
        col = ind.get("column", "close").capitalize()
        series = df[col] if col in df.columns else df["Close"]

        period = ind.get("period", 14)

        if itype == "sma":
            df[output_name] = ta.sma(series, length=period)
        elif itype == "ema":
            df[output_name] = ta.ema(series, length=period)
        elif itype == "rsi":
            df[output_name] = ta.rsi(series, length=period)
        elif itype == "macd":
            fast = ind.get("fast", 12)
            slow = ind.get("slow", 26)
            signal = ind.get("signal", 9)
            result = ta.macd(series, fast=fast, slow=slow, signal=signal)
            if result is not None:
                # Assign all columns with output_name prefix or as-is
                for c in result.columns:
                    df[c] = result[c]
                # Also expose main output_name as MACD line
                macd_col = [c for c in result.columns if c.startswith("MACD_") and "s" not in c.lower()[5:6]]
                if macd_col:
                    df[output_name] = result[macd_col[0]]
        elif itype == "bbands":
            std = ind.get("std", 2.0)
            result = ta.bbands(series, length=period, std=std)
            if result is not None:
                for c in result.columns:
                    df[c] = result[c]
                # Create short aliases: BBL_20_2.0_2.0 -> BBL_20_2.0, etc.
                for c in result.columns:
                    parts = c.split("_")
                    if len(parts) >= 4:
                        short = "_".join(parts[:3])
                        if short not in df.columns:
                            df[short] = result[c]
                # Expose output_name as middle band
                mid_col = [c for c in result.columns if "BBM" in c]
                if mid_col:
                    df[output_name] = result[mid_col[0]]
        elif itype == "stoch":
            high = df.get("High", df.get("high", series))
            low = df.get("Low", df.get("low", series))
            result = ta.stoch(high, low, series, k=period)
            if result is not None:
                for c in result.columns:
                    df[c] = result[c]
                if result.columns.any():
                    df[output_name] = result.iloc[:, 0]
        elif itype == "adx":
            high = df.get("High", df.get("high", series))
            low = df.get("Low", df.get("low", series))
            result = ta.adx(high, low, series, length=period)
            if result is not None:
                for c in result.columns:
                    df[c] = result[c]
        elif itype == "atr":
            high = df.get("High", df.get("high", series))
            low = df.get("Low", df.get("low", series))
            result = ta.atr(high, low, series, length=period)
            if result is not None:
                df[output_name] = result
        elif itype == "cci":
            high = df.get("High", df.get("high", series))
            low = df.get("Low", df.get("low", series))
            result = ta.cci(high, low, series, length=period)
            if result is not None:
                df[output_name] = result
        elif itype == "roc":
            result = ta.roc(series, length=period)
            if result is not None:
                df[output_name] = result
        elif itype == "stochrsi":
            smooth_k = ind.get("smooth_k", 3)
            smooth_d = ind.get("smooth_d", 3)
            result = ta.stochrsi(series, length=period, rsi_length=period, k=smooth_k, d=smooth_d)
            if result is not None:
                for c in result.columns:
                    df[c] = result[c]
        elif itype == "willr":
            high = df.get("High", df.get("high", series))
            low = df.get("Low", df.get("low", series))
            result = ta.willr(high, low, series, length=period)
            if result is not None:
                df[output_name] = result
        elif itype == "mfi":
            high = df.get("High", df.get("high", series))
            low = df.get("Low", df.get("low", series))
            volume = df.get("Volume", df.get("volume", None))
            if volume is not None:
                result = ta.mfi(high, low, series, volume, length=period)
                if result is not None:
                    df[output_name] = result
        elif itype == "obv":
            volume = df.get("Volume", df.get("volume", None))
            if volume is not None:
                result = ta.obv(series, volume)
                if result is not None:
                    df[output_name] = result
        elif itype == "psar":
            high = df.get("High", df.get("high", series))
            low = df.get("Low", df.get("low", series))
            af = ind.get("af", 0.02)
            max_af = ind.get("max_af", 0.2)
            result = ta.psar(high, low, af0=af, max_af=max_af)
            if result is not None:
                for c in result.columns:
                    df[c] = result[c]
                # Expose combined PSAR (long when trending up, short when down)
                long_col = [c for c in result.columns if "PSARl" in c]
                short_col = [c for c in result.columns if "PSARs" in c]
                if long_col and short_col:
                    psar_long = result[long_col[0]]
                    psar_short = result[short_col[0]]
                    df[output_name] = psar_long.fillna(psar_short)
                elif long_col:
                    df[output_name] = result[long_col[0]]
        elif itype == "ichimoku":
            high = df.get("High", df.get("high", series))
            low = df.get("Low", df.get("low", series))
            tenkan = ind.get("tenkan", 9)
            kijun = ind.get("kijun", 26)
            senkou = ind.get("senkou", 52)
            result = ta.ichimoku(high, low, series, tenkan=tenkan, kijun=kijun, senkou=senkou)
            if result is not None:
                # ichimoku returns a tuple of (ichimoku_df, span_df)
                if isinstance(result, tuple):
                    for r in result:
                        if isinstance(r, pd.DataFrame):
                            for c in r.columns:
                                if c not in df.columns:
                                    df[c] = r[c].reindex(df.index)
                elif isinstance(result, pd.DataFrame):
                    for c in result.columns:
                        df[c] = result[c]
        elif itype == "donchian":
            result = ta.donchian(df.get("High", df.get("high", series)),
                                 df.get("Low", df.get("low", series)),
                                 lower_length=period, upper_length=period)
            if result is not None:
                for c in result.columns:
                    df[c] = result[c]
                # Create short aliases: DCL_20_20 -> DCL_20, DCU_20_20 -> DCU_20
                for c in result.columns:
                    parts = c.split("_")
                    if len(parts) == 3:
                        short = "_".join(parts[:2])
                        if short not in df.columns:
                            df[short] = result[c]
        elif itype == "kc":
            high = df.get("High", df.get("high", series))
            low = df.get("Low", df.get("low", series))
            scalar = ind.get("scalar", 2.0)
            result = ta.kc(high, low, series, length=period, scalar=scalar)
            if result is not None:
                for c in result.columns:
                    df[c] = result[c]
                # Create short aliases: KCLe_20_2.0 -> KCL_20, KCUe_20_2.0 -> KCU_20, KCBe_20_2.0 -> KCM_20
                for c in result.columns:
                    if "KCLe" in c:
                        df[f"KCL_{period}"] = result[c]
                    elif "KCUe" in c:
                        df[f"KCU_{period}"] = result[c]
                    elif "KCBe" in c:
                        df[f"KCM_{period}"] = result[c]
        elif itype == "vwap":
            high = df.get("High", df.get("high", series))
            low = df.get("Low", df.get("low", series))
            volume = df.get("Volume", df.get("volume", None))
            if volume is not None:
                result = ta.vwap(high, low, series, volume)
                if result is not None:
                    df[output_name] = result
        elif itype == "highest":
            high = df.get("High", df.get("high", series))
            # shift(1): compare against prior period's high (today's close vs yesterday's 252d high)
            df[output_name] = high.rolling(window=period).max().shift(1)
        elif itype == "atr_stop":
            # Trailing stop = rolling_max(Close, period) - multiplier * ATR
            atr_col_name = ind.get("atr_col", "ATR_14")
            multiplier = float(ind.get("multiplier", 10.0))
            close = df.get("Close", df.get("close", series))
            if atr_col_name in df.columns:
                rolling_max_close = close.rolling(window=period).max()
                df[output_name] = rolling_max_close - multiplier * df[atr_col_name]
            else:
                logger.warning(f"ATR column not found for atr_stop: {atr_col_name}")
        else:
            logger.warning(f"Unknown indicator type: {itype}")

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        """Return Series of 1 (BUY), -1 (SELL), 0 (HOLD) indexed like df."""
        signals = pd.Series(0, index=df.index, dtype=int)

        entry_rules = self.strategy.rules.get("entry", [])
        exit_rules = self.strategy.rules.get("exit", [])

        entry_mask = self._eval_rules(df, entry_rules)
        exit_mask = self._eval_rules(df, exit_rules)

        signals[entry_mask] = 1
        signals[exit_mask] = -1
        return signals

    def _eval_rules(self, df: pd.DataFrame, rules: list[dict]) -> pd.Series:
        """Combine all rules with AND logic; return boolean mask."""
        if not rules:
            return pd.Series(False, index=df.index)

        combined = pd.Series(True, index=df.index)
        for rule in rules:
            mask = self._eval_single_rule(df, rule)
            combined &= mask
        return combined

    def _eval_single_rule(self, df: pd.DataFrame, rule: dict) -> pd.Series:
        condition = rule.get("condition", "")

        if condition == "crossover":
            return self._crossover(df, rule)
        elif condition == "threshold":
            return self._threshold(df, rule)
        elif condition == "threshold_compare":
            return self._threshold_compare(df, rule)
        elif condition == "band":
            return self._band(df, rule)
        else:
            logger.warning(f"Unknown condition type: {condition}")
            return pd.Series(False, index=df.index)

    def _crossover(self, df: pd.DataFrame, rule: dict) -> pd.Series:
        """Detect crossover between indicator_a and indicator_b (or a value)."""
        direction = rule.get("direction", "above")
        a_col = self._resolve_col(df, rule.get("indicator_a", ""))
        b_raw = rule.get("indicator_b", "")
        b_col = self._resolve_col(df, b_raw) if isinstance(b_raw, str) else None

        if a_col is None:
            logger.warning(f"Column not found: {rule.get('indicator_a')}")
            return pd.Series(False, index=df.index)

        a = df[a_col]

        # b can be another indicator column or a numeric value
        if b_col is not None:
            b = df[b_col]
        else:
            try:
                b = float(b_col)
            except (TypeError, ValueError):
                logger.warning(f"Column not found and not numeric: {b_col}")
                return pd.Series(False, index=df.index)

        a_prev = a.shift(1)
        if isinstance(b, pd.Series):
            b_prev = b.shift(1)
        else:
            b_prev = b

        if direction == "above":
            # a crosses above b: prev a <= prev b AND current a > b
            return (a_prev <= b_prev) & (a > b)
        else:  # below
            # a crosses below b: prev a >= prev b AND current a < b
            return (a_prev >= b_prev) & (a < b)

    def _threshold(self, df: pd.DataFrame, rule: dict) -> pd.Series:
        """Check if indicator is above/below a threshold value."""
        direction = rule.get("direction", "above")
        indicator = self._resolve_col(df, rule.get("indicator", ""))
        value = float(rule.get("value", 0))

        if indicator is None:
            logger.warning(f"Column not found: {rule.get('indicator')}")
            return pd.Series(False, index=df.index)

        series = df[indicator]
        if direction == "above":
            return series > value
        else:
            return series < value

    @staticmethod
    def _resolve_col(df: pd.DataFrame, name: str) -> str | None:
        """Find column by exact match, then case-insensitive fallback."""
        if name in df.columns:
            return name
        # Case-insensitive search
        lower = name.lower()
        for c in df.columns:
            if c.lower() == lower:
                return c
        return None

    def _threshold_compare(self, df: pd.DataFrame, rule: dict) -> pd.Series:
        """Check if indicator_a is above/below indicator_b (two column comparison)."""
        direction = rule.get("direction", "above")
        a_col = self._resolve_col(df, rule.get("indicator_a", ""))
        b_col = self._resolve_col(df, rule.get("indicator_b", ""))

        if a_col is None:
            logger.warning(f"Column not found: {rule.get('indicator_a')}")
            return pd.Series(False, index=df.index)
        if b_col is None:
            logger.warning(f"Column not found: {rule.get('indicator_b')}")
            return pd.Series(False, index=df.index)

        if direction == "above":
            return df[a_col] > df[b_col]
        else:
            return df[a_col] < df[b_col]

    def _band(self, df: pd.DataFrame, rule: dict) -> pd.Series:
        """Check if indicator is inside/outside a band [lower, upper]."""
        position = rule.get("position", "inside")  # inside or outside
        indicator = rule.get("indicator")
        lower_col = rule.get("lower")
        upper_col = rule.get("upper")

        if indicator not in df.columns:
            logger.warning(f"Column not found: {indicator}")
            return pd.Series(False, index=df.index)

        series = df[indicator]
        lower = df[lower_col] if lower_col in df.columns else float(lower_col or 0)
        upper = df[upper_col] if upper_col in df.columns else float(upper_col or 0)

        if position == "inside":
            return (series >= lower) & (series <= upper)
        else:
            return (series < lower) | (series > upper)
