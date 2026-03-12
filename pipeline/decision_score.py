"""Conviction classification for ensemble signals (paper-only logging)."""

import logging

logger = logging.getLogger("money_mani.pipeline.decision_score")


def classify_conviction(consensus_count: int, composite_score: float = None) -> str:
    """Classify signal conviction based on ensemble consensus count or composite score.

    Args:
        consensus_count: Number of strategies agreeing on the signal.
        composite_score: Optional composite score from multi-layer scorer (0.0-1.0).
            If provided, takes precedence over consensus_count.

    Returns:
        Conviction level: "HIGH", "MEDIUM", or "LOW".
    """
    if composite_score is not None:
        if composite_score >= 0.75:
            return "HIGH"
        elif composite_score >= 0.60:
            return "MEDIUM"
        else:
            return "LOW"

    # Legacy: consensus count based
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
            Optionally contains 'composite_score' from multi-layer scorer.

    Returns:
        Signal dict with added 'conviction' key.
    """
    count = signal.get("consensus_count", 0)
    composite = signal.get("composite_score")
    conviction = classify_conviction(count, composite)
    signal["conviction"] = conviction

    ticker = signal.get("ticker", "?")
    ticker_name = signal.get("ticker_name", ticker)
    signal_type = signal.get("signal_type", "?")
    score_info = f" composite={composite:.2%}" if composite is not None else ""

    logger.info(
        f"CONVICTION: {conviction} {signal_type} {ticker_name}({ticker}) "
        f"- {count} strategies agree{score_info}"
    )
    return signal
