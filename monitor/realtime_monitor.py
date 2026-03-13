"""Real-time stock monitoring loop with Discord alerts."""

import logging
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from broker.kis_client import KISClient
from broker.portfolio import PortfolioManager, HoldingInfo
from monitor.market_session import MarketSession
from monitor.rolling_buffer import RollingBuffer
from monitor.signal_tracker import SignalTracker, TickerConsensusTracker
from strategy.registry import StrategyRegistry
from strategy.models import Strategy
from backtester.signals import SignalGenerator
from alerts.discord_webhook import DiscordNotifier
from alerts.formatter import AlertFormatter
from market_data import KRXFetcher, USFetcher
from utils.config_loader import load_config, get_env

logger = logging.getLogger("money_mani.monitor")

KST = ZoneInfo("Asia/Seoul")


@dataclass
class TickerContext:
    ticker: str
    name: str
    market: str          # "KRX" or "US"
    mode: str            # "WATCH" (buy signals) or "HOLD" (sell signals)
    exchange: str = ""   # "NASDAQ", "NYSE", "AMEX" for US stocks
    holding: HoldingInfo | None = None
    fail_count: int = 0  # consecutive fetch failures


class RealtimeMonitor:
    """Orchestrates real-time price monitoring and signal alerting."""

    MAX_CONSECUTIVE_FAILURES = 5

    def __init__(self, config: dict = None, market_filter: str = None, on_signal=None):
        self.config = config or load_config()
        self.market_filter = market_filter
        self.on_signal = on_signal
        rt_cfg = self.config.get("realtime", {})

        self.interval = rt_cfg.get("interval_seconds", 60)
        self.warmup_bars = rt_cfg.get("warmup_bars", 60)
        self.max_buffer = rt_cfg.get("max_buffer_size", 200)
        cooldown = rt_cfg.get("cooldown_minutes", 30)

        self.kis = KISClient()
        self.portfolio = PortfolioManager(self.kis)
        self.session = MarketSession()
        self.discord = DiscordNotifier()
        self.registry = StrategyRegistry()
        self.tracker = SignalTracker(cooldown_minutes=cooldown)

        consensus_cfg = rt_cfg.get("consensus", {})
        self.consensus_tracker = TickerConsensusTracker(
            threshold=consensus_cfg.get("threshold", 0.55),
            hold_threshold=consensus_cfg.get("hold_threshold", 0.50),
            min_hold_minutes=consensus_cfg.get("min_hold_minutes", 60),
            urgent_threshold=consensus_cfg.get("urgent_threshold", 0.80),
        )

        from web.services.signal_service import SignalService
        self.signal_service = SignalService()

        self.buffers: dict[str, RollingBuffer] = {}
        self.strategies: list[Strategy] = []
        self.ticker_map: dict[str, TickerContext] = {}
        self._running = False
        self._intel_cache: dict[str, list[dict]] = {}
        self._intel_cache_lock = threading.Lock()
        self._intel_last_refresh = 0.0
        self._intel_refresh_interval = 300  # 5 minutes

    def _refresh_intel_cache(self):
        """Refresh intel issues cache (every 5 minutes)."""
        now = _time.time()
        if now - self._intel_last_refresh < self._intel_refresh_interval:
            return
        try:
            from web.services.market_intel_service import MarketIntelService
            service = MarketIntelService()
            cache = service.get_issues_by_ticker(days=7)
            with self._intel_cache_lock:
                self._intel_cache = cache
            self._intel_last_refresh = now
            logger.debug(f"Intel cache refreshed: {len(cache)} tickers")
        except Exception as e:
            logger.warning(f"Intel cache refresh failed: {e}")

    def _get_intel_context(self, ticker: str) -> list[dict]:
        """Get cached intel issues for a ticker."""
        with self._intel_cache_lock:
            return self._intel_cache.get(ticker, [])

    def start(self):
        """Main entry point: load data, seed buffers, run loop."""
        self._running = True
        self._load_strategies()
        if not self.strategies:
            logger.warning("No validated strategies. Add strategies with status='validated'.")
            print("No validated strategies found. Cannot start monitor.")
            return

        self._build_ticker_map()
        if not self.ticker_map:
            logger.warning("No tickers to monitor.")
            print("No tickers to monitor.")
            return

        self._seed_buffers()
        self.tracker.preload_states()
        self.consensus_tracker.preload_directions()
        self._send_startup_notification()
        self._run_loop()

    def _load_strategies(self):
        self.strategies = self.registry.get_validated()
        logger.info(f"Loaded {len(self.strategies)} validated strategies")

    def _build_ticker_map(self):
        """Build monitoring list from watchlist + portfolio holdings."""
        rt_cfg = self.config.get("realtime", {})
        watchlist = rt_cfg.get("watchlist", {})

        # Watchlist tickers
        krx_watch = watchlist.get("krx", [])
        us_watch = watchlist.get("us", [])

        for ticker in krx_watch:
            if self.market_filter and self.market_filter != "KRX":
                continue
            self.ticker_map[ticker] = TickerContext(
                ticker=ticker, name=ticker, market="KRX", mode="WATCH")

        # Common NYSE-listed tickers for exchange resolution
        nyse_tickers = {"BRK.B", "JNJ", "V", "WMT", "JPM", "PG", "UNH", "HD",
                        "BAC", "DIS", "KO", "PFE", "MRK", "VZ", "T", "ABBV",
                        "CVX", "XOM", "BA", "GE", "IBM", "CAT", "MMM", "GS"}

        for ticker in us_watch:
            if self.market_filter and self.market_filter != "US":
                continue
            exchange = "NYSE" if ticker in nyse_tickers else "NASDAQ"
            self.ticker_map[ticker] = TickerContext(
                ticker=ticker, name=ticker, market="US", mode="WATCH",
                exchange=exchange)

        # Portfolio holdings (override watchlist mode to HOLD)
        try:
            holdings = self.portfolio.fetch_all_holdings()
            for ticker, holding in holdings.items():
                if self.market_filter and self.market_filter != holding.market:
                    continue
                self.ticker_map[ticker] = TickerContext(
                    ticker=ticker,
                    name=holding.name,
                    market=holding.market,
                    mode="HOLD",
                    holding=holding,
                )
        except Exception as e:
            logger.warning(f"Failed to fetch portfolio: {e}. Monitoring watchlist only.")

        # Auto-add high-confidence intel tickers
        try:
            from web.services.market_intel_service import MarketIntelService
            service = MarketIntelService()
            intel_tickers = service.get_high_confidence_tickers(days=7, min_confidence=0.7)

            added = 0
            for ticker_info in intel_tickers.get("KRX", []):
                ticker = ticker_info["ticker"]
                if ticker not in self.ticker_map and (not self.market_filter or self.market_filter == "KRX"):
                    self.ticker_map[ticker] = TickerContext(
                        ticker=ticker, name=ticker_info["name"],
                        market="KRX", mode="WATCH")
                    added += 1

            nyse_tickers_set = {"BRK.B", "JNJ", "V", "WMT", "JPM", "PG", "UNH", "HD",
                                "BAC", "DIS", "KO", "PFE", "MRK", "VZ", "T", "ABBV",
                                "CVX", "XOM", "BA", "GE", "IBM", "CAT", "MMM", "GS"}
            for ticker_info in intel_tickers.get("US", []):
                ticker = ticker_info["ticker"]
                if ticker not in self.ticker_map and (not self.market_filter or self.market_filter == "US"):
                    exchange = "NYSE" if ticker in nyse_tickers_set else "NASDAQ"
                    self.ticker_map[ticker] = TickerContext(
                        ticker=ticker, name=ticker_info["name"],
                        market="US", mode="WATCH", exchange=exchange)
                    added += 1

            if added:
                logger.info(f"Auto-added {added} intel-detected tickers to watchlist")
        except Exception as e:
            logger.warning(f"Failed to load intel tickers: {e}")

        # Resolve KRX ticker names
        try:
            from market_data.krx_fetcher import KRXFetcher
            krx = KRXFetcher(delay=0.3)
            for ticker, ctx in self.ticker_map.items():
                if ctx.market == "KRX" and ctx.name == ticker:
                    try:
                        ctx.name = krx.get_ticker_name(ticker)
                    except Exception:
                        pass
        except Exception:
            pass

        logger.info(f"Monitoring {len(self.ticker_map)} tickers "
                    f"(HOLD: {sum(1 for c in self.ticker_map.values() if c.mode == 'HOLD')}, "
                    f"WATCH: {sum(1 for c in self.ticker_map.values() if c.mode == 'WATCH')})")

    def _seed_buffers(self):
        """Seed rolling buffers with historical daily data."""
        krx_fetcher = KRXFetcher(delay=0.3)
        us_fetcher = USFetcher()

        for ticker, ctx in self.ticker_map.items():
            try:
                fetcher = krx_fetcher if ctx.market == "KRX" else us_fetcher
                df = fetcher.get_ohlcv(ticker, "2024-01-01")
                buf = RollingBuffer(ticker, max_size=self.max_buffer,
                                    warmup_bars=self.warmup_bars)
                if not df.empty:
                    buf.seed(df)
                self.buffers[ticker] = buf
                logger.debug(f"Seeded {ticker}: {len(buf)} bars, warm={buf.is_warm()}")
            except Exception as e:
                logger.warning(f"Failed to seed buffer for {ticker}: {e}")
                self.buffers[ticker] = RollingBuffer(
                    ticker, max_size=self.max_buffer, warmup_bars=self.warmup_bars)

        warm = sum(1 for b in self.buffers.values() if b.is_warm())
        logger.info(f"Seeded {len(self.buffers)} buffers ({warm} warm)")

    def _send_startup_notification(self):
        """Send Discord notification on monitor start."""
        krx_count = sum(1 for c in self.ticker_map.values() if c.market == "KRX")
        us_count = sum(1 for c in self.ticker_map.values() if c.market == "US")
        hold_count = sum(1 for c in self.ticker_map.values() if c.mode == "HOLD")

        us_hours = self.session.get_us_hours_kst()
        msg = (
            f"**Money Mani 실시간 모니터 시작**\n"
            f"감시 종목: {len(self.ticker_map)}개 (KRX {krx_count}, US {us_count})\n"
            f"보유 종목: {hold_count}개\n"
            f"전략: {len(self.strategies)}개\n"
            f"주기: {self.interval}초\n"
            f"KRX: 09:00~15:30 KST | US: {us_hours}"
        )
        self.discord.send(content=msg)

    def _run_loop(self):
        """Main monitoring loop."""
        logger.info("Entering monitoring loop...")
        prev_markets = []

        while self._running:
            try:
                active = self.session.get_active_markets()
                if self.market_filter:
                    active = [m for m in active if m == self.market_filter]

                # Detect market session transitions
                if active != prev_markets:
                    if active and not prev_markets:
                        logger.info(f"Market opened: {active}")
                        self.portfolio.refresh()
                    elif not active and prev_markets:
                        logger.info(f"Market closed: {prev_markets}")
                    prev_markets = active[:]

                if not active:
                    info = self.session.next_session_info()
                    wait = min(info["seconds_until"], 300)
                    logger.info(f"No active market. Next: {info['market']} at {info['opens_at_kst']}. "
                                f"Waiting {wait}s...")
                    _time.sleep(wait)
                    continue

                self._tick(active)
                _time.sleep(self.interval)

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Monitor loop error: {e}", exc_info=True)
                _time.sleep(30)

    def _tick(self, active_markets: list[str]):
        """One monitoring cycle: fetch prices, compute signals, alert."""
        now_str = datetime.now(KST).strftime("%H:%M:%S")
        logger.debug(f"Tick at {now_str} - markets: {active_markets}")
        self._refresh_intel_cache()

        to_remove = []

        for ticker, ctx in self.ticker_map.items():
            if ctx.market not in active_markets:
                continue

            # Fetch current price
            bar = self._fetch_price(ticker, ctx)
            if bar is None:
                ctx.fail_count += 1
                if ctx.fail_count >= self.MAX_CONSECUTIVE_FAILURES:
                    logger.warning(f"Removing {ticker}: {self.MAX_CONSECUTIVE_FAILURES} consecutive failures")
                    to_remove.append(ticker)
                continue
            ctx.fail_count = 0

            # Update buffer
            buf = self.buffers.get(ticker)
            if not buf:
                continue
            buf.append(bar)

            if not buf.is_warm():
                continue

            # Compute signals for each strategy
            df = buf.to_dataframe()
            for strategy in self.strategies:
                try:
                    sig_gen = SignalGenerator(strategy)
                    df_ind = sig_gen.compute_indicators(df)
                    signals = sig_gen.generate_signals(df_ind)
                    last_signal = int(signals.iloc[-1])

                    self.tracker.update(ticker, strategy.name, last_signal)
                    self._save_realtime_signal_to_db(ticker, strategy.name, last_signal, ctx, float(df_ind.iloc[-1]["Close"]))
                    consensus_event = self.consensus_tracker.update(
                        ticker, strategy.name, last_signal, is_holding=(ctx.mode == "HOLD")
                    )
                    if consensus_event is not None:
                        self._handle_consensus_signal(ticker, consensus_event, ctx, float(df_ind.iloc[-1]["Close"]))
                except Exception as e:
                    logger.debug(f"Signal error {ticker}/{strategy.name}: {e}")

        # Remove tickers that exceeded max consecutive failures
        for ticker in to_remove:
            del self.ticker_map[ticker]
            self.buffers.pop(ticker, None)
            self.tracker.reset(ticker)

    def _fetch_price(self, ticker: str, ctx: TickerContext) -> dict | None:
        """Fetch current price from KIS API."""
        if ctx.market == "KRX":
            return self.kis.get_domestic_price(ticker)
        else:
            exchange = ctx.exchange or "NASDAQ"
            return self.kis.get_overseas_price(ticker, market=exchange)

    def _save_realtime_signal_to_db(
        self, ticker: str, strategy_name: str, signal: int, ctx: TickerContext, price: float
    ) -> None:
        """Save individual realtime signal to DB (no Discord)."""
        if signal == 0:
            return
        signal_type = "BUY" if signal == 1 else "SELL"
        try:
            self.signal_service.save_signal({
                "strategy_name": strategy_name,
                "ticker": ticker,
                "ticker_name": ctx.name,
                "market": ctx.market,
                "signal_type": signal_type,
                "price": price,
                "indicators": {},
                "source": "realtime",
            })
        except Exception as e:
            logger.debug(f"Failed to save realtime signal to DB: {e}")

    def _handle_consensus_signal(
        self, ticker: str, event: dict, ctx: TickerContext, price: float
    ) -> None:
        """Send Discord alert for ticker-level consensus direction change."""
        signal_type = event["signal_type"]
        buy_count = event["buy_count"]
        sell_count = event["sell_count"]
        total = event["total_strategies"]
        ratio = event["consensus_ratio"]
        urgent = event.get("urgent", False)
        prev = event["prev_direction"]
        prev_label = "매수" if prev == 1 else ("매도" if prev == -1 else "중립")
        curr_label = "매수" if signal_type == "BUY" else "매도"
        currency = "원" if ctx.market == "KRX" else "$"
        urgent_prefix = "⚠️ 긴급 " if urgent else ""

        content = (
            f"{'⬆️' if signal_type == 'BUY' else '⬇️'} "
            f"**{urgent_prefix}{ctx.name}({ticker}) {curr_label} 합의 {'긴급 ' if urgent else ''}확정**\n"
            f"전략 동의: {buy_count if signal_type == 'BUY' else sell_count}/{total} "
            f"({ratio:.0%})  |  이전: {prev_label} → 현재: {curr_label}\n"
            f"현재가: {price:,.0f}{currency}"
        )
        self.discord.send(content=content)

        # Save consensus signal to DB
        try:
            self.signal_service.save_signal({
                "strategy_name": f"합의({buy_count}매수/{sell_count}매도/{total}전략)",
                "ticker": ticker,
                "ticker_name": ctx.name,
                "market": ctx.market,
                "signal_type": signal_type,
                "price": price,
                "indicators": {"consensus_ratio": ratio, "urgent": urgent},
                "source": "realtime_consensus",
            })
        except Exception as e:
            logger.debug(f"Failed to save consensus signal: {e}")

    def _handle_signal(self, ticker: str, strategy: Strategy,
                       df_ind, event, ctx: TickerContext):
        """Format and send Discord alert for a signal transition."""
        signal_type = "BUY" if event.current_signal == 1 else "SELL"
        last_row = df_ind.iloc[-1]
        now_kst = datetime.now(KST)

        # Extract indicator values
        indicators = {}
        for col in df_ind.columns:
            if col not in ("Open", "High", "Low", "Close", "Volume"):
                val = last_row[col]
                if str(val) != "nan":
                    indicators[col] = float(val)

        currency = "원" if ctx.market == "KRX" else "$"

        signal_info = {
            "strategy_name": strategy.name,
            "ticker": ticker,
            "ticker_name": ctx.name,
            "signal_type": signal_type,
            "price": float(last_row["Close"]),
            "indicators": indicators,
            "timestamp": now_kst.strftime("%Y-%m-%d %H:%M KST"),
            "market": ctx.market,
            "is_holding": ctx.mode == "HOLD",
            "holding": ctx.holding,
            "currency": currency,
        }

        # Intel overlay: log coverage
        intel_ctx = self._get_intel_context(ticker)
        if intel_ctx:
            latest = intel_ctx[0]
            logger.info(
                f"INTEL OVERLAY {ticker}: {len(intel_ctx)} recent issues, "
                f"latest='{latest.get('title', '')[:40]}' "
                f"sentiment={latest.get('sentiment', 'N/A')} "
                f"direction={latest.get('direction', 'N/A')}"
            )
            signal_info["intel_context"] = intel_ctx[:3]  # Attach top 3 for reference
        else:
            logger.info(f"INTEL GAP: No intel coverage for {ctx.name}({ticker})")
            signal_info["intel_context"] = None

        embed = AlertFormatter.format_realtime_signal(signal_info)
        self.discord.send(embed=embed)

        # Invoke callback if registered
        if self.on_signal:
            try:
                self.on_signal(signal_info)
            except Exception as cb_err:
                logger.warning(f"on_signal callback error: {cb_err}")

        action = "BUY" if signal_type == "BUY" else "SELL"
        hold_tag = " [HOLD]" if ctx.mode == "HOLD" else ""
        logger.info(f"ALERT: {action} {ctx.name}({ticker}) @ {last_row['Close']:,.0f}{currency} "
                    f"[{strategy.name}]{hold_tag}")

    def stop(self):
        """Gracefully stop the monitor."""
        self._running = False
        self.discord.send(content="**Money Mani 실시간 모니터 종료**")
        self.kis.close()
        logger.info("Monitor stopped.")
