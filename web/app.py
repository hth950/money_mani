"""FastAPI application for money_mani web UI."""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from web.db.connection import init_db
from web.db.migrate import migrate_yaml_strategies

logger = logging.getLogger("money_mani.web")

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing database...")
    init_db()
    migrate_yaml_strategies()
    logger.info("Web app ready.")
    yield
    # Shutdown
    logger.info("Web app shutting down.")

app = FastAPI(
    title="Money Mani",
    description="Stock Investment Research & Alert Pipeline",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error: {exc}", exc_info=True)
    if request.headers.get("HX-Request"):
        return HTMLResponse(
            f'<div class="error">오류가 발생했습니다: {str(exc)}</div>',
            status_code=500,
        )
    return JSONResponse(
        {"error": str(exc)},
        status_code=500,
    )

# Import and include routers
from web.routers.pages import router as pages_router
from web.routers.strategies import router as strategies_router
from web.routers.backtest import router as backtest_router
app.include_router(pages_router)
app.include_router(strategies_router)
app.include_router(backtest_router)
from web.routers.monitor import router as monitor_router
app.include_router(monitor_router)
from web.routers.signals import router as signals_router
from web.routers.scan import router as scan_router
from web.routers.discovery import router as discovery_router
from web.routers.portfolio import router as portfolio_router
from web.routers.performance import router as performance_router
from web.routers.knowledge import router as knowledge_router
app.include_router(signals_router)
app.include_router(scan_router)
app.include_router(discovery_router)
app.include_router(portfolio_router)
app.include_router(performance_router)
app.include_router(knowledge_router)
