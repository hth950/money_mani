"""Performance tracking API endpoints."""

from fastapi import APIRouter
from web.services.performance_service import PerformanceService

router = APIRouter(prefix="/api/performance", tags=["performance"])

_service = PerformanceService()


@router.get("/daily")
async def daily_performance(date: str = None):
    """Get daily signal performance. If no date, returns today (KST)."""
    summary = _service.get_performance_summary(date)
    return summary


@router.get("/weekly")
async def weekly_performance(end_date: str = None):
    """Get weekly signal performance summary."""
    summary = _service.get_weekly_summary(end_date)
    return summary


@router.get("/records")
async def all_records(limit: int = 200):
    """Get all signal performance records for the dashboard."""
    records = _service.get_all_daily_records(limit)
    return {"records": records}


@router.get("/reports")
async def list_reports(report_type: str = None, limit: int = 30):
    """List saved performance reports."""
    reports = _service.list_reports(report_type, limit)
    return {"reports": reports}
