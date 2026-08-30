from datetime import UTC, datetime

from argosy.services.order_sheet_facts import collect_execution_facts
from argosy.services.target_allocation_doc import (
    AllocationClassDoc,
    AllocationInstrument,
    TargetAllocationDoc,
)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _doc() -> TargetAllocationDoc:
    return TargetAllocationDoc(
        anchor_sigma=0.18,
        blended_sigma=0.16,
        nvda_cap_pct=30,
        fi_pct=10,
        provenance="test",
        classes=[
            AllocationClassDoc(
                label="Ex-US",
                snapshot_category="ex_us",
                sigma_class="ex_us",
                target_pct=100,
                instruments=[
                    AllocationInstrument(
                        symbol="EXUS",
                        role="primary",
                        weight_within_class_pct=100,
                        rationale="",
                        domicile="IE",
                    )
                ],
                agreement="",
                rationale="",
                dissent="",
            )
        ],
        glide=[],
    )


def test_collects_price_market_cap_country_venue_with_timestamp() -> None:
    def fetch(symbol: str):
        assert symbol == "EXUS"
        return {
            "ticker": "EXUS.L",
            "resolved_ticker": "EXUS.L",
            "price": 24.0,
            "market_cap": 2_000_000_000,
            "country": None,
            "exchange": "LSE",
            "timestamp_utc": "2026-08-25T11:59:00Z",
        }

    facts, failures = collect_execution_facts(
        ["EXUS"],
        doc=_doc(),
        quote_facts_fn=fetch,
        observed_at=NOW,
    )
    assert failures == {}
    exus = facts["EXUS"]
    assert exus.venue == "LSE"
    assert exus.evidence.price_usd == 24
    assert exus.evidence.market_cap_usd == 2_000_000_000
    assert exus.evidence.incorporation_country == "IE"
    assert exus.evidence.incorporation_source == "canonical plan instrument domicile"


def test_missing_live_price_is_fail_loud_without_snapshot_fallback() -> None:
    facts, failures = collect_execution_facts(
        ["EXUS"],
        doc=_doc(),
        quote_facts_fn=lambda _s: None,
        observed_at=NOW,
    )
    assert facts == {}
    assert failures == {"EXUS": "no positive live-verified price"}


def test_bare_us_ticker_collision_cannot_price_foreign_plan_etf() -> None:
    facts, failures = collect_execution_facts(
        ["EXUS"],
        doc=_doc(),
        quote_facts_fn=lambda _symbol: {
            "ticker": "EXUS",
            "resolved_ticker": "EXUS",
            "price": 29.0,
            "exchange": "NasdaqGM",
            "quote_type": "ETF",
            "timestamp_utc": "2026-08-25T11:59:00Z",
        },
        observed_at=NOW,
    )
    assert facts == {}
    assert "listing identity mismatch" in failures["EXUS"]


def test_foreign_plan_etf_candidates_exclude_bare_us_collision() -> None:
    from argosy.services.order_sheet_facts import _quote_ticker_candidates

    candidates = _quote_ticker_candidates("EXUS", "IE")
    assert candidates[0] == "EXUS.L"
    assert "EXUS" not in candidates
