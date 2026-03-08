"""FastAPI router for strategy CRUD endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from typing import Optional

from web.services.strategy_service import StrategyService
from web.models.schemas import StrategyCreate, StrategyUpdate

router = APIRouter(prefix="/api/strategies", tags=["strategies"])
service = StrategyService()


@router.get("")
def list_strategies(
    status: Optional[str] = Query(None, description="Filter by status"),
    category: Optional[str] = Query(None, description="Filter by category"),
):
    """List all strategies with optional filters."""
    return service.list_all(status=status, category=category)


@router.get("/{id}")
def get_strategy(id: int):
    """Get a single strategy by ID."""
    strategy = service.get_by_id(id)
    if strategy is None:
        raise HTTPException(status_code=404, detail=f"Strategy {id} not found")
    return strategy


@router.post("", status_code=201)
def create_strategy(body: StrategyCreate):
    """Create a new strategy."""
    # Guard against duplicate name
    if service.get_by_name(body.name) is not None:
        raise HTTPException(status_code=400, detail=f"Strategy '{body.name}' already exists")
    try:
        new_id = service.create(body.model_dump())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return service.get_by_id(new_id)


@router.put("/{id}")
def update_strategy(id: int, body: StrategyUpdate):
    """Update an existing strategy (partial update)."""
    if service.get_by_id(id) is None:
        raise HTTPException(status_code=404, detail=f"Strategy {id} not found")
    try:
        updated = service.update(id, body.model_dump(exclude_none=True))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=400, detail="Update failed")
    return service.get_by_id(id)


@router.delete("/{id}", status_code=204)
def delete_strategy(id: int):
    """Delete a strategy by ID."""
    if service.get_by_id(id) is None:
        raise HTTPException(status_code=404, detail=f"Strategy {id} not found")
    deleted = service.delete(id)
    if not deleted:
        raise HTTPException(status_code=400, detail="Delete failed")
    return None


@router.post("/{id}/validate")
def validate_strategy(id: int):
    """Set strategy status to 'validated'."""
    if service.get_by_id(id) is None:
        raise HTTPException(status_code=404, detail=f"Strategy {id} not found")
    updated = service.update(id, {"status": "validated"})
    if not updated:
        raise HTTPException(status_code=400, detail="Validation failed")
    return service.get_by_id(id)


@router.post("/{id}/retire")
def retire_strategy(id: int):
    """Set strategy status to 'retired'."""
    if service.get_by_id(id) is None:
        raise HTTPException(status_code=404, detail=f"Strategy {id} not found")
    updated = service.update(id, {"status": "retired"})
    if not updated:
        raise HTTPException(status_code=400, detail="Retire failed")
    return service.get_by_id(id)
