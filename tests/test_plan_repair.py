from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import argosy.services.plan_repair as repair_module
from argosy.agents.plan_synthesizer_types import (
    HorizonSection,
    PlanSynthesisOutput,
    Section,
    SectionEvidence,
    SynthesisInputs,
    SynthTarget,
)
from argosy.services.plan_repair import (
    ContradictionCandidate,
    HandlerReceipt,
    PlanRepairRefused,
    _apply_allocation_projection,
    _apply_bounded_qualifiers,
    _apply_fact_tokens,
    _apply_fi_qualifiers,
    repair_plan_version,
    resolve_contradiction,
)
from argosy.services.target_allocation_doc import (
    AllocationClassDoc,
    AllocationInstrument,
    TargetAllocationDoc,
)
from argosy.state.models import Base, PlanRepairAttempt, PlanVersion, User


class _Resolved:
    def __init__(self, **values):
        self.values = {
            key: SimpleNamespace(status="resolved", value=value, unit=unit)
            for key, (value, unit) in values.items()
        }

    def get(self, key):
        return self.values.get(
            key, SimpleNamespace(status="unresolved", value=None, unit="")
        )


def _horizon(name: str, **kwargs) -> HorizonSection:
    freshness = {"long": "annual", "medium": "quarterly", "short": "monthly"}[name]
    return HorizonSection(
        horizon=name,
        freshness_expected=freshness,
        status="minor_revision",
        posture=kwargs.pop("posture", "Plan posture."),
        **kwargs,
    )


def _output(*, long=None, medium=None, short=None, sections=None) -> PlanSynthesisOutput:
    return PlanSynthesisOutput(
        long=long or _horizon("long"),
        medium=medium or _horizon("medium"),
        short=short or _horizon("short"),
        inputs=SynthesisInputs(decision_run_id=1),
        sections=sections or [],
    )


def test_fact_handler_tokenizes_matching_literal_and_repairs_anchored_drift():
    output = _output(long=_horizon(
        "long",
        posture=(
            "NVDA has a 13.0% binding ceiling. "
            "The NVDA hard cap was also typed as 12.0%."
        ),
    ))
    resolved = _Resolved(**{"concentration.nvda_cap_pct": (0.13, "pct")})
    receipt = _apply_fact_tokens(output, resolved)
    token = "{{fact:concentration.nvda_cap_pct}}"
    assert output.long.posture.count(token) == 2
    assert "12.0%" not in output.long.posture
    assert receipt.applied_count == 2


def test_fi_handler_reuses_same_sentence_shock_qualifier(monkeypatch):
    output = _output(long=_horizon(
        "long",
        posture=(
            "Before tax the margin is {{fact:retirement.fi_margin_signed_nis}} — "
            "financial independence is reached on the liquid basis."
        ),
    ))
    monkeypatch.setattr(
        "argosy.services.plan_repair._shock_requirements", lambda _resolved: (False, True)
    )
    receipt = _apply_fi_qualifiers(output, _Resolved())
    assert "current FX / currency mark" in output.long.posture
    assert receipt.applied_count == 1


def _allocation_doc() -> TargetAllocationDoc:
    instrument = AllocationInstrument(
        symbol="CSPX", role="primary", weight_within_class_pct=100.0, domicile="IE"
    )
    return TargetAllocationDoc(
        anchor_sigma=1.0,
        blended_sigma=1.0,
        nvda_cap_pct=13.0,
        fi_pct=80.0,
        provenance="test",
        classes=[
            AllocationClassDoc(
                label="Growth", snapshot_category="Core Equity", sigma_class="equity",
                target_pct=90.0, instruments=[instrument], rationale="Canonical growth.",
            ),
            AllocationClassDoc(
                label="Cash & T-bills", snapshot_category="Cash", sigma_class="cash",
                target_pct=10.0, instruments=[instrument], rationale="Canonical cash.",
            ),
        ],
        glide=[],
    )


def test_allocation_handler_rebuilds_full_medium_projection_and_ips_table():
    template = SynthTarget(
        label="Cash & T-bills", value=17.1, unit="pct_of_portfolio",
        stated_at=date(2026, 8, 25), revisit_after=date(2026, 11, 25),
    )
    section = Section(
        section_id="ips", horizon="medium", title="IPS",
        body_md="Cash and Treasury bills are 17.1% of the portfolio.",
        evidence=SectionEvidence(missing_data=["test evidence pending"]),
    )
    output = _output(medium=_horizon("medium", targets=[template]), sections=[section])
    pv = PlanVersion(user_id="ariel", target_allocation_json=_allocation_doc().model_dump_json())
    receipt = _apply_allocation_projection(output, pv)
    targets = [t for t in output.medium.targets if t.unit == "pct_of_portfolio"]
    assert [(t.label, t.value) for t in targets] == [("Growth", 90.0), ("Cash & T-bills", 10.0)]
    assert sum(t.value for t in targets) == 100.0
    assert "Cash and Treasury bills are 10%" in output.sections[0].body_md
    assert "| Growth | 90% |" in output.sections[0].body_md
    assert receipt.applied_count >= 2


def test_contradiction_policy_token_raw_and_both_raw():
    one = resolve_contradiction(ContradictionCandidate(
        winner_text="{{fact:x}}", loser_text="12", winner_kind="token",
        loser_kind="raw", canonical_fact_key="x",
    ))
    assert one.replacements == (("12", "{{fact:x}}"),)
    both = resolve_contradiction(ContradictionCandidate(
        winner_text="13", loser_text="12", winner_kind="raw", loser_kind="raw",
        canonical_fact_key="x",
    ))
    assert both.replacements == (("13", "{{fact:x}}"), ("12", "{{fact:x}}"))


def test_contradiction_policy_labels_different_timestamps():
    result = resolve_contradiction(ContradictionCandidate(
        winner_text="$214.72", loser_text="$210.74", winner_kind="raw", loser_kind="raw",
        canonical_fact_key=None, same_timestamp=False,
        winner_label="holdings mark as of 2026-08-23",
        loser_label="technical close as of 2026-08-24",
    ))
    assert result.refusal is None
    assert result.replacements[0][1].startswith("holdings mark as of")


def test_contradiction_policy_refuses_without_canonical_owner():
    result = resolve_contradiction(ContradictionCandidate(
        winner_text="8,918", loser_text="3,378", winner_kind="raw", loser_kind="raw",
        canonical_fact_key=None,
    ))
    assert result.replacements == ()
    assert result.refusal == "no single canonical owner"


def test_bounded_handlers_patch_only_two_settled_qualifiers():
    age = "The spending these ages are tested against is the permanent basis ₪300,000, in today's money."
    principal = "The mandate is to preserve capital and never spend the principal: the portfolio must live on its returns."
    output = _output(long=_horizon("long", posture=age, rationale=principal))
    reviews = {"whole_artifact_reader": {"findings": [
        {
            "detail": "The retirement-age headline rests on 270,000 while permanent is 300,000.",
            "surfaces_cited": [age],
        },
        {
            "detail": "The plan says never spend the principal but uses a liquid drawdown.",
            "surfaces_cited": [principal],
        },
    ]}}
    resolved = _Resolved(
        **{
            "spend.fi_basis_nis": (300_000, "nis"),
            "spend.mc_central_nis": (270_000, "nis"),
        }
    )
    receipt, refusals = _apply_bounded_qualifiers(output, reviews, resolved)
    assert refusals == []
    assert receipt.applied_count == 2
    assert "{{fact:spend.mc_central_nis}}" in output.long.posture
    assert "temporary liquid-capital drawdown" in output.long.rationale


def test_current_plan_requires_explicit_authorization_flag():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="ariel", plan="free"))
        plan = PlanVersion(user_id="ariel", role="current", raw_markdown="")
        session.add(plan)
        session.commit()
        with pytest.raises(PlanRepairRefused, match="allow_current=True"):
            repair_plan_version(
                session, plan_version_id=plan.id, trigger="manual"
            )


def test_scoped_render_preserves_existing_long_appendices():
    marker = repair_module._LONG_APPENDIX_MARKER
    pv = PlanVersion(
        user_id="ariel",
        horizon_long_md=f"old narrative{marker}\nold appendix bytes",
    )
    rendered = {
        "horizon_long_json": "new-json",
        "horizon_long_md": f"new narrative{marker}\nregenerated appendix bytes",
        "horizon_long_md_audit": "new-audit",
        "horizon_medium_json": "unrelated-medium-json",
        "horizon_medium_md": "unrelated-medium-md",
        "horizon_medium_md_audit": "unrelated-medium-audit",
        "sections_json": "new-sections",
    }

    scoped = repair_module._scoped_rendered_fields(
        pv, rendered, ["long.posture"]
    )

    assert scoped == {
        "horizon_long_json": "new-json",
        "horizon_long_md": f"new narrative{marker}\nold appendix bytes",
        "horizon_long_md_audit": "new-audit",
    }


def test_positive_handler_gate_delta_restores_exact_persisted_bytes(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    baseline_output = _output(long=_horizon("long", posture="exact base bytes"))
    with Session(engine) as session:
        session.add(User(id="ariel", plan="free"))
        plan = PlanVersion(
            user_id="ariel",
            role="draft",
            raw_markdown="",
            decision_run_id=1,
            synthesis_inputs_json=baseline_output.inputs.model_dump_json(),
            horizon_long_json=baseline_output.long.model_dump_json(),
            horizon_medium_json=baseline_output.medium.model_dump_json(),
            horizon_short_json=baseline_output.short.model_dump_json(),
            horizon_long_md="exact base bytes",
            horizon_medium_md="medium base",
            horizon_short_md="short base",
            horizon_long_md_audit="long audit base",
            horizon_medium_md_audit="medium audit base",
            horizon_short_md_audit="short audit base",
            sections_json="[]",
            target_allocation_json="{}",
        )
        session.add(plan)
        session.commit()

        monkeypatch.setattr(repair_module, "_assert_settled_policy", lambda _r: None)
        monkeypatch.setattr(repair_module, "_latest_reviews", lambda _s, _p: {})
        monkeypatch.setattr(
            "argosy.services.plan_numeric_resolver.resolve_plan_numbers",
            lambda *_args, **_kwargs: _Resolved(),
        )
        monkeypatch.setattr(
            repair_module,
            "_assembled_text",
            lambda _s, pv: "\n".join((
                pv.horizon_long_md or "",
                pv.horizon_medium_md or "",
                pv.horizon_short_md or "",
            )),
        )
        monkeypatch.setattr(
            repair_module,
            "_gate_counts",
            lambda _s, pv: {"synthetic": 3 if "regression" in (pv.horizon_long_md or "") else 2},
        )

        def bad_handler(output, _resolved):
            output.long.posture = "candidate regression"
            return HandlerReceipt(
                handler="bad_handler",
                changed_surfaces=["long.posture"],
                applied_count=1,
            )

        def no_change(name):
            return lambda *_args, **_kwargs: HandlerReceipt(handler=name)

        monkeypatch.setattr(repair_module, "_apply_fact_tokens", bad_handler)
        monkeypatch.setattr(repair_module, "_apply_fi_qualifiers", no_change("fi"))
        monkeypatch.setattr(repair_module, "_apply_allocation_projection", no_change("allocation"))
        monkeypatch.setattr(
            repair_module,
            "_apply_contradictions",
            lambda *_args, **_kwargs: (HandlerReceipt(handler="contradictions"), []),
        )
        monkeypatch.setattr(
            repair_module,
            "_apply_bounded_qualifiers",
            lambda *_args, **_kwargs: (HandlerReceipt(handler="bounded"), []),
        )

        def render(_session, pv, output):
            return {
                "horizon_long_json": output.long.model_dump_json(),
                "horizon_medium_json": output.medium.model_dump_json(),
                "horizon_short_json": output.short.model_dump_json(),
                "horizon_long_md": output.long.posture,
                "horizon_medium_md": pv.horizon_medium_md,
                "horizon_short_md": pv.horizon_short_md,
                "horizon_long_md_audit": "candidate audit",
                "horizon_medium_md_audit": pv.horizon_medium_md_audit,
                "horizon_short_md_audit": pv.horizon_short_md_audit,
                "sections_json": pv.sections_json,
                "target_allocation_json": pv.target_allocation_json,
            }

        monkeypatch.setattr(repair_module, "_render_output", render)

        result = repair_plan_version(
            session,
            plan_version_id=plan.id,
            trigger="manual",
            include_bounded=True,
        )

        receipt = result.handlers[0]
        assert (receipt.gate_before, receipt.gate_after, receipt.gate_delta) == (2, 3, 1)
        assert receipt.reverted is True
        assert plan.horizon_long_md == "exact base bytes"
        assert plan.horizon_long_md_audit == "long audit base"
        assert result.deterministic_before == result.deterministic_after == {"synthetic": 2}
        assert result.reviewer_findings_addressed == []
        attempt = session.query(PlanRepairAttempt).one()
        assert json.loads(attempt.affected_fields_json) == []
