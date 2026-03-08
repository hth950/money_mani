"""Backtest result interpreter: generates Korean language insights."""

from llm.client import OpenRouterClient
from llm.prompts import BACKTEST_INTERPRET_PROMPT


class BacktestInterpreter:
    """Generate Korean-language insights from backtest metrics using LLM."""

    def __init__(self, client: OpenRouterClient | None = None):
        self._client = client or OpenRouterClient()

    def interpret(self, metrics: dict) -> str:
        """Generate Korean insight text from backtest result metrics.

        Args:
            metrics: Dict of backtest metrics (e.g., total_return, sharpe_ratio,
                     max_drawdown, win_rate, total_trades, etc.)

        Returns:
            Korean language analysis string.
        """
        import json
        metrics_text = json.dumps(metrics, ensure_ascii=False, indent=2)
        prompt = BACKTEST_INTERPRET_PROMPT.format(metrics=metrics_text)
        return self._client.chat(
            [{"role": "user", "content": prompt}],
            model="fast",
            max_tokens=1024,
        )
