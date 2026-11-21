from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

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


@pytest.mark.parametrize("assessment_error", [False, True])
def test_unpriced_or_failed_staging_blocks_single_nvda_sale(alembic_engine_at_head, monkeypatch, assessment_error):
    from argosy.services.allocation_author.packet_assembly import assemble_author_packet
    from argosy.services.target_allocation_doc import TargetAllocationDoc

    context = SimpleNamespace(overall_age_label="unknown", is_any_stale=True, snapshot={}, nvda=None)
    monkeypatch.setattr(
        "argosy.services.deployment_market_context.assemble_deployment_market_context", lambda db: context,
    )
    if assessment_error:
        def fail(*args, **kwargs):
            raise RuntimeError("assessment unavailable")
        monkeypatch.setattr("argosy.services.staged_sell_policy.build_nvda_staged_sell_policy", fail)
    doc = TargetAllocationDoc(
        anchor_sigma=.18, blended_sigma=.16, nvda_cap_pct=13, fi_pct=10,
        provenance="test", classes=[], glide=[],
    )
    with Session(alembic_engine_at_head) as db:
        packet = assemble_author_packet(
            db, user_id="ariel", doc=doc, holdings_usd={"NVDA": 1_000_000.0},
            cash_usd=0.0, deployable_usd=0.0,
        )
    assert packet["staged_sell_policies"]["NVDA"]["status"] == "blocked"
    proposal = AllocationProposal(
        cash_to_deploy=0.0, sells=[Sell(symbol="NVDA", amount_usd=1_000_000.0)],
        rationale="Full sale with no staged dates.",
    )
    report = verify_allocation_proposal(proposal, packet, sale_resolver=_sale_resolver)
    assert any(f.code == "staged_sell_not_actionable" for f in report.failures)


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
