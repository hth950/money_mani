"""Read-only strategy validation endpoints."""

from fastapi import APIRouter, HTTPException, Query

from web.services.strategy_validation_service import StrategyValidationService

router = APIRouter(prefix="/api/validation", tags=["validation"])
service = StrategyValidationService()


@router.get("/overview")
async def get_overview(
    horizon: int = Query(10, description="Primary 5, 10, or 20 trading-day horizon"),
    market: str | None = Query(None, description="Optional KRX or US filter"),
):
    """Return evidence and forward outcomes without changing strategy status."""
    try:
        return service.build_overview(horizon=horizon, market=market)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
