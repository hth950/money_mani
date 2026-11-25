"""Regressions for executable next-open backtests and walk-forward folds."""

import json

import pandas as pd
import pytest

from backtester.engine import WARMUP_BARS, BacktestEngine
from backtester.walk_forward import WalkForwardValidator
from strategy.models import Strategy
from web.db import connection
from web.services.backtest_service import BacktestService


def _bar_frame(closes, opens=None):
    opens = opens if opens is not None else closes
    return pd.DataFrame({
        "Open": opens,
        "High": [max(opening, close) + 1 for opening, close in zip(opens, closes)],
        "Low": [min(opening, close) - 1 for opening, close in zip(opens, closes)],
        "Close": closes,
        "Volume": 1_000,
    }, index=pd.bdate_range("2024-01-01", periods=len(closes)))


def _strategy(*, momentum=False):
    return Strategy(
        name="synthetic", description="", source="test", category="trend",
        status="draft", parameters={},
        rules={
            "entry": [{"condition": "threshold", "indicator": "MOM" if momentum else "Close",
                       "direction": "above", "value": 5 if momentum else 105}],
            "exit": [{"condition": "threshold", "indicator": "Close",
                      "direction": "below", "value": 95}],
        },
        indicators=[{"type": "roc", "period": WARMUP_BARS, "output_name": "MOM"}] if momentum else [],
    )


def test_next_open_holds_long_between_buy_and_sell_and_uses_market_costs():
    closes = [90, 110, 100, 100, 90, 100]
    df = _bar_frame(closes, [91, 92, 120, 108, 105, 135])
    config = {"initial_capital": 100_000, "slippage": .001, "commission_krx": .00105, "commission_us": 0}
    us = BacktestEngine.for_market("US", config).run(df, _strategy(), market="US")
    krx = BacktestEngine.for_market("KRX", config).run(df, _strategy(), market="KRX")
    assert us.num_trades == krx.num_trades == 1
    trade = us.trades[0]
    assert trade["entry_time"].startswith(df.index[2].date().isoformat())
    assert trade["entry_price"] == pytest.approx(120 * 1.001)
    assert trade["exit_time"].startswith(df.index[5].date().isoformat())
    assert trade["exit_price"] == 135
    assert krx.total_return < us.total_return


def test_252_prior_sessions_are_indicator_only_and_first_evaluated_close_can_enter():
    prefix = [100] * (WARMUP_BARS - 1) + [110]
    closes = prefix + [110, 100, 90, 100]
    df = _bar_frame(closes)
    result = BacktestEngine.for_market("US").run(
        df, _strategy(momentum=True), market="US", evaluation_start=WARMUP_BARS,
    )
    assert result.period.startswith(df.index[WARMUP_BARS].date().isoformat())
    assert result.num_trades == 1
    assert result.trades[0]["entry_time"].startswith(df.index[WARMUP_BARS + 1].date().isoformat())


def test_walk_forward_matches_the_engine_and_excludes_no_trade_or_failed_folds():
    prefix = [100] * WARMUP_BARS
    leg = [110, 100, 100, 90, 100, 100]
    frame = _bar_frame(prefix + leg + leg + [100] * 6 + leg)
    validator = WalkForwardValidator(train_days=6, test_days=6, step_days=12, min_windows=1)
    result = validator.validate(_strategy(), frame, "SYN", "US")
    comparable = BacktestEngine.for_market("US").run(
        frame.iloc[6:WARMUP_BARS + 12], _strategy(), "SYN", "US", evaluation_start=WARMUP_BARS,
    )
    assert result.total_windows == 2
    assert result.valid_windows == 1
    assert result.windows[0].test_return == comparable.total_return
    assert result.windows[0].test_sharpe == comparable.sharpe_ratio
    assert result.windows[0].test_trades == comparable.num_trades == 1
    assert result.windows[1].invalid_reason == "no closed trades in one or both legs"

    bad_frame = frame.drop(columns=["Open"])
    failed = validator.validate(_strategy(), bad_frame, "SYN", "US")
    assert failed.valid_windows == 0
    assert failed.windows[0].invalid_reason.startswith("backtest failed:")
    assert not failed.is_overfit


def test_web_backtest_stores_the_requested_market_policy_and_warmup(tmp_path, monkeypatch):
    import market_data

    strategy = _strategy()
    closes = [100] * WARMUP_BARS + [110, 100, 90] + [100] * 97
    opens = closes.copy()
    opens[WARMUP_BARS + 3] = 120
    frame = _bar_frame(closes, opens)
    starts = []

    class Fetcher:
        def __init__(self, **_kwargs):
            pass

        def get_ohlcv(self, _ticker, start):
            starts.append(start)
            return frame

    monkeypatch.setattr(market_data, "KRXFetcher", Fetcher)
    monkeypatch.setattr(market_data, "USFetcher", Fetcher)
    monkeypatch.setattr(connection, "DB_PATH", tmp_path / "backtest.db")
    connection.init_db()
    with connection.get_db() as db:
        strategy_id = db.execute(
            """INSERT INTO strategies(name, status, rules_json, indicators_json, parameters_json)
               VALUES (?, ?, ?, '[]', '{}')""",
            (strategy.name, strategy.status, json.dumps(strategy.rules)),
        ).lastrowid

    service = BacktestService()
    us, = service.run_backtest(strategy_id, ["AAA"], "US")
    krx, = service.run_backtest(strategy_id, ["000001"], "KRX")
    us_policy = json.loads(service.get_result(us["id"])["validation_policy_json"])["execution"]
    krx_policy = json.loads(service.get_result(krx["id"])["validation_policy_json"])["execution"]
    assert us_policy["market"] == "US"
    assert us_policy["commission"] == 0
    assert krx_policy["commission"] == .00105
    assert us_policy["warmup_bars"] == WARMUP_BARS
    assert us["period"].startswith(frame.index[WARMUP_BARS].date().isoformat())
    assert us["total_return"] > krx["total_return"]
    assert all(start < "2013-01-01" for start in starts)
