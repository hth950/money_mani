"""Conviction classification for ensemble signals (paper-only logging)."""

import logging

logger = logging.getLogger("money_mani.pipeline.decision_score")


def classify_conviction(consensus_count: int) -> str:
    """Classify signal conviction based on ensemble consensus count.

    Args:
        consensus_count: Number of strategies agreeing on the signal.

    Returns:
        Conviction level: "HIGH", "MEDIUM", or "LOW".
    """
    if consensus_count >= 15:
        return "HIGH"
    elif consensus_count >= 10:
        return "MEDIUM"
    else:
        return "LOW"


def log_conviction(signal: dict) -> dict:
    """Add conviction classification to a signal dict and log it.

    Args:
        signal: Signal dict with 'consensus_count', 'ticker', 'signal_type' keys.

    Returns:
        Signal dict with added 'conviction' key.
    """
    count = signal.get("consensus_count", 0)
    conviction = classify_conviction(count)
    signal["conviction"] = conviction

    ticker = signal.get("ticker", "?")
    ticker_name = signal.get("ticker_name", ticker)
    signal_type = signal.get("signal_type", "?")

    logger.info(
        f"CONVICTION: {conviction} {signal_type} {ticker_name}({ticker}) "
        f"- {count} strategies agree"
    )
    return signal
