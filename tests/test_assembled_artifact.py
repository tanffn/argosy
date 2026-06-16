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
            snapshot_date=date(2026, 5, 26),
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
