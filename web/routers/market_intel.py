"""API endpoints for market intelligence."""

import logging
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from web.services.market_intel_service import MarketIntelService

logger = logging.getLogger("money_mani.web.routers.market_intel")
router = APIRouter(prefix="/api/intel", tags=["market-intel"])
service = MarketIntelService()


class ScanRequest(BaseModel):
    scan_type: str = "pre_market"


@router.get("/scans")
async def list_scans(limit: int = 20):
    """List recent scan records."""
    return service.list_scans(limit=limit)


@router.get("/scans/{scan_id}")
async def get_scan(scan_id: int):
    """Get scan with its issues."""
    result = service.get_scan(scan_id)
    if not result:
        raise HTTPException(status_code=404, detail="Scan not found")
    return result


@router.get("/issues")
async def list_issues(days: int = 7, category: str = None):
    """List recent issues."""
    return service.get_issues(days=days, category=category)


@router.get("/issues/{issue_id}")
async def get_issue(issue_id: int):
    """Get single issue with full details."""
    result = service.get_issue(issue_id)
    if not result:
        raise HTTPException(status_code=404, detail="Issue not found")
    return result


@router.get("/accuracy")
async def accuracy_stats():
    """Get prediction accuracy statistics."""
    return service.get_accuracy_stats()


@router.post("/scan")
async def trigger_scan(req: ScanRequest, background_tasks: BackgroundTasks):
    """Trigger a manual scan (runs in background)."""
    background_tasks.add_task(service.run_scan_now, req.scan_type)
    return {"message": f"Scan '{req.scan_type}' started in background"}
