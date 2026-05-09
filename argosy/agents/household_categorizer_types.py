"""Pydantic types for HouseholdCategorizerAgent's I/O."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class CategorizeRow(BaseModel):
    tx_id: int
    merchant_normalized: str
    merchant_raw: str
    amount_nis: float
    direction: Literal["debit", "credit"]
    occurred_on: date
    issuer_kind: Literal["bank", "card"]
    issuer_name: str
    issuer_category_he: str | None = None


class CategorizeRequest(BaseModel):
    transactions: list[CategorizeRow]
    taxonomy: list[str]


class CategorizeResult(BaseModel):
    tx_id: int
    category_slug: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class CategorizeResponse(BaseModel):
    results: list[CategorizeResult]
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
