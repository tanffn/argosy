"""Tests for the plan_synthesis phase-1 input assembler."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from argosy.state.models import Base, User, PlanVersion


def _make_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    return Session()


def test_phase1_inputs_dataclass_has_all_required_fields():
    """Every kwarg required by a phase-1 analyst's build_prompt must be a
    field on Phase1Inputs."""
    from argosy.orchestrator.flows.plan_synthesis.inputs import Phase1Inputs

    required_fields = {
        "positions_summary", "plan_targets",
        "fx_payload",
        "tickers", "fundamentals_payload",
        "news_payload",
        "macro_snapshot",
        "social_payload",
        "lots_summary", "dividends_summary", "rsu_schedule_summary", "domain_kb_files",
        "indicators_payload",
        "plan_label", "plan_markdown", "snapshot_label", "snapshot_summary",
        "user_context_yaml", "recent_events",
    }
    actual_fields = set(Phase1Inputs.__dataclass_fields__.keys())
    missing = required_fields - actual_fields
    assert not missing, f"Phase1Inputs missing required fields: {missing}"


def test_assemble_returns_empty_defaults_when_session_is_empty(tmp_path, monkeypatch):
    """An empty-session + no-adapters world returns Phase1Inputs populated
    entirely with empty defaults. No raises."""
    from argosy.orchestrator.flows.plan_synthesis.inputs import (
        Phase1Inputs,
        assemble_phase1_inputs,
    )

    # Isolate ARGOSY_HOME so the TSV walker sees no TSVs and the adapters
    # have no config to find.
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    try:
        from argosy.config import reload_settings

        reload_settings()
    except Exception:
        pass

    session = _make_session()
    session.add(User(id="ariel", plan="free"))
    session.commit()

    inputs = assemble_phase1_inputs(
        session,
        user_id="ariel",
        baseline=None,
        prior_current=None,
        decision_audit_token="plan-synth-42",
    )
    assert isinstance(inputs, Phase1Inputs)
    assert inputs.snapshot_label == "plan-synth-42"
    assert inputs.tickers == []
    assert inputs.fx_payload == {}
    assert inputs.fundamentals_payload == {}
    assert inputs.news_payload == {}
    assert inputs.macro_snapshot == {}
    assert inputs.social_payload == {}
    assert inputs.indicators_payload == {}
    assert inputs.lots_summary == ""
    assert inputs.dividends_summary == ""
    assert inputs.rsu_schedule_summary == ""


def test_assemble_uses_baseline_plan_label_and_markdown_when_present(
    tmp_path, monkeypatch,
):
    """When a baseline PlanVersion exists, its label + markdown flow into
    plan_label + plan_markdown."""
    from argosy.orchestrator.flows.plan_synthesis.inputs import (
        assemble_phase1_inputs,
    )

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    try:
        from argosy.config import reload_settings

        reload_settings()
    except Exception:
        pass

    session = _make_session()
    session.add(User(id="ariel", plan="free"))
    baseline = PlanVersion(
        user_id="ariel", role="baseline",
        version_label="my-baseline-v1",
        raw_markdown="# My Plan\n\nSome content.",
    )
    session.add(baseline)
    session.commit()
    session.refresh(baseline)

    inputs = assemble_phase1_inputs(
        session, user_id="ariel",
        baseline=baseline, prior_current=None,
        decision_audit_token="plan-synth-1",
    )
    assert inputs.plan_label == "my-baseline-v1"
    assert "My Plan" in inputs.plan_markdown


def test_assemble_never_raises_on_adapter_failure(tmp_path, monkeypatch):
    """The assembler must never raise from adapter failures — it must
    swallow + log + degrade to empty."""
    from argosy.orchestrator.flows.plan_synthesis import inputs as inputs_mod
    from argosy.orchestrator.flows.plan_synthesis.inputs import (
        Phase1Inputs,
        assemble_phase1_inputs,
    )

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    try:
        from argosy.config import reload_settings

        reload_settings()
    except Exception:
        pass

    # Force every adapter section to blow up loudly. If any of these
    # exceptions escape the assembler, the test fails.
    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic adapter failure")

    monkeypatch.setattr(inputs_mod, "_gather_news", _boom)
    monkeypatch.setattr(inputs_mod, "_gather_macro_snapshot", _boom)
    monkeypatch.setattr(inputs_mod, "_gather_fx_payload", _boom)
    monkeypatch.setattr(inputs_mod, "_gather_social_payload", _boom)
    monkeypatch.setattr(inputs_mod, "_find_latest_tsv", _boom)

    session = _make_session()
    session.add(User(id="ariel", plan="free"))
    session.commit()

    inputs = assemble_phase1_inputs(
        session, user_id="ariel",
        baseline=None, prior_current=None,
        decision_audit_token="plan-synth-99",
    )
    assert isinstance(inputs, Phase1Inputs)
    # All adapter-derived fields degrade to their empty default when
    # the section helpers raise.
    assert inputs.news_payload == {}
    assert inputs.macro_snapshot == {}
    assert inputs.fx_payload == {}
    assert inputs.social_payload == {}
    assert inputs.tickers == []
