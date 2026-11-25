"""Read-only strategy validation endpoints."""

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from web.services.strategy_validation_service import StrategyValidationService

router = APIRouter(prefix="/api/validation", tags=["validation"])
service = StrategyValidationService()


@router.get("/overview")
def get_overview(
    horizon: int = Query(10, description="Primary 5, 10, or 20 trading-day horizon"),
    market: str | None = Query(None, description="Optional KRX or US filter"),
):
    """Return evidence and forward outcomes without changing strategy status."""
    try:
        return service.build_overview(horizon=horizon, market=market)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/selection")
def get_selection(market: str | None = Query(None, pattern="^(KRX|US)$")):
    """Summarize prospective and historical selection using executable labels."""
    from web.services.selection_evaluation_service import SelectionEvaluationService

    report = SelectionEvaluationService().build_report(market=market)
    # An offline validation run on a fixed ticker panel remains separate from
    # the strategy evidence tables that the live scanner uses for selection.
    path = Path(__file__).resolve().parents[2] / "output/selection-audit/evidence/summary.json"
    if path.is_file():
        try:
            sample = json.loads(path.read_text(encoding="utf-8"))
            for venue, result in report["markets"].items():
                result["research_sample"] = {
                    "end": sample["data_end"],
                    **sample["markets"][venue],
                }
        except (OSError, KeyError, TypeError, ValueError):
            pass
    return report
