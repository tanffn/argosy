"""Deploy CASH FUNDING breakdown — WHERE the money sits (2026-07-06 incident).

The deploy surface said HOW MUCH to deploy but not that the pool spanned
Leumi USD + Leumi NIS + Schwab USD; the client filled everything from Leumi
USD and went ~$16.4k negative. These tests pin:

* the funding table derived from a synthetic snapshot with the exact live
  shape (Leumi USD + Leumi NIS + Schwab USD);
* required_actions fire when the single largest USD account can't cover the
  full deploy amount (convert-NIS + wire-from-Schwab notes);
* the table sums to the SAME deployable number the client is shown
  (same classifier as ``tradeable_holdings``);
* the post-fill negative-balance state generates a "convert to cover" action;
* an explicit override amount that differs from the pool is labelled.
"""
from argosy.ingest.tsv import PortfolioPosition, PortfolioSnapshot
from argosy.services.allocation_engine import tradeable_holdings
from argosy.services.deployment_funding import derive_cash_funding

FX_USD_NIS = 2.9415  # live pre-fill rate: NIS 58,944.86 ~= $20,040


def _cash(location, currency, local, usd_k):
    return PortfolioPosition(
        location=location, currency=currency, asset_type="Cash", symbol="",
        current_value_local=local, usd_value_k=usd_k,
    )


def _live_prefill_snapshot() -> PortfolioSnapshot:
    """The exact live shape: Leumi USD $144,941 + Leumi NIS ~₪58,945 (~$20,040)
    + Schwab USD $5,893, plus a blank-symbol real-estate row that must NOT be
    swept into cash, and one normal holding."""
    return PortfolioSnapshot(
        source_path="test", fx_usd_nis=FX_USD_NIS,
        positions=[
            _cash("Leumi", "USD", 144941.0, 144.941),
            _cash("Leumi", "NIS", 58944.86, 20.040),
            _cash("schwab 876", "USD", 5893.0, 5.893),
            PortfolioPosition(location="Aborad", currency="USD",
                              asset_type="Real estate", symbol="-",
                              usd_value_k=69.0),
            PortfolioPosition(location="Leumi", currency="USD",
                              asset_type="Core Equity", symbol="CSPX",
                              usd_value_k=100.0),
        ],
    )


def _postfill_snapshot() -> PortfolioSnapshot:
    """Snapshot-10 shape (post-fills): Leumi USD NEGATIVE -$16,434.66."""
    return PortfolioSnapshot(
        source_path="test", fx_usd_nis=3.0028,
        positions=[
            _cash("schwab 876", "USD", 5893.0, 6.0),
            _cash("Leumi", "NIS", 58944.86, 20.04),
            _cash("Leumi", "USD", -16434.66, -16.43466),
            PortfolioPosition(location="Aborad", currency="USD",
                              asset_type="Real estate", symbol="-",
                              usd_value_k=69.0),
        ],
    )


class TestFundingTable:
    def test_rows_per_account_and_currency(self):
        snap = _live_prefill_snapshot()
        fb = derive_cash_funding(snap, 170_874.0)
        by_key = {(r.account, r.currency): r for r in fb.rows}
        assert set(by_key) == {("Leumi", "USD"), ("Leumi", "NIS"),
                               ("schwab 876", "USD")}
        assert by_key[("Leumi", "USD")].balance == 144941.0
        assert by_key[("Leumi", "USD")].usd_equiv == 144941.0
        assert by_key[("Leumi", "NIS")].balance == 58944.86
        assert by_key[("Leumi", "NIS")].usd_equiv == 20040.0
        assert by_key[("schwab 876", "USD")].usd_equiv == 5893.0

    def test_sums_match_the_deployable_number_shown_to_the_client(self):
        """Same classifier as tradeable_holdings => same total, by construction."""
        snap = _live_prefill_snapshot()
        _, snap_cash = tradeable_holdings(snap)
        fb = derive_cash_funding(snap, snap_cash)
        assert fb.total_usd == snap_cash == 170_874.0
        assert fb.note == ""  # amounts match — no difference to label

    def test_real_estate_blank_symbol_row_is_not_cash(self):
        snap = _live_prefill_snapshot()
        fb = derive_cash_funding(snap, 170_874.0)
        assert all(r.account != "Aborad" for r in fb.rows)

    def test_override_amount_difference_is_labelled(self):
        snap = _live_prefill_snapshot()
        fb = derive_cash_funding(snap, 100_000.0)  # explicit cash_usd override
        assert fb.total_usd == 170_874.0
        assert "differs from the deploy amount" in fb.note
        assert "$100,000.00" in fb.note and "$170,874.00" in fb.note


class TestRequiredActions:
    def test_shortfall_fires_convert_and_wire_actions(self):
        """Largest USD account ($144,941) < deploy ($170,874) => the client
        must convert the NIS and wire/use Schwab — exactly the live miss."""
        snap = _live_prefill_snapshot()
        fb = derive_cash_funding(snap, 170_874.0)
        joined = "\n".join(fb.required_actions)
        assert "Convert ~NIS 58,945 -> USD" in joined
        assert "at Leumi" in joined
        assert "fills settle T+2" in joined
        assert "Wire $5,893.00 from schwab 876" in joined
        assert "Largest single USD account (Leumi)" in joined
        assert "do NOT fill everything from it" in joined

    def test_no_actions_when_largest_account_covers_the_deploy(self):
        snap = PortfolioSnapshot(
            source_path="test", fx_usd_nis=FX_USD_NIS,
            positions=[_cash("Leumi", "USD", 200_000.0, 200.0)],
        )
        fb = derive_cash_funding(snap, 170_874.0)
        assert fb.required_actions == ()

    def test_negative_balance_generates_convert_to_cover(self):
        """Post-fill snapshot-10 state: Leumi USD -$16,434.66 must generate a
        cover action even with no new deploy amount."""
        fb = derive_cash_funding(_postfill_snapshot(), 0.0)
        joined = "\n".join(fb.required_actions)
        assert "-$16,434.66" in joined
        assert "convert NIS to cover" in joined
        assert "before anything else" in joined

    def test_negative_balance_without_cover_source_still_flags(self):
        snap = PortfolioSnapshot(
            source_path="test",
            positions=[_cash("Leumi", "USD", -5000.0, -5.0)],
        )
        fb = derive_cash_funding(snap, 0.0)
        assert any("NEGATIVE (-$5,000.00)" in a for a in fb.required_actions)
        assert any("fund it" in a for a in fb.required_actions)


class TestRouteWiring:
    """GET /deploy-cash carries the funding table (best-effort, additive)."""

    def _doc(self):
        from argosy.services.target_allocation_doc import (
            AllocationClassDoc,
            AllocationInstrument,
            TargetAllocationDoc,
        )
        return TargetAllocationDoc(
            anchor_sigma=0.18, blended_sigma=0.16, nvda_cap_pct=13.0,
            fi_pct=10.0, provenance="test",
            classes=[AllocationClassDoc(
                label="US broad-market core", snapshot_category="Core Equity",
                sigma_class="us_equity", target_pct=100.0,
                instruments=[AllocationInstrument(
                    symbol="CSPX", role="primary",
                    weight_within_class_pct=100.0, rationale="",
                    domicile="IE")],
                agreement="", rationale="", dissent="")],
            glide=[],
        )

    def test_deploy_cash_response_includes_funding(self, monkeypatch):
        from fastapi.testclient import TestClient

        import argosy.api.routes.portfolio as portfolio
        from argosy.api.main import create_app

        snap = _live_prefill_snapshot()
        monkeypatch.setattr(portfolio, "_load_current_doc_and_holdings",
                            lambda user_id: (self._doc(), {}, 170_874.0))
        monkeypatch.setattr(portfolio, "get_latest_snapshot_row",
                            lambda db, user_id: object())
        monkeypatch.setattr(portfolio, "row_to_snapshot", lambda row: snap)

        client = TestClient(create_app())
        resp = client.get("/api/portfolio/deploy-cash")
        assert resp.status_code == 200
        funding = resp.json()["funding"]
        assert funding is not None
        assert funding["total_usd"] == 170_874.0
        accounts = {(r["account"], r["currency"]) for r in funding["rows"]}
        assert ("Leumi", "NIS") in accounts and ("schwab 876", "USD") in accounts
        assert any("Convert ~NIS 58,945" in a
                   for a in funding["required_actions"])

    def test_deploy_cash_funding_null_without_snapshot(self, monkeypatch):
        from fastapi.testclient import TestClient

        import argosy.api.routes.portfolio as portfolio
        from argosy.api.main import create_app

        monkeypatch.setattr(portfolio, "_load_current_doc_and_holdings",
                            lambda user_id: (self._doc(), {}, 0.0))
        monkeypatch.setattr(portfolio, "get_latest_snapshot_row",
                            lambda db, user_id: None)

        client = TestClient(create_app())
        resp = client.get("/api/portfolio/deploy-cash",
                          params={"cash_usd": 10000})
        assert resp.status_code == 200
        assert resp.json()["funding"] is None
