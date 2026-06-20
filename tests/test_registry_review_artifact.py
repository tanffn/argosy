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
    maybe_anchor_reader_artifact,
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


def test_assemble_appends_anchor_to_base_text():
    g = _seed_graph(_RUN117)
    base = "# Argosy Plan Snapshot\n\n## Current Plan\nbody prose here.\n"
    out = assemble_registry_review_artifact(
        None, user_id="x", decision_run_id=0, base_text=base, graph=g, resolved=None)
    assert out.startswith("# Argosy Plan Snapshot")
    assert "## Current Plan" in out
    assert "canonical reconciliation anchor" in out.lower()
    assert out.index("body prose here") < out.lower().index("canonical reconciliation anchor")


def test_assemble_no_canonical_surfaces_returns_base_unchanged_exactly():
    base = "# plan\n\n\n"
    out = assemble_registry_review_artifact(
        None, user_id="x", decision_run_id=0, base_text=base,
        graph=DerivationGraph(), resolved=None)
    assert out == base   # exact identity (no rstrip) when nothing to anchor


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


def test_maybe_anchor_off_is_identity(monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "0")
    base = "# plan body\n"

    def _boom(*a, **k):  # pragma: no cover — must not be called
        raise AssertionError("graph built while flag OFF")

    out = maybe_anchor_reader_artifact(
        None, user_id="x", decision_run_id=0, base_text=base, _builder=_boom)
    assert out == base


def test_maybe_anchor_on_appends(monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "1")
    g = _seed_graph(_RUN117)
    out = maybe_anchor_reader_artifact(
        None, user_id="x", decision_run_id=0, base_text="# plan body\n",
        _builder=lambda *a, **k: g)
    assert "canonical reconciliation anchor" in out.lower()


def test_maybe_anchor_failsoft_keeps_base(monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "1")
    base = "# plan body\n"

    def _boom(*a, **k):
        raise RuntimeError("graph build failed")

    out = maybe_anchor_reader_artifact(
        None, user_id="x", decision_run_id=0, base_text=base, _builder=_boom)
    assert out == base  # fail-soft: never lose the from-scratch artifact
