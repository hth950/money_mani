"""Backtest execution and results persistence service."""
import json
import logging
from web.db.connection import get_db

logger = logging.getLogger("money_mani.web.services.backtest")


class BacktestService:
    """Run backtests and persist results to SQLite."""

    def run_backtest(self, strategy_id: int, tickers: list[str], market: str = "KRX") -> list[dict]:
        """Fetch data, run backtest for each ticker, store results. Returns list of result dicts."""
        from strategy.models import Strategy
        from backtester.engine import WARMUP_BARS, BacktestEngine, evaluation_start_bar, history_start
        from market_data import KRXFetcher, USFetcher
        from utils.config_loader import load_config

        # Load strategy from SQLite
        with get_db() as db:
            row = db.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
            if not row:
                raise ValueError(f"Strategy id={strategy_id} not found")

        # Build Strategy object
        strat = Strategy(
            name=row["name"],
            description=row["description"],
            source=row["source"],
            category=row["category"],
            status=row["status"],
            rules=json.loads(row["rules_json"]),
            indicators=json.loads(row["indicators_json"]),
            parameters=json.loads(row["parameters_json"]),
            backtest_results=json.loads(row["backtest_results_json"]) if row["backtest_results_json"] else None,
        )

        fetcher = KRXFetcher(delay=0.5) if market == "KRX" else USFetcher()
        backtest_config = load_config().get("backtest", {})
        engine = BacktestEngine.for_market(market, backtest_config)
        execution_policy = engine.execution_policy(market, WARMUP_BARS)
        period_start = backtest_config.get("default_period", "2013-01-01")
        fetch_start = history_start(period_start)

        results = []
        for ticker in tickers:
            try:
                logger.info(f"Backtesting {strat.name} on {ticker} ({market})")
                df = fetcher.get_ohlcv(ticker, fetch_start)
                start_bar = evaluation_start_bar(df.index, period_start, WARMUP_BARS) if not df.empty else 0
                if len(df) - start_bar < 100:
                    logger.warning("Insufficient data after warmup for %s", ticker)
                    continue
                result = engine.run(
                    df.iloc[start_bar - WARMUP_BARS:],
                    strat, ticker, market, evaluation_start=WARMUP_BARS,
                )
                # Store in SQLite
                result_id = self._store_result(strategy_id, result, market, execution_policy)
                result_dict = {
                    "id": result_id,
                    "strategy_name": result.strategy_name,
                    "ticker": result.ticker,
                    "market": market,
                    "period": result.period,
                    "total_return": result.total_return,
                    "sharpe_ratio": result.sharpe_ratio,
                    "max_drawdown": result.max_drawdown,
                    "win_rate": result.win_rate,
                    "num_trades": result.num_trades,
                    "is_valid": result.is_valid,
                }
                results.append(result_dict)
            except Exception as e:
                logger.error(f"Backtest failed for {strat.name}/{ticker}: {e}")
        return results

    def _store_result(self, strategy_id: int, result, market: str, execution_policy: dict | None = None) -> int:
        """Insert BacktestResult into backtest_results table."""
        try:
            from utils.config_loader import load_config

            validation_policy = load_config().get("backtest", {}).get(
                "validation_thresholds", {}
            )
        except Exception:
            validation_policy = {}
        if execution_policy is not None:
            validation_policy = {**validation_policy, "execution": execution_policy}

        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO backtest_results
                   (strategy_id, strategy_name, ticker, market, period,
                    total_return, sharpe_ratio, max_drawdown, win_rate,
                    num_trades, is_valid, trades_json, avg_holding_days,
                    annual_trade_rate, validation_policy_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    strategy_id,
                    result.strategy_name,
                    result.ticker,
                    market,
                    result.period,
                    result.total_return,
                    result.sharpe_ratio,
                    result.max_drawdown,
                    result.win_rate,
                    result.num_trades,
                    1 if result.is_valid else 0,
                    json.dumps(result.trades, ensure_ascii=False, default=str),
                    getattr(result, "avg_holding_days", None),
                    getattr(result, "annual_trade_rate", None),
                    json.dumps(validation_policy, ensure_ascii=False, sort_keys=True),
                ),
            )
            return cursor.lastrowid

    def list_results(self, strategy_id: int = None, ticker: str = None, limit: int = 50) -> list[dict]:
        """List backtest results with optional filters."""
        with get_db() as db:
            query = "SELECT * FROM backtest_results WHERE 1=1"
            params = []
            if strategy_id:
                query += " AND strategy_id=?"
                params.append(strategy_id)
            if ticker:
                query += " AND ticker=?"
                params.append(ticker)
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = db.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_result(self, result_id: int) -> dict | None:
        """Get single backtest result by ID."""
        with get_db() as db:
            row = db.execute("SELECT * FROM backtest_results WHERE id=?", (result_id,)).fetchone()
            return dict(row) if row else None

    def delete_result(self, result_id: int) -> bool:
        """Delete a backtest result."""
        with get_db() as db:
            cursor = db.execute("DELETE FROM backtest_results WHERE id=?", (result_id,))
            return cursor.rowcount > 0
