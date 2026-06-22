"""REST surface for tax checks — the §102 RSU-withholding closed loop.

Two routes:

* ``GET  /api/tax/withholding-check?user_id=`` — the latest payslip-derived
  §102 equity-tax withholding verdict (status, period, the ₪ numbers, summary,
  caveats). Open (read-only). Returns an honest ``has_verdict=false`` /
  ``status="no_data"`` shape when nothing has been ingested.
* ``POST /api/tax/payslips/ingest?user_id=`` — manually trigger payslip
  ingestion. Admin-gated (``X-Argosy-Admin`` header, same gate as the other
  manual job triggers) since it does real ingest/parse work.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from argosy.api.auth import require_admin_token
from argosy.api.routes.plan import get_db  # reuse the existing sync get_db dep
from argosy.logging import get_logger
from argosy.services.payslip_ingest import (
    ingest_payslips,
    latest_withholding_verdict,
)

log = get_logger("argosy.api.routes.tax")

router = APIRouter(prefix="/api/tax", tags=["tax"])


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class WithholdingVerdictDTO(BaseModel):
    """Serialized :class:`WithholdingVerdict` (the §102 adequacy verdict).

    Mirrors the dataclass fields; monetary values are in NIS (₪). ``None`` for a
    derived number means it could not be computed (e.g. no equity yet) — never a
    fabricated default.
    """

    status: str
    period: int | None
    equity_ordinary_base: float | None
    equity_capital_base: float | None
    actual_tax_withheld: float | None
    expected_at_wire_rate: float | None
    reconc_residual: float | None
    conservative_liability: float | None
    potential_filing_topup: float | None
    effective_rate_pct: float | None
    summary: str
    confidence: str
    caveats: list[str]


class WithholdingCheckResponse(BaseModel):
    """``GET /api/tax/withholding-check`` body."""

    has_verdict: bool
    period_year: int | None
    period_month: int | None
    ingested_at: str | None
    status: str
    verdict: WithholdingVerdictDTO | None


class PayslipIngestResponse(BaseModel):
    """``POST /api/tax/payslips/ingest`` body — the ingest run summary."""

    ingested: int
    updated: int
    skipped: int
    errors: list[str]
    periods: list[dict[str, Any]]
    skipped_reason: str | None


@router.get("/withholding-check", response_model=WithholdingCheckResponse)
def get_withholding_check(
    user_id: str = Query("ariel"),
    db: Annotated[Session, Depends(get_db)] = ...,
) -> WithholdingCheckResponse:
    """Return Argosy's latest §102 equity-tax withholding verdict for the user.

    Read-only: reads the most-recent ingested payslip's stored verdict. The
    daily ``payslip_ingest`` job (or the manual ingest POST) populates it.
    """
    latest = latest_withholding_verdict(user_id, db)
    verdict = latest.get("verdict")
    return WithholdingCheckResponse(
        has_verdict=bool(latest["has_verdict"]),
        period_year=latest["period_year"],
        period_month=latest["period_month"],
        ingested_at=latest["ingested_at"],
        status=str(latest["status"]),
        verdict=WithholdingVerdictDTO(**verdict) if verdict else None,
    )


@router.post(
    "/payslips/ingest",
    response_model=PayslipIngestResponse,
    dependencies=[Depends(require_admin_token)],
)
def post_ingest_payslips(
    user_id: str = Query("ariel"),
) -> PayslipIngestResponse:
    """Manually trigger payslip ingestion (admin-gated).

    Discovers the user's payslip PDFs, catalogs + parses any new/changed ones,
    runs the §102 withholding check, and persists facts + verdict. Idempotent.
    Runs synchronously (the scan is cheap) and returns the run summary.
    """
    summary = ingest_payslips(user_id)
    log.info("tax.payslips_ingest.manual", user_id=user_id, **{
        k: v for k, v in summary.items() if k != "periods"
    })
    return PayslipIngestResponse(**summary)


__all__ = ["router"]
