from __future__ import annotations

from datetime import date

from argosy.services.after_tax import AfterTaxSale
from argosy.services.allocation_author.proposal import AllocationProposal, Sell
from argosy.services.allocation_author.verifier import verify_allocation_proposal


def _packet() -> dict:
    return {
        "deployable_usd": 0.0,
        "holdings": {"NVDA": 1_000_000.0},
        "known_symbols": {"NVDA"},
        "allow_sells": True,
        "staged_sell_policies": {
            "NVDA": {
                "status": "actionable",
                "as_of": "2026-08-29T00:00:00+00:00",
                "maximum_current_tranche_usd": 100_000.0,
                "execute_no_later_than": "2026-11-24",
            }
        },
    }


def _sale_resolver(symbol: str, gross: float) -> AfterTaxSale:
    return AfterTaxSale(eligible=True, net_fundable_usd=0.0)


def test_full_nvda_exit_is_bounced_to_current_tranche() -> None:
    proposal = AllocationProposal(
        cash_to_deploy=0.0,
        sells=[Sell(symbol="NVDA", amount_usd=1_000_000.0)],
        rationale="Exit everything at once.",
    )
    report = verify_allocation_proposal(
        proposal,
        _packet(),
        sale_resolver=_sale_resolver,
    )
    codes = {failure.code for failure in report.failures}
    assert "staged_sell_exceeds_current_tranche" in codes
    assert "staged_sell_metadata_missing" in codes
    assert "staged_sell_dates_missing" in codes


def test_current_tranche_metadata_satisfies_staging_contract() -> None:
    proposal = AllocationProposal(
        cash_to_deploy=0.0,
        sells=[
            Sell(
                symbol="NVDA",
                amount_usd=90_000.0,
                execution_style="staged_tranche",
                execute_by=date(2026, 9, 5),
                next_review_date=date(2026, 11, 24),
                tranche_reason="Reduce concentration now and refresh after the fill.",
            )
        ],
        rationale="One current tranche; no automatic later sale.",
    )
    report = verify_allocation_proposal(
        proposal,
        _packet(),
        sale_resolver=_sale_resolver,
    )
    staged_codes = {
        failure.code for failure in report.failures
        if failure.code.startswith("staged_sell")
    }
    assert staged_codes == set()
