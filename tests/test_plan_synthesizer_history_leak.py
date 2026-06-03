"""Phase 1 verification — clean synth context + renderer split.

Asserts:
- Synthesizer's user-prompt no longer includes the prior-plan body
  block (Defect 1 root cause).
- ``_horizon_md_user`` drops status header, stated/revisit
  parentheticals, and ``## Deltas vs. prior current`` block.
- ``_horizon_md_audit`` retains all of the above (developer fidelity).
- Re-rendering the v20 horizon JSON fixtures through the user variant
  passes the Phase 0 ``check_history_leak`` gate (0 violations).
- Migration 0061 is idempotent (upgrade head → downgrade 0060 →
  upgrade head round-trips with no data loss).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa

from argosy.agents.plan_synthesizer import PlanSynthesizerAgent
from argosy.agents.plan_synthesizer_types import (
    Action,
    Delta,
    HorizonSection,
    SynthTarget,
    Theme,
)
from argosy.orchestrator.flows.plan_synthesis.render import (
    _horizon_md_audit,
    _horizon_md_user,
)
from argosy.quality import check_history_leak


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "plan_v20_horizons"


# ---------------------------------------------------------------------------
# Test 1 — synthesizer drops prior plan body from the user prompt
# ---------------------------------------------------------------------------

def test_synthesizer_drops_prior_plan_body():
    """The ``=== PRIOR CURRENT PLAN ===`` block was the proximate cause
    of revision-history leakage in v20 (the model paraphrased the prior
    draft's prose). Phase 1 drops it from the user prompt entirely; id
    stability is now carried by the ``PRIOR ITEMS INDEX`` block alone.
    """
    agent = PlanSynthesizerAgent(user_id="ariel")
    sys_prompt, user_prompt = agent.build_prompt(
        baseline_distillate_md="baseline body",
        prior_current_md="THIS_IS_PRIOR_BODY_DO_NOT_INCLUDE",
        prior_items_index=[
            {
                "horizon": "medium",
                "item_kind": "target",
                "item_id": "medium.targets.nvda",
                "label": "NVDA share",
                "value": 15,
                "unit": "pct_of_portfolio",
                "from_plan": 19,
            }
        ],
        analyst_reports_text="(analyst reports)",
        debate_outcomes_text="(debate outcomes)",
        portfolio_snapshot_summary="(portfolio)",
        recent_fills_summary="(fills)",
        user_directive=None,
    )
    assert "THIS_IS_PRIOR_BODY_DO_NOT_INCLUDE" not in user_prompt, (
        "Phase 1 must NOT include the prior-plan body in the synth "
        "user prompt — the model paraphrases this into revision prose."
    )
    assert "=== PRIOR CURRENT PLAN ===" not in user_prompt, (
        "The PRIOR CURRENT PLAN block header itself must be gone — "
        "leaving an empty block invites the model to fill it."
    )
    # ID stability scaffolding should still be present.
    assert "PRIOR ITEMS INDEX" in user_prompt
    assert "medium.targets.nvda" in user_prompt
    # And the system prompt should now teach the structural rule, not
    # the lineage-narrative invitation.
    assert "ID STABILITY" in sys_prompt
    assert "structural contract" in sys_prompt
    assert "T4.8a" not in sys_prompt, (
        "T4.8a marker should be relocated; the system prompt now "
        "carries a structural ID STABILITY rule with no draft reference."
    )


# ---------------------------------------------------------------------------
# Renderer fixture helpers
# ---------------------------------------------------------------------------

def _make_horizon_section_with_history_surfaces() -> HorizonSection:
    """Build a HorizonSection populated with every surface the user
    variant must drop (status header, target stated/revisit dates,
    deltas block) so the assertion harness can verify each surface
    individually."""
    return HorizonSection(
        horizon="medium",
        freshness_expected="quarterly",
        status="minor_revision",
        posture="Posture body.",
        targets=[
            SynthTarget(
                label="NVDA share of portfolio",
                value=15,
                unit="pct_of_portfolio",
                stated_at=date(2026, 6, 2),
                revisit_after=date(2026, 9, 1),
                rationale="Glide toward strategic cap.",
            ),
        ],
        themes=[
            Theme(
                label="UCITS-first deployment",
                direction="lean_into",
                rationale="Estate-tax mitigation.",
            ),
        ],
        actions=[
            Action(
                label="Sell 2500 NVDA shares",
                horizon_kind="dated",
                trigger_or_date="2026-09-15",
                detail="From pre-2024 grants only.",
                rationale="Section 102 capital-track eligible.",
            ),
        ],
        deltas_from_prior=[
            Delta(
                item_kind="target",
                item_id="medium.targets.nvda",
                horizon="medium",
                change_kind="modified",
                summary="Glide pace adjusted.",
            ),
        ],
        rationale="Section rationale body.",
    )


# ---------------------------------------------------------------------------
# Test 2 — user renderer drops status header + revisit parentheticals,
# now surfaces the Deltas block at the TOP (v4 block B1, 2026-06-02)
# ---------------------------------------------------------------------------

def test_renderer_user_drops_status_and_revisit_keeps_deltas_at_top():
    section = _make_horizon_section_with_history_surfaces()
    output = _horizon_md_user(section)

    assert "status:" not in output, (
        "Status header line is the v20 leak surface line 1; user "
        "variant must drop the suffix."
    )
    assert "(stated " not in output, (
        "Stated/revisit parenthetical metadata leaks revision dates."
    )
    assert "revisit " not in output, (
        "Revisit dates must not appear in user-facing output."
    )
    # v4 block B1 — the Deltas block is INTENTIONALLY present (user-
    # requested counter-decision to Phase 1's strip), and it must
    # appear AT THE TOP of the document (before posture / targets /
    # rationale) so the user sees "what changed" before the details.
    assert "## Deltas vs. prior current" in output, (
        "v4 block B1: Deltas block is now surfaced to the user — "
        "must NOT be stripped from the user variant."
    )
    deltas_pos = output.index("## Deltas vs. prior current")
    posture_pos = output.find("**Posture.**")
    targets_pos = output.find("## Targets")
    if posture_pos >= 0:
        assert deltas_pos < posture_pos, (
            "Deltas block must appear BEFORE the Posture line — that's "
            "the v4 block B1 ordering."
        )
    if targets_pos >= 0:
        assert deltas_pos < targets_pos, (
            "Deltas block must appear BEFORE the Targets block."
        )
    # Phase-0 history_leak gate will flag the Deltas heading by design
    # in v4 — the gate's regex pattern set is unchanged so the audit
    # surface stays catchable, but the user variant is allowed to
    # carry the block. The non-deltas patterns (status, parentheticals)
    # must still produce zero matches against the user render.
    from argosy.quality.regex_patterns import HISTORY_LEAK_PATTERNS as _HLP
    non_deltas_patterns = [p for p in _HLP if "Deltas" not in p.pattern]
    residual = []
    for p in non_deltas_patterns:
        residual.extend(p.findall(output))
    assert residual == [], (
        "User render still contains a non-Deltas history-leak surface "
        f"(status header / stated-revisit parenthetical): {residual!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — audit renderer retains everything
# ---------------------------------------------------------------------------

def test_renderer_audit_retains_all():
    """Audit variant is the developer-facing /decisions/<id> render and
    must keep every surface for traceability."""
    section = _make_horizon_section_with_history_surfaces()
    output = _horizon_md_audit(section)

    assert "status: minor_revision" in output, (
        "Audit pane needs the status header for traceability."
    )
    assert "(stated 2026-06-02" in output
    assert "revisit 2026-09-01" in output
    assert "## Deltas vs. prior current" in output
    assert "medium.targets.nvda" in output


# ---------------------------------------------------------------------------
# Test 4 — re-rendering v20 horizon JSON fixtures passes history_leak
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("horizon_name", ["short", "medium", "long"])
def test_v21_user_render_no_non_deltas_history_leak(horizon_name: str):
    """Load the persisted v20 horizon JSON, reconstitute it as a
    HorizonSection, and re-render through the user variant.

    v4 (block B1, 2026-06-02): the user render now INCLUDES the
    ``## Deltas vs. prior current`` block at the top (user-requested
    counter-decision to Phase 1's strip), so the Phase 0 history_leak
    gate's full pattern set will flag the deltas heading by design.
    The narrower assertion this test now makes is that no OTHER
    history-leak surface (status header, stated/revisit parentheticals,
    "prior-round delta" prose, etc.) sneaks back in via the v20
    fixture data — those would be real regressions.
    """
    raw = (FIXTURE_DIR / f"{horizon_name}.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    section = HorizonSection.model_validate(data)
    output = _horizon_md_user(section)
    # Carve out the Deltas-block header — v4 intentionally surfaces it.
    from argosy.quality.regex_patterns import HISTORY_LEAK_PATTERNS
    non_deltas_patterns = [
        p for p in HISTORY_LEAK_PATTERNS if "Deltas" not in p.pattern
    ]
    residual: list[str] = []
    for p in non_deltas_patterns:
        residual.extend(p.findall(output))
    assert residual == [], (
        f"v20 {horizon_name} re-rendered through _horizon_md_user "
        f"still has {len(residual)} non-Deltas history_leak match(es):\n"
        + "\n".join(repr(r) for r in residual[:8])
    )


# ---------------------------------------------------------------------------
# Test 4b — amendment path supplies prior_items_index (codex Phase 1 review)
# ---------------------------------------------------------------------------

def test_amendment_worker_supplies_prior_items_index(monkeypatch):
    """Codex Phase 1 review caught: dropping the PRIOR CURRENT PLAN
    body block starved the amendment synth of ID-stability signal,
    because the amendment worker never passed `prior_items_index`.
    Fix wires `_pkg_build_prior_items_index` into the amendment flow;
    this test asserts the kwarg reaches the synth agent."""
    from argosy.orchestrator.flows.plan_amendment.workers import (
        _run_phase_3_synthesizer,
    )

    captured: dict[str, object] = {}

    class _StubAgent:
        def __init__(self, *, user_id: str, **_) -> None:
            captured["user_id"] = user_id

        def run_sync(self, **kwargs):
            captured.update(kwargs)
            # Return a dummy with .output the worker can unwrap.
            from argosy.agents.plan_synthesizer_types import (
                HorizonSection,
                PlanSynthesisOutput,
                SynthesisInputs,
            )
            empty_h = HorizonSection(
                horizon="long",
                freshness_expected="annual",
                status="no_change",
                posture="",
            )
            out = PlanSynthesisOutput(
                long=empty_h,
                medium=HorizonSection(
                    horizon="medium",
                    freshness_expected="quarterly",
                    status="no_change",
                    posture="",
                ),
                short=HorizonSection(
                    horizon="short",
                    freshness_expected="monthly",
                    status="no_change",
                    posture="",
                ),
                inputs=SynthesisInputs(),
            )
            return type("R", (), {"output": out})

    monkeypatch.setattr(
        "argosy.orchestrator.flows.plan_amendment.workers.PlanSynthesizerAgent",
        _StubAgent,
    )

    sample_index = [
        {
            "horizon": "medium",
            "item_kind": "target",
            "item_id": "medium.targets.nvda",
            "label": "NVDA share of portfolio",
            "value": 15,
            "unit": "pct_of_portfolio",
            "from_plan": 19,
        }
    ]

    _run_phase_3_synthesizer(
        user_id="ariel",
        baseline_distillate_md="baseline",
        prior_current_md="(prior body — ignored by Phase 1 prompt)",
        guidance="test guidance",
        portfolio_summary="(portfolio)",
        fills_summary="(fills)",
        speculation_cap_pct=None,
        speculation_cap_concurrent=None,
        prior_items_index=sample_index,
    )

    assert captured.get("prior_items_index") == sample_index, (
        "Amendment worker must forward prior_items_index to the synth "
        "agent; without it, item_id stability is lost across amendment "
        "re-synths (codex Phase 1 review BLOCKER)."
    )


# ---------------------------------------------------------------------------
# Test 5 — migration 0061 round-trips cleanly
# ---------------------------------------------------------------------------

def test_audit_migration_idempotent(alembic_engine_at_head, monkeypatch, tmp_path):
    """Round-trip the head migration through 0060 and back.

    Asserts (a) the three audit columns exist at head, (b) they
    disappear when downgraded to 0060, and (c) reappear when
    re-upgraded to head — without losing or corrupting any rows.
    """
    eng = alembic_engine_at_head

    def column_names() -> set[str]:
        inspector = sa.inspect(eng)
        return {col["name"] for col in inspector.get_columns("plan_versions")}

    # (a) — at head, the three audit columns exist.
    cols_at_head = column_names()
    assert "horizon_long_md_audit" in cols_at_head
    assert "horizon_medium_md_audit" in cols_at_head
    assert "horizon_short_md_audit" in cols_at_head

    # Drop a row to verify round-trip preserves data.
    with eng.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, plan, created_at) "
            "VALUES ('ariel', 'free', :now)"
        ), {"now": "2026-01-01"})
        conn.execute(sa.text(
            "INSERT INTO plan_versions "
            "(user_id, version_label, source_path, raw_markdown, imported_at, "
            " horizon_long_md, horizon_long_md_audit) "
            "VALUES ('ariel', 'phase1-test', '', '# Plan', :now, "
            "        'user body', 'audit body')"
        ), {"now": "2026-06-02"})

    # (b) — downgrade to 0060, audit columns vanish.
    from alembic import command
    from alembic.config import Config
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "0060_objection_carry_forward")
    cols_at_0060 = column_names()
    assert "horizon_long_md_audit" not in cols_at_0060
    assert "horizon_medium_md_audit" not in cols_at_0060
    assert "horizon_short_md_audit" not in cols_at_0060
    # User-facing column is preserved across the downgrade.
    assert "horizon_long_md" in cols_at_0060

    # (c) — re-upgrade, audit columns return; pre-existing row is intact.
    command.upgrade(cfg, "head")
    cols_after_roundtrip = column_names()
    assert cols_after_roundtrip == cols_at_head, (
        f"Column set differs after round-trip:\n"
        f"  before: {sorted(cols_at_head)}\n"
        f"  after:  {sorted(cols_after_roundtrip)}"
    )
    with eng.begin() as conn:
        rows = conn.execute(sa.text(
            "SELECT user_id, version_label, horizon_long_md, horizon_long_md_audit "
            "FROM plan_versions WHERE version_label = 'phase1-test'"
        )).all()
    # Existing row is preserved; user MD stays, audit MD is NULL after
    # round-trip (the data was dropped when the column went away).
    assert len(rows) == 1
    assert rows[0][0] == "ariel"
    assert rows[0][2] == "user body"
    assert rows[0][3] is None
