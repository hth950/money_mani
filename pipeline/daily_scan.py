"""Daily morning scan: run validated strategies on latest data and alert."""

import gc
import logging
from datetime import datetime, date, timedelta, timezone

from market_data import KRXFetcher, USFetcher
from market_data.calendar import KRXCalendar, NYSECalendar
from strategy.registry import StrategyRegistry
from backtester.signals import SignalGenerator
from alerts.discord_webhook import DiscordNotifier
from alerts.email_sender import EmailSender
from utils.config_loader import load_config
from web.db.connection import get_db
from web.services.signal_service import SignalService
from web.services.performance_service import PerformanceService
from web.services.position_service import PositionService
from web.services.conflict_resolver import ConflictResolver

KST = timezone(timedelta(hours=9))

logger = logging.getLogger("money_mani.pipeline.daily_scan")

TOP_N_STRATEGIES = 10


_sent_signals_today: dict[str, set] = {}  # date_str -> set of "strategy|ticker|signal_type"


def _signal_key(sig: dict) -> str:
    return f"{sig['strategy_name']}|{sig['ticker']}|{sig['signal_type']}"


class DailyScan:
    """Run validated strategies against latest data and send alerts."""

    def __init__(self, config: dict = None):
        self.config = config or load_config()
        self.registry = StrategyRegistry()
        self.krx_cal = KRXCalendar()
        self.nyse_cal = NYSECalendar()
        self.discord = DiscordNotifier()
        self.email = EmailSender(self.config.get("notifications", {}).get("email", {}))
        self.signal_service = SignalService()
        self.perf_service = PerformanceService()
        self.position_service = PositionService()
        self.conflict_resolver = ConflictResolver()

    def _get_top_strategies(self) -> list:
        """Get top N strategies ranked by backtest + live performance blend."""
        with get_db() as db:
            bt_rows = db.execute("""
                SELECT strategy_name,
                       AVG(total_return) as avg_return,
                       AVG(sharpe_ratio) as avg_sharpe,
                       AVG(win_rate) as avg_win_rate
                FROM backtest_results
                WHERE is_valid = 1
                GROUP BY strategy_name
            """).fetchall()

            live_rows = db.execute("""
                SELECT strategy_name, win_rate, avg_pnl_pct, total_trades
                FROM strategy_stats
                WHERE period = '30d' AND total_trades >= 10
            """).fetchall()

        live_map = {r["strategy_name"]: r for r in live_rows}

        scored = []
        for row in bt_rows:
            name = row["strategy_name"]
            bt_score = (row["avg_sharpe"] or 0) + (row["avg_win_rate"] or 0) + (row["avg_return"] or 0) / 100

            live = live_map.get(name)
            if live:
                live_score = (live["win_rate"] or 0) / 100 + (live["avg_pnl_pct"] or 0) / 10
                final_score = bt_score * 0.7 + live_score * 0.3
            else:
                final_score = bt_score

            scored.append((name, final_score))

        scored.sort(key=lambda x: x[1], reverse=True)
        top_names = [s[0] for s in scored[:TOP_N_STRATEGIES]]
        if top_names:
            logger.info(f"Top {len(top_names)} strategies: {top_names}")

        strategies = []
        for name in top_names:
            try:
                strategies.append(self.registry.load(name))
            except Exception:
                logger.warning(f"Could not load strategy: {name}")
        return strategies

    def run(self) -> dict:
        """Execute daily scan."""
        today = datetime.now(KST).date()
        is_krx_day = self.krx_cal.is_trading_day(today)
        is_nyse_day = self.nyse_cal.is_trading_day(today)

        if not is_krx_day and not is_nyse_day:
            logger.info(f"{today}: No markets open today. Skipping scan.")
            return {"date": str(today), "signals": [], "skipped": True}

        # Use top 3 strategies from backtest results, fallback to all validated
        strategies = self._get_top_strategies()
        if not strategies:
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
            gc.collect()

        return signals

    def _send_alerts(self, signals, date_str):
        """Send alerts via Discord and email, skipping duplicates."""
        global _sent_signals_today

        # Initialize today's set if needed
        if date_str not in _sent_signals_today:
            _sent_signals_today.clear()  # Clear old dates
            _sent_signals_today[date_str] = set()

        # Filter out already-sent signals
        new_signals = []
        for sig in signals:
            key = _signal_key(sig)
            if key not in _sent_signals_today[date_str]:
                _sent_signals_today[date_str].add(key)
                new_signals.append(sig)
            else:
                logger.info(f"Skipping duplicate signal: {key}")

        if not new_signals:
            logger.info(f"{date_str}: All signals already sent. Skipping alerts.")
            return

        # Save signals to DB, track performance, and manage positions
        for sig in new_signals:
            try:
                sig_id = self.signal_service.save_signal(sig)
                self.perf_service.record_signal(sig, signal_id=sig_id)

                # Position tracking
                if sig["signal_type"] == "BUY":
                    self.position_service.open_position(
                        strategy_name=sig["strategy_name"],
                        ticker=sig["ticker"],
                        ticker_name=sig.get("ticker_name", sig["ticker"]),
                        market=sig.get("market", "KRX"),
                        entry_price=sig["price"],
                        entry_date=sig.get("date", date_str),
                        signal_id=sig_id,
                    )
                elif sig["signal_type"] == "SELL":
                    self.position_service.close_position(
                        strategy_name=sig["strategy_name"],
                        ticker=sig["ticker"],
                        exit_price=sig["price"],
                        exit_date=sig.get("date", date_str),
                        signal_id=sig_id,
                    )
            except Exception as e:
                logger.error(f"Failed to save signal to DB: {e}")

        # Conflict resolution: send consensus embeds for conflicting tickers
        conflict_groups = self.conflict_resolver.resolve(new_signals)
        conflicting_tickers = set()
        for ticker, group in conflict_groups.items():
            if group.has_conflict:
                self.discord.send_consensus_alert(group)
                conflicting_tickers.add(ticker)

        # Individual signal alerts (skip tickers that got consensus embeds)
        for sig in new_signals:
            if sig["ticker"] not in conflicting_tickers:
                self.discord.send_signal_alert(sig)

        # Daily summary (only new signals)
        self.discord.send_daily_summary(new_signals, date_str)

        # Email backup
        if self.config.get("notifications", {}).get("email", {}).get("enabled"):
            summary = "\n".join(
                f"- {s['signal_type']} {s['ticker_name']}({s['ticker']}) @ {s['price']:,.0f} [{s['strategy_name']}]"
                for s in new_signals
            )
            self.email.send(
                subject=f"[money_mani] {date_str} 매매 시그널 ({len(new_signals)}건)",
                body=f"일일 스캔 결과:\n\n{summary}",
            )

        logger.info(f"Sent {len(new_signals)} new signal alerts (skipped {len(signals) - len(new_signals)} duplicates)")
