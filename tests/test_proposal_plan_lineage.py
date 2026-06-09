"""T4.4 — proposals carry plan_version_id (audit lineage to the canonical plan)."""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.state.models import Base, Proposal, User


def test_proposal_persists_and_reads_back_plan_version_id(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'p.db'}")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng, expire_on_commit=False)
    with S() as s:
        s.add(User(id="ariel", plan="free"))
        s.commit()
        s.add(Proposal(
            user_id="ariel", ticker="NVDA", action="sell", tier="T2",
            plan_version_id=30,
        ))
        s.commit()
    with S() as s:
        row = s.query(Proposal).filter_by(user_id="ariel").one()
        assert row.plan_version_id == 30
    eng.dispose()


def test_plan_version_id_is_nullable(tmp_path):
    # Existing rows / no-current-plan proposals leave it NULL (never fabricated).
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'p2.db'}")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng, expire_on_commit=False)
    with S() as s:
        s.add(User(id="ariel", plan="free"))
        s.commit()
        s.add(Proposal(user_id="ariel", ticker="VOO", action="buy", tier="T2"))
        s.commit()
    with S() as s:
        row = s.query(Proposal).filter_by(user_id="ariel").one()
        assert row.plan_version_id is None
    eng.dispose()
