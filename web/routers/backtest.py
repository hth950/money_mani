"""Backtest API endpoints."""
from fastapi import APIRouter, HTTPException, BackgroundTasks
from web.services.backtest_service import BacktestService
from web.services.job_service import JobService
from web.models.schemas import BacktestRequest

router = APIRouter(prefix="/api/backtest", tags=["backtest"])
service = BacktestService()
job_service = JobService()


@router.post("/run")
async def run_backtest(req: BacktestRequest):
    """Run backtest in background. Returns job_id for tracking."""
    job_id = await job_service.run_background(
        f"backtest_{req.strategy_id}",
        service.run_backtest,
        req.strategy_id,
        req.tickers,
        req.market,
    )
    return {"job_id": job_id, "message": "백테스트가 시작되었습니다."}


@router.get("/results")
async def list_results(strategy_id: int = None, ticker: str = None):
    """List backtest results."""
    return service.list_results(strategy_id=strategy_id, ticker=ticker)


@router.get("/results/{result_id}")
async def get_result(result_id: int):
    """Get single backtest result."""
    result = service.get_result(result_id)
    if not result:
        raise HTTPException(404, "결과를 찾을 수 없습니다.")
    return result


@router.delete("/results/{result_id}", status_code=204)
async def delete_result(result_id: int):
    """Delete a backtest result."""
    if not service.delete_result(result_id):
        raise HTTPException(404, "결과를 찾을 수 없습니다.")


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: int):
    """Check backtest job status."""
    job = job_service.get_job(job_id)
    if not job:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    return job
