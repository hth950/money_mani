"""HTML page routes."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {"request": request})

@router.get("/strategies", response_class=HTMLResponse)
async def strategies_page(request: Request):
    return templates.TemplateResponse(request, "strategies/list.html", {"request": request})

@router.get("/strategies/new", response_class=HTMLResponse)
async def strategy_new_page(request: Request):
    return templates.TemplateResponse(request, "strategies/form.html", {"request": request})

@router.get("/strategies/{strategy_id}", response_class=HTMLResponse)
async def strategy_detail_page(request: Request, strategy_id: int):
    from web.services.strategy_service import StrategyService
    service = StrategyService()
    strategy = service.get_by_id(strategy_id)
    if not strategy:
        return HTMLResponse("전략을 찾을 수 없습니다.", status_code=404)
    return templates.TemplateResponse(request, "strategies/detail.html", {"request": request, "strategy": strategy})

@router.get("/backtest", response_class=HTMLResponse)
async def backtest_page(request: Request):
    return templates.TemplateResponse(request, "backtest/index.html", {"request": request})

@router.get("/backtest/{result_id}", response_class=HTMLResponse)
async def backtest_detail_page(request: Request, result_id: int):
    from web.services.backtest_service import BacktestService
    service = BacktestService()
    result = service.get_result(result_id)
    if not result:
        return HTMLResponse("결과를 찾을 수 없습니다.", status_code=404)
    return templates.TemplateResponse(request, "backtest/result_detail.html", {"request": request, "result": result})

@router.get("/monitor", response_class=HTMLResponse)
async def monitor_page(request: Request):
    return templates.TemplateResponse(request, "monitor/index.html", {"request": request})

@router.get("/signals", response_class=HTMLResponse)
async def signals_page(request: Request):
    return templates.TemplateResponse(request, "signals/index.html", {"request": request})

@router.get("/scanner", response_class=HTMLResponse)
async def scanner_page(request: Request):
    return templates.TemplateResponse(request, "scanner/index.html", {"request": request})

@router.get("/discovery", response_class=HTMLResponse)
async def discovery_page(request: Request):
    return templates.TemplateResponse(request, "discovery/index.html", {"request": request})

@router.get("/discovery/{report_id}", response_class=HTMLResponse)
async def discovery_detail_page(request: Request, report_id: int):
    from web.services.discovery_service import DiscoveryService
    service = DiscoveryService()
    report = service.get_report(report_id)
    if not report:
        return HTMLResponse("리포트를 찾을 수 없습니다.", status_code=404)
    return templates.TemplateResponse(request, "discovery/report_detail.html", {"request": request, "report": report})

@router.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(request: Request):
    return templates.TemplateResponse(request, "portfolio/index.html", {"request": request})

@router.get("/paper-trading", response_class=HTMLResponse)
async def paper_trading_page(request: Request):
    return templates.TemplateResponse(request, "paper_trading/index.html", {"request": request})

@router.get("/performance", response_class=HTMLResponse)
async def performance_page(request: Request):
    return templates.TemplateResponse(request, "performance/index.html", {"request": request})

@router.get("/intel", response_class=HTMLResponse)
async def intel_page(request: Request):
    return templates.TemplateResponse(request, "intel/index.html", {"request": request})

@router.get("/scoring", response_class=HTMLResponse)
async def scoring_page(request: Request):
    return templates.TemplateResponse(request, "scoring/index.html", {"request": request})

@router.get("/guide", response_class=HTMLResponse)
async def guide_page(request: Request):
    return templates.TemplateResponse(request, "guide/index.html", {"request": request})
