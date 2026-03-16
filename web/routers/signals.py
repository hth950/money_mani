"""Signals API endpoints."""
from fastapi import APIRouter, Query
from web.services.signal_service import SignalService

router = APIRouter(prefix="/api/signals", tags=["signals"])
service = SignalService()


@router.get("")
async def list_signals(ticker: str = None, signal_type: str = None,
                       date_from: str = None, date_to: str = None):
    return service.list_signals(ticker=ticker, signal_type=signal_type,
                                date_from=date_from, date_to=date_to)


@router.get("/actions")
async def get_actions(days: int = Query(7, ge=1, le=30)):
    return service.get_actions(days=days)


@router.get("/exit-scores")
async def get_exit_scores():
    """Return exit scores for all open positions."""
    return service.get_exit_scores_for_holdings()


@router.get("/summary/{ticker}")
async def get_signal_summary(ticker: str, market: str = "KRX"):
    """Return AI-generated plain-language summary for a ticker's current signals."""
    return service.get_signal_summary(ticker, market)
