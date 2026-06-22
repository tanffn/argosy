"""Phase 6 — feature flag + gate enforcement at /accept.

Covers ``ARGOSY_PLAN_GATE_ENFORCE`` env var + the gate-check wired
into ``POST /api/plan/draft/{draft_id}/accept``:

- flag default is True (enforce mode; T2.6)
- warning mode: gate failures land in ``gate_warning`` on
  ``AcceptResponse``, accept proceeds.
- enforce mode: gate failures raise 422 before the role flip.
- ``?override_gate=true`` bypasses the check in enforce mode
  (audit-logged via ``plan.draft.accepted.override``).
- gate is silently skipped when neither horizon MD nor JSON is
  populated (defensive).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from argosy.state.models import PlanVersion, User


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "plan_v20_horizons"


@pytest.fixture(autouse=True)
def _restore_gate_settings():
    """Restore the cached Settings after each test in this module.

    These tests flip ``ARGOSY_PLAN_GATE_ENFORCE`` and call
    ``reload_settings()`` to populate the lru_cache. monkeypatch reverts
    the env var on teardown, but the *cached* Settings object keeps the
    flipped value — leaking enforce-mode into unrelated tests that run
    later in the same process. Re-reading the (now-reverted) env on
    teardown restores the default (enforce=False) so the leak can't
    cross module boundaries.
    """
    yield
    from argosy.config import reload_settings
    reload_settings()


# ---------------------------------------------------------------------------
# Settings — env var default + override
# ---------------------------------------------------------------------------

def test_settings_plan_gate_enforce_default_true(monkeypatch):
    """Default is True (T2.6) — the trust contract is enforced at /accept: a
    draft whose numbers don't trace to the plan is BLOCKED, not just warned."""
    monkeypatch.delenv("ARGOSY_PLAN_GATE_ENFORCE", raising=False)
    from argosy.config import reload_settings
    settings = reload_settings()
    assert settings.plan_gate_enforce is True


def test_settings_plan_gate_enforce_env_var_true(monkeypatch):
    monkeypatch.setenv("ARGOSY_PLAN_GATE_ENFORCE", "true")
    from argosy.config import reload_settings
    settings = reload_settings()
    assert settings.plan_gate_enforce is True


def test_settings_plan_gate_enforce_env_var_false(monkeypatch):
    monkeypatch.setenv("ARGOSY_PLAN_GATE_ENFORCE", "false")
    from argosy.config import reload_settings
    settings = reload_settings()
    assert settings.plan_gate_enforce is False


# ---------------------------------------------------------------------------
# /accept gate path — fixtures
# ---------------------------------------------------------------------------

def _insert_draft(
    client_with_db,
    *,
    horizon_long_md: str,
    horizon_medium_md: str,
    horizon_short_md: str,
    horizon_long_json: str = "",
    horizon_medium_json: str = "",
    horizon_short_json: str = "",
) -> int:
    """Insert a draft PlanVersion into the test DB and return its id."""
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        draft = PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="phase6-test",
            raw_markdown="",
            horizon_long_md=horizon_long_md,
            horizon_medium_md=horizon_medium_md,
            horizon_short_md=horizon_short_md,
            horizon_long_json=horizon_long_json or '{}',
            horizon_medium_json=horizon_medium_json or '{}',
            horizon_short_json=horizon_short_json or '{}',
        )
        sess.add(draft)
        sess.commit()
        return draft.id
    finally:
        sess.close()


CLEAN_MD_LONG = "# Long horizon\n\n**Posture.** Steady growth across diversified holdings.\n"
CLEAN_MD_MEDIUM = "# Medium horizon\n\n**Posture.** Continue NVDA glide-down to 15 percent.\n"
CLEAN_MD_SHORT = "# Short horizon\n\n**Posture.** Park RSU vest proceeds in short-Treasury.\n"


def _v20_fixture_md(name: str) -> str:
    return (FIXTURE_DIR / f"{name}.md").read_text(encoding="utf-8")


def _v20_fixture_json(name: str) -> str:
    return (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Accept path — warning mode (default)
# ---------------------------------------------------------------------------

def test_accept_clean_plan_no_gate_warning(client_with_db, monkeypatch):
    """Clean horizon MD → gate passes → gate_warning is None."""
    monkeypatch.setenv("ARGOSY_PLAN_GATE_ENFORCE", "false")
    from argosy.config import reload_settings
    reload_settings()
    draft_id = _insert_draft(
        client_with_db,
        horizon_long_md=CLEAN_MD_LONG,
        horizon_medium_md=CLEAN_MD_MEDIUM,
        horizon_short_md=CLEAN_MD_SHORT,
    )
    r = client_with_db.post(
        f"/api/plan/draft/{draft_id}/accept?user_id=ariel"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["new_current_id"] == draft_id
    # Clean MD only checks history+jargon; sections-coverage/evidence
    # will still flag because v20-shape PlanSynthesisOutput has no
    # `sections`. So gate_warning IS populated even for "clean MD"
    # until Phase 4 distillate + synth emit real sections. The clean
    # case here just asserts the warning surface works correctly.
    # (A future test will assert gate_warning is None for a fully
    # Phase-3-compliant draft.)


def test_accept_v20_draft_warning_mode_surfaces_violations(
    client_with_db, monkeypatch,
):
    """v20 fixture has many history+jargon violations; warning mode
    accepts but surfaces them on gate_warning."""
    monkeypatch.setenv("ARGOSY_PLAN_GATE_ENFORCE", "false")
    from argosy.config import reload_settings
    reload_settings()
    draft_id = _insert_draft(
        client_with_db,
        horizon_long_md=_v20_fixture_md("long"),
        horizon_medium_md=_v20_fixture_md("medium"),
        horizon_short_md=_v20_fixture_md("short"),
        horizon_long_json=_v20_fixture_json("long"),
        horizon_medium_json=_v20_fixture_json("medium"),
        horizon_short_json=_v20_fixture_json("short"),
    )
    r = client_with_db.post(
        f"/api/plan/draft/{draft_id}/accept?user_id=ariel"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["gate_warning"] is not None
    gw = body["gate_warning"]
    assert "GATE FAIL" in gw["summary"]
    assert gw["total_violations"] > 0
    # v20 has both history_leak and jargon_leak violations
    by_check = gw["violations_by_check"]
    assert by_check.get("history_leak", 0) > 0
    assert by_check.get("jargon_leak", 0) > 0


# ---------------------------------------------------------------------------
# Accept path — enforce mode
# ---------------------------------------------------------------------------

def test_accept_v20_draft_enforce_mode_returns_422(
    client_with_db, monkeypatch,
):
    """With plan_gate_enforce=True, the v20 draft is blocked at /accept
    with a structured 422 detail listing the violations."""
    monkeypatch.setenv("ARGOSY_PLAN_GATE_ENFORCE", "true")
    from argosy.config import reload_settings
    reload_settings()
    draft_id = _insert_draft(
        client_with_db,
        horizon_long_md=_v20_fixture_md("long"),
        horizon_medium_md=_v20_fixture_md("medium"),
        horizon_short_md=_v20_fixture_md("short"),
        horizon_long_json=_v20_fixture_json("long"),
        horizon_medium_json=_v20_fixture_json("medium"),
        horizon_short_json=_v20_fixture_json("short"),
    )
    r = client_with_db.post(
        f"/api/plan/draft/{draft_id}/accept?user_id=ariel"
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "plan_output_gate_failed"
    assert "history_leak" in detail["violations_by_check"]
    assert "jargon_leak" in detail["violations_by_check"]
    assert "hint" in detail

    # Draft remains in 'draft' role — not promoted.
    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        assert pv.role == "draft"
        assert pv.accepted_at is None
    finally:
        sess.close()


def test_accept_v20_draft_override_gate_force_accepts(
    client_with_db, monkeypatch,
):
    """?override_gate=true bypasses the check in enforce mode."""
    monkeypatch.setenv("ARGOSY_PLAN_GATE_ENFORCE", "true")
    from argosy.config import reload_settings
    reload_settings()
    draft_id = _insert_draft(
        client_with_db,
        horizon_long_md=_v20_fixture_md("long"),
        horizon_medium_md=_v20_fixture_md("medium"),
        horizon_short_md=_v20_fixture_md("short"),
        horizon_long_json=_v20_fixture_json("long"),
        horizon_medium_json=_v20_fixture_json("medium"),
        horizon_short_json=_v20_fixture_json("short"),
    )
    r = client_with_db.post(
        f"/api/plan/draft/{draft_id}/accept?user_id=ariel&override_gate=true"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    # gate_warning is None on override (the gate verdict was bypassed,
    # not surfaced — the override event is audit-logged separately
    # via plan.draft.accepted.override).
    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        assert pv.role == "current"
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# Defensive — gate failure does not break /accept
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# #24 — headline_numeric_source gate at /accept
# ---------------------------------------------------------------------------

# A self-consistent fi_base: spend / target == required_real_yield.
# fi_target = 17.3M, spend = 778,500 → required_real_yield = 0.045.
_WS_FI_BASE = {
    "fi_target_nis": 17_300_000,
    "retirement_age": 49,
    "annual_spend_nis": 778_500,
    "return_assumption_pct": 0.045,
    "required_real_yield_pct": 0.045,
    "method": "annual_spend / required real yield",
}


def _seed_ws_report(client_with_db, decision_run_id: int) -> None:
    """Persist a withdrawal_sequencer AgentReport for the run so the
    resolver resolves retirement.fi_target_nis = ₪17.3M."""
    import json as _json
    from argosy.state.models import AgentReport

    sess = client_with_db.app.state.session_factory()
    try:
        sess.add(
            AgentReport(
                user_id="ariel",
                agent_role="withdrawal_sequencer",
                decision_id=f"plan-synth-{decision_run_id}",
                response_text=_json.dumps({"fi_base": _WS_FI_BASE}),
            )
        )
        sess.commit()
    finally:
        sess.close()


def _seed_authority_phases(
    client_with_db, decision_run_id: int, *, codex="APPROVE", reader="APPROVE",
) -> None:
    """Persist the codex (phase_45) + whole-artifact reader (phase_55) verdicts a
    clean synthesis run would carry, so the unified promote_gate (fail-closed on a
    missing authority) sees them CLEARED. Tests isolating the numeric gate seed
    APPROVE for both — the numeric gate is the surface under test, not the
    authority gate."""
    import json as _json
    from datetime import datetime, timezone
    from argosy.state.models import DecisionPhase

    sess = client_with_db.app.state.session_factory()
    try:
        now = datetime.now(timezone.utc)
        sess.add(DecisionPhase(
            decision_run_id=decision_run_id, user_id="ariel", seq=45,
            kind="synthesis.phase_45", started_at=now, finished_at=now,
            phase_output_json=_json.dumps({"overall_assessment": codex}),
        ))
        sess.add(DecisionPhase(
            decision_run_id=decision_run_id, user_id="ariel", seq=55,
            kind="synthesis.phase_55", started_at=now, finished_at=now,
            phase_output_json=_json.dumps({"overall_assessment": reader}),
        ))
        sess.commit()
    finally:
        sess.close()


def _make_run(client_with_db) -> int:
    from datetime import datetime, timezone
    from argosy.state.models import DecisionRun

    sess = client_with_db.app.state.session_factory()
    try:
        now = datetime.now(timezone.utc)
        run = DecisionRun(
            user_id="ariel", ticker="(plan)", tier="T3",
            decision_kind="plan_revision", status="completed",
            started_at=now, finished_at=now,
        )
        sess.add(run)
        sess.commit()
        sess.refresh(run)
        return run.id
    finally:
        sess.close()


def test_accept_fabricated_headline_number_returns_422(client_with_db, monkeypatch):
    """#24: a draft whose user-facing markdown states a headline ₪21M FI
    target while the resolver resolved ₪17.3M is blocked at /accept (422)
    in enforce mode."""
    import argosy.api.routes.plan as plan_routes
    monkeypatch.setattr(plan_routes, "_auto_regen_narrative", lambda *a, **k: None)
    monkeypatch.setenv("ARGOSY_PLAN_GATE_ENFORCE", "true")
    from argosy.config import reload_settings
    reload_settings()

    run_id = _make_run(client_with_db)
    _seed_ws_report(client_with_db, run_id)
    draft_id = _insert_draft(
        client_with_db,
        # CLEAN of history/jargon; the ONLY violation should be numeric.
        horizon_long_md="Derived FI target net worth: **₪21.00M**.\n",
        horizon_medium_md="Steady growth.\n",
        horizon_short_md="Park RSU proceeds.\n",
    )
    # Wire the draft to the run.
    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        pv.decision_run_id = run_id
        sess.commit()
    finally:
        sess.close()

    r = client_with_db.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "plan_output_gate_failed"
    assert "headline_numeric_source" in detail["violations_by_check"]

    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        assert pv.role == "draft"  # not promoted
    finally:
        sess.close()


def test_accept_matching_headline_number_passes_numeric_gate(client_with_db, monkeypatch):
    """#24: a draft whose markdown matches the resolved ₪17.3M FI target and
    age 49 does NOT raise a headline_numeric_source violation."""
    import argosy.api.routes.plan as plan_routes
    monkeypatch.setattr(plan_routes, "_auto_regen_narrative", lambda *a, **k: None)
    monkeypatch.setenv("ARGOSY_PLAN_GATE_ENFORCE", "true")
    from argosy.config import reload_settings
    reload_settings()

    run_id = _make_run(client_with_db)
    _seed_ws_report(client_with_db, run_id)
    _seed_authority_phases(client_with_db, run_id)
    draft_id = _insert_draft(
        client_with_db,
        horizon_long_md="Derived FI target: **₪17.30M**; you could retire at age 49.\n",
        horizon_medium_md="Steady growth.\n",
        horizon_short_md="Park RSU proceeds.\n",
    )
    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        pv.decision_run_id = run_id
        sess.commit()
    finally:
        sess.close()

    r = client_with_db.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")
    # No numeric violation. (Section/evidence checks are skipped because the
    # json columns are '{}', so the only enforced surface is the markdown
    # checks + numeric gate; markdown is clean and numbers match → 200.)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"


def test_accept_derivation_pending_is_blocked_as_leakage(client_with_db, monkeypatch):
    """A draft still rendering '[derivation pending]' for an un-derived number is
    BLOCKED by the leakage gate (owner decision 2026-06-22: an unfinished
    placeholder must not reach an accepted plan — it is leakage, not a sanctioned
    escape hatch). Re-render / re-synthesize until clean, or override explicitly."""
    import argosy.api.routes.plan as plan_routes
    monkeypatch.setattr(plan_routes, "_auto_regen_narrative", lambda *a, **k: None)
    monkeypatch.setenv("ARGOSY_PLAN_GATE_ENFORCE", "true")
    from argosy.config import reload_settings
    reload_settings()

    run_id = _make_run(client_with_db)
    _seed_ws_report(client_with_db, run_id)
    _seed_authority_phases(client_with_db, run_id)
    draft_id = _insert_draft(
        client_with_db,
        horizon_long_md="Derived FI target net worth: **[derivation pending]**.\n",
        horizon_medium_md="Steady growth.\n",
        horizon_short_md="Park RSU proceeds.\n",
    )
    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        pv.decision_run_id = run_id
        sess.commit()
    finally:
        sess.close()

    r = client_with_db.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "artifact_leakage"
    assert any("derivation pending" in s for s in detail["leaks"])


def test_accept_fail_closed_when_no_decision_run_id_in_enforce(client_with_db, monkeypatch):
    """#24: in enforce mode, a draft with NO decision_run_id (resolver can't
    run) fails closed — the numeric gate records a violation and the accept
    is blocked."""
    import argosy.api.routes.plan as plan_routes
    monkeypatch.setattr(plan_routes, "_auto_regen_narrative", lambda *a, **k: None)
    monkeypatch.setenv("ARGOSY_PLAN_GATE_ENFORCE", "true")
    from argosy.config import reload_settings
    reload_settings()

    draft_id = _insert_draft(
        client_with_db,
        horizon_long_md="Steady growth across diversified holdings.\n",
        horizon_medium_md="Steady growth.\n",
        horizon_short_md="Park RSU proceeds.\n",
    )
    # No decision_run_id wired.
    r = client_with_db.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "plan_output_gate_failed"
    assert "headline_numeric_source" in detail["violations_by_check"]


def test_override_fm_rejection_does_not_bypass_numeric_gate(client_with_db, monkeypatch):
    """#24 composition: override_fm_rejection=true skips the FM-rejection
    block (orthogonal) but must STILL 422 on the numeric-source gate."""
    import argosy.api.routes.plan as plan_routes
    from argosy.state.models import DecisionRun

    monkeypatch.setattr(plan_routes, "_auto_regen_narrative", lambda *a, **k: None)
    monkeypatch.setenv("ARGOSY_PLAN_GATE_ENFORCE", "true")
    from argosy.config import reload_settings
    reload_settings()

    run_id = _make_run(client_with_db)
    _seed_ws_report(client_with_db, run_id)
    # Mark the run FM-rejected so override_fm_rejection is meaningful.
    sess = client_with_db.app.state.session_factory()
    try:
        run = sess.get(DecisionRun, run_id)
        run.fund_manager_decision = "rejected"
        sess.commit()
    finally:
        sess.close()

    draft_id = _insert_draft(
        client_with_db,
        horizon_long_md="Derived FI target net worth: **₪21.00M**.\n",
        horizon_medium_md="Steady growth.\n",
        horizon_short_md="Park RSU proceeds.\n",
    )
    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        pv.decision_run_id = run_id
        sess.commit()
    finally:
        sess.close()

    # override_fm_rejection clears the FM block, but the numeric gate still bites.
    r = client_with_db.post(
        f"/api/plan/draft/{draft_id}/accept"
        f"?user_id=ariel&override_fm_rejection=true"
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "plan_output_gate_failed"
    assert "headline_numeric_source" in detail["violations_by_check"]

    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        assert pv.role == "draft"  # not promoted
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# Living-plan cutover — /accept routed through the derivation-graph publish gate
# ---------------------------------------------------------------------------

def test_accept_incremental_gate_blocks_on_open_coherence_flag(client_with_db, monkeypatch):
    """With ARGOSY_INCREMENTAL_PLAN on, /accept routes promotion through the
    incremental cycle's publish gate. An open coherence flag (the cross-surface
    contradiction class) fails closed with the unified promote_gate 422, EVEN
    when every authority clears."""
    import argosy.api.routes.plan as plan_routes
    from argosy.orchestrator.flows import incremental_plan as inc
    from argosy.quality.promote_gate import PromoteDecision

    monkeypatch.setattr(plan_routes, "_auto_regen_narrative", lambda *a, **k: None)
    monkeypatch.setenv("ARGOSY_PLAN_GATE_ENFORCE", "true")
    monkeypatch.setenv("ARGOSY_INCREMENTAL_PLAN", "1")
    from argosy.config import reload_settings
    reload_settings()

    run_id = _make_run(client_with_db)
    _seed_ws_report(client_with_db, run_id)
    _seed_authority_phases(client_with_db, run_id)  # all authorities clear
    draft_id = _insert_draft(
        client_with_db,
        horizon_long_md="Derived FI target: **₪17.30M**; you could retire at age 49.\n",
        horizon_medium_md="Steady growth.\n",
        horizon_short_md="Park RSU proceeds.\n",
    )
    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        pv.decision_run_id = run_id
        sess.commit()
    finally:
        sess.close()

    # Force the cycle to report an open coherence flag (a blocking publish
    # decision) — exercises the route's use of CycleResult.publish_decision.
    def _fake_cycle(*a, **k):
        from argosy.orchestrator.flows.incremental_plan import CycleResult
        blocked = PromoteDecision(
            False, ["open-coherence-flag:cross_surface"],
            ["open-coherence-flag:cross_surface: node carries an open coherence flag"],
        )
        return CycleResult(
            closed=False, open_flags=["coherence:'fi_capital_sufficiency'"],
            promotable=False, publish_decision=blocked,
        )
    monkeypatch.setattr(inc, "run_incremental_cycle", _fake_cycle)

    r = client_with_db.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "promote_gate_blocked"
    assert any("coherence" in b for b in detail["blocking_authorities"])
    sess = client_with_db.app.state.session_factory()
    try:
        assert sess.get(PlanVersion, draft_id).role == "draft"  # not promoted
    finally:
        sess.close()


def test_accept_incremental_gate_promotes_when_clean(client_with_db, monkeypatch):
    """With ARGOSY_INCREMENTAL_PLAN on and the cycle reporting no open flags +
    all authorities clear, the incremental gate promotes (200)."""
    import argosy.api.routes.plan as plan_routes
    from argosy.orchestrator.flows import incremental_plan as inc
    from argosy.quality.promote_gate import PromoteDecision

    monkeypatch.setattr(plan_routes, "_auto_regen_narrative", lambda *a, **k: None)
    monkeypatch.setenv("ARGOSY_PLAN_GATE_ENFORCE", "true")
    monkeypatch.setenv("ARGOSY_INCREMENTAL_PLAN", "1")
    from argosy.config import reload_settings
    reload_settings()

    run_id = _make_run(client_with_db)
    _seed_ws_report(client_with_db, run_id)
    _seed_authority_phases(client_with_db, run_id)
    draft_id = _insert_draft(
        client_with_db,
        horizon_long_md="Derived FI target: **₪17.30M**; you could retire at age 49.\n",
        horizon_medium_md="Steady growth.\n",
        horizon_short_md="Park RSU proceeds.\n",
    )
    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        pv.decision_run_id = run_id
        sess.commit()
    finally:
        sess.close()

    def _fake_cycle(*a, **k):
        from argosy.orchestrator.flows.incremental_plan import CycleResult
        ok = PromoteDecision(True, [], [])
        return CycleResult(closed=True, open_flags=[], promotable=True,
                           publish_decision=ok)
    monkeypatch.setattr(inc, "run_incremental_cycle", _fake_cycle)

    r = client_with_db.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"


def test_accept_incremental_gate_degrades_when_cycle_raises(client_with_db, monkeypatch):
    """If the incremental cycle can't build the graph (raises), /accept degrades
    to the authority-only evaluate_promotion path — synthesis stays the fallback,
    promotion is NOT silently blocked by an infrastructure error."""
    import argosy.api.routes.plan as plan_routes
    from argosy.orchestrator.flows import incremental_plan as inc

    monkeypatch.setattr(plan_routes, "_auto_regen_narrative", lambda *a, **k: None)
    monkeypatch.setenv("ARGOSY_PLAN_GATE_ENFORCE", "true")
    monkeypatch.setenv("ARGOSY_INCREMENTAL_PLAN", "1")
    from argosy.config import reload_settings
    reload_settings()

    run_id = _make_run(client_with_db)
    _seed_ws_report(client_with_db, run_id)
    _seed_authority_phases(client_with_db, run_id)
    draft_id = _insert_draft(
        client_with_db,
        horizon_long_md="Derived FI target: **₪17.30M**; you could retire at age 49.\n",
        horizon_medium_md="Steady growth.\n",
        horizon_short_md="Park RSU proceeds.\n",
    )
    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        pv.decision_run_id = run_id
        sess.commit()
    finally:
        sess.close()

    def _boom(*a, **k):
        raise RuntimeError("resolver unavailable")
    monkeypatch.setattr(inc, "run_incremental_cycle", _boom)

    r = client_with_db.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")
    # Authorities all clear + clean markdown -> degrade path promotes.
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"


def test_accept_skips_gate_when_no_horizon_data(
    client_with_db, monkeypatch,
):
    """Pre-Phase-1 rows might lack horizon data entirely. The gate
    helper returns None in that case and accept proceeds cleanly
    without raising or warning."""
    monkeypatch.setenv("ARGOSY_PLAN_GATE_ENFORCE", "true")
    from argosy.config import reload_settings
    reload_settings()
    draft_id = _insert_draft(
        client_with_db,
        horizon_long_md="",
        horizon_medium_md="",
        horizon_short_md="",
    )
    # Wipe the json columns too so the gate has nothing to read.
    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        pv.horizon_long_json = ""
        pv.horizon_medium_json = ""
        pv.horizon_short_json = ""
        sess.commit()
    finally:
        sess.close()
    r = client_with_db.post(
        f"/api/plan/draft/{draft_id}/accept?user_id=ariel"
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"
    assert r.json()["gate_warning"] is None
