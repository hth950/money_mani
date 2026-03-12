"""Risk management API endpoints."""

from fastapi import APIRouter, Query
from web.services.risk_service import RiskService

router = APIRouter(prefix="/api/risk", tags=["risk"])
_service = RiskService()


@router.get("/status")
async def get_risk_status():
    return _service.get_status()


@router.get("/history")
async def get_block_history(limit: int = Query(50, le=200)):
    return _service.get_block_history(limit)


@router.get("/check/{ticker}")
async def check_ticker(ticker: str, market: str = Query("KRX")):
    return _service.check_ticker(ticker, market)
