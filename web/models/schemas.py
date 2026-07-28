"""Pydantic schemas for API request/response validation."""
from typing import Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

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


class PaperOrderPreviewRequest(BaseModel):
    """Validate a manual paper-order before fetching a quote."""

    model_config = ConfigDict(str_strip_whitespace=True)

    side: Literal["BUY", "SELL"]
    market: Literal["KRX", "US"]
    ticker: str = Field(min_length=1, max_length=32)
    quantity: StrictInt = Field(ge=1)
    risk_acknowledged: bool = False
    risk_snapshot_hash: Optional[str] = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$"
    )

    @field_validator("side", "market", mode="before")
    @classmethod
    def normalize_enum(cls, value):
        return str(value).strip().upper()

    @field_validator("ticker", mode="before")
    @classmethod
    def normalize_ticker(cls, value):
        return str(value).strip().upper()

    @field_validator("risk_snapshot_hash", mode="before")
    @classmethod
    def normalize_risk_hash(cls, value):
        if value is None or value == "":
            return None
        return str(value).strip().lower()

class PaperOrderRequest(PaperOrderPreviewRequest):
    """Confirm a paper order; the server always obtains a fresh quote."""

    idempotency_key: str = Field(min_length=8, max_length=128)
    preview_price: Optional[float] = Field(default=None, gt=0)
    preview_price_at: Optional[str] = None
    estimated_price: Optional[float] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def require_preview_price(self):
        if self.preview_price is None and self.estimated_price is None:
            raise ValueError("미리보기 가격 확인 후 주문을 확정해 주세요.")
        return self
