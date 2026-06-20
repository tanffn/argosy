"""Phase 2a — the registry-anchored reader artifact. The reviewer is given a
canonical reconciliation block rendered from the derivation graph; pending /
fail-closed-seeded figures are NEVER shown as authoritative."""
from __future__ import annotations

from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.live_surfaces import (
    CANONICAL_SUBJECT_NODE, register_canonical_surfaces,
)
from argosy.quality.registry_review_artifact import (
    FLAG_ENV,
    _flag_on,
    assemble_registry_review_artifact,
    compute_reader_anchor,
    render_canonical_reconciliation_block,
)


def _seed_graph(values: dict[str, float]) -> DerivationGraph:
    """A hermetic graph: canonical derived nodes as INPUTs + all canonical
    surfaces, with the given values applied (others left 0.0, mimicking the
    build_base_graph fail-closed seed)."""
    g = DerivationGraph()
    for node_key in set(CANONICAL_SUBJECT_NODE.values()):
        g.add_node(Node(key=node_key, kind=NodeKind.INPUT, value=0.0))
    register_canonical_surfaces(g)
    for k, v in values.items():
        g.set_input(k, v)
    g.recompute()
    return g


_RUN117 = {
    "retirement.fi_margin_signed_nis": -167_736.0,
    "retirement.fi_crossing_year": 2027.0,
    "net_worth.liquid_nis": 11_668_397.0,
    "net_worth.investable_nis": 11_871_533.0,
    "net_worth.total_incl_residence_nis": 14_049_622.0,
    "retirement.earliest_safe_age": 46.0,
    "tax.retention_at_vest_pct": 0.50,
    "tax.retention_capital_track_pct": 0.70,
    "estate.us_situs_exposure_nis": 9_447_090.0,
}


def test_reconciliation_block_renders_canonical_surfaces():
    block = render_canonical_reconciliation_block(_seed_graph(_RUN117))
    assert "canonical reconciliation anchor" in block.lower()
    assert "NOT reached" in block                      # fi_verdict
    assert "2027" in block                             # fi_crossing
    assert "11,668,397" in block and "liquid" in block.lower()
    assert "11,871,533" in block and "investable" in block.lower()
    assert "14,049,622" in block and "total" in block.lower()
    assert "50%" in block and "70%" in block           # retention split
    assert "46" in block                               # earliest safe age
    assert "9,447,090" in block                        # us-situs


def test_empty_graph_renders_blank():
    assert render_canonical_reconciliation_block(DerivationGraph()) == ""


def test_pending_seeded_zero_values_are_never_authoritative():
    """The build_base_graph fail-closed 0.0 seed (pending scalars) must NOT leak
    into the anchor as ₪0 net worth / age 0 / a reached-with-₪0 FI verdict
    (codex plan-review blocker #1)."""
    # All canonical nodes 0.0 (the default seed) -> nothing is authoritative.
    block = render_canonical_reconciliation_block(_seed_graph({}))
    assert block == ""
    # Only some resolved: a pending net-worth basis is omitted, resolved ones show.
    partial = render_canonical_reconciliation_block(_seed_graph({
        "net_worth.liquid_nis": 11_668_397.0,
        "retirement.earliest_safe_age": 46.0,
    }))
    assert "11,668,397" in partial and "46" in partial
    assert "₪0" not in partial            # no seeded-zero net worth/exposure
    assert "age 0" not in partial.lower()
    assert "NOT reached" not in partial   # pending FI margin not shown
    assert "REACHED" not in partial


def test_resolver_status_gate_overrides_graph_value():
    """When a manifest is supplied, a NON-resolved source key is skipped even if
    the graph carries a (stale) value for it."""
    class _RV:
        def __init__(self, value, status):
            self.value, self.status = value, status

    class _Resolved:
        def __init__(self, m):
            self._m = m

        def get(self, k):
            return self._m.get(k)

    g = _seed_graph(_RUN117)
    resolved = _Resolved({
        "retirement.earliest_safe_age": _RV(46.0, "resolved"),
        # everything else pending -> must be skipped despite non-zero graph values
    })
    block = render_canonical_reconciliation_block(g, resolved=resolved)
    assert "46" in block
    assert "11,668,397" not in block      # source pending -> omitted
    assert "2027" not in block


def test_assemble_appends_anchor_append_only():
    g = _seed_graph(_RUN117)
    # trailing whitespace must be PRESERVED — base_text is an exact prefix.
    base = "# Argosy Plan Snapshot\n\n## Current Plan\nbody prose here.\n\n\n"
    out = assemble_registry_review_artifact(
        None, user_id="x", decision_run_id=0, base_text=base, graph=g, resolved=None)
    assert out.startswith(base)   # append-only: never strips existing prose
    assert "## Current Plan" in out
    assert "canonical reconciliation anchor" in out.lower()
    assert out.index("body prose here") < out.lower().index("canonical reconciliation anchor")


def test_assemble_no_canonical_surfaces_returns_base_unchanged_exactly():
    base = "# plan\n\n\n"
    out = assemble_registry_review_artifact(
        None, user_id="x", decision_run_id=0, base_text=base,
        graph=DerivationGraph(), resolved=None)
    assert out == base   # exact identity when nothing to anchor


def test_production_node_key_overrides_render_via_resolver_authority():
    """Lock the PRODUCTION mapping: surfaces registered with SUBJECT_NODE_MAP
    (portfolio.* net worth, concentration.us_situs_estate_nis graph node) render
    only when their resolver AUTHORITY key (_SURFACE_RESOLVER_SOURCE) is resolved
    (codex impl review #4)."""
    from argosy.orchestrator.flows.incremental_plan import SUBJECT_NODE_MAP
    from argosy.quality.live_surfaces import register_canonical_surfaces

    g = DerivationGraph()
    # production graph node keys (the SUBJECT_NODE_MAP overrides + defaults)
    prod_nodes = {
        "portfolio.liquid_net_worth_nis": 11_668_397.0,
        "portfolio.net_worth_nis": 11_871_533.0,
        "portfolio.total_net_worth_incl_residence_nis": 14_049_622.0,
        "concentration.us_situs_estate_nis": 9_447_090.0,
        "retirement.fi_margin_signed_nis": -167_736.0,
        "retirement.fi_crossing_year": 2027.0,
        "retirement.earliest_safe_age": 46.0,
        "tax.retention_at_vest_pct": 0.50,
        "tax.retention_capital_track_pct": 0.70,
    }
    for k, v in prod_nodes.items():
        g.add_node(Node(key=k, kind=NodeKind.INPUT, value=v))
    register_canonical_surfaces(g, subject_node_map=SUBJECT_NODE_MAP)
    g.recompute()

    class _RV:
        def __init__(self, value, status):
            self.value, self.status = value, status

    class _Resolved:
        def __init__(self, m):
            self._m = m

        def get(self, k):
            return self._m.get(k)

    # Resolve the net-worth bases + us-situs AUTHORITY key; leave the rest pending.
    resolved = _Resolved({
        "portfolio.liquid_net_worth_nis": _RV(11_668_397.0, "resolved"),
        "portfolio.net_worth_nis": _RV(11_871_533.0, "resolved"),
        "portfolio.total_net_worth_incl_residence_nis": _RV(14_049_622.0, "resolved"),
        "concentration.us_situs_estate_exposure_nis": _RV(9_447_090.0, "resolved"),
    })
    block = render_canonical_reconciliation_block(g, resolved=resolved)
    assert "11,668,397" in block and "11,871,533" in block and "14,049,622" in block
    assert "9,447,090" in block          # us-situs authority key resolved -> shown
    assert "NOT reached" not in block     # fi margin authority pending -> omitted
    assert "46" not in block              # age authority pending -> omitted

    # Now mark the us-situs authority key PENDING: the nonzero graph value omitted.
    resolved2 = _Resolved({
        "concentration.us_situs_estate_exposure_nis": _RV(9_447_090.0, "pending"),
    })
    block2 = render_canonical_reconciliation_block(g, resolved=resolved2)
    assert "9,447,090" not in block2


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv(FLAG_ENV, raising=False)

    class _S:  # settings object with no registry-review attr
        pass

    monkeypatch.setattr("argosy.config.get_settings", lambda: _S())
    assert _flag_on() is False
    for falsy in ("0", "off", "no", "false"):
        monkeypatch.setenv(FLAG_ENV, falsy)
        assert _flag_on() is False
    for truthy in ("1", "true", "yes", "on"):
        monkeypatch.setenv(FLAG_ENV, truthy)
        assert _flag_on() is True


def test_registry_review_flag_defaults_on(monkeypatch):
    """The live default is ON (flipped after the run-117 A/B). Env still overrides;
    a future silent flip-back is caught here."""
    monkeypatch.delenv(FLAG_ENV, raising=False)
    from argosy.config import Settings
    assert Settings().argosy_registry_review_artifact is True
    assert _flag_on() is True  # no env override -> settings default (True)


def test_compute_anchor_off_is_empty(monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "0")

    def _boom(*a, **k):  # pragma: no cover — must not be called
        raise AssertionError("graph built while flag OFF")

    assert compute_reader_anchor(
        None, user_id="x", decision_run_id=0, _builder=_boom) == ""


def test_compute_anchor_on_returns_block(monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "1")
    g = _seed_graph(_RUN117)
    out = compute_reader_anchor(
        None, user_id="x", decision_run_id=0, _builder=lambda *a, **k: g)
    assert "canonical reconciliation anchor" in out.lower()
    assert "11,668,397" in out


def test_compute_anchor_failsoft_returns_empty(monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "1")

    def _boom(*a, **k):
        raise RuntimeError("graph build failed")

    out = compute_reader_anchor(
        None, user_id="x", decision_run_id=0, _builder=_boom)
    assert out == ""  # fail-soft: reader still runs on the from-scratch artifact


def test_reader_prompt_isolates_anchor_as_oracle_section():
    """The reader prompt must place the anchor in its OWN reviewer-only section
    with 'do not critique' framing — never inside the client-facing artifact
    section (codex impl review blocker #1)."""
    from argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader import (
        _build_prompt,
    )
    anchor = "- Net worth (liquid basis): X"
    prompt = _build_prompt(
        assembled_artifact="CLIENT PROSE BODY",
        external_context="today",
        prior_plan_text="",
        canonical_anchor=anchor,
    )
    assert "REVIEWER-ONLY CANONICAL RECONCILIATION ANCHOR" in prompt
    assert "Do NOT critique this section" in prompt
    assert anchor in prompt
    # the anchor sits AFTER the client artifact, in its own section.
    assert prompt.index("CLIENT PROSE BODY") < prompt.index("REVIEWER-ONLY CANONICAL")
    # with no anchor, the section shows its sentinel, not a bare placeholder.
    p2 = _build_prompt(
        assembled_artifact="BODY", external_context="t", prior_plan_text="",
        canonical_anchor=None)
    assert "no canonical registry anchor on this run" in p2


def test_build_reader_anchor_resolves_manifest_exactly_once(monkeypatch):
    """The production builder must resolve the manifest ONCE and seed the graph
    from THAT manifest — never two resolver passes (codex impl review #2)."""
    import argosy.services.plan_numeric_resolver as resolver_mod
    import argosy.orchestrator.flows.incremental_plan as inc

    calls = {"resolve": 0, "build": 0, "seeded_with_values": 0}

    class _RV:
        status, value = "resolved", 12_000_000.0

    class _Resolved:
        def get(self, k):
            return _RV()

    def _fake_resolve(*a, **k):
        calls["resolve"] += 1
        return _Resolved()

    def _fake_build(session, user_id, *, decision_run_id, resolver_values=None):
        calls["build"] += 1
        if resolver_values is not None:
            calls["seeded_with_values"] += 1
        return _seed_graph(_RUN117)

    monkeypatch.setattr(resolver_mod, "resolve_plan_numbers", _fake_resolve)
    monkeypatch.setattr(inc, "build_base_graph", _fake_build)

    from argosy.quality.registry_review_artifact import build_reader_anchor_block

    block = build_reader_anchor_block(None, user_id="x", decision_run_id=0)
    assert calls["resolve"] == 1            # resolved exactly once
    assert calls["build"] == 1
    assert calls["seeded_with_values"] == 1  # graph seeded from THAT manifest
    assert "canonical reconciliation anchor" in block.lower()
