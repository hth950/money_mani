"""Text report formatter for BacktestResult (Korean)."""

from __future__ import annotations

from .metrics import BacktestResult


def format_text_report(result: BacktestResult) -> str:
    """Return a Korean-formatted text summary of a BacktestResult."""
    valid_str = "통과" if result.is_valid else "미달"
    lines = [
        "=" * 50,
        f"[백테스트 결과] {result.strategy_name}",
        f"종목: {result.ticker}   기간: {result.period}",
        "=" * 50,
        f"  총 수익률    : {result.total_return:+.2%}",
        f"  샤프 비율    : {result.sharpe_ratio:.2f}",
        f"  최대 낙폭    : {result.max_drawdown:.2%}",
        f"  승률         : {result.win_rate:.2%}",
        f"  거래 횟수    : {result.num_trades}회",
        f"  유효성 검증  : {valid_str}",
        "=" * 50,
    ]
    if result.trades:
        lines.append(f"  최근 거래 (최대 5건):")
        for t in result.trades[-5:]:
            lines.append(
                f"    {t['entry_time'][:10]} ~ {t['exit_time'][:10]}"
                f"  수익률: {t['return_pct']:+.2%}"
                f"  PnL: {t['pnl']:,.0f}"
            )
        lines.append("=" * 50)
    return "\n".join(lines)
