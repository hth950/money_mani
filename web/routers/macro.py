"""API and page endpoints for macro environment monitoring."""

import logging
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from web.services.macro_service import MacroService

logger = logging.getLogger("money_mani.web.routers.macro")

router = APIRouter(tags=["macro"])
service = MacroService()

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/macro", response_class=HTMLResponse)
async def macro_page(request: Request):
    """Render macro environment monitor page."""
    return templates.TemplateResponse("macro/index.html", {"request": request})


@router.get("/api/macro/current")
async def macro_current(market: str = "KRX"):
    """Get latest macro snapshot."""
    return service.get_current(market=market)


@router.get("/api/macro/history")
async def macro_history(hours: int = 48, market: str = "KRX"):
    """Get macro history for last N hours."""
    return service.get_history(hours=hours, market=market)


@router.get("/api/macro/posts")
async def macro_posts(market: str = "KRX"):
    """Get latest community posts sample."""
    return service.get_community_posts(market=market)


@router.post("/api/macro/refresh")
async def macro_refresh(background_tasks: BackgroundTasks):
    """Trigger a fresh macro score computation in background."""
    background_tasks.add_task(service.trigger_refresh)
    return {"message": "Macro refresh started in background"}
