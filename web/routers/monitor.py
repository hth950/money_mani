"""Real-time monitor API endpoints."""
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from web.services.monitor_service import MonitorService

router = APIRouter(prefix="/api/monitor", tags=["monitor"])
service = MonitorService()


@router.post("/start")
async def start_monitor(market_filter: str = None):
    return service.start(market_filter=market_filter)


@router.post("/stop")
async def stop_monitor():
    return service.stop()


@router.get("/status")
async def monitor_status():
    return {"running": service.is_running()}


@router.get("/stream")
async def monitor_stream():
    """SSE endpoint for live signals."""
    return StreamingResponse(
        service.event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
