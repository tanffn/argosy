"""Sliced full synthesis — assembly units + fan-out/salvage.

Design: docs/design/sliced_full_synthesis.md §5:
* Assembly: adversarial stub slice that mutates every locked field →
  assembled output byte-matches skeleton locks; roster entry omitted by a
  slice → loud failure; invented sections/deltas dropped; pydantic
  round-trip.
* Fan-out/salvage: one slice raising transiently → siblings complete,
  slice retried, sub-checkpoint rows written per completed slice; slice
  dead after retries → phase fails; resume re-runs only the dead slice;
  skeleton-hash mismatch invalidates all slice checkpoints.

All agent paths are stubbed (no live LLM call) — same discipline as
tests/test_corrective_patch_flow.py.
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.agents.plan_skeleton_synthesizer import (
    PlanSkeleton,
    SkeletonAction,
    SkeletonDelta,
    SkeletonHorizon,
    SkeletonSectionEntry,
    SkeletonTheme,
)
from argosy.agents.plan_synthesizer_types import (
    Action,
    Delta,
    HorizonSection,
    Section,
    SectionEvidence,
    SpeculativeCandidate,
    SynthTarget,
    Theme,
)
from argosy.orchestrator.flows.plan_synthesis.sliced_phase3 import (
    SlicedAssemblyError,
    SliceExpansionError,
    _assemble_sliced_output,
)
from argosy.state.models import DecisionRun, PlanVersion, User

_FRESH = {"long": "annual", "medium": "quarterly", "short": "monthly"}


def _target(label, value, unit="pct_of_portfolio", rationale=""):
    return SynthTarget(
        label=label, value=value, unit=unit, rationale=rationale,
        stated_at=date(2026, 7, 8), revisit_after=date(2027, 7, 8),
    )


def _cand(ticker="MOON", pct=0.001):
    return SpeculativeCandidate(
        ticker=ticker, thesis_summary="skeleton thesis",
        suggested_position_usd=5000,
        suggested_position_pct_of_net_worth=pct, risk_ceiling_check=True,
        horizon_days=45, expected_drawdown_pct=60, exit_trigger="thesis break",
    )


def _skeleton() -> PlanSkeleton:
    return PlanSkeleton(
        long=SkeletonHorizon(
            horizon="long", freshness_expected="annual", status="no_change",
            posture_summary="long stance.",
        ),
        medium=SkeletonHorizon(
            horizon="medium", freshness_expected="quarterly",
            status="minor_revision", posture_summary="medium stance.",
            targets=[_target("NVDA target weight", 8.0)],
            theme_roster=[SkeletonTheme(
                label="Deconcentration", direction="lean_into",
            )],
            action_roster=[SkeletonAction(
                label="Quarterly NVDA trim", horizon_kind="parameterized",
                trigger_or_date="waypoint due",
            )],
        ),
        short=SkeletonHorizon(
            horizon="short", freshness_expected="monthly",
            status="major_revision", posture_summary="short stance.",
            speculative_candidates=[_cand()],
        ),
        delta_roster=[SkeletonDelta(
            item_kind="target", item_id="medium.targets.nvda",
            horizon="medium", change_kind="modified",
            summary="NVDA target moved",
        )],
        section_roster=[
            SkeletonSectionEntry(
                section_id="ips", horizon="medium",
                one_line_thesis="policy statement",
                key_facts=["NVDA target weight 8.0%"],
            ),
            SkeletonSectionEntry(
                section_id="concentration", horizon="short",
                one_line_thesis="trim status",
            ),
        ],
    )


def _faithful_horizon(sk: PlanSkeleton, name: str) -> HorizonSection:
    """A well-behaved expansion: locked fields reproduced, prose added."""
    hz = getattr(sk, name)
    return HorizonSection(
        horizon=hz.horizon, freshness_expected=hz.freshness_expected,
        status=hz.status, posture=f"{name} posture essay",
        rationale=f"{name} rationale essay",
        cited_sources=["agent_report:1"],
        targets=[
            t.model_copy(update={"rationale": "expanded target rationale"})
            for t in hz.targets
        ],
        themes=[Theme(label=t.label, direction=t.direction,
                      rationale="theme rationale",
                      cited_sources=["agent_report:2"])
                for t in hz.theme_roster],
        actions=[Action(label=a.label, horizon_kind=a.horizon_kind,
                        trigger_or_date=a.trigger_or_date,
                        detail="do the thing", rationale="because",
                        how_to="open /proposals", done_when="done bar",
                        cited_sources=["agent_report:3"])
                 for a in hz.action_roster],
        speculative_candidates=[
            c.model_copy(update={"thesis_summary": "expanded thesis",
                                 "exit_trigger": "expanded exit",
                                 "sourced_from": ["agent_report:4"]})
            for c in hz.speculative_candidates
        ],
        deltas_from_prior=[
            Delta(item_kind=d.item_kind, item_id=d.item_id,
                  horizon=d.horizon, change_kind=d.change_kind,
                  summary=d.summary, rationale="delta rationale",
                  prior={"value": 12.0}, proposed={"value": 8.0},
                  cited_sources=["agent_report:5"])
            for d in sk.delta_roster if d.horizon == name
        ],
    )


def _section(section_id, horizon, body="body text"):
    return Section(
        section_id=section_id, horizon=horizon, title=f"{section_id} title",
        body_md=body, evidence=SectionEvidence(missing_data=["stubbed"]),
    )


def _faithful_sections(sk: PlanSkeleton) -> dict[str, list[Section]]:
    out: dict[str, list[Section]] = {}
    for e in sk.section_roster:
        out.setdefault(e.horizon, []).append(
            _section(e.section_id, e.horizon)
        )
    return out


def _horizon_outputs(sk):
    return {h: _faithful_horizon(sk, h) for h in ("long", "medium", "short")}


# ----------------------------------------------------------------------
# Assembly units
# ----------------------------------------------------------------------


def _lock_view(output_medium: HorizonSection) -> dict:
    return {
        "status": output_medium.status,
        "freshness": output_medium.freshness_expected,
        "targets": [
            t.model_dump(exclude={"rationale"}) for t in output_medium.targets
        ],
        "themes": [(t.label, t.direction) for t in output_medium.themes],
        "actions": [
            (a.label, a.horizon_kind, a.trigger_or_date)
            for a in output_medium.actions
        ],
        "deltas": [
            (d.item_kind, d.item_id, d.horizon, d.change_kind, d.summary)
            for d in output_medium.deltas_from_prior
        ],
    }


def test_faithful_assembly_round_trips():
    sk = _skeleton()
    out, restored = _assemble_sliced_output(
        skeleton=sk, horizon_outputs=_horizon_outputs(sk),
        section_outputs=_faithful_sections(sk),
    )
    assert restored == 0
    assert out.medium.posture == "medium posture essay"
    assert out.medium.targets[0].rationale == "expanded target rationale"
    assert out.medium.targets[0].value == 8.0
    assert [s.section_id for s in out.sections] == ["ips", "concentration"]
    # Whole-artifact pydantic round-trip already ran inside; re-verify.
    from argosy.agents.plan_synthesizer_types import PlanSynthesisOutput
    PlanSynthesisOutput.model_validate(json.loads(out.model_dump_json()))


def test_adversarial_slice_cannot_perturb_locks():
    """A slice that mutates EVERY locked field yields byte-identical locks
    post-assembly (design §5 adversarial-stub test)."""
    sk = _skeleton()
    ho = _horizon_outputs(sk)
    evil = ho["medium"].model_copy(update={
        "status": "major_revision",              # locked: minor_revision
        "freshness_expected": "annual",           # locked: quarterly
        "targets": [_target("Nvidia strategic weight", 15.0, unit="pct",
                            rationale="evil rationale")],
        "themes": [Theme(label="Re-concentrate", direction="lean_away_from",
                         rationale="evil theme")],
        "actions": [Action(label="Buy more NVDA", horizon_kind="dated",
                           trigger_or_date="tomorrow", detail="evil")],
        "deltas_from_prior": [Delta(
            item_kind="action", item_id="medium.actions.buy_more",
            horizon="medium", change_kind="added", summary="evil delta",
            rationale="evil delta rationale",
        )],
    })
    ho["medium"] = evil
    out, restored = _assemble_sliced_output(
        skeleton=sk, horizon_outputs=ho,
        section_outputs=_faithful_sections(sk),
    )
    faithful, _ = _assemble_sliced_output(
        skeleton=sk, horizon_outputs=_horizon_outputs(sk),
        section_outputs=_faithful_sections(sk),
    )
    assert _lock_view(out.medium) == _lock_view(faithful.medium)
    assert restored > 0
    # The evil slice's PROSE does attach (positional pairing) — locks don't.
    assert out.medium.targets[0].rationale == "evil rationale"
    assert out.medium.targets[0].value == 8.0
    assert out.medium.targets[0].label == "NVDA target weight"
    assert out.medium.deltas_from_prior[0].item_id == "medium.targets.nvda"
    assert out.medium.deltas_from_prior[0].summary == "NVDA target moved"


def test_adversarial_candidate_numbers_restored():
    sk = _skeleton()
    ho = _horizon_outputs(sk)
    ho["short"] = ho["short"].model_copy(update={
        "speculative_candidates": [_cand(ticker="MOON", pct=0.9).model_copy(
            update={"suggested_position_usd": 999_999.0,
                    "horizon_days": 2,
                    "thesis_summary": "expanded thesis"},
        )],
    })
    out, _ = _assemble_sliced_output(
        skeleton=sk, horizon_outputs=ho,
        section_outputs=_faithful_sections(sk),
    )
    c = out.short.speculative_candidates[0]
    assert c.suggested_position_usd == 5000
    assert c.suggested_position_pct_of_net_worth == 0.001
    assert c.horizon_days == 45
    assert c.thesis_summary == "expanded thesis"


def test_omitted_roster_entry_fails_loud():
    sk = _skeleton()
    ho = _horizon_outputs(sk)
    ho["medium"] = ho["medium"].model_copy(update={"targets": []})
    with pytest.raises(SlicedAssemblyError, match="omitted"):
        _assemble_sliced_output(
            skeleton=sk, horizon_outputs=ho,
            section_outputs=_faithful_sections(sk),
        )


def test_omitted_section_fails_loud():
    sk = _skeleton()
    sections = _faithful_sections(sk)
    sections["short"] = []
    with pytest.raises(SlicedAssemblyError, match="omitted"):
        _assemble_sliced_output(
            skeleton=sk, horizon_outputs=_horizon_outputs(sk),
            section_outputs=sections,
        )


def test_invented_sections_and_deltas_dropped():
    sk = _skeleton()
    ho = _horizon_outputs(sk)
    ho["medium"] = ho["medium"].model_copy(update={
        "deltas_from_prior": list(ho["medium"].deltas_from_prior) + [Delta(
            item_kind="theme", item_id="medium.themes.invented",
            horizon="medium", change_kind="added", summary="not in roster",
        )],
    })
    sections = _faithful_sections(sk)
    sections["medium"] = sections["medium"] + [
        _section("estate", "medium", body="invented section"),
    ]
    out, _ = _assemble_sliced_output(
        skeleton=sk, horizon_outputs=ho, section_outputs=sections,
    )
    assert [d.item_id for d in out.medium.deltas_from_prior] == [
        "medium.targets.nvda",
    ]
    assert [s.section_id for s in out.sections] == ["ips", "concentration"]


# ----------------------------------------------------------------------
# Fan-out / salvage — _run_phase_3_sliced with stubbed agents
# ----------------------------------------------------------------------


@pytest.fixture
def session(alembic_engine_at_head):
    SessionLocal = sessionmaker(
        bind=alembic_engine_at_head, expire_on_commit=False,
    )
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.add(PlanVersion(
        user_id="ariel", role="baseline", version_label="Jacobs v2.0",
        raw_markdown="# Plan", distillate_rendered="# Plan distillate\n",
    ))
    run = DecisionRun(
        user_id="ariel", ticker="(plan)", tier="T3",
        decision_kind="plan_revision", status="running",
    )
    s.add(run)
    s.commit()
    s.refresh(run)
    s.run_id = run.id  # convenience
    yield s
    s.close()


class _StubSkeletonAgent:
    skeleton: PlanSkeleton | None = None

    def __init__(self, **kw):
        pass

    def run_sync(self, **kw):
        return SimpleNamespace(output=type(self).skeleton)


def _install_stubs(monkeypatch, flow, sk, *, fail_slices=None,
                   transient_slices=None):
    """Stub the three agent classes on the package namespace. ``fail_slices``
    die on EVERY attempt; ``transient_slices`` fail once then succeed."""
    fail_slices = set(fail_slices or ())
    transient = dict.fromkeys(transient_slices or (), 0)
    calls: list[str] = []

    _StubSkeletonAgent.skeleton = sk
    monkeypatch.setattr(flow, "PlanSkeletonSynthesizerAgent", _StubSkeletonAgent)

    def _slice_name(assignment_block: str) -> str:
        for h in ("long", "medium", "short"):
            if f"Expand horizon {h!r}" in assignment_block:
                return h
            if f"horizon {h!r} — one per" in assignment_block:
                return f"sections_{h}"
        raise AssertionError(f"unrecognized assignment: {assignment_block[:80]}")

    class _StubHorizonAgent:
        def __init__(self, **kw):
            pass

        def run_sync(self, *, shared_prefix, assignment_block, **kw):
            name = _slice_name(assignment_block)
            calls.append(name)
            if name in fail_slices:
                raise RuntimeError(f"{name} permanently dead")
            if name in transient and transient[name] == 0:
                transient[name] += 1
                raise RuntimeError(f"{name} transient flake")
            return SimpleNamespace(output=_faithful_horizon(sk, name))

    class _StubSectionAgent:
        def __init__(self, **kw):
            pass

        def run_sync(self, *, shared_prefix, assignment_block, **kw):
            name = _slice_name(assignment_block)
            calls.append(name)
            h = name.split("_", 1)[1]
            if name in fail_slices:
                raise RuntimeError(f"{name} permanently dead")
            if name in transient and transient[name] == 0:
                transient[name] += 1
                raise RuntimeError(f"{name} transient flake")
            from argosy.agents.plan_slice_synthesizer import SectionBatch
            return SimpleNamespace(output=SectionBatch(
                sections=_faithful_sections(sk).get(h, []),
            ))

    monkeypatch.setattr(flow, "PlanHorizonSliceSynthesizerAgent", _StubHorizonAgent)
    monkeypatch.setattr(
        flow, "PlanSectionBatchSliceSynthesizerAgent", _StubSectionAgent,
    )
    monkeypatch.setattr(flow, "_persist_agent_reports", lambda s, r: None)
    # Deterministic empty manifest — the gate's manifest floor is unit-
    # tested in tests/test_skeleton_gate.py; here the resolver would read
    # whatever the fixture DB happens to derive.
    import argosy.services.plan_numeric_resolver as _resolver
    from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers

    monkeypatch.setattr(
        _resolver, "resolve_plan_numbers",
        lambda *a, **k: ResolvedPlanNumbers(),
    )
    return calls


def _capture_checkpoints(monkeypatch, flow):
    rows: list[dict] = []

    def _record(**kw):
        rows.append(kw)

    monkeypatch.setattr(flow, "_record_phase3_sub_checkpoint", _record)
    return rows


def _run(flow, session, **kw):
    baseline = session.query(PlanVersion).filter_by(role="baseline").one()
    return flow._run_phase_3_sliced(
        session=session, user_id="ariel", baseline=baseline,
        prior_current=None,
        analyst_reports_text="(analysts)", debate_outcomes_text="(debates)",
        portfolio_summary="(portfolio)", fills_summary="(fills)",
        decision_run_id=f"plan-synth-{session.run_id}",
        decision_run_int=session.run_id,
        guidance="", corrective_ctx=None, **kw,
    )


def test_transient_slice_retried_and_all_checkpoints_written(
    session, monkeypatch,
):
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setenv("ARGOSY_SKELETON_COVERAGE_FLOOR", "0")
    sk = _skeleton()
    calls = _install_stubs(monkeypatch, flow, sk, transient_slices=["medium"])
    rows = _capture_checkpoints(monkeypatch, flow)

    output, reports, prov = _run(flow, session)
    assert output.medium.posture == "medium posture essay"
    # medium flaked once then succeeded — exactly one retry.
    assert calls.count("medium") == 2
    assert prov["slices"]["medium"]["retries"] == 1
    # skeleton + all 5 slice checkpoints written (2 section batches exist).
    suffixes = sorted(r["suffix"] for r in rows)
    assert suffixes == [
        "skeleton", "slice.long", "slice.medium",
        "slice.sections_medium", "slice.sections_short", "slice.short",
    ]
    sk_hash = prov["skeleton_sha256"]
    for r in rows:
        if r["suffix"].startswith("slice."):
            assert r["payload"]["skeleton_sha256"] == sk_hash


def test_dead_slice_fails_phase_but_siblings_checkpointed(
    session, monkeypatch,
):
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setenv("ARGOSY_SKELETON_COVERAGE_FLOOR", "0")
    monkeypatch.setenv("ARGOSY_SLICED_SLICE_RETRIES", "1")
    sk = _skeleton()
    calls = _install_stubs(monkeypatch, flow, sk, fail_slices=["short"])
    rows = _capture_checkpoints(monkeypatch, flow)

    with pytest.raises(SliceExpansionError) as exc_info:
        _run(flow, session)
    assert set(exc_info.value.failures) == {"short"}
    assert calls.count("short") == 2  # 1 attempt + 1 retry
    # Every SIBLING completed and was checkpointed — salvage, not loss.
    suffixes = {r["suffix"] for r in rows}
    assert "slice.long" in suffixes and "slice.medium" in suffixes
    assert "slice.sections_medium" in suffixes
    assert "slice.short" not in suffixes


def test_resume_reruns_only_dead_slice(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _sha256_text,
    )

    monkeypatch.setenv("ARGOSY_SKELETON_COVERAGE_FLOOR", "0")
    sk = _skeleton()
    sk_json = sk.model_dump_json()
    sk_hash = _sha256_text(sk_json)
    existing = {
        "skeleton": {"skeleton_json": sk_json, "skeleton_sha256": sk_hash},
    }
    for name in ("long", "medium", "sections_medium", "sections_short"):
        if name.startswith("sections_"):
            from argosy.agents.plan_slice_synthesizer import SectionBatch
            out_json = SectionBatch(
                sections=_faithful_sections(sk).get(name.split("_", 1)[1], []),
            ).model_dump_json()
        else:
            out_json = _faithful_horizon(sk, name).model_dump_json()
        existing[f"slice.{name}"] = {
            "skeleton_sha256": sk_hash, "output_json": out_json, "retries": 0,
        }
    monkeypatch.setattr(
        flow, "_load_phase3_sub_checkpoints",
        lambda session, *, decision_run_id: existing,
    )
    calls = _install_stubs(monkeypatch, flow, sk)
    rows = _capture_checkpoints(monkeypatch, flow)

    output, _reports, prov = _run(flow, session)
    # ONLY the dead slice (short) re-ran; skeleton was not re-called.
    assert calls == ["short"]
    assert prov["skeleton_resumed"] is True
    assert prov["slices"]["long"]["resumed"] is True
    assert prov["slices"]["short"]["resumed"] is False
    # Only the re-run slice writes a new checkpoint.
    assert [r["suffix"] for r in rows] == ["slice.short"]
    assert output.long.posture == "long posture essay"


def test_skeleton_hash_mismatch_invalidates_slice_checkpoints(
    session, monkeypatch,
):
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setenv("ARGOSY_SKELETON_COVERAGE_FLOOR", "0")
    sk = _skeleton()
    stale = {
        f"slice.{name}": {
            "skeleton_sha256": "stale-hash",
            "output_json": _faithful_horizon(sk, name).model_dump_json()
            if name in ("long", "medium", "short") else "{}",
            "retries": 0,
        }
        for name in ("long", "medium", "short",
                     "sections_medium", "sections_short")
    }
    monkeypatch.setattr(
        flow, "_load_phase3_sub_checkpoints",
        lambda session, *, decision_run_id: stale,
    )
    calls = _install_stubs(monkeypatch, flow, sk)
    _capture_checkpoints(monkeypatch, flow)

    _output, _reports, prov = _run(flow, session)
    # Every slice re-ran — the stale-skeleton checkpoints were ignored.
    assert sorted(calls) == sorted([
        "long", "medium", "short", "sections_medium", "sections_short",
    ])
    assert all(not v["resumed"] for v in prov["slices"].values())


def test_resumed_skeleton_failing_regate_is_discarded(session, monkeypatch):
    """A resumed skeleton checkpoint is RE-GATED; one that no longer passes
    (gate inputs moved / pre-gate row) is discarded and stage A re-runs
    (codex sliced review blocker #1)."""
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _sha256_text,
    )

    monkeypatch.setenv("ARGOSY_SKELETON_COVERAGE_FLOOR", "0")
    good = _skeleton()
    # The checkpointed skeleton violates the gate: a delta on a no_change
    # horizon.
    bad = good.model_copy(update={
        "delta_roster": list(good.delta_roster) + [SkeletonDelta(
            item_kind="target", item_id="long.targets.swr", horizon="long",
            change_kind="modified", summary="illegal on no_change",
        )],
    })
    bad_json = bad.model_dump_json()
    existing = {
        "skeleton": {
            "skeleton_json": bad_json,
            "skeleton_sha256": _sha256_text(bad_json),
        },
    }
    monkeypatch.setattr(
        flow, "_load_phase3_sub_checkpoints",
        lambda session, *, decision_run_id: existing,
    )
    skeleton_calls: list[int] = []

    class _CountingSkeletonAgent(_StubSkeletonAgent):
        def run_sync(self, **kw):
            skeleton_calls.append(1)
            return SimpleNamespace(output=good)

    _install_stubs(monkeypatch, flow, good)
    monkeypatch.setattr(
        flow, "PlanSkeletonSynthesizerAgent", _CountingSkeletonAgent,
    )
    _capture_checkpoints(monkeypatch, flow)

    _output, _reports, prov = _run(flow, session)
    assert skeleton_calls == [1], "stage A must re-run on a re-gate failure"
    assert prov["skeleton_resumed"] is False
    assert prov["skeleton_sha256"] == _sha256_text(good.model_dump_json())


def test_skeleton_gate_retry_then_abort(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.orchestrator.flows.plan_synthesis.sliced_phase3 import (
        SkeletonGateError,
    )

    # Coverage floor 12 with a 2-section skeleton → gate fails; the stub
    # returns the SAME bad skeleton on the retry → loud abort.
    monkeypatch.setenv("ARGOSY_SKELETON_COVERAGE_FLOOR", "12")
    sk = _skeleton()
    skeleton_calls: list[str] = []

    class _CountingSkeletonAgent(_StubSkeletonAgent):
        def run_sync(self, **kw):
            skeleton_calls.append(kw.get("gate_violations_block", ""))
            return SimpleNamespace(output=sk)

    _install_stubs(monkeypatch, flow, sk)
    monkeypatch.setattr(
        flow, "PlanSkeletonSynthesizerAgent", _CountingSkeletonAgent,
    )
    _capture_checkpoints(monkeypatch, flow)

    with pytest.raises(SkeletonGateError):
        _run(flow, session)
    # Exactly ONE retry, with the violations fed back.
    assert len(skeleton_calls) == 2
    assert skeleton_calls[0] == ""
    assert "coverage" in skeleton_calls[1]
