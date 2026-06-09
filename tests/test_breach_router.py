"""T4.5 — NVDA concentration-breach tranche routing to approval (G4)."""
from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services import breach_router as br
from argosy.services.breach_router import (
    DECON_TRANCHE_MARKER,
    BreachTranche,
    size_deconcentration_tranche,
)
from argosy.state.models import Base, Proposal, User


def test_size_tranche_spreads_over_quarters():
    assert size_deconcentration_tranche(total_over_cap_nis=4_000_000.0, n_quarters=8) == pytest.approx(500_000.0)
    # n_quarters clamps to >= 1; negative over-cap clamps to 0.
    assert size_deconcentration_tranche(total_over_cap_nis=1_000.0, n_quarters=0) == pytest.approx(1_000.0)
    assert size_deconcentration_tranche(total_over_cap_nis=-5.0, n_quarters=4) == 0.0


def _session(tmp_path, name="b.db"):
    eng = sa.create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng, expire_on_commit=False)()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    return s, eng


_TRANCHE = BreachTranche(
    nvda_current_pct=64.86, nvda_cap_pct=13.0, over_cap_pct=51.86,
    total_over_cap_nis=4_000_000.0, n_quarters=8, tranche_nis=500_000.0,
)


def test_route_creates_awaiting_human_proposal_with_lineage(tmp_path, monkeypatch):
    s, eng = _session(tmp_path)
    monkeypatch.setattr(br, "compute_breach_tranche", lambda *a, **k: _TRANCHE)

    class _PV:
        id = 30

    monkeypatch.setattr("argosy.state.queries.get_current_plan", lambda sess, u: _PV())
    pid = br.route_breach_tranche(s, "ariel")
    s.commit()
    assert pid is not None
    row = s.query(Proposal).one()
    assert row.ticker == "NVDA" and row.action == "sell"
    # routed to APPROVAL, never executed
    assert row.status == "awaiting_human"
    assert float(row.size_shares_or_currency) == pytest.approx(500_000.0)
    assert row.size_units == "currency"
    assert row.plan_version_id == 30  # T4.4 lineage
    assert DECON_TRANCHE_MARKER in row.rationale_summary
    s.close()
    eng.dispose()


def test_route_is_idempotent_while_one_open(tmp_path, monkeypatch):
    s, eng = _session(tmp_path)
    monkeypatch.setattr(br, "compute_breach_tranche", lambda *a, **k: _TRANCHE)
    monkeypatch.setattr("argosy.state.queries.get_current_plan", lambda sess, u: None)
    first = br.route_breach_tranche(s, "ariel")
    s.commit()
    second = br.route_breach_tranche(s, "ariel")
    s.commit()
    assert first is not None and second is None  # no duplicate while one is open
    assert s.query(Proposal).count() == 1
    s.close()
    eng.dispose()


def test_route_noop_when_not_breaching(tmp_path, monkeypatch):
    s, eng = _session(tmp_path)
    monkeypatch.setattr(br, "compute_breach_tranche", lambda *a, **k: None)
    assert br.route_breach_tranche(s, "ariel") is None
    assert s.query(Proposal).count() == 0
    s.close()
    eng.dispose()
