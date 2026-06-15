"""Unit tests for the /retirement derived-inputs service (no magic numbers)."""
from __future__ import annotations

import textwrap
from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.retirement.derived_inputs import compute_derived_inputs
from argosy.state.models import Base, User, UserContext

IDENTITY = textwrap.dedent("""
    user_date_of_birth: '1982-06-17'
    monthly_expenses_total_nis: 23084
    employment_user_net_monthly_nis: 34000
    spouse_net_monthly_nis: 11835
    dependents_count: 2
    children:
    - age: 10
    - age: 6
    mortgage_balance:
      keret_1_nis: 350000
    pensions:
      keren_hishtalmut:
        balance_nis: 384000
      kupat_gemel:
        balance_nis: 75000
      kupat_pensia:
        balance_nis: 800147
    pensions_ariel:
      keren_hishtalmut_nis: 384000
      pension_nis: 800147
      executive_insurance_nis: 755907
""")


@pytest.fixture
def session(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path/'d.db'}")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng, expire_on_commit=False)()
    s.add(User(id="ariel", plan="free"))
    s.add(UserContext(user_id="ariel", identity_yaml=IDENTITY, goals_yaml=""))
    s.commit()
    yield s
    s.close(); eng.dispose()


def test_identity_fields_derive_no_magic_numbers(session):
    d = compute_derived_inputs(session, user_id="ariel", today=date(2026, 6, 4))
    assert d["current_age"]["value"] == pytest.approx(43.96, abs=0.05)
    assert d["current_age"]["status"] == "resolved"
    assert d["monthly_burn_nis"]["value"] == 23084
    assert d["monthly_income_nis"]["value"] == 45835  # 34000 + 11835
    assert d["mortgage_balance_nis"]["value"] == 350000
    assert d["hishtalmut_balance_nis"]["value"] == 384000
    assert d["kupat_gemel_balance_nis"]["value"] == 75000
    assert d["pension_balance_nis"]["value"] == 800147
    assert d["dependents_count"]["value"] == 2
    assert d["has_kids_under_18"]["value"] is True


def test_missing_datum_is_pending_not_guessed(session):
    # No residence value in identity → pending, NEVER a hardcoded ₪3.5M.
    d = compute_derived_inputs(session, user_id="ariel", today=date(2026, 6, 4))
    assert d["residence_value_nis"]["status"] == "pending"
    assert d["residence_value_nis"]["value"] is None


def test_mortgage_rate_term_and_hishtalmut_date_pending_when_absent(session):
    # The base IDENTITY has no mortgage rate/term and no hishtalmut first-deposit
    # date → all three are pending (NEVER the old hardcoded 4.5% / 240 / 2018-01-01).
    d = compute_derived_inputs(session, user_id="ariel", today=date(2026, 6, 4))
    for k in ("mortgage_annual_rate", "mortgage_term_months", "hishtalmut_first_deposit_date"):
        assert d[k]["status"] == "pending", k
        assert d[k]["value"] is None, k


def test_mortgage_rate_term_and_hishtalmut_date_resolve_from_identity(tmp_path):
    identity = textwrap.dedent("""
        user_date_of_birth: '1982-06-17'
        mortgage_balance:
          keret_1_nis: 350000
          annual_rate: 0.039
          term_months: 300
        pensions:
          keren_hishtalmut:
            balance_nis: 384000
            first_deposit_date: '2017-03-01'
    """)
    eng = sa.create_engine(f"sqlite:///{tmp_path/'d2.db'}")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng, expire_on_commit=False)()
    s.add(User(id="ariel", plan="free"))
    s.add(UserContext(user_id="ariel", identity_yaml=identity, goals_yaml=""))
    s.commit()
    try:
        d = compute_derived_inputs(s, user_id="ariel", today=date(2026, 6, 4))
        assert d["mortgage_annual_rate"]["status"] == "resolved"
        assert d["mortgage_annual_rate"]["value"] == pytest.approx(0.039)
        assert d["mortgage_term_months"]["value"] == 300
        assert d["hishtalmut_first_deposit_date"]["status"] == "resolved"
        assert d["hishtalmut_first_deposit_date"]["value"] == "2017-03-01"
    finally:
        s.close()
        eng.dispose()


def test_fire_bridge_is_present_and_uses_permanent_spend_basis(session):
    """The FIRE-bridge requirement must be DERIVED (codex residual: it was
    sized at the T12 burn, not the ₪311.6k permanent-equivalent). With no plan
    run the retirement age is unresolved, so the bridge is pending — never a
    fabricated figure — but the field + source locator must exist."""
    d = compute_derived_inputs(session, user_id="ariel", today=date(2026, 6, 4))
    assert "fire_bridge_requirement_nis" in d
    fld = d["fire_bridge_requirement_nis"]
    assert fld["status"] == "pending"  # no plan run → retirement age unresolved
    assert "permanent_annual_spend_nis" in fld["source"]


def test_mc_spend_basis_fields_present_with_source(session):
    """The MC solvency spend basis (central + stress) must be surfaced as
    DerivedFields so /retirement can show the SAME number the dual-track age
    runs on (the bridge to the headline permanent-equivalent spend). With no
    plan run the resolver can't supply them → pending, never fabricated, but
    the field + source locator must exist."""
    d = compute_derived_inputs(session, user_id="ariel", today=date(2026, 6, 4))
    for k in ("mc_central_spend_nis", "mc_stress_spend_nis"):
        assert k in d, f"{k} missing"
        assert d[k]["source"], f"{k} has no source locator"
        assert d[k]["status"] in ("resolved", "pending")


def test_every_field_carries_source(session):
    d = compute_derived_inputs(session, user_id="ariel", today=date(2026, 6, 4))
    for k, v in d.items():
        if k == "decision_run_id":
            continue
        assert v["source"], f"{k} has no source locator"
        assert v["status"] in ("resolved", "pending")


def test_retirement_age_prefers_canonical_dual_track(session, monkeypatch):
    """The user-facing retirement_age must be the CANONICAL dual-track
    earliest-safe age (retirement_plan.canonical_feasible_dual_track), NOT the
    stale withdrawal_sequencer fi_age. Monkeypatch the (heavy MC) canonical fn
    to a known age and assert it flows through value + source."""
    import argosy.services.retirement.retirement_plan as rp
    from argosy.services.retirement.scenario_mc import FeasibleAgeResult

    canonical_age = 46.0

    def _fake_canon(*, session, user_id, **kwargs):  # noqa: ARG001 — signature parity
        return FeasibleAgeResult(
            earliest_feasible_age=canonical_age,
            p_solvent_at_age=0.91,
            target_p_solvent=0.90,
            operational_target_age=49.0,
            statutory_lump_age=60,
            statutory_annuity_age=67,
            current_age=43.96,
            reserve_netted_nis=0.0,
            basis={"preservation_age": 53.0, "source": "retirement_plan.canonical_feasible_dual_track"},
        )

    monkeypatch.setattr(rp, "canonical_feasible_dual_track", _fake_canon)

    d = compute_derived_inputs(session, user_id="ariel", today=date(2026, 6, 4))
    fld = d["retirement_age"]
    assert fld["value"] == canonical_age
    assert fld["status"] == "resolved"
    assert "canonical_feasible_dual_track" in fld["source"]
    # And NOT the stale withdrawal_sequencer fi_age locator.
    assert "withdrawal_sequencer" not in fld["source"]


def test_retirement_age_falls_back_when_canonical_fails(session, monkeypatch):
    """Best-effort: if the canonical dual-track raises (thin data / no FI
    basis), retirement_age falls back to the resolved retirement.fi_age value
    (here pending, since there is no plan run) — never crashes, never the
    canonical source string."""
    import argosy.services.retirement.retirement_plan as rp

    def _boom(*, session, user_id, **kwargs):  # noqa: ARG001
        raise RuntimeError("no FI basis")

    monkeypatch.setattr(rp, "canonical_feasible_dual_track", _boom)

    d = compute_derived_inputs(session, user_id="ariel", today=date(2026, 6, 4))
    fld = d["retirement_age"]
    # No plan run in this fixture → fi_age is pending; the fallback path is taken.
    assert fld["status"] == "pending"
    assert "canonical_feasible_dual_track" not in fld["source"]
