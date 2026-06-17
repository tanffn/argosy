# tests/coherence/test_ledger.py
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from argosy.state.models import Base
from argosy.quality.coherence.ledger import record_ruling, load_active_rulings, supersede


def _s():
    eng = sa.create_engine("sqlite://"); Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_record_then_load_active():
    s = _s()
    record_ruling(s, user_id="ariel", decision_run_id=1, dispute_key="k1",
                  subject_type="rsu_vest_policy", question="q", ruling="sell->sgov",
                  rationale="deconcentration", basis="user_directive", resolved_by="resolver",
                  invariants=[{"kind": "forbidden_claim", "pattern": "retain"}],
                  conformed_surfaces=["short_md"])
    active = load_active_rulings(s, user_id="ariel")
    assert len(active) == 1 and active[0].dispute_key == "k1"


def test_supersede_keeps_old_for_audit_and_drops_from_active():
    s = _s()
    old = record_ruling(s, user_id="ariel", decision_run_id=1, dispute_key="k1",
                        subject_type="x", question="q", ruling="v1", rationale="r",
                        basis="b", resolved_by="resolver", invariants=[], conformed_surfaces=[])
    new = record_ruling(s, user_id="ariel", decision_run_id=2, dispute_key="k1",
                        subject_type="x", question="q", ruling="v2", rationale="r",
                        basis="b", resolved_by="arbitrator", invariants=[], conformed_surfaces=[])
    supersede(s, old_id=old.id, new_id=new.id)
    active = load_active_rulings(s, user_id="ariel")
    assert [r.ruling for r in active] == ["v2"]
    assert s.query(type(old)).count() == 2
