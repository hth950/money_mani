"""Discovery API endpoints."""
from fastapi import APIRouter, HTTPException
from web.services.discovery_service import DiscoveryService
from web.services.job_service import JobService
from web.models.schemas import DiscoveryRequest

router = APIRouter(prefix="/api/discovery", tags=["discovery"])
service = DiscoveryService()
job_service = JobService()


@router.post("/run")
async def run_discovery(req: DiscoveryRequest):
    job_id = await job_service.run_background(
        "discovery",
        service.run_discovery,
        queries=req.queries,
        market=req.market,
        top_n=req.top_n,
        use_trends=req.use_trends,
    )
    return {"job_id": job_id, "message": "전략 탐색이 시작되었습니다."}


@router.get("/reports")
async def list_reports():
    return service.list_reports()


@router.get("/reports/{report_id}")
async def get_report(report_id: int):
    report = service.get_report(report_id)
    if not report:
        raise HTTPException(404, "리포트를 찾을 수 없습니다.")
    return report
