"""Scoring dashboard API endpoints."""

from fastapi import APIRouter, Query
from web.services.scoring_service import ScoringService

router = APIRouter(prefix="/api/scoring", tags=["scoring"])
_service = ScoringService()


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
