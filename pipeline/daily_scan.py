"""Daily morning scan: run validated strategies on latest data and alert."""

import gc
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from pipeline.decision_score import log_conviction

KST = timezone(timedelta(hours=9))

logger = logging.getLogger("money_mani.pipeline.daily_scan")

USE_ALL_STRATEGIES = True  # False면 TOP_N만 사용
TOP_N_STRATEGIES = 10
ENSEMBLE_CONSENSUS_N = 7  # N개 이상 전략이 동의해야 시그널 발생


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
        self._last_krx_ohlcv: dict = {}
        self._last_us_ohlcv: dict = {}

        # DiversityScorer for ensemble filter
        try:
            from scoring.diversity_scorer import DiversityScorer
            import yaml
            from pathlib import Path
            scoring_yaml = Path(__file__).parent.parent / "config" / "scoring.yaml"
            if scoring_yaml.exists():
                with open(scoring_yaml) as f:
                    _scoring_cfg = yaml.safe_load(f) or {}
            else:
                _scoring_cfg = {}
            _ensemble_cfg = _scoring_cfg.get("ensemble", {})
            self._diversity_scorer = DiversityScorer(
                min_category_diversity=_ensemble_cfg.get("min_category_diversity", 3)
            )
        except ImportError:
            self._diversity_scorer = None

    def _get_top_strategies(self) -> list:
        """Get strategies ranked by backtest + live performance blend.

        If USE_ALL_STRATEGIES is True, returns all validated strategies.
        Otherwise returns only top N.
        """
        with get_db() as db:
            # Check if walk_forward_results table exists
            try:
                db.execute("SELECT 1 FROM walk_forward_results LIMIT 1")
                use_wf_filter = True
            except Exception:
                use_wf_filter = False

            if use_wf_filter:
                bt_rows = db.execute("""
                    SELECT strategy_name,
                           AVG(total_return) as avg_return,
                           AVG(sharpe_ratio) as avg_sharpe,
                           AVG(win_rate) as avg_win_rate
                    FROM backtest_results
                    WHERE is_valid = 1
                      AND strategy_name NOT IN (
                          SELECT DISTINCT strategy_name FROM walk_forward_results WHERE is_overfit = 1
                      )
                    GROUP BY strategy_name
                """).fetchall()
            else:
                bt_rows = db.execute("""
                    SELECT strategy_name,
                           AVG(total_return) as avg_return,
                           AVG(sharpe_ratio) as avg_sharpe,
                           AVG(win_rate) as avg_win_rate
                    FROM backtest_results
                    WHERE is_valid = 1
                    GROUP BY strategy_name
                """).fetchall()

            if use_wf_filter:
                overfit_count = db.execute(
                    "SELECT COUNT(DISTINCT strategy_name) as cnt FROM walk_forward_results WHERE is_overfit = 1"
                ).fetchone()["cnt"]
                if overfit_count > 0:
                    logger.info(f"Excluded {overfit_count} overfit strategies from daily scan")

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

        if USE_ALL_STRATEGIES:
            selected_names = [s[0] for s in scored]
            logger.info(f"Using ALL {len(selected_names)} strategies (ranked): {selected_names}")
        else:
            selected_names = [s[0] for s in scored[:TOP_N_STRATEGIES]]
            logger.info(f"Top {len(selected_names)} strategies: {selected_names}")

        strategies = []
        for name in selected_names:
            try:
                strat = self.registry.load(name)
                # Consistent with StrategyRegistry.get_validated(): only run strategies
                # with status 'validated' or 'validated_v2'. The DB backtest_results table
                # stores is_valid=1 for rows that passed validation, but the strategy YAML
                # status field is the authoritative source — strategies may have been
                # invalidated or promoted since last backtest.
                if strat.status not in ("validated", "validated_v2"):
                    logger.debug(f"Skipping strategy {name}: status={strat.status}")
                    continue
                strategies.append(strat)
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

        # Get strategies: all validated if USE_ALL_STRATEGIES, else top N ranked
        strategies = self._get_top_strategies()
        if not strategies:
            strategies = self.registry.get_validated()
            logger.info(f"Fallback to all validated strategies: {len(strategies)}")
        if not strategies:
            logger.warning("No validated strategies found.")
            return {"date": str(today), "signals": [], "skipped": False}

        signals = []

        # Auto-add high-confidence intel tickers
        intel_krx_extras = []
        intel_us_extras = []
        try:
            from web.services.market_intel_service import MarketIntelService
            intel_tickers = MarketIntelService().get_high_confidence_tickers(days=7, min_confidence=0.7)
            intel_krx_extras = [t["ticker"] for t in intel_tickers.get("KRX", [])]
            intel_us_extras = [t["ticker"] for t in intel_tickers.get("US", [])]
            if intel_krx_extras or intel_us_extras:
                logger.info(f"Intel auto-add: KRX +{len(intel_krx_extras)}, US +{len(intel_us_extras)}")
        except Exception as e:
            logger.warning(f"Failed to load intel tickers for daily scan: {e}")

        # Factor strategies scan (cross-sectional)
        factor_signals = self._scan_factor_strategies("KRX" if is_krx_day else None, "US" if is_nyse_day else None)
        signals.extend(factor_signals)

        # KRX scan (KRX/ALL strategies only)
        if is_krx_day:
            krx_tickers = self.config["pipeline"]["targets"].get("custom_tickers", [])
            # Merge intel tickers (deduplicated)
            for t in intel_krx_extras:
                if t not in krx_tickers:
                    krx_tickers.append(t)
            krx_strategies = [s for s in strategies if s.market in ("KRX", "ALL")]
            logger.info(f"KRX scan: {len(krx_strategies)} strategies, {len(krx_tickers)} tickers")
            krx_signals = self._scan_market(krx_strategies, krx_tickers, "KRX")
            signals.extend(krx_signals)

        # US scan (US/ALL strategies only)
        if is_nyse_day:
            us_tickers = self.config["pipeline"].get("us_targets", {}).get("custom_tickers", [])
            # Merge intel tickers (deduplicated)
            for t in intel_us_extras:
                if t not in us_tickers:
                    us_tickers.append(t)
            if us_tickers:
                us_strategies = [s for s in strategies if s.market in ("US", "ALL")]
                logger.info(f"US scan: {len(us_strategies)} strategies, {len(us_tickers)} tickers")
                us_signals = self._scan_market(us_strategies, us_tickers, "US")
                signals.extend(us_signals)

        # Apply ensemble consensus filter
        ensemble_signals, consensus_summary = self._apply_ensemble_filter(signals)

        # Apply multi-layer scoring (with fallback to original signals)
        ensemble_signals = self._apply_multi_layer_scoring(ensemble_signals, len(strategies))

        # Apply portfolio risk gate (Phase 2)
        ensemble_signals, blocked_signals = self._apply_risk_gate(ensemble_signals)

        # --- Phase: Exit Scoring for open positions ---
        exit_signals = []
        if is_krx_day:
            try:
                krx_ohlcv = getattr(self, '_last_krx_ohlcv', {})
                exit_signals.extend(self._evaluate_open_positions(krx_ohlcv, "KRX"))
            except Exception as e:
                logger.error(f"KRX exit scoring failed: {e}")
        if is_nyse_day:
            try:
                us_ohlcv = getattr(self, '_last_us_ohlcv', {})
                exit_signals.extend(self._evaluate_open_positions(us_ohlcv, "US"))
            except Exception as e:
                logger.error(f"US exit scoring failed: {e}")

        # Save scoring results to DB — include blocked signals for tracking
        self._save_scoring_results(ensemble_signals + blocked_signals, str(today))

        # Classify conviction for each consensus signal
        for sig in ensemble_signals:
            log_conviction(sig)

        # Save ALL individual signals to DB and track performance
        if signals:
            self._save_signals_to_db(signals, str(today))
            # Record performance for ALL signals (not just consensus)
            for sig in signals:
                try:
                    self.perf_service.record_signal(sig)
                except Exception as e:
                    logger.debug(f"Performance record for {sig.get('ticker')}: {e}")

        # Send alerts only for consensus signals
        if ensemble_signals:
            self._send_alerts(ensemble_signals, str(today), consensus_summary)
        else:
            logger.info(f"{today}: No consensus signals (individual: {len(signals)}).")
            self.discord.send(content=f"📊 {today} 일일 스캔 완료: 합의 시그널 없음 (개별 {len(signals)}건, 기준 N={ENSEMBLE_CONSENSUS_N})")

        # Send exit scoring alerts
        if exit_signals:
            self._send_exit_alerts(exit_signals, str(today))

        return {"date": str(today), "signals": ensemble_signals, "all_signals": signals, "skipped": False}

    def _scan_factor_strategies(self, krx_market=None, us_market=None) -> list[dict]:
        """Scan factor-based strategies (low volatility, F-Score).

        These strategies rank the entire universe rather than per-ticker rules.
        """
        from backtester.factor_ranker import FactorRanker
        from strategy.registry import StrategyRegistry

        signals = []
        ranker = FactorRanker()
        registry = StrategyRegistry()

        # Find factor strategies (strategy_type == "factor")
        factor_strategies = []
        for name in registry.list_strategies():
            try:
                s = registry.load(name)
                if getattr(s, 'strategy_type', 'indicator') == 'factor' and s.status in ('validated', 'validated_v2', 'draft'):
                    factor_strategies.append(s)
            except Exception:
                pass

        if not factor_strategies:
            return signals

        for strat in factor_strategies:
            try:
                market = strat.market  # "KRX" or "US"
                if market == "KRX" and not krx_market:
                    continue
                if market == "US" and not us_market:
                    continue

                # Get universe from config
                if market == "KRX":
                    universe = self.config["pipeline"]["targets"].get("custom_tickers", [])
                else:
                    universe = self.config["pipeline"].get("us_targets", {}).get("custom_tickers", [])

                factor_name = strat.parameters.get("factor_metric", "low_volatility")

                if factor_name == "low_volatility":
                    rankings = ranker.rank_low_volatility(universe, market)
                elif factor_name == "piotroski_fscore":
                    rankings = ranker.rank_piotroski(universe, market)
                else:
                    continue

                # Convert rankings to signals
                for ticker, signal_val in rankings.items():
                    if signal_val == 1:
                        signals.append({
                            "strategy_name": strat.name,
                            "category": strat.category,
                            "ticker": ticker,
                            "ticker_name": ticker,
                            "signal_type": "BUY",
                            "price": 0.0,  # Will be filled by scoring
                            "market": market,
                            "date": datetime.now(KST).strftime("%Y-%m-%d"),
                            "indicators": {"factor": factor_name, "rank_signal": signal_val},
                        })
                    elif signal_val == -1:
                        signals.append({
                            "strategy_name": strat.name,
                            "category": strat.category,
                            "ticker": ticker,
                            "ticker_name": ticker,
                            "signal_type": "SELL",
                            "price": 0.0,
                            "market": market,
                            "date": datetime.now(KST).strftime("%Y-%m-%d"),
                            "indicators": {"factor": factor_name, "rank_signal": signal_val},
                        })
            except Exception as e:
                logger.error(f"Factor strategy {strat.name} failed: {e}")

        return signals

    def _scan_market(self, strategies, tickers, market):
        """Scan tickers with all strategies. Optimized: fetch once per ticker, compute in parallel."""
        scan_start = time.time()
        fetcher = KRXFetcher(delay=0.5) if market == "KRX" else USFetcher()
        signals = []

        # --- Phase 1: Pre-fetch OHLCV data (once per ticker) ---
        ohlcv_cache: dict = {}
        ticker_names: dict = {}

        start_date = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")

        if market == "KRX":
            # Sequential — pykrx is NOT thread-safe
            for ticker in tickers:
                try:
                    df = fetcher.get_ohlcv(ticker, start_date)
                    if df.empty or len(df) < 60:
                        logger.debug(f"Skip {ticker}: insufficient data ({len(df) if not df.empty else 0} rows)")
                        continue
                    ohlcv_cache[ticker] = df
                    try:
                        name = fetcher.get_ticker_name(ticker)
                        # Guard: pykrx may return empty string or non-string for some tickers
                        if isinstance(name, str) and name and "DataFrame" not in name:
                            ticker_names[ticker] = name
                        else:
                            ticker_names[ticker] = ticker
                    except Exception:
                        ticker_names[ticker] = ticker
                except Exception as e:
                    logger.error(f"Fetch error {ticker}: {e}")
        else:
            # Parallel for US (yfinance is thread-safe)
            def _fetch_us(t):
                return t, fetcher.get_ohlcv(t, start_date)

            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(_fetch_us, t): t for t in tickers}
                for future in as_completed(futures):
                    try:
                        ticker, df = future.result()
                        if not df.empty and len(df) >= 60:
                            ohlcv_cache[ticker] = df
                            # Try to get company name from yfinance
                            try:
                                import yfinance as yf
                                info = yf.Ticker(ticker).info
                                name = info.get("shortName") or info.get("longName") or ticker
                                ticker_names[ticker] = name
                            except Exception:
                                ticker_names[ticker] = ticker
                    except Exception as e:
                        logger.warning(f"US fetch failed for {futures[future]}: {e}")

        fetch_time = time.time() - scan_start
        logger.info(f"[{market}] OHLCV fetch: {len(ohlcv_cache)}/{len(tickers)} tickers in {fetch_time:.1f}s")

        if not ohlcv_cache:
            return signals

        # --- Phase 2: Compute indicators in parallel ---
        def _compute_one(strat, ticker, df, t_name):
            try:
                sig_gen = SignalGenerator(strat)
                df_ind = sig_gen.compute_indicators(df)
                sigs = sig_gen.generate_signals(df_ind)
                if len(sigs) == 0:
                    return None
                last_signal = sigs.iloc[-1]
                if last_signal == 0:
                    return None
                signal_type = "BUY" if last_signal == 1 else "SELL"
                last_row = df_ind.iloc[-1]
                return {
                    "strategy_name": strat.name,
                    "category": strat.category,
                    "ticker": ticker,
                    "ticker_name": t_name,
                    "signal_type": signal_type,
                    "price": float(last_row["Close"]),
                    "indicators": {col: float(last_row[col])
                                   for col in df_ind.columns
                                   if col not in ["Open", "High", "Low", "Close", "Volume"]
                                   and not str(last_row[col]) == "nan"},
                    "date": str(df.index[-1].date()),
                    "market": market,
                }
            except Exception as e:
                logger.error(f"Compute error {strat.name}/{ticker}: {e}")
                return None

        compute_start = time.time()
        max_workers = 2 if len(ohlcv_cache) > 30 else min(4, max(1, len(ohlcv_cache)))

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = []
            for strat in strategies:
                for ticker, df in ohlcv_cache.items():
                    futures.append(pool.submit(
                        _compute_one, strat, ticker, df, ticker_names.get(ticker, ticker)
                    ))
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result:
                        signals.append(result)
                        logger.info(f"Signal: {result['signal_type']} {result['ticker']} ({result['strategy_name']})")
                except Exception as e:
                    logger.error(f"Compute future failed: {e}")

        compute_time = time.time() - compute_start
        total_time = time.time() - scan_start
        logger.info(
            f"[{market}] Compute: {len(signals)} signals from {len(strategies)}x{len(ohlcv_cache)} "
            f"pairs in {compute_time:.1f}s (total: {total_time:.1f}s)"
        )

        # Cache OHLCV data for exit scoring
        if market == "KRX":
            self._last_krx_ohlcv = ohlcv_cache
        else:
            self._last_us_ohlcv = ohlcv_cache

        gc.collect()
        return signals

    def _apply_multi_layer_scoring(self, ensemble_signals: list[dict], total_strategies: int) -> list[dict]:
        """Apply multi-layer scoring to ensemble signals.

        Falls back to original signals if scoring fails or is disabled.
        """
        try:
            from scoring.multi_layer_scorer import MultiLayerScorer
            scorer = MultiLayerScorer()

            if not scorer.enabled:
                logger.info("Multi-layer scoring disabled, using consensus only")
                return ensemble_signals

            # Build combined OHLCV lookup {ticker: df} across both markets
            all_ohlcv = {}
            all_ohlcv.update(self._last_krx_ohlcv)
            all_ohlcv.update(self._last_us_ohlcv)

            scored_signals = []
            for sig in ensemble_signals:
                try:
                    ohlcv_df = all_ohlcv.get(sig["ticker"])
                    result = scorer.score(
                        ticker=sig["ticker"],
                        market=sig.get("market", "KRX"),
                        consensus_count=sig.get("consensus_count", 1),
                        total_strategies=total_strategies,
                        ohlcv_df=ohlcv_df,
                    )

                    # Attach scoring data to signal
                    sig["composite_score"] = result["composite_score"]
                    sig["score_decision"] = result["decision"]
                    sig["score_breakdown"] = result["scores"]
                    sig["score_details"] = result["details"]
                    sig["score_weights"] = result["weights"]

                    # Filter: only EXECUTE and WATCH pass through
                    if result["decision"] in ("EXECUTE", "WATCH"):
                        scored_signals.append(sig)
                    else:
                        logger.info(f"SKIP {sig['ticker']}: composite={result['composite_score']:.2%}")

                except Exception as e:
                    logger.warning(f"Scoring failed for {sig['ticker']}, keeping signal: {e}")
                    sig["composite_score"] = None
                    sig["score_decision"] = "FALLBACK"
                    scored_signals.append(sig)

            execute_count = sum(1 for s in scored_signals if s.get("score_decision") == "EXECUTE")
            watch_count = sum(1 for s in scored_signals if s.get("score_decision") == "WATCH")
            skip_count = len(ensemble_signals) - len(scored_signals)
            logger.info(f"Multi-layer scoring: {len(ensemble_signals)} -> {len(scored_signals)} "
                        f"(EXECUTE={execute_count}, WATCH={watch_count}, SKIP={skip_count})")

            return scored_signals

        except ImportError:
            logger.warning("scoring module not available, using consensus only")
            return ensemble_signals
        except Exception as e:
            logger.error(f"Multi-layer scoring error, falling back: {e}")
            return ensemble_signals

    def _apply_risk_gate(self, ensemble_signals: list[dict]) -> tuple[list[dict], list[dict]]:
        """Apply portfolio risk management gate (Phase 2).

        BUY signals that violate risk constraints are marked BLOCKED.
        Returns (passed_signals, blocked_signals) so blocked can still be saved to DB.
        Falls back to passing all signals if risk module unavailable.
        """
        try:
            from scoring.risk_manager import PortfolioRiskManager
            manager = PortfolioRiskManager()

            if not manager.enabled:
                logger.info("Portfolio risk management disabled")
                return ensemble_signals, []

            passed = []
            blocked_list = []
            for sig in ensemble_signals:
                if sig["signal_type"] == "BUY":
                    allowed, reason = manager.check_can_buy(
                        sig["ticker"], sig.get("market", "KRX")
                    )
                    if not allowed:
                        sig["score_decision"] = "BLOCKED"
                        sig["block_reason"] = reason
                        logger.info(f"BLOCKED {sig['ticker']}: {reason}")
                        blocked_list.append(sig)
                        continue
                passed.append(sig)

            if blocked_list:
                logger.info(f"Risk gate: {len(blocked_list)} signals blocked, {len(passed)} passed")
            return passed, blocked_list

        except ImportError:
            logger.warning("risk_manager module not available, skipping risk gate")
            return ensemble_signals, []
        except Exception as e:
            logger.error(f"Risk gate error, passing all: {e}")
            return ensemble_signals, []

    def _apply_ensemble_filter(self, signals: list[dict]) -> tuple[list[dict], dict]:
        """Group signals by ticker, keep only those meeting consensus threshold.

        Returns (filtered_signals, summary_dict).
        Summary: {ticker: {"buy_count": N, "sell_count": N, "buy_strategies": [...], ...}}
        """
        from collections import defaultdict

        by_ticker = defaultdict(lambda: {"buy": [], "sell": []})
        for sig in signals:
            key = "buy" if sig["signal_type"] == "BUY" else "sell"
            by_ticker[sig["ticker"]][key].append(sig)

        filtered = []
        summary = {}

        # Load ensemble config once (outside ticker loop)
        try:
            import yaml
            from pathlib import Path
            scoring_yaml = Path(__file__).parent.parent / "config" / "scoring.yaml"
            if scoring_yaml.exists():
                with open(scoring_yaml) as f:
                    _scoring_cfg = yaml.safe_load(f) or {}
            else:
                _scoring_cfg = {}
        except Exception:
            _scoring_cfg = {}
        ensemble_cfg = _scoring_cfg.get("ensemble", {})
        consensus_cfg = _scoring_cfg.get("consensus", {})
        buy_threshold = consensus_cfg.get("buy_threshold", ENSEMBLE_CONSENSUS_N)
        sell_threshold = consensus_cfg.get("sell_threshold", ENSEMBLE_CONSENSUS_N)
        use_diversity = ensemble_cfg.get("use_diversity_scorer", False) and self._diversity_scorer is not None

        # Strategy-level directional bias warning
        strategy_buy: dict[str, int] = defaultdict(int)
        strategy_sell: dict[str, int] = defaultdict(int)
        for sig in signals:
            name = sig["strategy_name"]
            if sig["signal_type"] == "BUY":
                strategy_buy[name] += 1
            else:
                strategy_sell[name] += 1
        all_strategy_names = set(strategy_buy) | set(strategy_sell)
        for name in all_strategy_names:
            total = strategy_buy[name] + strategy_sell[name]
            if total == 0:
                continue
            sell_pct = strategy_sell[name] / total
            buy_pct = strategy_buy[name] / total
            if sell_pct >= 0.9:
                logger.warning(f"Strategy bias: {name} has {sell_pct:.0%} SELL signals ({strategy_sell[name]}/{total})")
            elif buy_pct >= 0.9:
                logger.warning(f"Strategy bias: {name} has {buy_pct:.0%} BUY signals ({strategy_buy[name]}/{total})")

        for ticker, groups in by_ticker.items():
            buy_count = len(groups["buy"])
            sell_count = len(groups["sell"])
            buy_names = [s["strategy_name"] for s in groups["buy"]]
            sell_names = [s["strategy_name"] for s in groups["sell"]]

            summary[ticker] = {
                "buy_count": buy_count,
                "sell_count": sell_count,
                "buy_strategies": buy_names,
                "sell_strategies": sell_names,
                "ticker_name": groups["buy"][0]["ticker_name"] if groups["buy"] else
                               groups["sell"][0]["ticker_name"] if groups["sell"] else ticker,
            }

            # Build signals_by_strategy for DiversityScorer (inside ticker loop)
            if use_diversity:
                signals_by_strategy = {}
                for sig in groups["buy"] + groups["sell"]:
                    signals_by_strategy[sig["strategy_name"]] = {
                        "category": sig.get("category") or "unknown",
                        "signal_type": sig["signal_type"],
                    }

            # Emit consensus BUY: pick one representative signal, annotate with consensus info
            if use_diversity:
                diversity_buy = self._diversity_scorer.score_ensemble(signals_by_strategy, "BUY")
                passes_diversity = (
                    diversity_buy["weighted_score"] >= ensemble_cfg.get("min_weighted_score", 3.0)
                    and diversity_buy["meets_diversity_min"]
                )
                passes_fallback = buy_count >= buy_threshold

                passes_fallback_multi = (
                    passes_fallback and diversity_buy.get("agreeing_categories", 0) >= 2
                )
                if passes_diversity or passes_fallback_multi:
                    rep = groups["buy"][0].copy()
                    rep["consensus_count"] = buy_count
                    rep["consensus_strategies"] = buy_names
                    rep["signal_type"] = "BUY"
                    rep["diversity_score"] = diversity_buy
                    filtered.append(rep)
                    logger.info(
                        f"ENSEMBLE BUY {ticker}: {buy_count} strategies, "
                        f"diversity={diversity_buy['weighted_score']:.2f}, "
                        f"categories={diversity_buy['agreeing_categories']}/{diversity_buy['total_categories']}"
                    )
            else:
                # Original logic (fallback) — use asymmetric buy_threshold
                if buy_count >= buy_threshold:
                    rep = groups["buy"][0].copy()
                    rep["consensus_count"] = buy_count
                    rep["consensus_strategies"] = buy_names
                    rep["signal_type"] = "BUY"
                    filtered.append(rep)
                    logger.info(f"ENSEMBLE BUY {ticker}: {buy_count}/{len(buy_names)} strategies agree (threshold={buy_threshold})")

            # Emit consensus SELL
            if use_diversity:
                diversity_sell = self._diversity_scorer.score_ensemble(signals_by_strategy, "SELL")
                passes_diversity_sell = (
                    diversity_sell["weighted_score"] >= ensemble_cfg.get("min_weighted_score", 3.0)
                    and diversity_sell["meets_diversity_min"]
                )
                passes_fallback_sell = sell_count >= sell_threshold

                passes_fallback_sell_multi = (
                    passes_fallback_sell and diversity_sell.get("agreeing_categories", 0) >= 2
                )
                if passes_diversity_sell or passes_fallback_sell_multi:
                    rep = groups["sell"][0].copy()
                    rep["consensus_count"] = sell_count
                    rep["consensus_strategies"] = sell_names
                    rep["signal_type"] = "SELL"
                    rep["diversity_score"] = diversity_sell
                    filtered.append(rep)
                    logger.info(
                        f"ENSEMBLE SELL {ticker}: {sell_count} strategies, "
                        f"diversity={diversity_sell['weighted_score']:.2f}, "
                        f"categories={diversity_sell['agreeing_categories']}/{diversity_sell['total_categories']}"
                    )
            else:
                # Original logic (fallback) — use asymmetric sell_threshold
                if sell_count >= sell_threshold:
                    rep = groups["sell"][0].copy()
                    rep["consensus_count"] = sell_count
                    rep["consensus_strategies"] = sell_names
                    rep["signal_type"] = "SELL"
                    filtered.append(rep)
                    logger.info(f"ENSEMBLE SELL {ticker}: {sell_count}/{len(sell_names)} strategies agree (threshold={sell_threshold})")

        logger.info(f"Ensemble filter: {len(signals)} individual -> {len(filtered)} consensus (N>={ENSEMBLE_CONSENSUS_N})")
        return filtered, summary

    def _save_scoring_results(self, signals: list[dict], date_str: str):
        """Save multi-layer scoring results to DB (Phase 5)."""
        try:
            from web.services.scoring_service import ScoringService
            from web.db.connection import get_db
            service = ScoringService()
            # Pre-fetch known ticker names from DB as fallback when name resolution fails
            try:
                with get_db() as db:
                    name_rows = db.execute(
                        "SELECT ticker, ticker_name FROM scoring_results "
                        "WHERE ticker_name != ticker GROUP BY ticker"
                    ).fetchall()
                known_names = {r["ticker"]: r["ticker_name"] for r in name_rows}
            except Exception:
                known_names = {}
            for sig in signals:
                if sig.get("composite_score") is not None:
                    ticker = sig["ticker"]
                    ticker_name = sig.get("ticker_name", ticker)
                    if ticker_name == ticker and ticker in known_names:
                        ticker_name = known_names[ticker]
                    service.save_scoring_result(
                        ticker=ticker,
                        ticker_name=ticker_name,
                        market=sig.get("market", "KRX"),
                        scan_date=date_str,
                        scores={
                            "technical": sig.get("score_breakdown", {}).get("technical"),
                            "fundamental": sig.get("score_breakdown", {}).get("fundamental"),
                            "flow": sig.get("score_breakdown", {}).get("flow"),
                            "intel": sig.get("score_breakdown", {}).get("intel"),
                            "macro": sig.get("score_breakdown", {}).get("macro"),
                            "composite": sig.get("composite_score"),
                        },
                        decision=sig.get("score_decision", "UNKNOWN"),
                        block_reason=sig.get("block_reason"),
                        weights=sig.get("score_weights"),
                    )
        except ImportError:
            logger.debug("scoring_service not available, skipping scoring DB save")
        except Exception as e:
            logger.error(f"Failed to save scoring results: {e}")

    def _evaluate_open_positions(self, ohlcv_cache: dict, market: str) -> list[dict]:
        """Evaluate all open positions for exit signals."""
        try:
            from scoring.exit_scorer import ExitScorer
            exit_scorer = ExitScorer()
        except ImportError:
            logger.debug("exit_scorer module not available, skipping exit evaluation")
            return []

        if not exit_scorer.enabled:
            logger.info("Exit scoring disabled")
            return []

        positions = self.position_service.get_open_positions()
        # Filter by market
        positions = [p for p in positions if p.get("market") == market]

        if not positions:
            return []

        exit_signals = []
        fallback_count = 0
        MAX_FALLBACK = 10  # rate limit prevention

        for pos in positions:
            ticker = pos["ticker"]
            df = ohlcv_cache.get(ticker)
            if df is None or df.empty:
                if fallback_count >= MAX_FALLBACK:
                    logger.warning(f"Exit fallback limit reached ({MAX_FALLBACK}), skipping {ticker}")
                    continue
                try:
                    start = (datetime.now(KST) - timedelta(days=120)).strftime("%Y-%m-%d")
                    if market == "KRX":
                        from market_data.krx_fetcher import KRXFetcher
                        df = KRXFetcher(delay=0.5).get_ohlcv(ticker, start)
                    else:
                        from market_data.us_fetcher import USFetcher
                        df = USFetcher().get_ohlcv(ticker, start)
                    fallback_count += 1
                    logger.info(f"Exit fallback fetch for {ticker}: {len(df) if df is not None else 0} rows")
                except Exception as e:
                    logger.warning(f"Exit fallback fetch failed for {ticker}: {e}")
                    continue
                if df is None or df.empty:
                    continue

            try:
                result = exit_scorer.evaluate(
                    ticker=ticker,
                    market=market,
                    entry_price=pos["entry_price"],
                    entry_date=pos["entry_date"],
                    ohlcv_df=df,
                )

                if result["decision"] != "HOLD":
                    exit_signals.append({
                        "ticker": ticker,
                        "ticker_name": pos.get("ticker_name", ticker),
                        "market": market,
                        "signal_type": "SELL",
                        "exit_score": result["exit_score"],
                        "exit_decision": result["decision"],
                        "exit_reason": result["reason"],
                        "exit_details": result["details"],
                        "exit_scores": result["scores"],
                        "position_id": pos["id"],
                        "entry_price": pos["entry_price"],
                        "pnl_pct": result["details"].get("pnl_pct", 0),
                        "price": result["details"].get("current_price", 0),
                        "date": datetime.now(KST).strftime("%Y-%m-%d"),
                    })
            except Exception as e:
                logger.error(f"Exit evaluation failed for {ticker}: {e}")

        if exit_signals:
            logger.info(f"[{market}] Exit signals: {len(exit_signals)} positions flagged for exit")
        return exit_signals

    def _send_exit_alerts(self, exit_signals: list[dict], date_str: str):
        """Send Discord alerts for exit scoring results."""
        from alerts.formatter import AlertFormatter

        for sig in exit_signals:
            try:
                embed = AlertFormatter.format_exit_signal_alert(sig)
                self.discord.send(embed=embed)
            except Exception as e:
                logger.error(f"Failed to send exit alert for {sig.get('ticker')}: {e}")

        logger.info(f"Sent {len(exit_signals)} exit scoring alerts")

    def _save_signals_to_db(self, signals: list[dict], date_str: str):
        """Save all individual signals to DB for tracking (no alerts)."""
        for sig in signals:
            try:
                self.signal_service.save_signal(sig)
            except Exception as e:
                logger.error(f"Failed to save signal to DB: {e}")

    def _send_alerts(self, signals, date_str, consensus_summary=None):
        """Send alerts via Discord and email, skipping duplicates.

        Signals here are already ensemble-filtered (consensus >= N).
        """
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

        # Manage positions for consensus signals (performance already tracked above)
        for sig in new_signals:
            try:
                sig_id = self.signal_service.save_signal(sig)

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

        # Send consensus signal alerts
        for sig in new_signals:
            consensus_n = sig.get("consensus_count", 1)
            strat_names = sig.get("consensus_strategies", [sig["strategy_name"]])
            extra = {
                "consensus": f"{consensus_n}개 전략 합의 (기준: {ENSEMBLE_CONSENSUS_N})",
                "strategies": ", ".join(strat_names[:5]) + (f" 외 {len(strat_names)-5}개" if len(strat_names) > 5 else ""),
            }
            # Add scoring info if available
            if sig.get("composite_score") is not None:
                breakdown = sig.get("score_breakdown", {})
                extra["composite_score"] = f"{sig['composite_score']:.0%}"
                extra["score_decision"] = sig.get("score_decision", "N/A")
                extra["score_breakdown"] = " | ".join(
                    f"{k}:{v:.0%}" for k, v in breakdown.items()
                ) if breakdown else ""
            self.discord.send_signal_alert(sig, extra_info=extra)

        # Daily summary with consensus info
        self.discord.send_daily_summary(new_signals, date_str, ensemble_n=ENSEMBLE_CONSENSUS_N, consensus_summary=consensus_summary)

        # Email backup
        if self.config.get("notifications", {}).get("email", {}).get("enabled"):
            summary = "\n".join(
                f"- {s['signal_type']} {s['ticker_name']}({s['ticker']}) @ {s['price']:,.0f} "
                f"[합의 {s.get('consensus_count', 1)}개 전략]"
                for s in new_signals
            )
            self.email.send(
                subject=f"[money_mani] {date_str} 앙상블 시그널 ({len(new_signals)}건, N>={ENSEMBLE_CONSENSUS_N})",
                body=f"앙상블 합의 스캔 결과:\n\n{summary}",
            )

        logger.info(f"Sent {len(new_signals)} consensus alerts (skipped {len(signals) - len(new_signals)} duplicates)")
