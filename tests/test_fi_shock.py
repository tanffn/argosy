from argosy.services.retirement.fi_shock import (
    derive_fx_shock_inputs,
    derive_nvda_shock_inputs,
    fi_sufficiency_under_fx_shock,
    fi_sufficiency_under_shock,
    primary_fx_shock_net_worth_nis,
    primary_nvda_shock_net_worth_nis,
)


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


def test_primary_shock_net_worth_matches_gate_row():
    """Synthesizer keys must equal the gate's shock_0.30 / fx_shock_-0.10 rows."""
    nw, nvda = 11_954_153.0, 6_807_040.0
    shocked = primary_nvda_shock_net_worth_nis(
        net_worth_nis=nw, nvda_value_nis=nvda,
    )
    gate = fi_sufficiency_under_shock(
        net_worth_nis=nw, nvda_value_nis=nvda,
        perpetuity_base_nis=10_386_133, fi_total_nis=11_836_133,
    )
    assert shocked == gate["shock_0.30"]["net_worth_nis"]

    usd = 8_000_000.0
    fx_shocked = primary_fx_shock_net_worth_nis(
        net_worth_nis=nw, usd_exposure_nis=usd,
    )
    fx_gate = fi_sufficiency_under_fx_shock(
        net_worth_nis=nw, usd_exposure_nis=usd,
        perpetuity_base_nis=10_386_133, fi_total_nis=11_836_133,
    )
    assert fx_shocked == fx_gate["fx_shock_-0.10"]["net_worth_nis"]


class _RV:
    def __init__(self, value, status="resolved"):
        self.value = value
        self.status = status


class _Resolved:
    def __init__(self, d):
        self._d = d

    def get(self, k):
        return self._d.get(k)


def test_derive_shock_inputs_shared_with_gate_shape():
    resolved = _Resolved({
        "portfolio.net_worth_nis": _RV(12_000_000),
        "retirement.fi_target_nis": _RV(10_000_000),
        "retirement.fi_total_capital_nis": _RV(11_000_000),
        "concentration.nvda_current_pct": _RV(0.50),
        "portfolio.usd_exposure_nis": _RV(9_000_000),
    })
    nvda = derive_nvda_shock_inputs(resolved)
    assert nvda == {
        "net_worth_nis": 12_000_000.0,
        "nvda_value_nis": 6_000_000.0,
        "perpetuity_base_nis": 10_000_000.0,
        "fi_total_nis": 11_000_000.0,
    }
    fx = derive_fx_shock_inputs(resolved)
    assert fx["usd_exposure_nis"] == 9_000_000.0
    assert derive_nvda_shock_inputs(_Resolved({})) is None
