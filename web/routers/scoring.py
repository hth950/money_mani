"""Scoring dashboard API endpoints."""

from fastapi import APIRouter, HTTPException, Query
from web.services.scoring_service import ScoringService
from web.services.outcome_analytics_service import OutcomeAnalyticsService

router = APIRouter(prefix="/api/scoring", tags=["scoring"])
_service = ScoringService()
_outcome_analytics = OutcomeAnalyticsService()


@router.get("/today")
async def get_today(scan_date: str = None):
    return _service.get_today_results(scan_date)


@router.get("/history")
async def get_history(days: int = Query(30, le=90)):
    return _service.get_history(days)


@router.get("/ticker/{ticker}")
async def get_ticker_history(ticker: str, limit: int = Query(30, le=100)):
    return _service.get_ticker_history(ticker, limit)


@router.get("/summary")
async def get_summary(days: int = Query(30, le=90)):
    return _service.get_summary(days)


def _outcome_filters(
    horizon: int,
    days: int,
    market: str | None,
    signal_action: str | None,
    recommendation: str | None,
    execution_state: str | None,
    label_source: str | None,
    provenance: str | None,
) -> dict:
    """Keep the two outcome endpoints on the same read-only filter contract."""
    return {
        "horizon": horizon,
        "days": days,
        "market": market,
        "signal_action": signal_action,
        "recommendation": recommendation,
        "execution_state": execution_state,
        "label_source": label_source,
        "provenance": provenance,
    }


@router.get("/outcomes/summary")
async def get_outcome_summary(
    horizon: int = Query(5, ge=1, le=20, description="Forward trading-day horizon"),
    days: int = Query(30, ge=1, le=3650, description="Decision cohort lookback in calendar days"),
    market: str | None = Query(None, description="Optional KRX or US market filter"),
    signal_action: str | None = Query(None, description="Optional BUY or SELL filter"),
    recommendation: str | None = Query(None, description="Optional recommendation filter"),
    execution_state: str | None = Query(None, description="Optional execution-state filter"),
    label_source: str | None = Query(None, description="Optional outcome label-source filter"),
    provenance: str | None = Query(None, description="Optional provenance substring filter"),
):
    """Return evaluated outcome returns and label coverage by market/action."""
    try:
        return _outcome_analytics.get_summary(**_outcome_filters(
            horizon=horizon,
            days=days,
            market=market,
            signal_action=signal_action,
            recommendation=recommendation,
            execution_state=execution_state,
            label_source=label_source,
            provenance=provenance,
        ))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/outcomes/calibration")
async def get_outcome_calibration(
    horizon: int = Query(5, ge=1, le=20, description="Forward trading-day horizon"),
    days: int = Query(30, ge=1, le=3650, description="Decision cohort lookback in calendar days"),
    market: str | None = Query(None, description="Optional KRX or US market filter"),
    signal_action: str | None = Query(None, description="Optional BUY or SELL filter"),
    recommendation: str | None = Query(None, description="Optional recommendation filter"),
    execution_state: str | None = Query(None, description="Optional execution-state filter"),
    label_source: str | None = Query(None, description="Optional outcome label-source filter"),
    provenance: str | None = Query(None, description="Optional provenance substring filter"),
):
    """Return descriptive score-response buckets, not auto-tuned weights."""
    try:
        return _outcome_analytics.get_calibration(**_outcome_filters(
            horizon=horizon,
            days=days,
            market=market,
            signal_action=signal_action,
            recommendation=recommendation,
            execution_state=execution_state,
            label_source=label_source,
            provenance=provenance,
        ))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
