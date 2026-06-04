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


def test_every_field_carries_source(session):
    d = compute_derived_inputs(session, user_id="ariel", today=date(2026, 6, 4))
    for k, v in d.items():
        if k == "decision_run_id":
            continue
        assert v["source"], f"{k} has no source locator"
        assert v["status"] in ("resolved", "pending")
