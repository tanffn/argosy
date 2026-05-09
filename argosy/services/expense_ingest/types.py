"""Shared pydantic types for the expense-ingest pipeline.

Parsers return ``ParseResult``; the orchestrator persists those into
``ExpenseStatement`` + ``ExpenseTransaction`` ORM rows. ``GroundTruth`` is
the parser-independent oracle (used only by tests in §17.1 of the spec).
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ParserName(StrEnum):
    LEUMI_OSH = "leumi_osh"
    ISRACARD = "isracard"
    MAX = "max"
    CAL = "cal"
    AMEX = "amex"
    DINERS = "diners"
    DISCOUNT = "discount"


Direction = Literal["debit", "credit"]
TxType = Literal["regular", "standing_order", "installment", "refund"]
SourceKind = Literal["bank", "card"]


class NormalizedTransaction(BaseModel):
    """One transaction, parser-output. Persistence is in the orchestrator."""

    occurred_on: date
    posted_on: date | None = None
    merchant_raw: str
    merchant_normalized: str
    amount_nis: float | None = None            # always positive; None for foreign rows
    amount_orig: float | None = None
    currency_orig: str | None = None           # 'USD' / 'EUR' / None
    direction: Direction
    tx_type: TxType
    reference: str | None = None
    issuer_category: str | None = None         # raw ענף when issuer provides one
    raw_row: dict[str, Any] = Field(default_factory=dict)


class StatementMeta(BaseModel):
    period_start: date
    period_end: date
    charge_date: date | None = None            # 'לחיוב ב-' for cards
    declared_total_nis: float | None = None    # issuer-stated footer total
    parsed_total_nis: float                     # sum of our parsed rows


class SourceHint(BaseModel):
    """Parser's best guess at which source the file is from. Used by the
    orchestrator to register the source on first sight (or match an existing
    one). Not all parsers can fill all fields.
    """

    kind: SourceKind
    issuer: str                                  # 'leumi' | 'isracard' | 'max' | …
    external_id: str                             # last-4 (cards) / account # (banks)
    cardholder_name: str | None = None
    display_name: str | None = None              # may be filled by orchestrator


class ParseResult(BaseModel):
    statement: StatementMeta
    transactions: list[NormalizedTransaction]
    source_hint: SourceHint | None = None       # None for parsers that can't infer


class GroundTruth(BaseModel):
    """Parser-independent ground truth — computed directly from raw cells.

    See ``tests/expense_ground_truth.py`` for the per-issuer oracle functions.
    """

    row_count: int
    sum_debits_nis: float
    sum_credits_nis: float
    declared_total_nis: float | None
