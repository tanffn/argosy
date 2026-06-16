from datetime import date
from types import SimpleNamespace

from argosy.quality.gate_types import GateCheck
from argosy.quality.instage_gate import run_deterministic_gate_instage


class _Artifact:
    # Two surfaces disagree on the same concept -> cross-surface violation.
    full_text = "Net worth is 11.95M in the body and 14.15M on the dashboard."
    surface_values = {"net_worth_nis": [("body", 11_950_000.0), ("dashboard", 14_150_000.0)]}
    extraction_errors: dict = {}


def test_instage_gate_runs_suite_and_returns_violations():
    draft = SimpleNamespace(
        id=42, user_id="u1", decision_run_id=106,
        horizon_long_md="Net worth is 11.95M.", horizon_medium_md="", horizon_short_md="",
        target_allocation_json='{"nvda_cap_pct": 18.0}',
        sections_json="[]",
    )
    verdict = run_deterministic_gate_instage(
        session=object(), user_id="u1", draft=draft, decision_run_id=106,
        today=date(2026, 6, 16),
        assemble=lambda session, user_id: _Artifact(),
        resolve=lambda session, user_id, decision_run_id: None,
        current_plan=lambda session, user_id: SimpleNamespace(target_allocation_json='{"nvda_cap_pct": 13.0}'),
        snapshot_date=date(2026, 6, 16),
    )
    # The assembled artifact's cross-surface divergence is caught deterministically.
    assert verdict.violations[GateCheck.CROSS_SURFACE_COHERENCE]


def test_instage_gate_clean_artifact_passes():
    class _Clean:
        full_text = "All consistent."
        surface_values = {"net_worth_nis": [("body", 11_950_000.0), ("dashboard", 11_950_000.0)]}
        extraction_errors: dict = {}

    draft = SimpleNamespace(
        id=1, user_id="u1", decision_run_id=1,
        horizon_long_md="All consistent.", horizon_medium_md="", horizon_short_md="",
        target_allocation_json=None, sections_json="[]",
    )
    verdict = run_deterministic_gate_instage(
        session=object(), user_id="u1", draft=draft, decision_run_id=1,
        today=date(2026, 6, 16),
        assemble=lambda session, user_id: _Clean(),
        resolve=lambda session, user_id, decision_run_id: None,
        current_plan=lambda session, user_id: None,
        snapshot_date=date(2026, 6, 16),
    )
    assert verdict.passes


def test_helper_exported_on_flow_package():
    from argosy.orchestrator.flows import plan_synthesis as flow
    assert hasattr(flow, "run_deterministic_gate_instage")


from datetime import date as _d
from types import SimpleNamespace as _NS

from argosy.quality.gate_types import GateCheck as _GC
from argosy.quality.instage_gate import run_deterministic_gate_instage as _run


def test_default_path_calls_services_by_keyword_not_positional():
    """Regression for the live run-107 bug: the real services
    (assemble_plan_artifact(session, *, user_id);
    resolve_plan_numbers(session, *, user_id, decision_run_id)) take user_id /
    decision_run_id as KEYWORD-ONLY. Stubs here mirror that signature; a
    positional call inside the helper would raise -> degrade to artifact=None and
    SKIP the artifact-based invariants (silent). Keyword-only stubs prove the
    helper passes by keyword (the artifact IS used -> the divergence is caught)."""
    class _Art:
        full_text = "x"
        surface_values = {"net_worth_nis": [("body", 11_950_000.0), ("dashboard", 14_150_000.0)]}
        extraction_errors: dict = {}

    def _assemble(session, *, user_id):           # keyword-only, like the real fn
        return _Art()

    def _resolve(session, *, user_id, decision_run_id):  # keyword-only, like the real fn
        return None

    draft = SimpleNamespace(
        id=7, user_id="u", decision_run_id=7,
        horizon_long_md="x", horizon_medium_md="", horizon_short_md="",
        target_allocation_json=None, sections_json="[]",
    )
    verdict = run_deterministic_gate_instage(
        session=object(), user_id="u", draft=draft, decision_run_id=7,
        today=date(2026, 6, 16),
        assemble=_assemble, resolve=_resolve,
        current_plan=lambda session, user_id: None,
        snapshot_date=date(2026, 6, 16),
    )
    assert verdict.violations[GateCheck.CROSS_SURFACE_COHERENCE], (
        "the helper must call assemble/resolve by KEYWORD so the live default "
        "path (keyword-only services) runs the artifact-based invariants"
    )


def test_ips_style_divergence_caught_instage_not_left_to_reader():
    class _Art:
        full_text = "IPS"
        # same concept, two surfaces, >1% apart -> deterministic catch
        surface_values = {"nvda_weight_pct": [("body", 12.0), ("dashboard", 13.2)]}
        extraction_errors: dict = {}

    draft = _NS(id=9, user_id="u", decision_run_id=9,
                horizon_long_md="x", horizon_medium_md="", horizon_short_md="",
                target_allocation_json=None, sections_json="[]")
    verdict = _run(
        session=object(), user_id="u", draft=draft, decision_run_id=9,
        today=_d(2026, 6, 16),
        assemble=lambda session, user_id: _Art(),
        resolve=lambda session, user_id, decision_run_id: None,
        current_plan=lambda session, user_id: None,
        snapshot_date=_d(2026, 6, 16),
    )
    assert verdict.violations[_GC.CROSS_SURFACE_COHERENCE], (
        "a cross-surface divergence must be caught by the in-stage deterministic "
        "gate, not deferred to the LLM reader"
    )
