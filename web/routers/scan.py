"""Daily scan API endpoints."""
from fastapi import APIRouter
from web.services.scan_service import ScanService
from web.services.job_service import JobService

router = APIRouter(prefix="/api/scan", tags=["scan"])
service = ScanService()
job_service = JobService()


@router.post("/run")
async def run_scan():
    job_id = await job_service.run_background("daily_scan", service.run_scan)
    return {"job_id": job_id, "message": "일일 스캔이 시작되었습니다."}


@router.get("/history")
async def scan_history():
    return service.list_scans()
