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
        from backtester.engine import BacktestEngine
        from market_data import KRXFetcher, USFetcher

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
        engine = BacktestEngine(
            initial_capital=10_000_000 if market == "KRX" else 100_000,
            commission=0.00105 if market == "KRX" else 0.0,
        )

        results = []
        for ticker in tickers:
            try:
                logger.info(f"Backtesting {strat.name} on {ticker} ({market})")
                try:
                    from utils.config_loader import load_config
                    _period = load_config().get("backtest", {}).get("default_period", "2013-01-01")
                except Exception:
                    _period = "2013-01-01"
                df = fetcher.get_ohlcv(ticker, _period)
                if df.empty or len(df) < 100:
                    logger.warning(f"Insufficient data for {ticker}")
                    continue
                result = engine.run(df, strat, ticker)
                # Store in SQLite
                result_id = self._store_result(strategy_id, result, market)
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

    def _store_result(self, strategy_id: int, result, market: str) -> int:
        """Insert BacktestResult into backtest_results table."""
        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO backtest_results
                   (strategy_id, strategy_name, ticker, market, period,
                    total_return, sharpe_ratio, max_drawdown, win_rate,
                    num_trades, is_valid, trades_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
