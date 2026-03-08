"""Daily morning scan: run validated strategies on latest data and alert."""

import logging
from datetime import datetime, date

from market_data import KRXFetcher, USFetcher
from market_data.calendar import KRXCalendar, NYSECalendar
from strategy.registry import StrategyRegistry
from backtester.signals import SignalGenerator
from alerts.discord_webhook import DiscordNotifier
from alerts.email_sender import EmailSender
from utils.config_loader import load_config

logger = logging.getLogger("money_mani.pipeline.daily_scan")


class DailyScan:
    """Run validated strategies against latest data and send alerts."""

    def __init__(self, config: dict = None):
        self.config = config or load_config()
        self.registry = StrategyRegistry()
        self.krx_cal = KRXCalendar()
        self.nyse_cal = NYSECalendar()
        self.discord = DiscordNotifier()
        self.email = EmailSender(self.config.get("notifications", {}).get("email", {}))

    def run(self) -> dict:
        """Execute daily scan."""
        today = date.today()
        is_krx_day = self.krx_cal.is_trading_day(today)
        is_nyse_day = self.nyse_cal.is_trading_day(today)

        if not is_krx_day and not is_nyse_day:
            logger.info(f"{today}: No markets open today. Skipping scan.")
            return {"date": str(today), "signals": [], "skipped": True}

        strategies = self.registry.get_validated()
        if not strategies:
            logger.warning("No validated strategies found.")
            return {"date": str(today), "signals": [], "skipped": False}

        signals = []

        # KRX scan
        if is_krx_day:
            krx_tickers = self.config["pipeline"]["targets"].get("custom_tickers", [])
            krx_signals = self._scan_market(strategies, krx_tickers, "KRX")
            signals.extend(krx_signals)

        # US scan
        if is_nyse_day:
            us_tickers = self.config["pipeline"].get("us_targets", {}).get("custom_tickers", [])
            if us_tickers:
                us_signals = self._scan_market(strategies, us_tickers, "US")
                signals.extend(us_signals)

        # Send alerts
        if signals:
            self._send_alerts(signals, str(today))
        else:
            logger.info(f"{today}: No signals triggered.")
            self.discord.send(content=f"📊 {today} 일일 스캔 완료: 시그널 없음")

        return {"date": str(today), "signals": signals, "skipped": False}

    def _scan_market(self, strategies, tickers, market):
        """Scan a set of tickers with all strategies."""
        fetcher = KRXFetcher(delay=0.5) if market == "KRX" else USFetcher()
        signals = []

        for strat in strategies:
            sig_gen = SignalGenerator(strat)
            for ticker in tickers:
                try:
                    df = fetcher.get_ohlcv(ticker, "2024-01-01")
                    if df.empty or len(df) < 60:
                        continue

                    df_ind = sig_gen.compute_indicators(df)
                    sigs = sig_gen.generate_signals(df_ind)

                    if len(sigs) == 0:
                        continue

                    last_signal = sigs.iloc[-1]
                    if last_signal != 0:
                        signal_type = "BUY" if last_signal == 1 else "SELL"
                        last_row = df_ind.iloc[-1]

                        # Get ticker name
                        try:
                            if market == "KRX":
                                ticker_name = KRXFetcher().get_ticker_name(ticker)
                            else:
                                ticker_name = ticker
                        except Exception:
                            ticker_name = ticker

                        signal_info = {
                            "strategy_name": strat.name,
                            "ticker": ticker,
                            "ticker_name": ticker_name,
                            "signal_type": signal_type,
                            "price": float(last_row["Close"]),
                            "indicators": {col: float(last_row[col])
                                           for col in df_ind.columns
                                           if col not in ["Open", "High", "Low", "Close", "Volume"]
                                           and not str(last_row[col]) == "nan"},
                            "date": str(df.index[-1].date()),
                            "market": market,
                        }
                        signals.append(signal_info)
                        logger.info(f"Signal: {signal_type} {ticker} ({strat.name})")
                except Exception as e:
                    logger.error(f"Scan error {strat.name}/{ticker}: {e}")

        return signals

    def _send_alerts(self, signals, date_str):
        """Send alerts via Discord and email."""
        # Individual signal alerts
        for sig in signals:
            self.discord.send_signal_alert(sig)

        # Daily summary
        self.discord.send_daily_summary(signals, date_str)

        # Email backup
        if self.config.get("notifications", {}).get("email", {}).get("enabled"):
            summary = "\n".join(
                f"- {s['signal_type']} {s['ticker_name']}({s['ticker']}) @ {s['price']:,.0f} [{s['strategy_name']}]"
                for s in signals
            )
            self.email.send(
                subject=f"[money_mani] {date_str} 매매 시그널 ({len(signals)}건)",
                body=f"일일 스캔 결과:\n\n{summary}",
            )

        logger.info(f"Sent {len(signals)} signal alerts")
