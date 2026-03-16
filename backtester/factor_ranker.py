"""Factor-based portfolio ranker for cross-sectional strategies.

Supports:
  - low_volatility: rank by 252-day rolling std, select bottom decile
  - piotroski_fscore: rank by F-Score (yfinance financials), select top score
"""
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("money_mani.backtester.factor_ranker")
KST = timezone(timedelta(hours=9))


class FactorRanker:
    """Rank universe of tickers by factor, return buy/sell/hold signals."""

    def rank_low_volatility(
        self, universe: list[str], market: str, lookback_days: int = 252
    ) -> dict[str, int]:
        """Low volatility factor: buy bottom 20% by 252d rolling std.

        Returns {ticker: 1 (BUY), 0 (HOLD), -1 (SELL)}
        """
        import yfinance as yf

        vols: dict[str, float] = {}
        start_date = (datetime.now(KST) - timedelta(days=lookback_days + 60)).strftime(
            "%Y-%m-%d"
        )

        for ticker in universe:
            try:
                yf_ticker = ticker + ".KS" if market == "KRX" else ticker
                df = yf.download(
                    yf_ticker,
                    start=start_date,
                    progress=False,
                    timeout=15,
                    auto_adjust=True,
                )
                if df is None or df.empty or len(df) < 60:
                    logger.debug(
                        f"[low_vol] Skip {ticker}: insufficient data "
                        f"({len(df) if df is not None else 0} rows)"
                    )
                    continue
                # Annualized volatility from daily log returns
                close = df["Close"].squeeze()
                log_ret = np.log(close / close.shift(1)).dropna()
                if len(log_ret) < 60:
                    continue
                ann_vol = float(log_ret.tail(lookback_days).std() * np.sqrt(252))
                if np.isfinite(ann_vol) and ann_vol > 0:
                    vols[ticker] = ann_vol
            except Exception as e:
                logger.warning(f"[low_vol] {ticker} failed: {e}")
                continue

        if not vols:
            logger.warning("[low_vol] No volatility data collected, returning empty")
            return {}

        vol_series = pd.Series(vols)
        buy_thresh = vol_series.quantile(0.20)
        sell_thresh = vol_series.quantile(0.80)

        signals: dict[str, int] = {}
        for ticker, vol in vols.items():
            if vol <= buy_thresh:
                signals[ticker] = 1   # BUY — lowest volatility
            elif vol >= sell_thresh:
                signals[ticker] = -1  # SELL — highest volatility
            else:
                signals[ticker] = 0   # HOLD

        buy_n = sum(1 for v in signals.values() if v == 1)
        sell_n = sum(1 for v in signals.values() if v == -1)
        logger.info(
            f"[low_vol] {market}: {len(vols)} tickers ranked — "
            f"BUY={buy_n}, SELL={sell_n}, HOLD={len(signals)-buy_n-sell_n}"
        )
        return signals

    def rank_piotroski(self, universe: list[str], market: str) -> dict[str, int]:
        """Piotroski F-Score: buy score >= 7, sell score <= 2.

        Uses yfinance financials (income_stmt, balance_sheet, cashflow).
        Returns {ticker: 1 (BUY), 0 (HOLD), -1 (SELL)}
        """
        import yfinance as yf

        signals: dict[str, int] = {}

        for ticker in universe:
            try:
                yf_ticker = ticker + ".KS" if market == "KRX" else ticker
                t = yf.Ticker(yf_ticker)

                income = t.income_stmt
                balance = t.balance_sheet
                cashflow = t.cashflow

                if income is None or balance is None or cashflow is None:
                    logger.debug(f"[piotroski] {ticker}: missing financial statements")
                    continue
                if income.empty or balance.empty or cashflow.empty:
                    logger.debug(f"[piotroski] {ticker}: empty financial statements")
                    continue
                if income.shape[1] < 2 or balance.shape[1] < 2:
                    logger.debug(f"[piotroski] {ticker}: insufficient history")
                    continue

                score = self._compute_fscore(income, balance, cashflow)
                if score is None:
                    continue

                if score >= 7:
                    signals[ticker] = 1   # BUY
                elif score <= 2:
                    signals[ticker] = -1  # SELL
                else:
                    signals[ticker] = 0   # HOLD

                logger.debug(f"[piotroski] {ticker}: F-Score={score} -> {signals[ticker]}")

            except Exception as e:
                logger.warning(f"[piotroski] {ticker} failed: {e}")
                continue

        buy_n = sum(1 for v in signals.values() if v == 1)
        sell_n = sum(1 for v in signals.values() if v == -1)
        logger.info(
            f"[piotroski] {market}: {len(signals)} tickers scored — "
            f"BUY={buy_n}, SELL={sell_n}, HOLD={len(signals)-buy_n-sell_n}"
        )
        return signals

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_row(self, df: pd.DataFrame, *candidates: str):
        """Return the first matching row series from a DataFrame by row label."""
        for name in candidates:
            if name in df.index:
                return df.loc[name]
        return None

    def _val(self, series, col_idx: int = 0):
        """Safely extract a scalar from a Series at column index."""
        try:
            if series is None:
                return None
            v = series.iloc[col_idx]
            if pd.isna(v):
                return None
            return float(v)
        except Exception:
            return None

    def _compute_fscore(
        self,
        income: pd.DataFrame,
        balance: pd.DataFrame,
        cashflow: pd.DataFrame,
    ) -> int | None:
        """Compute Piotroski F-Score (0–9) from financial statement DataFrames.

        Profitability (4 criteria):
          F1: ROA > 0
          F2: CFO > 0
          F3: ROA increased YoY
          F4: CFO > ROA (accruals)

        Leverage / Liquidity (3 criteria):
          F5: Long-term debt ratio decreased YoY
          F6: Current ratio increased YoY
          F7: No new shares issued

        Efficiency (2 criteria):
          F8: Gross margin increased YoY
          F9: Asset turnover increased YoY
        """
        try:
            # --- Current period (col 0) and prior period (col 1) ---
            total_assets_0 = self._val(self._get_row(balance, "Total Assets"), 0)
            total_assets_1 = self._val(self._get_row(balance, "Total Assets"), 1)

            if not total_assets_0 or not total_assets_1:
                return None

            net_income_0 = self._val(self._get_row(income, "Net Income", "Net Income Common Stockholders"), 0)
            net_income_1 = self._val(self._get_row(income, "Net Income", "Net Income Common Stockholders"), 1)

            cfo_0 = self._val(self._get_row(cashflow, "Operating Cash Flow", "Cash Flow From Continuing Operating Activities"), 0)

            revenue_0 = self._val(self._get_row(income, "Total Revenue", "Revenue"), 0)
            revenue_1 = self._val(self._get_row(income, "Total Revenue", "Revenue"), 1)

            gross_profit_0 = self._val(self._get_row(income, "Gross Profit"), 0)
            gross_profit_1 = self._val(self._get_row(income, "Gross Profit"), 1)

            lt_debt_0 = self._val(self._get_row(balance, "Long Term Debt", "Long-Term Debt"), 0) or 0.0
            lt_debt_1 = self._val(self._get_row(balance, "Long Term Debt", "Long-Term Debt"), 1) or 0.0

            current_assets_0 = self._val(self._get_row(balance, "Current Assets", "Total Current Assets"), 0)
            current_assets_1 = self._val(self._get_row(balance, "Current Assets", "Total Current Assets"), 1)

            current_liab_0 = self._val(self._get_row(balance, "Current Liabilities", "Total Current Liabilities"), 0)
            current_liab_1 = self._val(self._get_row(balance, "Current Liabilities", "Total Current Liabilities"), 1)

            shares_0 = self._val(self._get_row(balance, "Ordinary Shares Number", "Share Issued", "Common Stock"), 0)
            shares_1 = self._val(self._get_row(balance, "Ordinary Shares Number", "Share Issued", "Common Stock"), 1)

            # --- Compute criteria ---
            score = 0

            # F1: ROA > 0
            if net_income_0 is not None and total_assets_0:
                roa_0 = net_income_0 / total_assets_0
                score += 1 if roa_0 > 0 else 0
            else:
                roa_0 = None

            # F2: CFO > 0
            if cfo_0 is not None:
                score += 1 if cfo_0 > 0 else 0

            # F3: ROA increased YoY
            if net_income_1 is not None and total_assets_1:
                roa_1 = net_income_1 / total_assets_1
                if roa_0 is not None:
                    score += 1 if roa_0 > roa_1 else 0
            else:
                roa_1 = None

            # F4: CFO > ROA (quality of earnings)
            if cfo_0 is not None and roa_0 is not None and total_assets_0:
                score += 1 if cfo_0 / total_assets_0 > roa_0 else 0

            # F5: Long-term debt ratio decreased
            if total_assets_0 and total_assets_1:
                lev_0 = lt_debt_0 / total_assets_0
                lev_1 = lt_debt_1 / total_assets_1
                score += 1 if lev_0 < lev_1 else 0

            # F6: Current ratio increased
            if (current_assets_0 is not None and current_liab_0 and
                    current_assets_1 is not None and current_liab_1):
                cr_0 = current_assets_0 / current_liab_0
                cr_1 = current_assets_1 / current_liab_1
                score += 1 if cr_0 > cr_1 else 0

            # F7: No new shares issued
            if shares_0 is not None and shares_1 is not None:
                score += 1 if shares_0 <= shares_1 else 0

            # F8: Gross margin increased
            if (gross_profit_0 is not None and revenue_0 and
                    gross_profit_1 is not None and revenue_1):
                gm_0 = gross_profit_0 / revenue_0
                gm_1 = gross_profit_1 / revenue_1
                score += 1 if gm_0 > gm_1 else 0

            # F9: Asset turnover increased
            if revenue_0 is not None and revenue_1 is not None and total_assets_0 and total_assets_1:
                at_0 = revenue_0 / total_assets_0
                at_1 = revenue_1 / total_assets_1
                score += 1 if at_0 > at_1 else 0

            return score

        except Exception as e:
            logger.debug(f"[piotroski] _compute_fscore error: {e}")
            return None
