"""Knowledge base API endpoints."""

from fastapi import APIRouter
from pydantic import BaseModel
from web.services.knowledge_service import KnowledgeService

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

_service = KnowledgeService()


class KnowledgeEntryCreate(BaseModel):
    category: str = "manual"
    subject: str = ""
    content: str
    tags: list[str] = []


@router.get("/search")
async def search_knowledge(q: str, category: str = None, limit: int = 10):
    """Search knowledge base."""
    results = _service.search(q, category=category, limit=limit)
    return {"results": results, "query": q}


@router.get("/entries")
async def list_entries(category: str = None, subject: str = None, limit: int = 50):
    """List knowledge entries."""
    entries = _service.get_entries(category=category, subject=subject, limit=limit)
    return {"entries": entries}


@router.post("/entries")
async def create_entry(entry: KnowledgeEntryCreate):
    """Manually add a knowledge entry."""
    entry_id = _service.add_entry(
        category=entry.category,
        subject=entry.subject,
        content=entry.content,
        tags=entry.tags,
        source="manual",
    )
    return {"id": entry_id, "status": "created"}
