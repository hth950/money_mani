"""Portfolio API endpoints."""
from fastapi import APIRouter
from web.services.portfolio_service import PortfolioService
from web.services.job_service import JobService

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])
service = PortfolioService()
job_service = JobService()


@router.get("/live")
async def live_holdings():
    """Fetch live holdings (cached latest snapshot if available)."""
    return service.get_latest_snapshot()


@router.post("/refresh")
async def refresh_portfolio():
    job_id = await job_service.run_background("portfolio_refresh", service.fetch_live)
    return {"job_id": job_id, "message": "포트폴리오 업데이트가 시작되었습니다."}


@router.get("/snapshots")
async def list_snapshots(ticker: str = None):
    return service.list_snapshots(ticker=ticker)
