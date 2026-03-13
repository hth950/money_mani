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
