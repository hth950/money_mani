"""Performance tracking API endpoints."""

from fastapi import APIRouter
from web.services.performance_service import PerformanceService
from web.services.analytics_service import AnalyticsService
from web.services.position_service import PositionService

router = APIRouter(prefix="/api/performance", tags=["performance"])

_service = PerformanceService()
_analytics = AnalyticsService()
_positions = PositionService()


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


@router.get("/strategies")
async def strategy_leaderboard(period: str = "30d", limit: int = 10):
    """Strategy leaderboard ranked by live performance."""
    stats = _analytics.get_strategy_leaderboard(period, limit)
    return {"strategies": stats, "period": period}


@router.get("/strategies/{name}")
async def strategy_detail(name: str):
    """Detailed stats and ticker affinity for a strategy."""
    affinity = _analytics.get_ticker_affinity(name)
    return {"strategy_name": name, "ticker_affinity": affinity}


@router.get("/positions")
async def list_positions(status: str = "open", strategy: str = None, limit: int = 100):
    """List positions (open or closed)."""
    if status == "open":
        positions = _positions.get_open_positions(strategy_name=strategy)
    else:
        positions = _positions.get_closed_positions(strategy_name=strategy, limit=limit)
    return {"positions": positions, "status": status}
