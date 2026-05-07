from __future__ import annotations
import config


def sentiment_size_multiplier(sentiment_score: int) -> float:
    """
    Maps research sentiment score to a position size multiplier.
    Uses config.SENTIMENT_SIZE_TIERS (sorted descending by score threshold).
    Falls back to config.SENTIMENT_SIZE_FLOOR for very negative scores.

    Args:
        sentiment_score: Research sentiment score (typically -100 to 100).

    Returns:
        Position size multiplier (typically 0.50 to 1.25).
    """
    for threshold, multiplier in config.SENTIMENT_SIZE_TIERS:
        if sentiment_score >= threshold:
            return multiplier
    return config.SENTIMENT_SIZE_FLOOR
