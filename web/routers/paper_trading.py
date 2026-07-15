"""Manual paper-trading API endpoints."""

import ast
import json

from fastapi import APIRouter, HTTPException, Query

from web.models.schemas import PaperOrderPreviewRequest, PaperOrderRequest
from web.services.job_service import JobService
from web.services.paper_quote_service import PaperQuoteUnavailable
from web.services.paper_trading_service import (
    PaperTradingConflict,
    PaperTradingNotFound,
    PaperTradingService,
)

router = APIRouter(prefix="/api/paper-trading", tags=["paper-trading"])
service = PaperTradingService()
job_service = JobService()


def _run_refresh_job() -> dict:
    """Keep JobService's 500-character summary parseable for partial results."""
    result = service.refresh_open_positions(refresh_source="manual")
    errors = result.get("errors") or []
    compact_errors = [
        {
            "position_id": error.get("position_id"),
            "ticker": error.get("ticker"),
            "error": str(error.get("error") or "")[:80],
        }
        for error in errors[:2]
    ]
    return {
        "total": int(result.get("total") or 0),
        "succeeded": int(result.get("succeeded") or 0),
        "failed": int(result.get("failed") or 0),
        "errors": compact_errors,
        "errors_truncated": max(0, len(errors) - len(compact_errors)),
    }


def _translate_error(exc: Exception):
    if isinstance(exc, PaperTradingConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, PaperQuoteUnavailable):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, PaperTradingNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.get("/overview")
async def paper_overview():
    return service.get_overview()


@router.post("/orders/preview")
async def preview_paper_order(request: PaperOrderPreviewRequest):
    try:
        return service.preview_order(request)
    except Exception as exc:
        _translate_error(exc)


@router.post("/orders")
async def place_paper_order(request: PaperOrderRequest):
    try:
        return service.place_order(request)
    except Exception as exc:
        _translate_error(exc)


@router.get("/trades")
async def paper_trades(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return service.list_trades(limit=limit, offset=offset)


@router.get("/positions/{position_id}/marks")
async def paper_position_marks(
    position_id: int,
    days: int = Query(30, ge=1, le=3650),
):
    try:
        return service.get_position_marks(position_id=position_id, days=days)
    except Exception as exc:
        _translate_error(exc)


@router.post("/refresh")
async def refresh_paper_positions():
    job_id = await job_service.run_background(
        "paper_trading_refresh",
        _run_refresh_job,
    )
    return {"job_id": job_id, "message": "모의 보유 종목 갱신이 시작되었습니다."}


@router.get("/jobs/{job_id}")
async def paper_refresh_job(job_id: int):
    job = job_service.get_job(job_id)
    if not job or job.get("job_name") != "paper_trading_refresh":
        raise HTTPException(status_code=404, detail="모의투자 갱신 작업을 찾을 수 없습니다.")
    job = dict(job)
    job["job_status"] = job.get("status")
    summary = job.get("result_summary")
    parsed = None
    if summary:
        for parser in (json.loads, ast.literal_eval):
            try:
                candidate = parser(summary)
                if isinstance(candidate, dict):
                    parsed = candidate
                    break
            except (TypeError, ValueError, SyntaxError):
                continue
    job["result"] = parsed
    if (
        job.get("job_status") == "success"
        and parsed
        and int(parsed.get("failed") or 0) > 0
    ):
        job["status"] = "partial"
    return job
