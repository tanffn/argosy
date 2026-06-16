# Shift-Left In-Stage Deterministic Gate — Implementation Plan (Phase 1, Slice 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the existing deterministic plan-output gate *during synthesis* — right after the draft body is assembled and persisted, BEFORE the LLM whole-artifact reader — so cheap deterministic defects (IPS sum ≠ 100, cross-surface divergence, stale date, FX unit, fabricated number, cap regression) are surfaced in-stage instead of being discovered ~80 minutes later by the reader.

**Architecture:** A new pure-ish helper `run_deterministic_gate_instage` gathers the same inputs `/accept` already feeds `gate_plan_output` (assembled artifact, resolver manifest, today, snapshot date, FX, NVDA caps) and returns the `GateVerdict`. The orchestrator calls it at "Layer B" (post-persist, pre-reader) and records the verdict as a decision phase (`synthesis.phase_53`) so it shows in `/decisions/[id]`. This is the first slice of the fact-centric design (`docs/superpowers/specs/2026-06-16-checks-all-the-way-and-section-surgical-fix-design.md`); it surfaces defects only — it does NOT yet auto-correct.

**Tech Stack:** Python 3.12, SQLAlchemy, pytest. Windows PowerShell (`;` not `&&`). Interpreter `D:/Projects/financial-advisor/.venv/Scripts/python.exe`. Tests: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" <path> -q -p no:cacheprovider`.

---

## File Structure

- **Create** `argosy/quality/instage_gate.py` — `run_deterministic_gate_instage(...)`: gathers gate inputs from a persisted draft + DB and returns a `GateVerdict`. Dependency-injected `assemble`/`resolve`/`current_plan` callables (default to the real ones) so it unit-tests without a live synthesis. One clear responsibility: "run the deterministic gate suite on an already-persisted draft."
- **Create** `tests/test_instage_gate.py` — unit tests for the helper (injected stubs).
- **Modify** `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` — call the helper at Layer B (immediately before the whole-artifact reader block) and record `synthesis.phase_53`.
- **Modify** `argosy/orchestrator/flows/plan_synthesis/__init__.py` — re-export the helper so the orchestrator calls it via `_pkg.` (tests can monkeypatch).

Why these boundaries: the helper is independently testable (no orchestrator import needed); the orchestrator change is a thin call + phase record; the gate logic itself is already tested in `tests/test_plan_output_gate.py` and is not touched.

---

### Task 1: The in-stage gate helper

**Files:**
- Create: `argosy/quality/instage_gate.py`
- Test: `tests/test_instage_gate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_instage_gate.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_instage_gate.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'argosy.quality.instage_gate'`.

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/quality/instage_gate.py
"""Run the deterministic plan-output gate IN-STAGE (during synthesis), on the
just-persisted draft, before the expensive LLM whole-artifact reader.

This is the shift-left point ("Layer B" in the checks-all-the-way design): the
same checks /accept runs at promotion, run here at synthesis time so a cheap
deterministic defect (IPS sum, cross-surface divergence, stale date, FX unit,
fabricated number, cap regression) is surfaced before the reader spends ~80 min
finding it. Surfaces only — does not auto-correct (that is a later slice).

Dependency-injected ``assemble`` / ``resolve`` / ``current_plan`` callables
default to the real services so the orchestrator calls it with no extra wiring,
while tests inject stubs and avoid a live synthesis.
"""
from __future__ import annotations

import json
import logging
from datetime import date as _date
from typing import Any, Callable

from argosy.quality.gate_types import GateVerdict
from argosy.quality.plan_output_gate import gate_plan_output

log = logging.getLogger(__name__)


def _nvda_cap(plan: Any) -> float | None:
    raw = getattr(plan, "target_allocation_json", None)
    if not raw:
        return None
    try:
        return json.loads(raw).get("nvda_cap_pct")
    except Exception:  # noqa: BLE001 — best-effort
        return None


def run_deterministic_gate_instage(
    *,
    session: Any,
    user_id: str,
    draft: Any,
    decision_run_id: int,
    today: _date | None = None,
    snapshot_date: _date | None = None,
    assemble: Callable[[Any, str], Any] | None = None,
    resolve: Callable[[Any, str, int], Any] | None = None,
    current_plan: Callable[[Any, str], Any] | None = None,
) -> GateVerdict:
    """Assemble the persisted draft + run the deterministic gate suite. Never
    raises — a gathering failure degrades to an empty-input gate call (which
    simply runs fewer checks), so synthesis is never aborted by this surface."""
    if assemble is None:
        from argosy.services.assembled_artifact import assemble_plan_artifact as assemble
    if resolve is None:
        from argosy.services.plan_numeric_resolver import resolve_plan_numbers as resolve
    if current_plan is None:
        from argosy.state.queries import get_current_plan as current_plan

    today = today or _date.today()

    artifact = None
    try:
        artifact = assemble(session, user_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("instage_gate.assemble_failed user=%s err=%s", user_id, exc)

    resolved = None
    try:
        resolved = resolve(session, user_id, decision_run_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("instage_gate.resolve_failed user=%s err=%s", user_id, exc)

    prior_cap = None
    try:
        prior = current_plan(session, user_id)
        prior_cap = _nvda_cap(prior) if prior is not None else None
    except Exception as exc:  # noqa: BLE001
        log.warning("instage_gate.current_plan_failed user=%s err=%s", user_id, exc)

    fx_usd_nis = None
    try:
        rv = resolved.get("fx.usd_nis") if resolved is not None else None
        if rv is not None and getattr(rv, "status", None) == "resolved" and getattr(rv, "value", None) is not None:
            fx_usd_nis = float(rv.value)
    except Exception:  # noqa: BLE001
        fx_usd_nis = None

    horizon_text = {
        "long": getattr(draft, "horizon_long_md", "") or "",
        "medium": getattr(draft, "horizon_medium_md", "") or "",
        "short": getattr(draft, "horizon_short_md", "") or "",
    }
    return gate_plan_output(
        horizon_text=horizon_text,
        synth=None,
        distillate=None,
        resolved=resolved,
        artifact=artifact,
        today=today,
        snapshot_date=snapshot_date,
        fx_usd_nis=fx_usd_nis,
        current_nvda_cap_pct=_nvda_cap(draft),
        prior_nvda_cap_pct=prior_cap,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_instage_gate.py -q -p no:cacheprovider`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/instage_gate.py tests/test_instage_gate.py
git commit -m "feat(quality): in-stage deterministic gate helper (shift-left Layer B)"
```

---

### Task 2: Re-export the helper for the orchestrator namespace

**Files:**
- Modify: `argosy/orchestrator/flows/plan_synthesis/__init__.py`
- Test: `tests/test_instage_gate.py` (add an import test)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_instage_gate.py
def test_helper_exported_on_flow_package():
    from argosy.orchestrator.flows import plan_synthesis as flow
    assert hasattr(flow, "run_deterministic_gate_instage")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_instage_gate.py::test_helper_exported_on_flow_package -q -p no:cacheprovider`
Expected: FAIL — `AttributeError: module ... has no attribute 'run_deterministic_gate_instage'`.

- [ ] **Step 3: Write minimal implementation**

Add to `argosy/orchestrator/flows/plan_synthesis/__init__.py` (near the other `from argosy.quality...` / helper re-exports):

```python
from argosy.quality.instage_gate import run_deterministic_gate_instage  # noqa: F401
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_instage_gate.py::test_helper_exported_on_flow_package -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add argosy/orchestrator/flows/plan_synthesis/__init__.py tests/test_instage_gate.py
git commit -m "feat(quality): export in-stage gate on the plan_synthesis flow package"
```

---

### Task 3: Call the in-stage gate at Layer B (pre-reader) + record the phase

**Files:**
- Modify: `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` (the whole-artifact reader block in `run_synthesis`, immediately BEFORE the `def _assemble_and_read():` / first reader dispatch — locate it by searching `FINAL STAGE — whole-artifact adversarial reader`).
- Test: `tests/test_plan_synthesis_instage_gate.py`

- [ ] **Step 1: Write the failing test** (reuses the synth wire-test harness)

```python
# tests/test_plan_synthesis_instage_gate.py
from __future__ import annotations

from sqlalchemy import select

from argosy.state.models import DecisionPhase

from tests.test_plan_synthesis_whole_artifact import (  # noqa: F401 — fixtures
    _reset_global_state_after_each_test, _wire_phase_stubs, synth_db,
)
from tests.test_plan_synthesis_reader_reconcile import _isolate_external_phases


def test_instage_gate_phase_recorded(synth_db, monkeypatch):
    """A synthesis run records a synthesis.phase_53 row holding the in-stage
    deterministic gate summary — proving the suite ran BEFORE the reader."""
    session, user_id = synth_db, "test_ariel"
    from argosy.orchestrator.flows import plan_synthesis as flow
    _wire_phase_stubs(monkeypatch, flow, user_id)
    _isolate_external_phases(monkeypatch)

    result = flow.run_synthesis(session, user_id=user_id, trigger="scheduled")

    session.expire_all()
    row = session.execute(
        select(DecisionPhase).where(
            DecisionPhase.decision_run_id == result.decision_run_id,
            DecisionPhase.kind == "synthesis.phase_53",
        )
    ).scalars().first()
    assert row is not None, "expected a synthesis.phase_53 in-stage gate row"
    assert row.phase_output_json  # carries the gate summary
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_plan_synthesis_instage_gate.py -q -p no:cacheprovider`
Expected: FAIL — no `synthesis.phase_53` row (`assert row is not None`).

- [ ] **Step 3: Write minimal implementation**

In `run_synthesis`, immediately before the `def _assemble_and_read():` definition in the reader block, insert:

```python
    # Layer B (shift-left): run the deterministic gate suite on the persisted
    # draft BEFORE the expensive LLM reader, so a cheap deterministic defect is
    # surfaced in-stage. Best-effort + never aborts synthesis; recorded as
    # phase 5.3 so /decisions/[id] shows it ran before the reader.
    try:
        _instage_started = datetime.now(timezone.utc)
        _instage_verdict = _pkg.run_deterministic_gate_instage(
            session=session, user_id=user_id, draft=draft,
            decision_run_id=decision_run_id,
        )
        _pkg._record_phase_completion(
            user_id=user_id, decision_run_id=decision_run_id,
            phase_n=53, started_at=_instage_started,
            phase_output=_instage_verdict.summary(),
            agent_report_rows=[],
        )
        if not _instage_verdict.passes:
            log.warning(
                "plan_synthesis.instage_gate_violations",
                user_id=user_id, decision_run_id=decision_run_id,
                summary=_instage_verdict.summary(),
            )
    except Exception as exc:  # noqa: BLE001 — surfacing only; never abort
        log.warning(
            "plan_synthesis.instage_gate_failed",
            user_id=user_id, decision_run_id=decision_run_id, error=str(exc),
        )
```

Note: confirm `_record_phase_completion` accepts `agent_report_rows=[]` (it does for the reader/codex guards). `datetime`/`timezone` are already imported in this module.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_plan_synthesis_instage_gate.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add argosy/orchestrator/flows/plan_synthesis/orchestrator.py tests/test_plan_synthesis_instage_gate.py
git commit -m "feat(synthesis): run the deterministic gate in-stage before the reader (Layer B)"
```

---

### Task 4: Regression guard — the no-surprise contract on one finding

**Files:**
- Test: `tests/test_instage_gate.py` (add)

This locks the slice's value: a cross-surface divergence (the run-106 class the LLM reader currently catches as a coherence finding) is caught by the DETERMINISTIC in-stage gate, so it never has to reach the reader to be found.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_instage_gate.py
from datetime import date as _d
from types import SimpleNamespace as _NS

from argosy.quality.gate_types import GateCheck as _GC
from argosy.quality.instage_gate import run_deterministic_gate_instage as _run


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
        assemble=lambda s, u: _Art(),
        resolve=lambda s, u, d: None,
        current_plan=lambda s, u: None,
        snapshot_date=_d(2026, 6, 16),
    )
    assert verdict.violations[_GC.CROSS_SURFACE_COHERENCE], (
        "a cross-surface divergence must be caught by the in-stage deterministic "
        "gate, not deferred to the LLM reader"
    )
```

- [ ] **Step 2: Run test to verify it fails**

If Task 1 is implemented this should already PASS. To honor TDD, first comment out the `artifact=artifact` argument in `run_deterministic_gate_instage` (Task 1) and run — confirm it FAILS (no cross-surface violation because the artifact wasn't forwarded) — then restore the argument.

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_instage_gate.py::test_ips_style_divergence_caught_instage_not_left_to_reader -q -p no:cacheprovider`
Expected (with `artifact=` removed): FAIL. Expected (restored): PASS.

- [ ] **Step 3: Implementation** — none beyond Task 1 (the test pins existing behavior; restore the `artifact=artifact` arg).

- [ ] **Step 4: Run the whole slice's tests**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_instage_gate.py tests/test_plan_synthesis_instage_gate.py tests/test_plan_output_gate.py -q -p no:cacheprovider`
Expected: all PASS (the existing `test_plan_output_gate.py` confirms no regression in the gate itself).

- [ ] **Step 5: Commit**

```bash
git add tests/test_instage_gate.py
git commit -m "test(quality): in-stage gate catches a cross-surface divergence before the reader"
```

---

## Subsequent plans (out of scope here — each its own plan/spec slice)

This slice surfaces deterministic defects in-stage. It does NOT build the
fact-centric machinery; those are the next plans, in order:

1. **Fact + RenderedFactSite ledger + attribution** (the spec's keystone): the
   addressable substrate, renderers emit the fact→site ledger, typed
   `FindingLocation`, attribution from the ledger, the run-106 reader fixture.
2. **New invariants + pre-render TargetAllocationDoc**: the run-106 table's
   net-new invariants (FI timeline, bridge sizing-age labeling, RSU retention,
   tax-event currency, SGLN taxonomy, evidence-readiness, estate routing,
   coverage status) + flip `_assemble_draft_bodies` to build the allocation doc
   before rendering, run Layer A per-analyst typed checks.
3. **Fact-level surgical correction**: deterministic re-render of
   template/structured_field sites + the prose editor for llm_prose sites +
   scoped-plus-global re-verify, demoting full re-synth to structural-only.

## Notes / gotchas (from this session)

- The synthesis wire-tests hang on a REAL `claude.exe` call via
  `run_alternatives_phase`; `_isolate_external_phases` (in
  `tests/test_plan_synthesis_reader_reconcile.py`) patches it off. Reuse it in any
  test that drives `run_synthesis`.
- Console is cp1252 — never print ₪/Hebrew to stdout in scripts.
- Do NOT run the full suite concurrently with a live synthesis (CPU/codex
  contention).
