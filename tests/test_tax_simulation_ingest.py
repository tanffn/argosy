import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.tax_simulation_ingest import eligible_shares, ingest_report
from argosy.services.tax_simulation_parser import TaxSimLot, TaxSimReport
from argosy.state.models import TaxSimulationLot


def _db():
    eng = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    TaxSimulationLot.__table__.create(eng)
    return sessionmaker(bind=eng)()


def _report(sim_date):
    return TaxSimReport(simulation_date=sim_date, lots=[
        TaxSimLot(plan_type="RSU", shares=100, holding_period="OK", eligible=True, grant_id="213000"),
        TaxSimLot(plan_type="RSU", shares=30, holding_period="Breaking", eligible=False, grant_id="331375"),
        TaxSimLot(plan_type="ESPP", shares=20, holding_period="OK", eligible=True),
    ])


def test_ingest_and_eligibility():
    s = _db()
    res = ingest_report(s, user_id="ariel", report=_report("18/06/2026"))
    assert res["lots"] == 3 and res["eligible_shares"] == 120 and res["breaking_shares"] == 30
    assert eligible_shares(s, "ariel") == 120
    assert eligible_shares(s, "ariel", eligible=False) == 30
    assert eligible_shares(s, "ariel", plan_type="RSU") == 100


def test_reingest_same_date_is_idempotent():
    s = _db()
    ingest_report(s, user_id="ariel", report=_report("18/06/2026"))
    ingest_report(s, user_id="ariel", report=_report("18/06/2026"))  # re-ingest
    assert s.execute(sa.select(sa.func.count()).select_from(TaxSimulationLot)).scalar() == 3


def test_latest_report_wins():
    s = _db()
    ingest_report(s, user_id="ariel", report=_report("01/01/2026"))
    rep2 = TaxSimReport(simulation_date="18/06/2026", lots=[
        TaxSimLot(plan_type="RSU", shares=500, holding_period="OK", eligible=True, grant_id="213000")])
    ingest_report(s, user_id="ariel", report=rep2)
    assert eligible_shares(s, "ariel") == 500  # latest ingested report
