"""Pydantic schemas for API request/response validation."""
from pydantic import BaseModel
from typing import Optional

class StrategyCreate(BaseModel):
    name: str
    description: str = ""
    source: str = ""
    category: str = ""
    status: str = "draft"
    rules: dict = {}
    indicators: list = []
    parameters: dict = {}

class StrategyUpdate(BaseModel):
    description: Optional[str] = None
    category: Optional[str] = None
    status: Optional[str] = None
    rules: Optional[dict] = None
    indicators: Optional[list] = None
    parameters: Optional[dict] = None

class BacktestRequest(BaseModel):
    strategy_id: int
    tickers: list[str]
    market: str = "KRX"

class DiscoveryRequest(BaseModel):
    queries: Optional[list[str]] = None
    market: str = "KRX"
    top_n: int = 3
    use_trends: bool = False

class SignalFilter(BaseModel):
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    ticker: Optional[str] = None
    signal_type: Optional[str] = None
