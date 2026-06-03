"""v4 plan-renderer block B1 — surface the 20 sections + deltas at top
+ assumption ledger + fleet receipts.

Renderer-wins block (per ``tmp_review/plan_document_v4_spec.md`` §4 B1):

1. ``render_section_evidence_appendix`` exposes the synth's flat
   ``sections: list[Section]`` (Phase 3 output, ~20 entries) in the
   user-facing plan markdown — previously invisible.
2. The user-facing horizon md now carries the
   ``## Deltas vs. prior current`` block at the TOP of the document
   (counter-decision to Phase 1's strip; user explicitly asked).
3. ``render_assumption_ledger_appendix`` emits a hard-coded 15-row v1
   table sourced from the v4 spec §2/§3 (real return, FX, NVDA cap,
   T12 spend, etc).
4. ``render_fleet_receipts_appendix`` queries the per-decision-run
   ``agent_reports`` rows and renders one row per agent (role / size
   / model / tokens / cost / key finding).

Together, these surface ~50 KB of currently-hidden agent reasoning
that drun 71 produced but the user-facing markdown didn't expose.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.agents.plan_synthesizer_types import (
    Action,
    Assumption,
    Citation,
    Delta,
    FactClaim,
    HorizonSection,
    PlanSynthesisOutput,
    Section,
    SectionEvidence,
    SynthTarget,
    SynthesisInputs,
    Theme,
)
from argosy.orchestrator.flows.plan_synthesis.render import (
    _horizon_md_audit,
    _horizon_md_user,
    render_assumption_ledger_appendix,
    render_fleet_receipts_appendix,
    render_plan_appendices,
    render_section_evidence_appendix,
)
from argosy.quality.canonical_sections import CANONICAL_SECTION_IDS
from argosy.state.models import AgentReport, Base, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_section(section_id: str, horizon: str, n_facts: int = 2) -> Section:
    """Build one Section with valid evidence (passes Pydantic validators).

    ``n_facts`` ≥ 1 because SectionEvidence requires facts OR missing_data
    to be non-empty, AND every fact must be cited.
    """
    facts = [
        FactClaim(
            text=f"Section {section_id} fact {i} (long enough text here).",
            kind="qualitative",
        )
        for i in range(n_facts)
    ]
    citations = [
        Citation(
            source_kind="plan_doc",
            source_locator=f"distillate.section_{section_id}[{i}]",
            extract=f"verbatim extract for fact {i} sourced from plan_doc.",
            supports_fact_index=i,
        )
        for i in range(n_facts)
    ]
    return Section(
        section_id=section_id,
        horizon=horizon,  # type: ignore[arg-type]
        title=CANONICAL_SECTION_IDS[section_id],
        body_md=(
            f"Body markdown for **{section_id}** in the {horizon} horizon. "
            f"This is the prose the user reads — currently ~{n_facts} facts."
        ),
        evidence=SectionEvidence(
            facts=facts,
            source_span=citations,
            assumptions=[],
            missing_data=[f"missing datum for {section_id}"] if n_facts > 0 else [],
        ),
    )


def _make_section_with_assumption(section_id: str, horizon: str) -> Section:
    """Build a Section whose evidence carries a soft (inference) citation
    backed by an Assumption — covers the assumption-rendering branch."""
    return Section(
        section_id=section_id,
        horizon=horizon,  # type: ignore[arg-type]
        title=CANONICAL_SECTION_IDS[section_id],
        body_md=f"Section {section_id} prose with an inference citation.",
        evidence=SectionEvidence(
            facts=[
                FactClaim(
                    text=(
                        f"Inference-based fact in {section_id} "
                        "with sufficient length."
                    ),
                    kind="qualitative",
                ),
            ],
            source_span=[
                Citation(
                    source_kind="inference",
                    source_locator="analyst.macro.real_return",
                    supports_fact_index=0,
                ),
            ],
            assumptions=[
                Assumption(
                    text="Real return assumption",
                    default_value=Decimal("0.05"),
                    rationale="Anchored to macro_analyst baseline",
                ),
            ],
            missing_data=[],
        ),
    )


def _make_full_plan_output() -> PlanSynthesisOutput:
    """Build a 20-section PlanSynthesisOutput that mirrors the drun 71
    shape (3 horizon-sections + 20 flat Sections covering canonical IDs).

    Sections distribute as: long gets the strategic IDs (cover, goals,
    NW, IPS, withdrawal, MC, estate, FI bridge, life events, equity comp,
    tax_plan), medium gets the tactical (cashflow, capital_sufficiency,
    insurance, healthcare, action_items), short gets the immediate-risk
    (concentration, cross_border) plus 2 repeats so the total = 20.
    """
    long_ids = [
        "cover_assumptions", "client_goals", "net_worth", "ips",
        "withdrawal", "monte_carlo", "estate", "fi_bridge",
        "life_events", "equity_comp", "tax_plan",
    ]
    medium_ids = [
        "cashflow", "capital_sufficiency", "insurance",
        "healthcare", "action_items",
    ]
    short_ids = [
        "concentration", "cross_border",
    ]
    # 11 long + 5 medium + 2 short = 18. Repeat 2 canonical ids on short
    # (concentration shows up across horizons in the real synth output)
    # to land at 20 sections — matches the drun 71 shape.
    repeats = ["concentration", "tax_plan"]
    sections: list[Section] = []
    for sid in long_ids:
        sections.append(_make_section(sid, "long"))
    for sid in medium_ids:
        sections.append(_make_section(sid, "medium"))
    for sid in short_ids:
        sections.append(_make_section(sid, "short"))
    for sid in repeats:
        sections.append(_make_section_with_assumption(sid, "short"))
    assert len(sections) == 20, f"expected 20 sections, got {len(sections)}"

    long_h = HorizonSection(
        horizon="long",
        freshness_expected="annual",
        status="minor_revision",
        posture="Long-horizon posture body.",
        targets=[
            SynthTarget(
                label="Portfolio target ₪25.83M (cushion)",
                value=25.83,
                unit="usd",  # placeholder unit; renderer doesn't care
                stated_at=date(2026, 6, 2),
                revisit_after=date(2027, 1, 1),
                rationale="Cushion target per A3.",
            ),
        ],
        themes=[
            Theme(label="UCITS-first", direction="lean_into"),
        ],
        actions=[],
        deltas_from_prior=[
            Delta(
                item_kind="target",
                item_id="long.targets.cushion",
                horizon="long",
                change_kind="modified",
                summary="Cushion target raised from ₪21M to ₪25.83M.",
            ),
        ],
        rationale="Long rationale body.",
    )
    medium_h = HorizonSection(
        horizon="medium",
        freshness_expected="quarterly",
        status="minor_revision",
        posture="Medium-horizon posture body.",
        actions=[
            Action(
                label="Continue NVDA reduction",
                horizon_kind="directional",
                detail="Reduce toward 20% cap.",
                rationale="A10 cap.",
            ),
        ],
        deltas_from_prior=[
            Delta(
                item_kind="action",
                item_id="medium.actions.nvda",
                horizon="medium",
                change_kind="modified",
                summary="Glide pace adjusted to 1-yr tolerance.",
            ),
        ],
    )
    short_h = HorizonSection(
        horizon="short",
        freshness_expected="monthly",
        status="minor_revision",
        posture="Short-horizon posture body.",
        deltas_from_prior=[
            Delta(
                item_kind="action",
                item_id="short.actions.tax",
                horizon="short",
                change_kind="added",
                summary="Refresh IL tax knowledge files before any trade.",
            ),
        ],
    )
    return PlanSynthesisOutput(
        long=long_h,
        medium=medium_h,
        short=short_h,
        inputs=SynthesisInputs(decision_run_id=71),
        sections=sections,
    )


@pytest.fixture
def db_session_with_drun_71(tmp_path):
    """File-backed SQLite session seeded with 26 AgentReport rows
    against decision_id='plan-synth-71' — mirrors the drun 71 fleet
    receipts shape so render_fleet_receipts_appendix has rows to
    surface."""
    db_path = tmp_path / "renderer_v4.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    s = SF()
    try:
        s.add(User(id="ariel", plan="free"))
        s.flush()
        # 26 agent rows — matches drun 71's fleet shape.
        roles = [
            "concentration_analyst", "fundamentals_analyst", "fx_analyst",
            "household_budget_analyst", "macro_analyst", "news_analyst",
            "plan_critique", "sentiment_analyst", "tax_analyst",
            "technical_analyst", "plan_coverage_analyst",
            "withdrawal_sequencer", "bull_researcher", "bear_researcher",
            "researcher_facilitator", "bull_researcher", "bear_researcher",
            "researcher_facilitator", "bull_researcher", "bear_researcher",
            "researcher_facilitator", "plan_synthesizer", "risk_officer",
            "risk_officer", "fund_manager", "codex_second_opinion",
        ]
        assert len(roles) == 26
        for i, role in enumerate(roles):
            row = AgentReport(
                user_id="ariel",
                agent_role=role,
                decision_id="plan-synth-71",
                prompt_hash=f"hash-{i}",
                response_text=(
                    f"{{\"verdict\": \"finding {i} from {role}: ok\","
                    f" \"detail\": \"body\"}}"
                ),
                tokens_in=1000 + i * 10,
                tokens_out=200 + i * 5,
                cost_usd=0.01 + i * 0.001,
                model="claude-opus-4" if role != "codex_second_opinion" else "gpt-5",
            )
            s.add(row)
        s.commit()
        yield s
    finally:
        s.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_user_horizon_md_carries_deltas_at_top():
    """v4 block B1: the Deltas block is no longer stripped from the
    user-facing horizon md — it appears at the TOP, before posture /
    targets / rationale."""
    output = _make_full_plan_output()
    long_md = _horizon_md_user(output.long)

    assert "## Deltas vs. prior current" in long_md, (
        "User render must surface the Deltas block (v4 block B1)."
    )
    deltas_pos = long_md.index("## Deltas vs. prior current")
    posture_pos = long_md.index("**Posture.**")
    assert deltas_pos < posture_pos, (
        f"Deltas must appear ABOVE posture (deltas@{deltas_pos}, "
        f"posture@{posture_pos}) — that's the v4 ordering."
    )


def test_audit_horizon_md_retains_deltas_at_bottom():
    """Audit variant keeps deltas, status header, and revisit
    parentheticals — all retained for /decisions/<id> developer pane."""
    output = _make_full_plan_output()
    audit_md = _horizon_md_audit(output.long)
    assert "status: minor_revision" in audit_md
    assert "## Deltas vs. prior current" in audit_md
    # Audit emits deltas AFTER targets/actions, not before posture.
    deltas_pos = audit_md.index("## Deltas vs. prior current")
    targets_pos = audit_md.index("## Targets")
    assert targets_pos < deltas_pos


def test_section_evidence_appendix_renders_all_20_sections():
    """Every Section.body_md must surface in the appendix, and every
    section's evidence sub-tree must be wrapped in a collapsible
    <details> block."""
    output = _make_full_plan_output()
    appendix = render_section_evidence_appendix(output)

    assert "## Appendix — Section-by-section evidence" in appendix
    # Every section's body_md must be present.
    for s in output.sections:
        assert s.body_md.rstrip() in appendix, (
            f"section {s.section_id}/{s.horizon} body_md missing from "
            "appendix render"
        )
    # 20 collapsible blocks — one per section.
    assert appendix.count("<details>") == 20
    assert appendix.count("</details>") == 20
    assert appendix.count("<summary>Evidence subtree</summary>") == 20
    # Evidence sub-tree headers appear when populated.
    assert "**Facts**" in appendix
    assert "**Citations**" in appendix
    # The repeat sections use assumption_register-style soft citations.
    assert "**Assumptions**" in appendix


def test_section_evidence_appendix_empty_when_no_sections():
    """Legacy PlanSynthesisOutput rows have ``sections=[]`` — the
    renderer must return the empty string so callers can
    unconditionally append."""
    output = PlanSynthesisOutput(
        long=HorizonSection(
            horizon="long",
            freshness_expected="annual",
            status="no_change",
            posture="",
        ),
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
        sections=[],
    )
    assert render_section_evidence_appendix(output) == ""


def test_assumption_ledger_appendix_renders_15_rows():
    """v1 ledger emits the 15 canonical assumption rows."""
    md = render_assumption_ledger_appendix()
    assert "## Appendix — Assumption ledger" in md
    # All 15 rows referenced by ID.
    for i in range(1, 16):
        assert f"| A{i} " in md, f"row A{i} missing from ledger"
    # Key locked values from v4 spec §2 surface verbatim.
    assert "5.0% real" in md
    assert "2.4% real" in md
    assert "20% of portfolio" in md
    assert "₪341k/yr" in md
    # Header row.
    assert "| ID | Assumption | Value | Source | Year | Confidence | Affects |" in md


def test_fleet_receipts_appendix_renders_26_rows(db_session_with_drun_71):
    """Fleet receipts must list every agent_reports row for drun 71."""
    md = render_fleet_receipts_appendix(
        db_session_with_drun_71, decision_run_id=71,
    )
    assert "## Appendix — Fleet receipts" in md
    # 26 numbered rows (one per agent invocation).
    for i in range(1, 27):
        assert f"| {i} " in md, f"fleet receipt row {i} missing"
    # Roles surface in backticks.
    assert "`concentration_analyst`" in md
    assert "`plan_synthesizer`" in md
    assert "`fund_manager`" in md
    assert "`codex_second_opinion`" in md
    # Models surface.
    assert "claude-opus-4" in md
    assert "gpt-5" in md
    # Cost summary in the header paragraph.
    assert "total cost" in md
    # Key finding extracted from the JSON ``verdict`` field.
    assert "finding" in md.lower()


def test_fleet_receipts_empty_when_no_rows(db_session_with_drun_71):
    """No rows for a different decision_run_id → empty string."""
    md = render_fleet_receipts_appendix(
        db_session_with_drun_71, decision_run_id=9999,
    )
    assert md == ""


def test_render_plan_appendices_contains_all_four_required_surfaces(
    db_session_with_drun_71,
):
    """The combined appendix builder produces a single markdown block
    containing all three appendices (ledger + sections + receipts).

    Combined with the user-facing horizon md (which now carries the
    Deltas block at the top), this is the v4 "what the user sees"
    surface — ~50 KB of previously-hidden reasoning made visible.
    """
    output = _make_full_plan_output()
    appendices = render_plan_appendices(
        output, session=db_session_with_drun_71, decision_run_id=71,
    )
    long_md = _horizon_md_user(output.long)
    combined = long_md.rstrip() + "\n\n" + appendices

    # All four user-visible surfaces (per spec §4 B1) must appear.
    assert "Appendix — Section-by-section evidence" in combined
    assert "Deltas vs. prior current" in combined
    assert "Assumption ledger" in combined
    assert "Fleet receipts" in combined

    # Spec §4 B1 asserts: section-evidence block must contain body_md
    # for ALL 20 sections.
    for s in output.sections:
        assert s.body_md.rstrip() in combined, (
            f"section {s.section_id}/{s.horizon} not surfaced to user"
        )

    # Ordering: ledger before sections before receipts.
    ledger_pos = combined.index("Assumption ledger")
    sections_pos = combined.index("Section-by-section evidence")
    receipts_pos = combined.index("Fleet receipts")
    assert ledger_pos < sections_pos < receipts_pos, (
        f"ordering broken: ledger@{ledger_pos} < "
        f"sections@{sections_pos} < receipts@{receipts_pos}"
    )

    # And the Deltas block is at the TOP — before all appendices.
    deltas_pos = combined.index("## Deltas vs. prior current")
    assert deltas_pos < ledger_pos


def test_render_plan_appendices_skips_receipts_when_no_session():
    """When called without a session (e.g. dry-run / test path), the
    fleet-receipts block is skipped but ledger + sections still render."""
    output = _make_full_plan_output()
    appendices = render_plan_appendices(output)
    assert "Assumption ledger" in appendices
    assert "Section-by-section evidence" in appendices
    assert "Fleet receipts" not in appendices


def test_section_evidence_appendix_render_size_meets_spec_minimum():
    """Sanity check on the magnitude — the appendix must surface a
    nontrivial chunk of content (else "20 sections" is a lie). drun 71
    produced ~50 KB; a fresh fixture should clear ≥10 KB once 20
    sections each carry a body_md + evidence sub-tree."""
    output = _make_full_plan_output()
    appendix = render_section_evidence_appendix(output)
    # Each section's body_md alone is ~100 chars; with 20 sections plus
    # evidence sub-trees the total should clear 10 KB easily.
    assert len(appendix) >= 10_000, (
        f"section-evidence appendix is too small ({len(appendix)} bytes) "
        "— suggests sections are being skipped or evidence is missing"
    )
