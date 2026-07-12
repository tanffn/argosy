"""Item I — READ-time ``{{fact:key}}`` rendering + staleness seam + literal gate.

Zero live LLM. Offline stubs for the resolver; optional DB only for the
monitor-flag write path.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from argosy.quality.fact_registry import PENDING_LABEL, format_fact
from argosy.quality.gate_types import GateCheck
from argosy.quality.numeric_source_gate import check_fact_literal_should_be_token
from argosy.services.fact_token_render import (
    PLAN_LOGIC_STALE_KIND,
    clear_fact_render_cache,
    detect_claim_boundary_crossings,
    render_plan_facts,
    write_plan_logic_stale_flag,
)
from argosy.services.plan_numeric_resolver import (
    ResolvedPlanNumbers,
    ResolvedValue,
)


def _rv(key: str, value: float, unit: str = "nis") -> ResolvedValue:
    return ResolvedValue(
        key=key,
        value=value,
        unit=unit,
        status="resolved",
        source_locator=f"{key} (test)",
    )


def _resolved(**vals: float) -> ResolvedPlanNumbers:
    units = {
        "portfolio.liquid_net_worth_nis": "nis",
        "retirement.fi_target_nis": "nis",
        "retirement.fi_total_capital_nis": "nis",
        "retirement.fi_margin_signed_nis": "nis",
        "retirement.fi_shock_net_worth_nis": "nis",
        "retirement.fi_fx_shock_net_worth_nis": "nis",
        "retirement.fi_perpetuity_nis": "nis",
        "retirement.fi_age": "age",
        "fx.usd_nis": "fx",
    }
    out = {
        k: _rv(k, v, units.get(k, "nis"))
        for k, v in vals.items()
    }
    return ResolvedPlanNumbers(values=out)


def _plan(**fields) -> SimpleNamespace:
    defaults = dict(
        id=42,
        horizon_long_md=None,
        horizon_medium_md=None,
        horizon_short_md=None,
        sections_json=None,
        narrative_json=None,
    )
    defaults.update(fields)
    return SimpleNamespace(**defaults)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_fact_render_cache()
    yield
    clear_fact_render_cache()


# ---------------------------------------------------------------------------
# RENDERER
# ---------------------------------------------------------------------------


def test_render_resolves_tokens_with_provenance(monkeypatch):
    resolved = _resolved(
        **{
            "portfolio.liquid_net_worth_nis": 12_500_000.0,
            "retirement.fi_target_nis": 17_300_000.0,
        }
    )
    monkeypatch.setattr(
        "argosy.services.plan_numeric_resolver.resolve_plan_numbers",
        lambda *a, **k: resolved,
    )
    monkeypatch.setattr(
        "argosy.services.fact_token_render._latest_snapshot_id",
        lambda *a, **k: 7,
    )
    plan = _plan(
        horizon_long_md=(
            "Liquid NW is {{fact:portfolio.liquid_net_worth_nis}}; "
            "FI target {{fact:retirement.fi_target_nis}}."
        ),
    )
    session = MagicMock()
    bundle = render_plan_facts(
        session, user_id="ariel", plan_version=plan, write_staleness_flag=False,
    )
    assert "₪12.50M" in (bundle.horizon_long_md or "")
    assert "₪17.30M" in (bundle.horizon_long_md or "")
    assert "{{fact:" not in (bundle.horizon_long_md or "")
    assert bundle.provenance["portfolio.liquid_net_worth_nis"]["status"] == "resolved"
    assert bundle.provenance["portfolio.liquid_net_worth_nis"]["value"] == 12_500_000.0
    assert "liquid_net_worth" in bundle.provenance["portfolio.liquid_net_worth_nis"][
        "source_locator"
    ]
    assert bundle.pending_keys == []
    assert bundle.snapshot_id == 7


def test_unresolvable_key_renders_pending_and_logs(monkeypatch, caplog):
    resolved = _resolved(**{"portfolio.liquid_net_worth_nis": 10_000_000.0})
    monkeypatch.setattr(
        "argosy.services.plan_numeric_resolver.resolve_plan_numbers",
        lambda *a, **k: resolved,
    )
    monkeypatch.setattr(
        "argosy.services.fact_token_render._latest_snapshot_id",
        lambda *a, **k: 1,
    )
    plan = _plan(
        horizon_long_md="Missing: {{fact:retirement.fi_target_nis}}.",
    )
    with caplog.at_level(logging.WARNING, logger="argosy.services.fact_token_render"):
        bundle = render_plan_facts(
            MagicMock(),
            user_id="ariel",
            plan_version=plan,
            write_staleness_flag=False,
        )
    assert PENDING_LABEL in (bundle.horizon_long_md or "")
    assert "retirement.fi_target_nis" in bundle.pending_keys
    assert any("unresolvable" in r.message for r in caplog.records)


def test_sections_json_and_narrative_render(monkeypatch):
    resolved = _resolved(**{"fx.usd_nis": 2.944})
    monkeypatch.setattr(
        "argosy.services.plan_numeric_resolver.resolve_plan_numbers",
        lambda *a, **k: resolved,
    )
    monkeypatch.setattr(
        "argosy.services.fact_token_render._latest_snapshot_id",
        lambda *a, **k: 2,
    )
    sections = [
        {
            "section_id": "fx",
            "horizon": "long",
            "title": "FX",
            "body_md": "FX is {{fact:fx.usd_nis}}.",
        }
    ]
    plan = _plan(
        sections_json=json.dumps(sections),
        narrative_json=json.dumps(
            {"narrative_md_en": "Rate {{fact:fx.usd_nis}} holds."}
        ),
    )
    bundle = render_plan_facts(
        MagicMock(),
        user_id="ariel",
        plan_version=plan,
        write_staleness_flag=False,
    )
    sj = json.loads(bundle.sections_json or "[]")
    assert "2.944" in sj[0]["body_md"]
    assert "2.944" in (bundle.narrative_md or "")
    assert bundle.provenance["fx.usd_nis"]["status"] == "resolved"


def test_cache_keyed_by_plan_and_snapshot(monkeypatch):
    calls: list[int] = []

    def fake_resolve(*a, **k):
        calls.append(1)
        return _resolved(**{"portfolio.liquid_net_worth_nis": 11_000_000.0})

    monkeypatch.setattr(
        "argosy.services.plan_numeric_resolver.resolve_plan_numbers",
        fake_resolve,
    )
    snap = {"id": 10}
    monkeypatch.setattr(
        "argosy.services.fact_token_render._latest_snapshot_id",
        lambda *a, **k: snap["id"],
    )
    plan = _plan(
        id=99,
        horizon_long_md="NW {{fact:portfolio.liquid_net_worth_nis}}.",
    )
    session = MagicMock()
    b1 = render_plan_facts(
        session, user_id="ariel", plan_version=plan, write_staleness_flag=False,
    )
    b2 = render_plan_facts(
        session, user_id="ariel", plan_version=plan, write_staleness_flag=False,
    )
    assert b1 is b2
    assert len(calls) == 1
    # New snapshot → cache miss → re-resolve
    snap["id"] = 11
    b3 = render_plan_facts(
        session, user_id="ariel", plan_version=plan, write_staleness_flag=False,
    )
    assert b3 is not b1
    assert len(calls) == 2
    assert b3.snapshot_id == 11


# ---------------------------------------------------------------------------
# GATE — literal matching a fact key should be a token
# ---------------------------------------------------------------------------


def test_literal_matching_fact_is_violation():
    resolved = _resolved(**{"retirement.fi_target_nis": 17_300_000.0})
    md = {"long": "FI target is **₪17.30M** on the derived path."}
    viols = check_fact_literal_should_be_token(md, resolved)
    assert viols, "literal matching a registry fact must violate"
    assert all(v.check is GateCheck.FACT_PLACEHOLDER_PROTOCOL for v in viols)
    assert any("placeholder protocol" in v.detail for v in viols)
    assert any("retirement.fi_target_nis" in v.detail for v in viols)


def test_tokenised_body_not_flagged_as_literal():
    """Bodies that already emit tokens are mid-protocol — no digit to match."""
    resolved = _resolved(**{"retirement.fi_target_nis": 17_300_000.0})
    md = {"long": "FI target is {{fact:retirement.fi_target_nis}}."}
    # find_unauthorized_numbers skips inside {{fact:}}; nothing to match.
    assert check_fact_literal_should_be_token(md, resolved) == []


def test_fact_literal_demoted_to_warn_when_enforce_off(monkeypatch):
    from argosy.api.routes.plan import _gate_blocking_checks
    from argosy.quality.gate_types import GateVerdict, GateViolation

    monkeypatch.setenv("ARGOSY_FACT_LITERAL_GATE_ENFORCE", "0")
    from argosy.config import get_settings

    get_settings.cache_clear()
    try:
        v = GateViolation(
            check=GateCheck.FACT_PLACEHOLDER_PROTOCOL,
            detail=(
                "literal nis `₪17.30M` matches registry fact "
                "`retirement.fi_target_nis` — emit {{fact:retirement.fi_target_nis}} "
                "instead of typing digits (placeholder protocol)"
            ),
            locator="horizon=long",
        )
        viol_map = {c: [] for c in GateCheck}
        viol_map[GateCheck.FACT_PLACEHOLDER_PROTOCOL] = [v]
        gv = GateVerdict(violations=viol_map)
        blocking, warned = _gate_blocking_checks(
            gv, SimpleNamespace(sections_json=None),
        )
        assert GateCheck.FACT_PLACEHOLDER_PROTOCOL not in blocking
        assert GateCheck.FACT_PLACEHOLDER_PROTOCOL in warned
        # Grounding check stays clean — rederivation input undisturbed.
        assert GateCheck.HEADLINE_NUMERIC_SOURCE not in blocking
        assert GateCheck.HEADLINE_NUMERIC_SOURCE not in warned
    finally:
        get_settings.cache_clear()


def test_fact_literal_blocks_when_enforce_on(monkeypatch):
    from argosy.api.routes.plan import _gate_blocking_checks
    from argosy.quality.gate_types import GateVerdict, GateViolation

    monkeypatch.setenv("ARGOSY_FACT_LITERAL_GATE_ENFORCE", "1")
    from argosy.config import get_settings

    get_settings.cache_clear()
    try:
        v = GateViolation(
            check=GateCheck.FACT_PLACEHOLDER_PROTOCOL,
            detail=(
                "literal nis `₪17.30M` matches registry fact "
                "`retirement.fi_target_nis` — emit {{fact:...}} "
                "(placeholder protocol)"
            ),
            locator="horizon=long",
        )
        viol_map = {c: [] for c in GateCheck}
        viol_map[GateCheck.FACT_PLACEHOLDER_PROTOCOL] = [v]
        gv = GateVerdict(violations=viol_map)
        blocking, warned = _gate_blocking_checks(
            gv, SimpleNamespace(sections_json="[]"),
        )
        assert GateCheck.FACT_PLACEHOLDER_PROTOCOL in blocking
        assert GateCheck.FACT_PLACEHOLDER_PROTOCOL not in warned
    finally:
        monkeypatch.delenv("ARGOSY_FACT_LITERAL_GATE_ENFORCE", raising=False)
        get_settings.cache_clear()


def test_matching_literal_keeps_headline_numeric_clean_for_rederivation():
    """Regression 96dff85: grounded matching literals must NOT dirty HNS.

    Rederivation clears on HEADLINE_NUMERIC_SOURCE alone. Folding the
    placeholder-protocol finding into that check blocked /accept on v89-style
    literal bodies that still match the resolver.
    """
    from argosy.quality.numeric_source_gate import check_headline_numeric_source

    resolved = _resolved(
        **{
            "retirement.fi_target_nis": 17_300_000.0,
            "retirement.fi_age": 49.0,
        }
    )
    md = {
        "long": "Derived FI target: **₪17.30M**; you could retire at age 49.\n",
    }
    assert check_headline_numeric_source(md, resolved) == []
    lit = check_fact_literal_should_be_token(md, resolved)
    assert lit
    assert all(v.check is GateCheck.FACT_PLACEHOLDER_PROTOCOL for v in lit)



# ---------------------------------------------------------------------------
# STALENESS SEAM
# ---------------------------------------------------------------------------


def test_claim_boundary_cross_when_margin_negative():
    resolved = _resolved(**{"retirement.fi_margin_signed_nis": -50_000.0})
    text = "Capital sufficiency: reached on the base path."
    findings = detect_claim_boundary_crossings(text, resolved)
    assert findings
    assert any("plan logic stale" in f.detail for f in findings)
    assert findings[0].fact_key == "retirement.fi_margin_signed_nis"


def test_claim_boundary_clean_when_margin_positive():
    resolved = _resolved(**{"retirement.fi_margin_signed_nis": 80_000.0})
    text = "Capital sufficiency: reached on the base path."
    assert detect_claim_boundary_crossings(text, resolved) == []


def test_write_plan_logic_stale_flag(client_with_db):
    from argosy.state.models import MonitorFlag, User

    SF = client_with_db.app.state.session_factory
    with SF() as s:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel"))
            s.commit()
    findings = detect_claim_boundary_crossings(
        "FI: reached.",
        _resolved(**{"retirement.fi_margin_signed_nis": -1.0}),
    )
    assert findings
    with SF() as s:
        row = write_plan_logic_stale_flag(
            s, user_id="ariel", plan_version_id=77, findings=findings,
        )
        s.commit()
        assert row is not None
        assert row.kind == PLAN_LOGIC_STALE_KIND
        payload = json.loads(row.payload)
        assert payload["message"] == "plan logic stale — corrective needed"
    with SF() as s:
        flags = (
            s.query(MonitorFlag)
            .filter(MonitorFlag.kind == PLAN_LOGIC_STALE_KIND)
            .all()
        )
        assert len(flags) == 1


# ---------------------------------------------------------------------------
# E2E OFFLINE — tokens + changed snapshot → new numbers + staleness
# ---------------------------------------------------------------------------


def test_e2e_token_plan_rerenders_on_snapshot_change_and_flags_stale(monkeypatch):
    """Plan text stays tokenised; live book changes → new digits + staleness."""
    books = {
        1: _resolved(
            **{
                "portfolio.liquid_net_worth_nis": 12_000_000.0,
                "retirement.fi_margin_signed_nis": 100_000.0,
                "retirement.fi_total_capital_nis": 12_000_000.0,
            }
        ),
        2: _resolved(
            **{
                "portfolio.liquid_net_worth_nis": 9_500_000.0,  # post-trade
                "retirement.fi_margin_signed_nis": -40_000.0,  # sufficiency flips
                "retirement.fi_total_capital_nis": 9_500_000.0,
            }
        ),
    }
    snap = {"id": 1}

    def fake_resolve(*a, **k):
        return books[snap["id"]]

    monkeypatch.setattr(
        "argosy.services.plan_numeric_resolver.resolve_plan_numbers",
        fake_resolve,
    )
    monkeypatch.setattr(
        "argosy.services.fact_token_render._latest_snapshot_id",
        lambda *a, **k: snap["id"],
    )
    # Avoid DB write on MagicMock session for the e2e render path
    monkeypatch.setattr(
        "argosy.services.fact_token_render.write_plan_logic_stale_flag",
        lambda *a, **k: None,
    )

    plan = _plan(
        id=501,
        horizon_long_md=(
            "Liquid NW {{fact:portfolio.liquid_net_worth_nis}}. "
            "Capital sufficiency: reached on the base path."
        ),
        sections_json=json.dumps([
            {
                "section_id": "fi",
                "horizon": "long",
                "title": "FI",
                "body_md": "Margin context at {{fact:portfolio.liquid_net_worth_nis}}.",
            }
        ]),
    )
    session = MagicMock()
    b1 = render_plan_facts(
        session, user_id="ariel", plan_version=plan, write_staleness_flag=True,
    )
    assert "₪12.00M" in (b1.horizon_long_md or "")
    assert b1.provenance["portfolio.liquid_net_worth_nis"]["value"] == 12_000_000.0
    assert b1.staleness == []

    # Trade lands → new snapshot; same tokenised plan text
    snap["id"] = 2
    b2 = render_plan_facts(
        session, user_id="ariel", plan_version=plan, write_staleness_flag=True,
    )
    assert "₪9.50M" in (b2.horizon_long_md or "")
    assert "₪12.00M" not in (b2.horizon_long_md or "")
    assert b2.provenance["portfolio.liquid_net_worth_nis"]["value"] == 9_500_000.0
    assert b2.staleness, "margin flip under 'reached' claim must raise staleness"
    assert any("plan logic stale" in f.detail for f in b2.staleness)
    sj = json.loads(b2.sections_json or "[]")
    assert "₪9.50M" in sj[0]["body_md"]
    # Persisted plan text unchanged (tokens intact on the plan object)
    assert "{{fact:portfolio.liquid_net_worth_nis}}" in plan.horizon_long_md


def test_format_fact_display_matches_renderer_expectation():
    """Sanity: gate + renderer share format_fact so literals match tokens."""
    assert format_fact(17_300_000.0, "nis", display="nis_millions") == "₪17.30M"


# ---------------------------------------------------------------------------
# SYNTHESIZER CONTRACT (offline — scaffolding only)
# ---------------------------------------------------------------------------


def test_synth_numbers_block_emits_fact_tokens_when_protocol_on(monkeypatch):
    from argosy.services.plan_numeric_resolver import render_numbers_for_synth

    monkeypatch.setenv("ARGOSY_FACT_PLACEHOLDERS", "1")
    from argosy.config import get_settings

    get_settings.cache_clear()
    try:
        resolved = _resolved(
            **{
                "portfolio.liquid_net_worth_nis": 12_000_000.0,
                "retirement.fi_target_nis": 17_300_000.0,
                "retirement.fi_margin_signed_nis": 50_000.0,
            }
        )
        block = render_numbers_for_synth(resolved)
        assert "PLACEHOLDER PROTOCOL" in block
        assert "{{fact:portfolio.liquid_net_worth_nis}}" in block
        assert "{{fact:retirement.fi_target_nis}}" in block
    finally:
        get_settings.cache_clear()


def test_synth_numbers_block_omits_tokens_when_protocol_off(monkeypatch):
    from argosy.services.plan_numeric_resolver import render_numbers_for_synth

    monkeypatch.setenv("ARGOSY_FACT_PLACEHOLDERS", "0")
    from argosy.config import get_settings

    get_settings.cache_clear()
    try:
        resolved = _resolved(**{"portfolio.liquid_net_worth_nis": 12_000_000.0})
        block = render_numbers_for_synth(resolved)
        assert "PLACEHOLDER PROTOCOL" not in block
        assert "{{fact:portfolio.liquid_net_worth_nis}}" not in block
    finally:
        get_settings.cache_clear()
