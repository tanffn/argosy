"""Tests for the assembled-artifact builder.

``assemble_plan_artifact`` concatenates EVERY surface the user reads — the
plan body (export render path), the wealth-dashboard block, and the
appendices baked into the long-horizon markdown — into one artifact, plus a
typed per-surface headline map keyed by shared concept names. This is the
artifact no existing review stage ever holds in one place; a downstream
coherence gate / whole-artifact reader compares the per-surface headline
values against each other.

The artifact must REPRODUCE the export (it reuses ``build_plan_export_markdown``
and ``render_plan_appendices``), never re-invent rendering.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.assembled_artifact import (
    AssembledArtifact,
    assemble_plan_artifact,
)
from argosy.state.models import (
    AgentReport,
    Base,
    PlanVersion,
    PortfolioSnapshotRow,
    User,
    UserContext,
)

DRUN = 71
DECISION_ID = f"plan-synth-{DRUN}"


@pytest.fixture
def session(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'artifact.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    s = SF()
    try:
        s.add(User(id="ariel", plan="free"))
        s.flush()
        yield s
    finally:
        s.close()
        engine.dispose()


def _household_budget_json(*, monthly: float = 23_083.0) -> str:
    return json.dumps(
        {
            "runway_class": "comfortable",
            "monthly_burn_nis": monthly,
            "monthly_income_nis": 40000.0,
            "monthly_net_nis": 16917.0,
            "headroom_summary": "ok ok.",
            "key_concerns": [],
            "confidence": "HIGH",
            "cited_sources": ["household_budget/identity_yaml"],
        }
    )


def _seed_full_plan(s) -> None:
    """Seed a current plan + snapshot + household-budget report so every
    surface (body, dashboard, appendices-in-long-md) is populated."""
    # Snapshot with real positions so the wealth dashboard + the resolver's
    # snapshot-derived net worth / NVDA weight / US-situs estate all populate.
    s.add(
        PortfolioSnapshotRow(
            user_id="ariel",
            # Recent snapshot: marks inside the normal-staleness window so the
            # body/resolver surface values NVDA (a weeks-old fixture would
            # degrade-loud — correct production behavior, not what this
            # every-surface assembly test probes).
            snapshot_date=date.today(),
            imported_at=datetime.now(UTC),
            fx_usd_nis=3.10,
            fx_usd_eur=0.92,
            positions_json=json.dumps(
                [
                    {
                        "symbol": "NVDA",
                        "asset_type": "Individual stocks",
                        "usd_value_k": 2299.0,
                        "location": "Schwab US",
                        "currency": "USD",
                        "current_price": 175.0,
                        "shares": 13140,
                        # NVDA is deliberately UNMANAGED (held at Schwab, out of
                        # the tradeable sleeve) — the real policy, NOT pinned
                        # managed. Present-but-unmanaged is a CONCENTRATION fact:
                        # the resolver must still report NVDA's ~63.8% weight of
                        # the tradeable book (2299/(2299+1301)) on the body
                        # surface, so the cross-surface weight-agreement this
                        # test proves is exercised on the genuinely unmanaged
                        # name (regression guard for the ``excluded``/0.0 bug
                        # that zeroed the deconcentration math).
                    },
                    {
                        "symbol": "VOO",
                        "asset_type": "Core equity",
                        "usd_value_k": 1301.0,
                        "location": "Schwab US",
                        "currency": "USD",
                    },
                    {
                        "symbol": "SGOV",
                        "asset_type": "Cash",
                        "usd_value_k": 200.0,
                        "location": "Schwab US",
                        "currency": "USD",
                    },
                ]
            ),
            totals_json=json.dumps({"total_usd_value_k": 3800.0}),
            allocations_json=json.dumps([]),
        )
    )
    s.add(
        UserContext(
            user_id="ariel",
            identity_yaml=(
                "user_date_of_birth: '1981-04-15'\n"
                "fx_rate:\n"
                "  usd_nis: 3.10\n"
            ),
            goals_yaml="",
            constraints_yaml="",
        )
    )
    s.add(
        AgentReport(
            user_id="ariel",
            agent_role="household_budget",
            decision_id=DECISION_ID,
            prompt_hash="h",
            response_text=_household_budget_json(),
        )
    )
    # Current accepted plan. The appendices are baked into horizon_long_md at
    # synthesis time (see render_plan_appendices docstring), so we mirror that
    # here: the long-horizon markdown carries an "## Appendix" block.
    s.add(
        PlanVersion(
            user_id="ariel",
            role="current",
            version_label="plan-vNow",
            decision_run_id=DRUN,
            raw_markdown="# Plan v1\n\n## Quick Reference\n- SWR: 3.5%\n",
            horizon_long_md=(
                "### Long horizon\n"
                "- Reduce NVDA to 45% over 18 months.\n"
                "\n"
                "## Appendix — Assumption ledger\n"
                "- A1: net worth derived from snapshot.\n"
            ),
            horizon_medium_md="### Medium horizon\n- Quarterly tranches.\n",
            horizon_short_md="### Short horizon\n- Engage estate attorney.\n",
            accepted_at=datetime.now(UTC),
        )
    )
    s.commit()


def test_assemble_includes_every_user_facing_surface(session):
    """The assembled artifact must contain EVERY surface the user reads — body,
    dashboard, appendices — in one string, plus a typed map of each surface's
    headline values. This is the artifact no existing review stage ever holds."""
    _seed_full_plan(session)
    art = assemble_plan_artifact(session, user_id="ariel")
    assert isinstance(art, AssembledArtifact)

    # Every user-facing surface is present in the one concatenated text.
    assert "## Wealth Dashboard" in art.full_text
    assert "## Long-horizon plan" in art.full_text
    assert "Long horizon" in art.full_text
    assert "Appendix" in art.full_text

    # The typed per-surface headline map carries the shared concepts the gate
    # depends on, keyed by the short concept names.
    assert "net_worth_nis" in art.surface_values
    assert len(art.surface_values["net_worth_nis"]) >= 1
    # The body (resolver) states the LIQUID/investable net worth under
    # net_worth_nis; the dashboard states the TOTAL (incl. real estate) under a
    # DISTINCT concept key. They are different concepts and must not collide.
    liquid_surfaces = {s for s, _ in art.surface_values["net_worth_nis"]}
    assert "body" in liquid_surfaces
    assert "dashboard" not in liquid_surfaces
    assert "net_worth_total_nis" in art.surface_values
    total_surfaces = {s for s, _ in art.surface_values["net_worth_total_nis"]}
    assert "dashboard" in total_surfaces

    # NVDA weight present on both surfaces, in percent-POINTS (not fraction).
    assert "nvda_weight_pct" in art.surface_values
    nvda_vals = dict(art.surface_values["nvda_weight_pct"])
    assert nvda_vals.get("body") is not None
    # NVDA is ~63.8% of the tradeable book (2299 / (2299+1301)); both surfaces
    # must agree it's a large double-digit percentage, not a 0–1 fraction.
    assert nvda_vals["body"] > 1.0

    # US-situs estate exposure surfaced.
    assert "us_situs_estate_nis" in art.surface_values
    assert len(art.surface_values["us_situs_estate_nis"]) >= 1

    # The single signed FI margin concept key exists (Task 1's resolver key).
    assert "fi_margin_signed_nis" in art.surface_values


def test_liquid_and_total_net_worth_land_under_distinct_keys():
    """The body resolver's LIQUID net worth and the dashboard's TOTAL (incl. real
    estate) net worth are DIFFERENT concepts and must be recorded under distinct
    concept keys, so a divergent total-vs-liquid value never collides under one
    key (the false 11.95M-vs-14.15M contradiction the coherence gate caught)."""
    from types import SimpleNamespace

    from argosy.quality.coherence_gate import check_cross_surface_coherence
    from argosy.services.assembled_artifact import (
        CONCEPT_NET_WORTH,
        CONCEPT_NET_WORTH_TOTAL,
        _add_body_values,
        _add_dashboard_values,
    )

    # Resolver body figure: liquid/investable net worth ≈ 11.95M.
    resolved = {
        "portfolio.net_worth_nis": SimpleNamespace(
            status="resolved", value=11_950_000.0
        ),
    }
    resolved_obj = SimpleNamespace(get=lambda k: resolved.get(k))

    # Dashboard figure: total net worth incl. real estate ≈ 14.15M.
    dash = SimpleNamespace(
        retirement=SimpleNamespace(net_worth_nis=14_150_000.0),
        concentration=None,
        estate_exposure=None,
    )

    bag: dict[str, list[tuple[str, float]]] = {}
    _add_body_values(resolved_obj, bag)
    _add_dashboard_values(dash, bag)

    # Distinct keys — the resolver liquid figure stays under net_worth_nis, the
    # dashboard total under net_worth_total_nis.
    assert dict(bag[CONCEPT_NET_WORTH]) == {"body": 11_950_000.0}
    assert dict(bag[CONCEPT_NET_WORTH_TOTAL]) == {"dashboard": 14_150_000.0}
    assert "dashboard" not in dict(bag[CONCEPT_NET_WORTH])

    # And the deterministic coherence gate no longer false-flags: each concept
    # has only one contributing surface, so no contradiction is raised.
    art = SimpleNamespace(surface_values=bag)
    assert check_cross_surface_coherence(art) == []


def test_surface_values_are_floats_keyed_by_concept(session):
    """surface_values is dict[concept] -> list[(surface_name, float)]."""
    _seed_full_plan(session)
    art = assemble_plan_artifact(session, user_id="ariel")
    for concept, pairs in art.surface_values.items():
        assert isinstance(concept, str)
        for surface, value in pairs:
            assert isinstance(surface, str)
            assert isinstance(value, float)


def test_extraction_failure_is_recorded_not_swallowed(session, monkeypatch):
    """A per-surface extraction collapse must be VISIBLE on the artifact, not
    silently degraded to ABSENT (which would let the downstream coherence gate
    pass vacuously). The dashboard extraction is patched to raise; the call must
    still return an artifact (assembly never crashes the synthesis flow), the
    failure must be recorded in ``extraction_errors["dashboard"]``, and the
    export ``full_text`` must be unaffected."""
    _seed_full_plan(session)

    def _boom(*args, **kwargs):
        raise RuntimeError("dashboard compute exploded")

    # Patch where assembled_artifact looks it up (it imports inside the fn).
    monkeypatch.setattr(
        "argosy.services.wealth_dashboard.compute_wealth_dashboard", _boom
    )

    art = assemble_plan_artifact(session, user_id="ariel")

    assert isinstance(art, AssembledArtifact)
    assert "dashboard" in art.extraction_errors
    assert isinstance(art.extraction_errors["dashboard"], str)
    assert art.extraction_errors["dashboard"]  # non-empty
    # The export render path is independent and must still produce full_text.
    assert "## Wealth Dashboard" in art.full_text


# ── Prose extraction tests ────────────────────────────────────────────────────


def test_prose_extraction_fires_on_stale_hardcoded_cap():
    """Core defect: resolver/alloc_doc both say 13% cap, but the LLM hardcoded
    '12% hard cap' in plan prose.  The existing body/alloc_doc comparison
    passes silently (both canonical surfaces agree).  The prose surface catches
    the divergence.

    This is the EXACT defect that motivated prose extraction: a stale literal
    percentage written by the LLM into the plan body goes undetected until the
    prose is itself a compared surface."""
    from types import SimpleNamespace

    from argosy.services.assembled_artifact import (
        CONCEPT_NVDA_CAP,
        _extract_prose_nvda_values,
    )
    from argosy.quality.coherence_gate import check_cross_surface_coherence

    # Canonical surfaces agree on 13%.
    bag: dict = {
        CONCEPT_NVDA_CAP: [("body", 13.0), ("alloc_doc", 13.0)],
    }
    art_before = SimpleNamespace(surface_values=bag)
    # Before prose extraction: gate is silent (13 == 13).
    assert check_cross_surface_coherence(art_before) == []

    # Prose has the stale hardcode.
    prose = (
        "NVDA policy: the 8% steering target sits inside the 12% hard cap and governs "
        "deconcentration pacing."
    )
    _extract_prose_nvda_values(prose, bag)

    # Prose surface now records 12.0 pp.
    prose_vals = dict(bag[CONCEPT_NVDA_CAP])
    # prose key may appear multiple times if multiple values; check 12.0 is present.
    prose_entries = [(s, v) for s, v in bag[CONCEPT_NVDA_CAP] if s == "prose"]
    assert any(v == 12.0 for _, v in prose_entries), (
        f"Expected prose=12.0 in {bag[CONCEPT_NVDA_CAP]}"
    )

    # Gate now fires: prose(12) diverges from body(13) / alloc_doc(13).
    art_after = SimpleNamespace(surface_values=bag)
    violations = check_cross_surface_coherence(art_after)
    assert any(CONCEPT_NVDA_CAP in v.detail for v in violations), (
        f"Expected nvda_cap_pct violation, got: {violations}"
    )


def test_prose_extraction_passes_when_prose_consistent_with_canonical():
    """Prose that correctly states the canonical cap ('13% hard cap') must not
    fire — the prose surface matches the canonical surfaces."""
    from types import SimpleNamespace

    from argosy.services.assembled_artifact import (
        CONCEPT_NVDA_CAP,
        _extract_prose_nvda_values,
    )
    from argosy.quality.coherence_gate import check_cross_surface_coherence

    bag: dict = {
        CONCEPT_NVDA_CAP: [("body", 13.0), ("alloc_doc", 13.0)],
    }
    prose = (
        "NVDA concentration cap is 13.0% (canonical binding ceiling) — "
        "deconcentrate toward the 8% IPS sleeve over three years."
    )
    _extract_prose_nvda_values(prose, bag)

    # Prose finds 13.0 — consistent with canonical.
    prose_entries = [(s, v) for s, v in bag[CONCEPT_NVDA_CAP] if s == "prose"]
    assert any(v == 13.0 for _, v in prose_entries)

    art = SimpleNamespace(surface_values=bag)
    assert check_cross_surface_coherence(art) == [], (
        "Consistent prose must not fire the gate"
    )


def test_prose_extraction_no_false_positive_on_unrelated_percentages():
    """Percentages in the plan prose that are NOT about the NVDA cap or steering
    target must not be picked up.

    Tested false-positive candidates:
      - 28.5% US broad-market allocation
      - 25% CGT / 30% effective rate
      - 12% NI/health tax ceiling
      - 50% ordinary-income retention
      - 3.5% SWR
      - 59.9% NVDA current weight (how large it is, not what the cap is)
    None of these mention 'hard cap', 'binding ceiling', 'steering target',
    or 'IPS sleeve', so none should register a prose surface entry."""
    from argosy.services.assembled_artifact import (
        CONCEPT_NVDA_CAP,
        CONCEPT_NVDA_TARGET,
        _extract_prose_nvda_values,
    )

    prose = (
        "US broad-market core: 28.5% of portfolio. "
        "Section 102 capital-track sales realize 25% CGT plus surtax (~30% effective). "
        "Sell-to-cover at income-tax only (~50%) — no 12% NI/health ceiling applies. "
        "Perpetual SWR: 3.5%. "
        "NVDA is 59.9% of the tradeable book and supplies ~98% of variance."
    )
    bag: dict = {}
    _extract_prose_nvda_values(prose, bag)

    # No prose surface must be registered for either NVDA policy concept.
    assert CONCEPT_NVDA_CAP not in bag or all(
        s != "prose" for s, _ in bag.get(CONCEPT_NVDA_CAP, [])
    ), f"False positive on cap: {bag.get(CONCEPT_NVDA_CAP)}"
    assert CONCEPT_NVDA_TARGET not in bag or all(
        s != "prose" for s, _ in bag.get(CONCEPT_NVDA_TARGET, [])
    ), f"False positive on target: {bag.get(CONCEPT_NVDA_TARGET)}"


def test_prose_extraction_no_false_positive_cap_value_before_steering_target():
    """Regression: 'NVDA cap 13.0% and 8% IPS steering target' must NOT register
    13.0 as a target value — 13.0% is the cap and 8% is the target.  The '%' in
    the gap between '13.0%' and 'IPS steering target' gates this out via the
    no-intervening-percent rule in _PROSE_NVDA_TARGET_RE."""
    from argosy.services.assembled_artifact import (
        CONCEPT_NVDA_TARGET,
        _extract_prose_nvda_values,
    )

    prose = (
        "NVDA cap 13.0% and 8% IPS steering target are canonical binding values; "
        "sell/target counts derive from them on current marks."
    )
    bag: dict = {}
    _extract_prose_nvda_values(prose, bag)

    target_prose = [v for s, v in bag.get(CONCEPT_NVDA_TARGET, []) if s == "prose"]
    assert 13.0 not in target_prose, (
        f"13.0 must not be registered as a prose target; got {target_prose}"
    )
    # 8.0 IS a legitimate target extraction from '8% IPS steering target'.
    assert 8.0 in target_prose, (
        f"Expected 8.0 as prose target; got {target_prose}"
    )


def test_prose_extraction_no_false_positive_cap_inside_steering_target_phrase():
    """Regression: 'the 8% steering target inside the 13% cap' must NOT register
    13.0 as a target value — 13% follows 'inside the' and is the cap, caught by
    the negative lookahead in _PROSE_NVDA_TARGET_RE's phrase-before-number branch."""
    from argosy.services.assembled_artifact import (
        CONCEPT_NVDA_TARGET,
        _extract_prose_nvda_values,
    )

    prose = (
        "NVDA: keeping/accelerating the glide toward the 8% steering target inside "
        "the 13% cap, executed only from the capital-track-eligible pool."
    )
    bag: dict = {}
    _extract_prose_nvda_values(prose, bag)

    target_prose = [v for s, v in bag.get(CONCEPT_NVDA_TARGET, []) if s == "prose"]
    assert 13.0 not in target_prose, (
        f"13.0 must not be registered as a prose target; got {target_prose}"
    )
    assert 8.0 in target_prose, (
        f"Expected 8.0 as prose target; got {target_prose}"
    )


def test_prose_extraction_catches_binding_ceiling_variant():
    """'12% binding ceiling' (a second common phrasing for the cap) is caught."""
    from argosy.services.assembled_artifact import (
        CONCEPT_NVDA_CAP,
        _extract_prose_nvda_values,
    )

    bag: dict = {}
    prose = "The NVDA endpoint is coherent: 12% binding ceiling, 8% policy steering target."
    _extract_prose_nvda_values(prose, bag)

    cap_vals = [(s, v) for s, v in bag.get(CONCEPT_NVDA_CAP, []) if s == "prose"]
    assert any(v == 12.0 for _, v in cap_vals), f"Expected prose cap 12.0 in {cap_vals}"


def test_prose_extraction_catches_steering_target_variant():
    """'8% steering target' (target phrasing) is caught for CONCEPT_NVDA_TARGET."""
    from argosy.services.assembled_artifact import (
        CONCEPT_NVDA_TARGET,
        _extract_prose_nvda_values,
    )

    bag: dict = {}
    prose = "NVDA: accelerate deconcentration toward the 8% steering target inside the 13% hard cap."
    _extract_prose_nvda_values(prose, bag)

    tgt_vals = [(s, v) for s, v in bag.get(CONCEPT_NVDA_TARGET, []) if s == "prose"]
    assert any(v == 8.0 for _, v in tgt_vals), f"Expected prose target 8.0 in {tgt_vals}"


def test_prose_extraction_deduplicates_repeated_values():
    """Repeated identical mentions of '13% hard cap' must register prose=13.0
    only once, not as multiple entries."""
    from argosy.services.assembled_artifact import (
        CONCEPT_NVDA_CAP,
        _extract_prose_nvda_values,
    )

    bag: dict = {}
    # Repeat the phrase three times.
    prose = (
        "NVDA policy. The 13% hard cap governs. "
        "Reiteration: the 13% hard cap is the ceiling. "
        "And again, the 13% hard cap."
    )
    _extract_prose_nvda_values(prose, bag)

    prose_entries = [(s, v) for s, v in bag.get(CONCEPT_NVDA_CAP, []) if s == "prose"]
    assert prose_entries.count(("prose", 13.0)) == 1, (
        f"Expected exactly one prose=13.0, got {prose_entries}"
    )


def test_nvda_cap_and_target_extracted_from_alloc_doc_surface() -> None:
    """The ``_add_alloc_doc_values`` helper extracts NVDA cap and target from a
    TargetAllocationDoc and records them under the canonical concept keys so the
    coherence gate can compare them against the resolver body.

    This is the structural guard against the 12%/8%/13% three-value contradiction:
    if the alloc_doc says 8% and the body resolver also says 8%, no violation.
    If they diverge (stale doc or stale constant), the gate fires."""
    from types import SimpleNamespace
    from argosy.services.assembled_artifact import (
        CONCEPT_NVDA_CAP,
        CONCEPT_NVDA_TARGET,
        _add_alloc_doc_values,
    )
    from argosy.quality.coherence_gate import check_cross_surface_coherence

    # Build a minimal TargetAllocationDoc-shaped object.
    nvda_class = SimpleNamespace(
        label="Strategic single-stock (NVDA)",
        target_pct=8.0,
    )
    other_class = SimpleNamespace(
        label="US broad-market core",
        target_pct=28.5,
    )
    doc = SimpleNamespace(
        nvda_cap_pct=13.0,
        classes=[nvda_class, other_class],
    )

    bag: dict = {}
    _add_alloc_doc_values(doc, bag)

    # Cap extracted: 13.0 pp (already in %-points in the doc).
    assert CONCEPT_NVDA_CAP in bag
    assert dict(bag[CONCEPT_NVDA_CAP]) == {"alloc_doc": 13.0}

    # Target extracted from the NVDA class row: 8.0 pp.
    assert CONCEPT_NVDA_TARGET in bag
    assert dict(bag[CONCEPT_NVDA_TARGET]) == {"alloc_doc": 8.0}

    # When body matches alloc_doc, coherence gate is silent.
    bag[CONCEPT_NVDA_CAP].append(("body", 13.0))
    bag[CONCEPT_NVDA_TARGET].append(("body", 8.0))
    art = SimpleNamespace(surface_values=bag)
    assert check_cross_surface_coherence(art) == []

    # When body diverges (stale 12%), the coherence gate fires a violation.
    bag[CONCEPT_NVDA_TARGET] = [("alloc_doc", 8.0), ("body", 12.0)]
    art2 = SimpleNamespace(surface_values=bag)
    violations = check_cross_surface_coherence(art2)
    assert any(CONCEPT_NVDA_TARGET in v.detail for v in violations), (
        f"Expected a {CONCEPT_NVDA_TARGET} violation, got: {violations}"
    )


def test_prose_extraction_ignores_non_nvda_hard_cap():
    """Sol re-review: another asset's 'hard cap' must not be filed as NVDA's.

    The phrase patterns anchor on domain terms, not on NVDA, so extraction is
    scoped to NVDA-mentioning sentences first. Without that scoping this text
    would register 25.0 as the NVDA cap and the gate would fire on a
    contradiction that does not exist.
    """
    from argosy.services.assembled_artifact import (
        CONCEPT_NVDA_CAP,
        _extract_prose_nvda_values,
    )

    bag: dict = {}
    _extract_prose_nvda_values(
        "The alternatives sleeve carries a 25% hard cap. "
        "Gold is held to a 4% binding ceiling.",
        bag,
    )
    assert not bag.get(CONCEPT_NVDA_CAP), bag


def test_prose_extraction_still_catches_nvda_scoped_cap():
    """The scoping must not break the real detection."""
    from argosy.services.assembled_artifact import (
        CONCEPT_NVDA_CAP,
        _extract_prose_nvda_values,
    )

    bag: dict = {}
    _extract_prose_nvda_values("NVDA sits under a 12% hard cap this year.", bag)
    assert (12.0 in [v for _, v in bag.get(CONCEPT_NVDA_CAP, [])]), bag


def test_prose_extraction_tolerates_none_text():
    """Sol re-review: the helper must not rely on the caller's try/except."""
    from argosy.services.assembled_artifact import _extract_prose_nvda_values

    bag: dict = {}
    _extract_prose_nvda_values(None, bag)  # type: ignore[arg-type]
    assert bag == {}
