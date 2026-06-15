from argosy.services.retirement.fi_shock import fi_sufficiency_under_shock


def test_fi_reached_only_at_full_nvda_mark():
    """The 2026-06-15 reality: NW ₪11.95M, perpetuity ₪10.39M, NVDA ₪6.81M in
    the book. A −30% NVDA move drops NW below the perpetuity base — so 'FI
    reached' is true ONLY at the full NVDA mark. This composes the synthesizer's
    sufficiency claim with the risk officer's concentration; no single agent did."""
    out = fi_sufficiency_under_shock(
        net_worth_nis=11_954_153, nvda_value_nis=6_807_040,
        perpetuity_base_nis=10_386_133, fi_total_nis=11_836_133,
        shocks=(0.30, 0.50),
    )
    assert out["base"]["total_reached"] is True
    assert out["shock_0.30"]["perpetuity_reached"] is False  # ₪9.91M < ₪10.39M
    assert out["shock_0.50"]["perpetuity_reached"] is False
