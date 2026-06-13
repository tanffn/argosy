"""Per-property real-estate net equity — figures cross-checked against the
codex net-equity review (net = Home c7 − |Loan c7|, FX-converted)."""
from __future__ import annotations

from types import SimpleNamespace

from argosy.services.real_estate_equity import compute_real_estate_equity

FX_NIS = 2.94161
FX_EUR = 0.84931


def _re(location, currency, role, value_local):
    return SimpleNamespace(location=location, currency=currency, role=role,
                           value_local=value_local)


def _book():
    # The four properties as parsed from the June TSV (value_local = c7).
    return [
        _re("Pipera", "EUR", "Home", 113_219.0),
        _re("Pipera", "EUR", "Loan", 99_046.0),
        _re("Obor", "EUR", "Home", 118_020.0),
        _re("Obor", "EUR", "Loan", 73_314.0),
        _re("Atlanta, Georgia- Scenic Parkway", "USD", "Home", 318_000.0),
        _re("Atlanta, Georgia- Scenic Parkway", "USD", "Loan", 219_475.0),
        _re("Keret 1", "NIS", "Home", 2_500_000.0),
        _re("Keret 1", "NIS", "Loan", -350_000.0),
    ]


def test_four_properties_net_equity_matches_codex_figures():
    eq = compute_real_estate_equity(_book(), fx_usd_nis=FX_NIS, fx_usd_eur=FX_EUR)
    by = {p.name: p for p in eq.properties}
    assert len(eq.properties) == 4
    # Codex verdict figures (rounded USD $K).
    assert round(by["Pipera"].net_usd_k) == 17
    assert round(by["Obor"].net_usd_k) == 53
    assert round(by["Atlanta, Georgia- Scenic Parkway"].net_usd_k) == 99
    assert round(by["Keret 1"].net_usd_k) == 731
    # Total ≈ 900K (17+53+99+731), conserving the per-property sum.
    assert round(eq.total_net_usd_k) == round(sum(p.net_usd_k for p in eq.properties))


def test_loan_sign_is_absolute():
    # Keret's loan is stored as -350,000; net must subtract its magnitude.
    eq = compute_real_estate_equity(_book(), fx_usd_nis=FX_NIS, fx_usd_eur=FX_EUR)
    keret = next(p for p in eq.properties if p.name == "Keret 1")
    assert keret.net_local == 2_500_000.0 - 350_000.0


def test_missing_fx_warns_not_crashes():
    eq = compute_real_estate_equity(
        [_re("X", "EUR", "Home", 100_000.0), _re("X", "EUR", "Loan", 40_000.0)],
        fx_usd_nis=None, fx_usd_eur=None,
    )
    p = eq.properties[0]
    assert p.net_usd_k is None
    assert any("FX" in w for w in p.warnings)
    assert p.net_local == 60_000.0  # local net still computed
