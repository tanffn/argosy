# tests/coherence/test_ledger_model.py
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from argosy.state.models import Base, CoherenceDecision


def _mem_session():
    eng = sa.create_engine("sqlite://")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_coherence_decision_persists_with_json_columns():
    s = _mem_session()
    row = CoherenceDecision(
        user_id="ariel", decision_run_id=109, dispute_key="abc123",
        subject_type="retirement_age_headline", question="which age leads?",
        ruling="age 46 leads; 54 strict track", rationale="prime directive",
        basis="prime_directive", resolved_by="arbitrator",
        coherence_invariant_json='[{"kind":"required_framing_role"}]',
        conformed_surfaces_json='["long_md","medium_md"]',
    )
    s.add(row); s.commit()
    got = s.query(CoherenceDecision).filter_by(dispute_key="abc123").one()
    assert got.resolved_by == "arbitrator"
    assert got.superseded_by_id is None
    assert got.created_at is not None
