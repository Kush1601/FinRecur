"""Canonical shapes the rule engine sees. Pydantic v2, boundary validation only —
the rule engine trusts these once constructed. Spec 3.3 / 3.6."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Counterparty(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    external_id: str
    name: str
    simulated: bool = False
    meta: dict = {}


class Receivable(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    counterparty_id: str
    total: int  # centavos
    shipping: int  # centavos, part of total
    issued_at: datetime
    status: Literal["open", "partially_paid", "settled", "cancelled"] = "open"
    source: str = "olist"
    simulated: bool = False
    meta: dict = {}


class Receipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    counterparty_id: str
    amount: int  # centavos
    received_at: datetime
    reference: str = ""
    original_reference: str | None = None
    memo: str | None = None
    simulated: bool = False
    duplicate_of: str | None = None
    meta: dict = {}
