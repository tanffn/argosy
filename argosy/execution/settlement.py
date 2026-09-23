"""Broker-reported settlement facts; unknown is never assumed to be zero."""
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from argosy.execution.fill_evidence import canonical_currency, ledger_amount

# These integrations deliberately accept user/broker-document receipts and
# have no remote order-status API. Missing automation is not a poll failure.
MANUAL_RECEIPT_BROKERS = frozenset({"schwab_csv", "leumi_tsv"})


class FillSettlement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: Literal["USD", "NIS", "ILS", "EUR"]
    tax_withheld: Decimal
    net_cash_delta: Decimal
    reference: str = Field(min_length=1, max_length=512)
    listing_symbol: str | None = Field(default=None, max_length=32)

    @field_validator("currency", mode="before")
    @classmethod
    def canonical_unit(cls, value):
        return canonical_currency(value)

    @field_validator("tax_withheld", "net_cash_delta")
    @classmethod
    def exact_amount(cls, value: Decimal, info):
        result = ledger_amount(value, positive=False)
        if info.field_name == "tax_withheld" and result < 0:
            raise ValueError("withholding cannot be negative")
        return result

    @field_validator("reference")
    @classmethod
    def nonblank_reference(cls, value: str):
        if not value.strip():
            raise ValueError("broker settlement reference is required")
        return value.strip()

    @field_validator("listing_symbol")
    @classmethod
    def canonical_listing(cls, value: str | None):
        if value is None:
            return None
        if not value.strip():
            raise ValueError("listing symbol cannot be blank")
        return value.strip().upper()
