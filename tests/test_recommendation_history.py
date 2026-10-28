from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from argosy.services.recommendation_history import compare_window, inventory, summarize
from argosy.state.models import Base, Prediction, Proposal, User, Verdict


def test_windows_never_use_same_day_or_future_close():
    subject = {date(2026, 1, 5): 1, date(2026, 1, 6): 100, date(2026, 2, 4): 120,
               date(2026, 2, 5): 900}
    spy = {date(2026, 1, 6): 500, date(2026, 2, 4): 550}
    result = compare_window(event_at="2026-01-05T23:00:00+00:00", days=30,
                            subject=subject, benchmark=spy, as_of=date(2026, 2, 5))
    assert result["entry_date"] == "2026-01-06"
    assert result["exit_date"] == "2026-02-04"
    assert round(result["subject_return_pct"], 5) == 20
    assert round(result["sell_to_spy_vs_hold_pp"], 5) == -10
    assert result["after_tax_return_pct"] is None


def test_future_and_missing_are_not_zero():
    assert compare_window(event_at="2026-01-05", days=180, subject={}, benchmark={},
                          as_of=date(2026, 2, 5))["status"] == "not_due"
    result = compare_window(event_at="2026-01-05", days=30, subject={}, benchmark={},
                            as_of=date(2026, 2, 5))
    assert result["status"] == "missing_price_data"
    assert result["subject_return_pct"] is None


def test_summary_keeps_denominator_but_does_not_count_repeated_calls():
    rows = [{"channel": "shadow", "policy_version": "unversioned", "action": "BUY",
             "first_call_in_cohort": first, "outcomes": [{"horizon_days": 30, "status": status,
              "subject_return_pct": value, "benchmark_return_pct": 2, "buy_vs_spy_pp": 3}]}
            for first, status, value in [(True, "available", 5), (True, "missing_price_data", None),
                                         (False, "available", 99)]]
    result = summarize(rows)[0]
    assert result["first_calls"] == 2
    assert result["available"] == 1
    assert result["mean_underlying_return_pct"] == 5


def test_inventory_preserves_rejected_shadow_and_manual_and_lineage():
    import json
    from datetime import datetime
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="owner"))
        db.add(User(id="other"))
        db.flush()
        for ident, owner, shadow, source in [(1, "owner", True, "decision_funnel"),
                                            (2, "owner", False, "manual"), (3, "other", False, "manual")]:
            db.add(Proposal(id=ident, user_id=owner, ticker="XYZ", action="buy", instrument="stock",
                            size_shares_or_currency=1, size_units="shares", tier="T1", account_class="limited",
                            shadow=shadow, source=source, status="rejected", decision_run_id=None))
        db.add(Verdict(user_id="owner", subject="XYZ", verdict="BUY", conviction="LOW",
                       reasoning_md="Thesis", source_decision_run_id=None))
        for days in (30, 180, 365):
            db.add(Prediction(user_id="owner", source="signal_stream:discovery_evaluation",
                              source_ref=json.dumps({"verdict": "WATCH"}), ticker="XYZ", direction="long",
                              timeframe_days=days, event_at=datetime(2026, 1, 1),
                              evaluation_due_at=datetime(2027, 1, 1),
                              message_id=f"clock|event|{days}d", evaluation_method="fixed_lookahead_30d"))
        db.flush()
        result = inventory(db, user_id="owner")
        assert result["coverage"]["call_events"] == 4
        assert result["coverage"]["by_channel"] == {"shadow": 1, "manual": 1, "verdict_only": 1, "discovery_research": 1}
        clock = next(r for r in result["calls"] if r["channel"] == "discovery_research")
        assert len(clock["lineage"]) == 3
        assert clock["policy_version"] == "unversioned"
    engine.dispose()


def test_midnight_utc_uses_the_decision_exchange_date():
    result = compare_window(event_at="2026-01-06T01:00:00+00:00", days=30,
                            subject={date(2026, 1, 6): 100, date(2026, 2, 4): 110},
                            benchmark={date(2026, 1, 6): 100, date(2026, 2, 4): 100},
                            as_of=date(2026, 2, 10))
    assert result["entry_date"] == "2026-01-06"
    assert result["target_date"] == "2026-02-04"


def test_clock_identity_preserves_distinct_policy_and_orphan_records():
    import json
    from datetime import datetime
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="owner"))
        db.flush()
        for version in ("v1", "v2"):
            for days in (30, 180, 365):
                db.add(Prediction(user_id="owner", source="signal_stream:discovery_evaluation",
                                  source_ref=json.dumps({"verdict": "BUY", "mandate_version": version,
                                                         "research_context": {"final_report_id": version}}),
                                  ticker="XYZ", direction="long", timeframe_days=days,
                                  event_at=datetime(2026, 1, 1), evaluation_due_at=datetime(2027, 1, 1),
                                  message_id=f"{version}|event|{days}d", evaluation_method="fixed_lookahead_30d"))
        db.add(Prediction(user_id="owner", source="signal_stream:deep_decision_verdict",
                          source_ref=json.dumps({"verdict_id": 999, "verdict": "BUY"}),
                          ticker="MISSING", direction="long", timeframe_days=30,
                          event_at=datetime(2026, 1, 1), evaluation_due_at=datetime(2027, 1, 1),
                          message_id="orphan|30d", evaluation_method="fixed_lookahead_30d"))
        db.flush()
        calls = inventory(db, user_id="owner")["calls"]
        assert len(calls) == 3
        assert {r["policy_version"] for r in calls if r["ticker"] == "XYZ"} == {"v1", "v2"}
        assert next(r for r in calls if r["ticker"] == "MISSING")["channel"] == "unlinked_clock"
    engine.dispose()


def test_grade_parser_prefers_validated_blob_and_accepts_legacy_fences():
    import json
    from types import SimpleNamespace

    from argosy.services.recommendation_history import _grade_output
    output = {"ticker": "XYZ", "verdict": "BUY", "conviction": "MED", "thesis_md": "Thesis", "cites": []}
    report = SimpleNamespace(user_id="test", blobs=[], response_text="```json\n" + json.dumps(output) + "\n``` trailing")
    assert _grade_output(report)["verdict"] == "BUY"
    report.blobs = [SimpleNamespace(key="output_json", value=json.dumps({**output, "verdict": "PASS"}))]
    assert _grade_output(report)["verdict"] == "PASS"


def test_all_production_tenant_routing_spellings_are_rejected(monkeypatch):
    import pytest

    from argosy.services.recommendation_history import analysis_database_path
    for mode in ("tenant", "TENANT", "per-tenant", "Per-Tenant"):
        monkeypatch.setenv("ARGOSY_TENANCY", mode)
        with pytest.raises(ValueError, match="per-tenant"):
            analysis_database_path()
