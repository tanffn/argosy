# Plan tab — synthesis button + live cascade — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Run synthesis" button to `/plan` that fires the Opus fleet (9 analysts → debates → synthesizer → risk → fund manager) and shows live agent activity while it runs; on completion, surface a link to the new draft on `/proposals`.

**Architecture:** Backend changes turn `/check-in` from sync into a real async background task (pre-create DecisionRun row + schedule via FastAPI `BackgroundTasks`), and propagate `decision_id` through all 5 phases of the synthesis flow so WS events carry it. Frontend adds the button + state on `/plan` and extends `AgentCascadePanel` (and its hook) to filter by `decisionId`. Completion is signalled via the existing `plan.draft.completed` WS event.

**Tech Stack:** Python 3 + FastAPI + SQLAlchemy (sync `Session`); Next.js 15 + React + TypeScript + Tailwind; SQLite (dev); existing `useDecisionStream` + `useWSEvents` hooks; in-process pub/sub via `argosy.api.events.publish_event_threadsafe`.

**Spec:** `docs/superpowers/specs/2026-05-24-plan-tab-synthesis-button-design.md` (committed `8d3ed49`).

**Codex tandem policy:** Every backend task ends with a Codex `role="reviewer"` checkpoint before commit. Frontend tasks substitute `npm run lint` + `npm run typecheck`. If Codex returns `BLOCKERS`, fix and re-review before committing. If Codex returns `LOOKS GOOD` (with or without nits), commit and proceed.

---

## File Structure

**Created:**
- `tests/test_plan_synthesis_decision_id_propagation.py` — new test module asserting `decision_id` reaches all 5 phases' agent calls.

**Modified — backend:**
- `argosy/agents/base.py:696` — add `decision_id` + `intake_session_id` to success `agent.run.finished` payload.
- `argosy/agents/base.py:732` — add `decision_id` + `intake_session_id` to failure `agent.run.finished` payload.
- `argosy/orchestrator/flows/plan_synthesis/orchestrator.py:382` — add `decision_id=decision_audit_token` to phase-1 `common_kwargs`.
- `argosy/orchestrator/flows/plan_synthesis/orchestrator.py:561,571,581` — add `decision_id=decision_audit_token` to phase-2 bull/bear/facilitator direct calls.
- `argosy/orchestrator/flows/plan_synthesis/orchestrator.py:619` — add `decision_id=decision_audit_token` to phase-3 synthesizer call.
- `argosy/orchestrator/flows/plan_synthesis/orchestrator.py:831` — add `decision_id=decision_run_id` (already string) to phase-4 risk-officer call. Note: in phase 4 the local variable is named `decision_run_id` but it's the string audit token — verify at edit time.
- `argosy/orchestrator/flows/plan_synthesis/orchestrator.py:764` — add `decision_id=decision_run_id` to risk-facilitator call.
- `argosy/orchestrator/flows/plan_synthesis/orchestrator.py:881` — add `decision_id=decision_run_id` to phase-5 fund-manager call.
- `argosy/api/routes/advisor.py:1300-1356` — `/check-in` becomes async: baseline guard first, pre-create DecisionRun, schedule wrapper via `BackgroundTasks`, add `decision_audit_token` to response model.
- `tests/test_advisor_route.py:1317-1402` — update 3 existing `/check-in` tests for new shape; add 2 new tests (no-leak on 404, mark-failed on background exception).
- `tests/test_agent_run_events.py` (or `tests/test_events.py` — verify which exists) — extend existing started-payload test to also assert finished-payload includes `decision_id` for both success and failure.

**Modified — frontend:**
- `ui/src/lib/useDecisionStream.ts:210` — extend `opts` to accept `decisionId?: string` filter (additive; spec said "no change" but inspecting the hook shows extending it is far cleaner than per-row post-filtering in the panel).
- `ui/src/components/advisor/AgentCascadePanel.tsx:28-37` — add `decisionId?: string | null` to props; pass through to the hook.
- `ui/src/lib/api.ts:845` — widen `advisorCheckIn` return type: add `decision_audit_token: string`, change `draft_id: number` → `number | null`.
- `ui/src/app/plan/page.tsx:1-180` — add button + state + WS subscription + completion handler + cascade rendering.

---

## Task 1: Add `decision_id` + `intake_session_id` to `agent.run.finished` event payloads

**Files:**
- Modify: `argosy/agents/base.py:696-712` (success path)
- Modify: `argosy/agents/base.py:732-740` (failure path inside `except Exception as run_exc`)
- Test: `tests/test_agent_run_events.py` (or `tests/test_events.py` — whichever exists today)

**Why:** Frontend cascade panel filters by `decision_id`. Today `agent.run.finished` omits `decision_id` even though `agent.run.started` includes it. A dropped or reordered started-event would leave a row unfilterable. The failure path also needs it so failures show up in the filtered cascade view.

- [ ] **Step 1: Locate the existing finished-payload test**

```bash
grep -nE "agent\.run\.finished|agent_run_finished|decision_id" tests/test_agent_run_events.py tests/test_events.py 2>$null | head -40
```

Read the file that contains existing finished-payload assertions. If neither file has them, create the assertion inside `tests/test_agent_run_events.py`.

- [ ] **Step 2: Write failing test — success path emits `decision_id` on finished**

In whichever test module already covers `agent.run.started` for `decision_id`, add a sibling test for the finished payload. Use the same fixture pattern as the existing started test. Replace `<TEST_AGENT_FIXTURE>` with the actual fixture name from the file:

```python
def test_agent_run_finished_includes_decision_id_success(<existing fixture>):
    """The agent.run.finished payload (success) must include decision_id so
    the UI cascade panel can filter rows by it. Mirrors the started payload."""
    captured: list[dict] = []
    # Use whichever subscribe pattern the existing started-payload test uses.
    # Pseudocode — adapt to the file's existing helper:
    with subscribe_events(["agent.run.finished"], captured):
        agent = <fixture>(decision_id="plan-synth-42")
        agent.run_sync(...)  # any inputs the fixture supports
    finished = [e for e in captured if e["event"] == "agent.run.finished"]
    assert len(finished) >= 1
    assert finished[0]["payload"]["decision_id"] == "plan-synth-42"
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
.venv/Scripts/python.exe -m pytest tests/test_agent_run_events.py::test_agent_run_finished_includes_decision_id_success -v
```

Expected: FAIL with `KeyError: 'decision_id'` or `AssertionError: assert None == 'plan-synth-42'`.

- [ ] **Step 4: Edit `argosy/agents/base.py` success-path payload (line 696)**

Replace the `_finished_payload` dict construction:

```python
_finished_payload: dict[str, Any] = {
    "user_id": self.user_id,
    "agent_role": self.agent_role,
    "decision_id": inputs.get("decision_id"),         # NEW — mirror started payload
    "intake_session_id": inputs.get("intake_session_id"),  # NEW — keep schema parity
    "run_correlation_id": run_correlation_id,
    "finished_at": datetime.now(timezone.utc).isoformat(),
    "status": "done",
    "tokens_in": call.tokens_in,
    "tokens_out": call.tokens_out,
    "cache_input_tokens": call.cache_input_tokens,
    "cache_creation_tokens": call.cache_creation_tokens,
    "thinking_tokens": call.thinking_tokens,
    "citations_count": _citations_count,
    "cost_usd": cost,
    "confidence": confidence.value if confidence else None,
    "agent_report_id": None,
    "turn_id": turn_id,
}
```

- [ ] **Step 5: Run the success-path test again**

```bash
.venv/Scripts/python.exe -m pytest tests/test_agent_run_events.py::test_agent_run_finished_includes_decision_id_success -v
```

Expected: PASS.

- [ ] **Step 6: Write failing test — failure path emits `decision_id` on finished**

In the same test file:

```python
def test_agent_run_finished_includes_decision_id_failure(<existing fixture>):
    """The agent.run.finished payload (failure) must include decision_id so
    a crashed agent still appears in the filtered cascade view.

    We force _call_model to raise inside BaseAgent.run; the except branch
    at base.py:732 emits a status='failed' finished event."""
    captured: list[dict] = []
    with subscribe_events(["agent.run.finished"], captured):
        agent = <fixture>(decision_id="plan-synth-99")
        agent._call_model = AsyncMock(side_effect=RuntimeError("boom"))  # or monkeypatch
        with pytest.raises(Exception):
            agent.run_sync(...)
    finished = [e for e in captured if e["event"] == "agent.run.finished"]
    assert len(finished) >= 1
    assert finished[0]["payload"]["status"] == "failed"
    assert finished[0]["payload"]["decision_id"] == "plan-synth-99"
```

- [ ] **Step 7: Run the failure-path test to verify it fails**

```bash
.venv/Scripts/python.exe -m pytest tests/test_agent_run_events.py::test_agent_run_finished_includes_decision_id_failure -v
```

Expected: FAIL with `KeyError: 'decision_id'` on the failure event.

- [ ] **Step 8: Edit `argosy/agents/base.py` failure-path payload (line 732)**

Replace the failure-path dict literal inside `except Exception as run_exc:`:

```python
publish_event_threadsafe("agent.run.finished", {
    "user_id": self.user_id,
    "agent_role": self.agent_role,
    "decision_id": inputs.get("decision_id"),         # NEW
    "intake_session_id": inputs.get("intake_session_id"),  # NEW
    "run_correlation_id": run_correlation_id,
    "turn_id": turn_id,
    "finished_at": datetime.now(timezone.utc).isoformat(),
    "status": "failed",
    "error": str(run_exc)[:500],
})
```

- [ ] **Step 9: Run both new tests + the existing started-payload test**

```bash
.venv/Scripts/python.exe -m pytest tests/test_agent_run_events.py -v
```

Expected: all PASS (no regression to existing started-payload tests).

- [ ] **Step 10: Codex review the diff**

Run the following from the repo root:

```bash
.venv/Scripts/python.exe - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, "D:/Projects/financial-advisor/tools/codex-tandem/scripts")
from engine_codex import run_codex

prompt = """
Review the diff for Task 1 of the plan-tab synthesis button feature.

Files changed:
- argosy/agents/base.py (success and failure agent.run.finished payloads)
- tests/test_agent_run_events.py (two new tests)

Goal: agent.run.finished payloads now carry decision_id + intake_session_id,
matching the agent.run.started payload schema. Verify:
1. Both success (base.py:696) and failure (base.py:732) emit sites add the new fields.
2. The field values come from inputs.get(...), not from elsewhere.
3. No regression risk to existing subscribers (the new fields are additive).
4. Tests assert both code paths (success + failure).

Run `git diff HEAD` to see the changes. Return LOOKS GOOD or BLOCKERS list.
"""
r = run_codex(node_dir=Path("D:/Projects/financial-advisor"),
              prompt=prompt, agent_name="task1_finished_payload", role="reviewer")
print(r.verdict_text)
PY
```

Address any BLOCKERS by re-editing + re-running tests + re-reviewing.

- [ ] **Step 11: Commit**

```bash
git add argosy/agents/base.py tests/test_agent_run_events.py
git commit -m "$(cat <<'EOF'
feat(events): add decision_id to agent.run.finished payloads (success + failure)

Mirrors the agent.run.started payload schema so UI cascade panels can filter
finished events by decision_id. Required for the plan-tab synthesis button
live-cascade feature.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Propagate `decision_id` to every agent call in the 5-phase synthesis flow

**Files:**
- Modify: `argosy/orchestrator/flows/plan_synthesis/orchestrator.py:382` (phase 1 common_kwargs)
- Modify: `argosy/orchestrator/flows/plan_synthesis/orchestrator.py:561,571,581` (phase 2 bull/bear/facilitator)
- Modify: `argosy/orchestrator/flows/plan_synthesis/orchestrator.py:619` (phase 3 synthesizer)
- Modify: `argosy/orchestrator/flows/plan_synthesis/orchestrator.py:764,831` (phase 4 risk facilitator + officer)
- Modify: `argosy/orchestrator/flows/plan_synthesis/orchestrator.py:881` (phase 5 fund manager)
- Create: `tests/test_plan_synthesis_decision_id_propagation.py`

**Why:** Without this, `AgentReport.decision_id` is NULL for all synthesis-run agents and WS events omit `decision_id`. The frontend cascade panel's filter (`row.decision_id === "plan-synth-N"`) matches zero rows.

**Encoding note:** Pass the string audit token `decision_audit_token` (which inside phase helpers is the local variable `decision_run_id`, already a string — re-confirm at edit time by reading the helper's signature). Do NOT pass the integer.

- [ ] **Step 1: Write the failing test**

Create `tests/test_plan_synthesis_decision_id_propagation.py`:

```python
"""Assert decision_id reaches every agent.run_sync call in the 5-phase synthesis flow.

Mocks each phase's agent class to capture kwargs and verifies that
`decision_id="plan-synth-<N>"` is present in every call's kwargs.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _stub_report(text="{}"):
    """A minimal stand-in for AgentReport.output that satisfies the orchestrator's
    accessor chains (.output.model_dump_json(), .output.approved, etc.)."""
    out = SimpleNamespace(
        model_dump_json=lambda: text,
        approved=True,
    )
    return SimpleNamespace(output=out)


def test_decision_id_propagates_to_all_phase_agent_calls(monkeypatch):
    """A single integration-style test that runs run_synthesis end-to-end
    with every agent stubbed, and asserts every captured kwargs dict
    contains decision_id == "plan-synth-<int_id>"."""
    from argosy.orchestrator.flows import plan_synthesis as flow

    calls: list[tuple[str, dict]] = []  # (agent_role_or_class_name, kwargs)

    def _capture_factory(name: str):
        def _capture(self, *args, **kwargs):
            calls.append((name, kwargs))
            return _stub_report()
        return _capture

    # Phase 1 agents — patch their run_sync at the class level.
    for cls_name in flow._PHASE_1_AGENT_NAMES:
        cls = getattr(flow, cls_name)
        monkeypatch.setattr(cls, "run_sync", _capture_factory(cls_name), raising=True)

    # Phase 2 agents.
    from argosy.agents.researcher import BullResearcherAgent, BearResearcherAgent
    from argosy.agents.researcher_facilitator import ResearcherFacilitatorAgent
    monkeypatch.setattr(BullResearcherAgent, "run_sync",
                        _capture_factory("BullResearcherAgent"), raising=True)
    monkeypatch.setattr(BearResearcherAgent, "run_sync",
                        _capture_factory("BearResearcherAgent"), raising=True)
    monkeypatch.setattr(ResearcherFacilitatorAgent, "run_sync",
                        _capture_factory("ResearcherFacilitatorAgent"), raising=True)

    # Phase 3 synthesizer.
    from argosy.agents.plan_synthesizer import PlanSynthesizerAgent
    # PlanSynthesizerAgent.run_sync must return an output that the orchestrator
    # can call .model_copy on after `_enforce_speculation_cap`. Use a richer stub.
    def _synth_stub(self, *args, **kwargs):
        calls.append(("PlanSynthesizerAgent", kwargs))
        from argosy.agents.plan_synthesizer_types import (
            PlanSynthesisOutput, Horizon, SynthesisInputs,
        )
        empty_h = Horizon(targets=[], principles=[], speculative_candidates=[])
        out = PlanSynthesisOutput(
            long=empty_h, medium=empty_h, short=empty_h,
            inputs=SynthesisInputs(baseline_id=None, prior_current_id=None,
                                   decision_run_id=None),
        )
        return SimpleNamespace(output=out)
    monkeypatch.setattr(PlanSynthesizerAgent, "run_sync", _synth_stub, raising=True)

    # Phase 4 risk + Phase 5 fund manager — patch the package-level factories.
    fake_officer = MagicMock()
    fake_officer.run_sync = _capture_factory("RiskOfficer")
    monkeypatch.setattr(flow, "_make_risk_officer",
                        lambda *a, **kw: fake_officer)
    from argosy.agents.risk_facilitator import RiskFacilitatorAgent
    monkeypatch.setattr(RiskFacilitatorAgent, "run_sync",
                        _capture_factory("RiskFacilitatorAgent"), raising=True)
    fake_fm = MagicMock()
    fake_fm.run_sync = _capture_factory("FundManager")
    monkeypatch.setattr(flow, "_make_fund_manager", lambda *a, **kw: fake_fm)

    # Stub the DB-touching helpers so the test stays in-memory and fast.
    monkeypatch.setattr(flow, "_assemble_portfolio_summary",
                        lambda *, session, user_id: "(empty)")
    monkeypatch.setattr(flow, "_assemble_fills_summary",
                        lambda *, session, user_id: "(empty)")
    monkeypatch.setattr(flow, "_load_user_context_yaml",
                        lambda *, session, user_id: "")

    # Real run_synthesis needs a real Session for the DecisionRun + PlanVersion
    # writes. Use the in-memory SQLite fixture.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from argosy.state.models import Base, PlanVersion, User

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    session.add(User(id="ariel", plan="free"))
    session.add(PlanVersion(user_id="ariel", role="baseline",
                            distillate_rendered="# Plan"))
    session.commit()

    # Act.
    result = flow.run_synthesis(session, user_id="ariel", trigger="check_in")

    # Assert: every captured call carries decision_id="plan-synth-<id>"
    expected_token = f"plan-synth-{result.decision_run_id}"
    bad = [(name, kw) for (name, kw) in calls
           if kw.get("decision_id") != expected_token]
    assert not bad, (
        f"{len(bad)} agent.run_sync call(s) missing decision_id="
        f"{expected_token!r}: {[name for name, _ in bad]}"
    )
    # Sanity: we actually invoked all expected phases.
    invoked_names = {name for name, _ in calls}
    assert "BullResearcherAgent" in invoked_names
    assert "PlanSynthesizerAgent" in invoked_names
    assert "RiskOfficer" in invoked_names
    assert "FundManager" in invoked_names
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/Scripts/python.exe -m pytest tests/test_plan_synthesis_decision_id_propagation.py -v
```

Expected: FAIL with assertion `"N agent.run_sync call(s) missing decision_id='plan-synth-...': [...]"`.

- [ ] **Step 3: Edit phase 1 — `orchestrator.py:382`**

In `_run_phase_1_analysts`, replace `common_kwargs` to add the new key. Phase 1's `decision_run_id` parameter is already the string audit token (the caller passes `decision_audit_token` at line 162). Add this entry:

```python
common_kwargs = dict(
    plan_label=baseline.version_label or "Imported plan",
    plan_markdown=baseline.distillate_rendered or "",
    snapshot_label=f"synthesis-{decision_run_id}",
    snapshot_summary=_pkg._assemble_portfolio_summary(session=session, user_id=user_id),
    user_context_yaml=_pkg._load_user_context_yaml(session=session, user_id=user_id),
    domain_kb_files={},  # Each analyst's prompt picks its own; pass empty.
    recent_events="",
    decision_id=decision_run_id,  # NEW — string audit token "plan-synth-<id>"
)
```

- [ ] **Step 4: Edit phase 2 — `orchestrator.py:561, 571, 581`**

In `_run_one_horizon_debate`, add `decision_id=decision_run_id` to all three `run_sync(...)` calls (bull, bear, facilitator). The local `decision_run_id` parameter is already the string token.

```python
bull_report = bull.run_sync(
    analyst_reports=analyst_reports_payload,
    prior_rounds=[],
    round_index=1,
    n_max=2,
    ticker=ticker,
    decision_id=decision_run_id,  # NEW
)
# ... bear and fac calls get the same kwarg.
```

Apply to all three calls (bull at line 561, bear at line 571, fac at line 581).

- [ ] **Step 5: Edit phase 3 — `orchestrator.py:619`**

In `_run_phase_3_synthesizer`, the local `decision_run_id` parameter is already the string token (caller passes `decision_audit_token`). Add to the `run_sync` call:

```python
result = agent.run_sync(
    baseline_distillate_md=baseline_md,
    prior_current_md=prior_md,
    analyst_reports_text=analyst_reports_text,
    debate_outcomes_text=debate_outcomes_text,
    portfolio_snapshot_summary=portfolio_summary,
    recent_fills_summary=fills_summary,
    speculation_cap_pct=speculation_cap_pct,
    speculation_cap_concurrent=speculation_cap_concurrent,
    decision_id=decision_run_id,  # NEW
)
```

- [ ] **Step 6: Edit phase 4 — `orchestrator.py:764, 831`**

Risk facilitator call at line 764:

```python
merged = facilitator.run_sync(
    verdicts=verdicts,
    rounds_run=1,
    decision_id=decision_run_id,  # NEW
)
```

Risk officer call at line 831 (inside `_run_one_risk_perspective`):

```python
result = officer.run_sync(
    proposal=proposal,
    analyst_reports=analyst_reports_payload,
    user_constraints="",
    risk_caps={},
    prior_rounds=[],
    round_index=1,
    n_max=1,
    decision_id=decision_run_id,  # NEW
)
```

- [ ] **Step 7: Edit phase 5 — `orchestrator.py:881`**

In `_run_phase_5_fund_manager`:

```python
result = fm.run_sync(
    decision_kind="plan_revision",
    draft_plan=draft_output.model_dump_json(),
    risk_verdict=risk_verdict,
    decision_id=decision_run_id,  # NEW
)
```

- [ ] **Step 8: Run the propagation test**

```bash
.venv/Scripts/python.exe -m pytest tests/test_plan_synthesis_decision_id_propagation.py -v
```

Expected: PASS.

- [ ] **Step 9: Run the broader synthesis test suite to catch regressions**

```bash
.venv/Scripts/python.exe -m pytest tests/ -k "plan_synthesis or synthesis_orchestrator" -m "not llm_eval" -v
```

Expected: all PASS. Any failures must be triaged before proceeding — typically caused by `run_sync` mock fixtures that didn't whitelist `decision_id` in their `**kwargs` signature; the fix is to add `**kw` capture in the stub or to add `decision_id` to its allowed kwargs.

- [ ] **Step 10: Codex review the diff**

```bash
.venv/Scripts/python.exe - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, "D:/Projects/financial-advisor/tools/codex-tandem/scripts")
from engine_codex import run_codex
r = run_codex(node_dir=Path("D:/Projects/financial-advisor"), prompt="""
Review Task 2 diff. Files: argosy/orchestrator/flows/plan_synthesis/orchestrator.py
+ tests/test_plan_synthesis_decision_id_propagation.py.

Verify:
1. Every direct .run_sync(...) call in run_synthesis's 5 phases now passes decision_id.
2. The value passed is the string audit token (not the integer PK).
3. No agent's build_prompt signature would reject decision_id as an unknown kwarg
   (BaseAgent.run forwards kwargs into inputs dict and reads inputs.get("decision_id"),
   so this is safe — but verify by reading BaseAgent.run signature).
4. The new test covers all 5 phases.
5. No leftover phases unpatched.

Run `git diff HEAD` to see the changes. LOOKS GOOD or BLOCKERS.
""", agent_name="task2_decision_id_propagation", role="reviewer")
print(r.verdict_text)
PY
```

Address BLOCKERS. The most likely Codex concern: agent `build_prompt` signatures don't accept `decision_id` and would TypeError. Mitigation: read `BaseAgent.run` (around line 575+ in base.py) to confirm it consumes `**kwargs` into `inputs` dict before calling `build_prompt`. If `build_prompt` is called with the kwargs, the existing `_safe_run_agent` already has a TypeError fallback path that narrows kwargs — but the direct phase-2/3/4/5 calls do NOT have such fallback. We may need to add the same narrowing pattern, or just confirm by reading each agent's `build_prompt` that they accept `**kwargs`. If they don't, the fix is to NOT pass `decision_id` as a `run_sync` kwarg but to thread it through a different channel (e.g. an `agent._decision_id` attribute set before `run_sync`). Resolve based on Codex's specific finding.

- [ ] **Step 11: Commit**

```bash
git add argosy/orchestrator/flows/plan_synthesis/orchestrator.py tests/test_plan_synthesis_decision_id_propagation.py
git commit -m "$(cat <<'EOF'
feat(synthesis): propagate decision_id through all 5 phases of plan_synthesis

Threads the string audit token "plan-synth-<id>" into every agent.run_sync call
in phases 1-5 of run_synthesis. agent_reports.decision_id now populates for
synthesis-flow agents, and WS agent.run.started/finished events carry it
for live cascade filtering.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `/check-in` becomes truly async

**Files:**
- Modify: `argosy/api/routes/advisor.py:1300-1356`
- Modify: `tests/test_advisor_route.py:1317-1402` (update 3 existing tests, add 2 new)

**Why:** The current sync handler blocks the HTTP response until the entire Opus fleet finishes (potentially many minutes). Frontend cannot render a live cascade scoped to a decision_run_id it doesn't receive until everything is done. Must return 202 immediately with the decision_run_id; the actual work moves to a background task.

- [ ] **Step 1: Update existing test for new response shape**

Edit `tests/test_advisor_route.py` test `test_post_advisor_checkin_returns_decision_run_id` (around line 1317). New shape:
- Response `draft_id` is `None` (the background task fills it later).
- Response includes new `decision_audit_token: str` field.
- `run_synthesis` is invoked via BackgroundTasks; in `TestClient` the task drains synchronously after the response is sent, so the assertion on `captured["user_id"]` etc. still works.

Replace the assertions:

```python
assert r.status_code == 202, r.text
out = r.json()
assert isinstance(out["decision_run_id"], int)
assert out["draft_id"] is None  # populated later via plan.draft.completed WS event
assert out["decision_audit_token"] == f"plan-synth-{out['decision_run_id']}"
# After background tasks drain (TestClient runs them in the same call):
assert captured["user_id"] == "ariel"
assert captured["trigger"] == "check_in"
assert "tax analyst" in captured["guidance"]
# The pre-created DecisionRun row exists.
from argosy.state.models import DecisionRun
sess = client_with_db.app.state.session_factory()
try:
    row = sess.get(DecisionRun, out["decision_run_id"])
    assert row is not None
    assert row.user_id == "ariel"
    assert row.decision_kind == "plan_revision"
finally:
    sess.close()
```

The stub `_fake_run` should also accept the new `existing_decision_run_id` kwarg:

```python
def _fake_run(session, *, user_id, trigger, guidance="", existing_decision_run_id=None):
    captured["user_id"] = user_id
    captured["trigger"] = trigger
    captured["guidance"] = guidance
    captured["existing_decision_run_id"] = existing_decision_run_id
    class _R:
        decision_run_id = existing_decision_run_id or 1
        draft_id = 42
    return _R()
```

And assert: `assert captured["existing_decision_run_id"] == out["decision_run_id"]`.

- [ ] **Step 2: Update 404 test to assert no-row-leak**

Edit `test_post_advisor_checkin_404_when_no_baseline` (around line 1355):

```python
def test_post_advisor_checkin_404_when_no_baseline(client_with_db):
    body = {"user_id": "ghost", "guidance": "", "urgency": "now"}
    r = client_with_db.post("/api/advisor/check-in", json=body)
    assert r.status_code == 404

    # CRITICAL: baseline guard runs BEFORE any DecisionRun row insert.
    # Without this assertion the regression is invisible — the spec's whole
    # point is to eliminate leaked status='running' rows on 404.
    from argosy.state.models import DecisionRun
    sess = client_with_db.app.state.session_factory()
    try:
        rows = sess.query(DecisionRun).filter_by(user_id="ghost").all()
        assert rows == [], f"unexpected DecisionRun rows leaked for ghost: {rows}"
    finally:
        sess.close()
```

- [ ] **Step 3: Add new test — mark-failed on background exception**

Append to `tests/test_advisor_route.py`:

```python
def test_post_advisor_checkin_marks_decision_run_failed_on_exception(
    client_with_db, monkeypatch,
):
    """If the BackgroundTask wrapper's run_synthesis raises, the pre-created
    DecisionRun row must be marked status='failed' with finished_at set.
    Without this, the row leaks as a permanent 'running' zombie."""
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.state.models import DecisionRun, PlanVersion, User

    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.add(PlanVersion(user_id="ariel", role="baseline", raw_markdown="# Plan"))
            sess.commit()
    finally:
        sess.close()

    def _bomb(session, *, user_id, trigger, guidance="", existing_decision_run_id=None):
        raise RuntimeError("synthesis exploded")

    monkeypatch.setattr(flow, "run_synthesis", _bomb)

    r = client_with_db.post(
        "/api/advisor/check-in",
        json={"user_id": "ariel", "guidance": "", "urgency": "now"},
    )
    # The 202 returns BEFORE the background task runs; the failure surfaces
    # in the DecisionRun row state, not the HTTP response.
    assert r.status_code == 202, r.text
    out = r.json()

    # TestClient drains background tasks after sending the response; by the
    # time r.json() returns, the wrapper has caught + marked the row failed.
    sess = client_with_db.app.state.session_factory()
    try:
        row = sess.get(DecisionRun, out["decision_run_id"])
        assert row is not None
        assert row.status == "failed", f"row.status={row.status!r}"
        assert row.finished_at is not None
    finally:
        sess.close()
```

- [ ] **Step 4: Run the test suite to verify the new shape fails**

```bash
.venv/Scripts/python.exe -m pytest tests/test_advisor_route.py -k "checkin" -v
```

Expected: 3 of 5 tests FAIL (new shape, no-leak assertion, mark-failed) plus the existing 2 still pass. If existing tests fail because of an unrelated change, triage first.

- [ ] **Step 5: Edit `argosy/api/routes/advisor.py` — pydantic models**

Replace `CheckInResponse` (around line 1318):

```python
class CheckInResponse(BaseModel):
    status: str
    decision_run_id: int
    decision_audit_token: str  # NEW — "plan-synth-<id>"; UI uses verbatim as cascade filter key
    draft_id: int | None        # populated later, surfaced via plan.draft.completed WS event
```

- [ ] **Step 6: Edit `post_check_in` route**

Replace the handler at line 1324:

```python
@router.post("/check-in", response_model=CheckInResponse, status_code=202)
def post_check_in(
    body: CheckInRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> CheckInResponse:
    """User-initiated plan synthesis (spec §7.6).

    Pre-creates a DecisionRun row (status='running'), returns 202 immediately
    with that row's id, and schedules run_synthesis in a BackgroundTask. The
    background wrapper opens its own Session (the request-scoped `db` is
    closed when the response returns) and marks the row 'failed' on exception.

    404 when the user has no active baseline plan — the baseline guard runs
    BEFORE the row is created so 404s never leak a status='running' zombie.
    """
    from argosy.orchestrator.flows.plan_synthesis import (
        NoBaselineError,
        get_active_baseline,
        run_synthesis,
    )

    # (a) Baseline guard FIRST. NoBaselineError → 404, no row writes.
    baseline = get_active_baseline(db, body.user_id)
    if baseline is None:
        raise HTTPException(
            status_code=404,
            detail=f"user {body.user_id!r} has no active baseline plan",
        )

    # (b) Pre-create the DecisionRun row so the response can carry its id.
    from datetime import datetime, timezone
    from argosy.state.models import DecisionRun

    decision_run = DecisionRun(
        user_id=body.user_id,
        ticker="(plan)",
        tier="T3",
        decision_kind="plan_revision",
        started_at=datetime.now(timezone.utc),
        status="running",
    )
    db.add(decision_run)
    db.commit()
    db.refresh(decision_run)
    decision_run_id = decision_run.id
    decision_audit_token = f"plan-synth-{decision_run_id}"

    # (c) Schedule the background wrapper. The wrapper opens its own Session
    # because `db` is closed by FastAPI's Depends teardown once we return.
    background_tasks.add_task(
        _run_synthesis_background,
        user_id=body.user_id,
        guidance=body.guidance,
        decision_run_id=decision_run_id,
    )

    return CheckInResponse(
        status="accepted",
        decision_run_id=decision_run_id,
        decision_audit_token=decision_audit_token,
        draft_id=None,
    )


def _run_synthesis_background(
    *,
    user_id: str,
    guidance: str,
    decision_run_id: int,
) -> None:
    """Background wrapper for /check-in.

    Opens a fresh sync Session (request-scoped db from get_db is closed once
    the route response returns — Starlette/FastAPI background tasks run
    AFTER response is sent). Calls run_synthesis; on exception, marks the
    pre-created DecisionRun row 'failed'.

    Replicates the session pattern from argosy.orchestrator.flows.plan_amendment
    .dispatcher.dispatch_async:308.
    """
    from datetime import datetime, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from argosy.config import get_settings
    from argosy.orchestrator.flows.plan_synthesis import run_synthesis
    from argosy.state.models import DecisionRun

    sync_url = get_settings().database_url.replace("+aiosqlite", "")
    engine = create_engine(sync_url, connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    session = SessionLocal()
    try:
        run_synthesis(
            session,
            user_id=user_id,
            trigger="check_in",
            guidance=guidance,
            existing_decision_run_id=decision_run_id,
        )
    except Exception as exc:  # noqa: BLE001 — must mark the row + log, never re-raise here
        _log.error(
            "plan_synthesis.background_failed",
            user_id=user_id,
            decision_run_id=decision_run_id,
            error=str(exc),
        )
        # Re-open a fresh session because the run_synthesis exception may have
        # left the original session in an inconsistent state (rolled back, etc.).
        fail_session = SessionLocal()
        try:
            row = fail_session.get(DecisionRun, decision_run_id)
            if row is not None:
                row.status = "failed"
                row.finished_at = datetime.now(timezone.utc)
                fail_session.commit()
        finally:
            fail_session.close()
    finally:
        session.close()
```

Note: `_log` is the module-level logger (already defined in advisor.py — confirm during edit; if named differently, adapt). `get_active_baseline` is imported from `argosy.orchestrator.flows.plan_synthesis` (verify the symbol is exported; if it lives in a submodule, import accordingly).

- [ ] **Step 7: Verify imports**

```bash
.venv/Scripts/python.exe -c "from argosy.orchestrator.flows.plan_synthesis import get_active_baseline; print('OK')"
```

Expected: prints `OK`. If `ImportError`, find the real location of `get_active_baseline` (likely in `argosy/orchestrator/flows/plan_synthesis/__init__.py` or `orchestrator.py` or a sibling submodule) and adjust the import.

- [ ] **Step 8: Run the /check-in tests**

```bash
.venv/Scripts/python.exe -m pytest tests/test_advisor_route.py -k "checkin" -v
```

Expected: all 5 PASS.

- [ ] **Step 9: Codex review the diff**

```bash
.venv/Scripts/python.exe - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, "D:/Projects/financial-advisor/tools/codex-tandem/scripts")
from engine_codex import run_codex
r = run_codex(node_dir=Path("D:/Projects/financial-advisor"), prompt="""
Review Task 3 diff. Files:
- argosy/api/routes/advisor.py (post_check_in + new _run_synthesis_background)
- tests/test_advisor_route.py (3 updated tests + 1 new)

Verify:
1. Baseline guard runs BEFORE the DecisionRun is created — confirm by reading
   the code path: get_active_baseline check, raise HTTPException, THEN
   db.add(DecisionRun) + commit. The 404-no-leak test covers this.
2. The background wrapper opens its OWN sync Session and does not reuse the
   request-scoped `db` (which is closed by FastAPI Depends teardown).
3. On exception in the background task: the row is marked 'failed' via a
   re-opened session (not the partially-rolled-back original).
4. The response includes `decision_audit_token` matching f"plan-synth-{id}".
5. Test `test_post_advisor_checkin_marks_decision_run_failed_on_exception`
   actually exercises the failure path — confirm TestClient drains background
   tasks before the test reads the DecisionRun status.
6. No regression to home_brief cache invalidation (it happens inside
   run_synthesis, so it's now in the background path — confirm the existing
   test still passes by exercising that path).

Run `git diff HEAD` to see changes. LOOKS GOOD or BLOCKERS.
""", agent_name="task3_checkin_async", role="reviewer")
print(r.verdict_text)
PY
```

Address BLOCKERS. Known likely concerns: `get_active_baseline` may not be exported from `plan_synthesis.__init__` — fix the import path. The background-task session may need additional cleanup (engine dispose) on error paths.

- [ ] **Step 10: Commit**

```bash
git add argosy/api/routes/advisor.py tests/test_advisor_route.py
git commit -m "$(cat <<'EOF'
feat(check-in): make POST /api/advisor/check-in truly async

Was: synchronous, blocked the HTTP response until run_synthesis finished
(potentially minutes), preventing UI from rendering a live agent cascade
because the decision_run_id arrived only at the end.

Now: baseline guard first → pre-create DecisionRun row → return 202 with
{decision_run_id, decision_audit_token, draft_id:null} → schedule run_synthesis
via FastAPI BackgroundTasks. Background wrapper opens its own sync Session
(the Depends(get_db) session is closed when the response returns) and marks
the row 'failed' on exception.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Extend `useDecisionStream` to accept `decisionId` filter

**Files:**
- Modify: `ui/src/lib/useDecisionStream.ts:210, 557-573`

**Why:** The hook currently filters by `turnId` only. Spec section 4 said "useDecisionStream itself is not modified" but inspection reveals the hook computes per-decision aggregates internally; doing the filter inside the hook is far cleaner than post-filtering rows in the panel. This is a small additive change: an extra optional `decisionId` filter, applied after `turnId` if `turnId` is unset.

- [ ] **Step 1: Edit the hook signature + filter logic**

In `ui/src/lib/useDecisionStream.ts`, find the function signature around line 210:

```typescript
export function useDecisionStream(
  userId: string,
  opts?: { turnId?: string; decisionId?: string },  // ADD decisionId
): { decisions: DecisionGroup[] }
```

And the filter block around line 557-573:

```typescript
const turnId = opts?.turnId;
const decisionId = opts?.decisionId;  // NEW

// ... existing memo body ...

// Apply turnId filter if requested, else decisionId filter if requested.
const filtered = turnId
  ? allRows.filter((r) => r.turn_id === turnId)
  : decisionId
    ? allRows.filter((r) => r.decision_id === decisionId)
    : allRows;
```

Update the memo dependency array to include `decisionId`:

```typescript
}, [byCorrelationId, restRows, turnId, decisionId, wireGroupsMap]);
```

- [ ] **Step 2: Verify the hook still typechecks**

```bash
cd ui ; npm run typecheck
```

Expected: PASS. If existing callers (the panel passing only `turnId`) break, it's because the new field's typing is stricter than expected — `decisionId?: string` is optional so existing callers should be unaffected.

- [ ] **Step 3: Commit**

```bash
git add ui/src/lib/useDecisionStream.ts
git commit -m "$(cat <<'EOF'
feat(ui/hook): useDecisionStream accepts optional decisionId filter

Additive: existing turnId-only callers unchanged. New decisionId filter
matches against AgentRow.decision_id (string audit token like
"plan-synth-<N>"). When both are unset the hook returns all rows.

Used by the plan-tab synthesis live cascade.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Add `decisionId` prop to `AgentCascadePanel`

**Files:**
- Modify: `ui/src/components/advisor/AgentCascadePanel.tsx:28-37, 93-101`

**Why:** Expose the new hook filter through the panel's existing prop interface so `/plan` can use the panel verbatim.

- [ ] **Step 1: Edit the props type around line 28**

```typescript
type AgentCascadePanelProps = {
  userId: string;
  /** null when no turn is in flight. Keep set after POST resolves so the
   *  panel stays visible; reset at the top of the NEXT call to askNext. */
  turnId: string | null;
  /** Filter by decision_id (string audit token, e.g. "plan-synth-42").
   *  Mutually exclusive with turnId — pass exactly one. */
  decisionId?: string | null;
  /** true once api.advisorTurn() has returned (either success or error). */
  isResolved: boolean;
  /** Backend-status / last-agent-step diagnostic line, visually subordinated. */
  diagnosticLine?: React.ReactNode;
};
```

- [ ] **Step 2: Edit the function signature + hook call around line 93**

```typescript
export function AgentCascadePanel({
  userId,
  turnId,
  decisionId,            // NEW
  isResolved,
  diagnosticLine,
}: AgentCascadePanelProps) {
  const { decisions } = useDecisionStream(userId, {
    turnId: turnId ?? undefined,
    decisionId: decisionId ?? undefined,   // NEW
  });
```

- [ ] **Step 3: Adjust the "nothing to show" guard around line 136**

The current guard is `if (turnId === null && allRows.length === 0) return null`. Widen it so the panel also stays mounted (with the cascade visible) when a decisionId is set and rows are still streaming:

```typescript
// Nothing to show when neither filter is set AND there are no rows.
if (turnId === null && (decisionId === null || decisionId === undefined) && allRows.length === 0) return null;
```

- [ ] **Step 4: Typecheck + lint**

```bash
cd ui ; npm run typecheck ; npm run lint
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/advisor/AgentCascadePanel.tsx
git commit -m "$(cat <<'EOF'
feat(ui/panel): AgentCascadePanel accepts optional decisionId prop

Forwards to useDecisionStream's new decisionId filter. Mutually exclusive
with turnId. Render guard widened so panel stays mounted while a
decisionId is set even before the first row streams in.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Update `api.ts` `advisorCheckIn` return type

**Files:**
- Modify: `ui/src/lib/api.ts:845`

**Why:** Match the new backend response shape (`decision_audit_token` added, `draft_id` widened to nullable).

- [ ] **Step 1: Edit the helper**

```typescript
advisorCheckIn: (userId: string, guidance = "") =>
  postJSON<{
    status: string;
    decision_run_id: number;
    decision_audit_token: string;   // NEW — e.g. "plan-synth-42"
    draft_id: number | null;        // null until plan.draft.completed WS event fires
  }>(
    `/api/advisor/check-in`,
    { user_id: userId, guidance, urgency: "now" },
  ),
```

- [ ] **Step 2: Typecheck**

```bash
cd ui ; npm run typecheck
```

Expected: PASS (no existing callers consume `draft_id` since the helper had zero callers before this feature).

- [ ] **Step 3: Commit**

```bash
git add ui/src/lib/api.ts
git commit -m "$(cat <<'EOF'
feat(ui/api): advisorCheckIn return type matches new async response shape

Adds decision_audit_token (string) and widens draft_id to number | null.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Wire `/plan` page — button, state, cascade, WS completion

**Files:**
- Modify: `ui/src/app/plan/page.tsx:1-180`

**Why:** The user-facing piece. Adds the button to the existing header, drives synthesis kickoff, renders the cascade, and handles completion via the existing `plan.draft.completed` WS event.

- [ ] **Step 1: Add imports**

At the top of `ui/src/app/plan/page.tsx`, add the new imports (preserving the existing ones):

```typescript
"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { AgentCascadePanel } from "@/components/advisor/AgentCascadePanel";
import { Markdown } from "@/components/markdown";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type PlanCurrentDTO } from "@/lib/api";
import { useWSEvents } from "@/lib/ws";

const USER_ID = "ariel";
```

(`Link` from `next/link`, `AgentCascadePanel`, `useWSEvents` are the new ones.)

- [ ] **Step 2: Add state inside `PlanPage`**

Right under the existing `useState` lines (`plan`, `loading`, `running`, `error`), add:

```typescript
const [synthesisDecisionToken, setSynthesisDecisionToken] = useState<string | null>(null);
const [synthesisRunning, setSynthesisRunning] = useState(false);
const [synthesisDraftId, setSynthesisDraftId] = useState<number | null>(null);
const [synthesisError, setSynthesisError] = useState<string | null>(null);
```

- [ ] **Step 3: Add the click handler**

Below the existing `onRecritique` callback:

```typescript
const onRunSynthesis = useCallback(async () => {
  setSynthesisError(null);
  setSynthesisRunning(true);
  setSynthesisDraftId(null);
  try {
    const r = await api.advisorCheckIn(USER_ID);
    setSynthesisDecisionToken(r.decision_audit_token);
  } catch (e) {
    setSynthesisError(String(e));
    setSynthesisRunning(false);
  }
}, []);
```

- [ ] **Step 4: Add the WS subscription for completion**

Below the click handler:

```typescript
useWSEvents({
  topics: ["plan.draft.completed"],
  onEvent: (e) => {
    const payload = e.payload as { user_id?: string; draft_id?: number };
    if (payload.user_id !== USER_ID) return;
    if (synthesisDecisionToken === null) return;  // not our run
    if (typeof payload.draft_id === "number") setSynthesisDraftId(payload.draft_id);
    setSynthesisRunning(false);
    refresh();  // re-fetch planCurrent
  },
});
```

Note: the exact `useWSEvents` signature may differ — read `ui/src/lib/ws.ts` first and adapt accordingly. The signature above is illustrative.

- [ ] **Step 5: Add the button to the header**

Locate the existing header `<Button>` for `Re-critique now` (around line 83). Replace the surrounding container with a flex group of two buttons:

```tsx
<div className="flex items-center gap-2">
  <Button
    variant="default"
    onClick={onRunSynthesis}
    disabled={synthesisRunning || !plan?.plan_version_id}
    title={!plan?.plan_version_id ? "Import a baseline plan first" : undefined}
  >
    {synthesisRunning ? "Synthesizing…" : "Run synthesis"}
  </Button>
  <Button
    variant="outline"
    onClick={onRecritique}
    disabled={running || !plan?.plan_version_id}
  >
    {running ? "Re-critiquing…" : "Re-critique now"}
  </Button>
</div>
```

Note: `Re-critique` demoted to `variant="outline"` so the primary action is the new synthesis button. Verify this with eyes on the running app; revert variant if it looks wrong.

- [ ] **Step 6: Render the cascade between header and Critique findings card**

After the error/loading lines (around line 92), before the `{critique && ...}` Critique findings card:

```tsx
{synthesisError && (
  <p className="text-sm text-error font-mono">{synthesisError}</p>
)}

{synthesisDecisionToken !== null && (
  <AgentCascadePanel
    userId={USER_ID}
    turnId={null}
    decisionId={synthesisDecisionToken}
    isResolved={!synthesisRunning}
  />
)}

{!synthesisRunning && synthesisDraftId !== null && (
  <p className="text-sm">
    Draft #{synthesisDraftId} ready ·{" "}
    <Link href="/proposals" className="text-primary hover:underline">
      → Review draft on /proposals
    </Link>
  </p>
)}
```

- [ ] **Step 7: Typecheck + lint**

```bash
cd ui ; npm run typecheck ; npm run lint
```

Expected: PASS. Fix any type errors by reading the actual types in `useWSEvents`, `AgentCascadePanel`, etc.

- [ ] **Step 8: Commit**

```bash
git add ui/src/app/plan/page.tsx
git commit -m "$(cat <<'EOF'
feat(plan): add Run synthesis button + live agent cascade

Header gets a primary [Run synthesis] button alongside Re-critique now.
Click POSTs /api/advisor/check-in; the returned decision_audit_token drives
AgentCascadePanel which streams the Opus fleet (9 analysts → debates →
synthesizer → risk → fund manager) live. Completion via plan.draft.completed
WS event: cascade collapses, inline link to /proposals appears, plan data
re-fetched.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Live browser verification

**Why:** No frontend test runner is wired (binding pref). The browser is the only end-to-end check.

- [ ] **Step 1: Start the backend**

In one terminal:

```bash
$env:ARGOSY_EXPENSE_SAMPLES_ROOT = "D:/Google Drive/Family/Finances/Portfolio/Resources"
.venv/Scripts/python.exe -m uvicorn argosy.api.main:create_app --factory --host 127.0.0.1 --port 8000
```

Wait for `Application startup complete`.

- [ ] **Step 2: Start the UI**

In a second terminal:

```bash
cd ui ; npm run dev
```

Wait for `ready - started server on 0.0.0.0:1337`.

- [ ] **Step 3: Confirm button visibility + disabled state**

Open `http://localhost:1337/plan`. Verify:
- `[Run synthesis]` button present in the header, next to `[Re-critique now]`.
- Button is **enabled** if a baseline plan exists for `ariel`; **disabled with tooltip** "Import a baseline plan first" otherwise.

If disabled but you expected enabled: verify a baseline PlanVersion exists for ariel in `db/argosy.db`. If needed, run `argosy ingest plan <path>` first.

- [ ] **Step 4: Click and observe**

Click `[Run synthesis]`. Within ~1 s expect:
- Button label changes to `Synthesizing…` and becomes disabled.
- The `<AgentCascadePanel>` appears between the header and the Critique findings card.
- Within a few seconds, agent rows start streaming: `FundamentalsAnalystAgent`, `TechnicalAnalystAgent`, … (phase 1, 9 parallel analysts), then phase-2 bull/bear/facilitator per horizon, etc.

If no rows appear within 30 s: open browser devtools → Network → WS frame inspector; confirm `agent.run.started` events are arriving. If they are but the panel shows nothing, the row filter likely doesn't match — check that `row.decision_id` on the WS payload matches the panel's `decisionId` exactly (both strings, same `"plan-synth-<N>"` format).

- [ ] **Step 5: Confirm cascade scoping**

While synthesis is running, open another tab to `/advisor` and start a chat turn (or just leave it idle). Confirm the `/plan` cascade does NOT pull in unrelated rows (advisor turns shouldn't appear because their decision_id differs).

- [ ] **Step 6: Wait for completion**

Wait until the cascade shows ~15-20 agent rows and a "Cascade complete: X agents · $Y.YY · Zs" summary line. Verify:
- Button label returns to `Run synthesis`, enabled again.
- Inline line appears: `Draft #N ready · → Review draft on /proposals`.
- Critique findings card updates (or remains the same, if synthesis didn't produce a new critique — that's fine; the spec only requires plan data re-fetched).

- [ ] **Step 7: Click the proposals link**

Click `→ Review draft on /proposals`. Land on `/proposals`. Verify the new draft is the top row (`role="draft"`, recent timestamp).

- [ ] **Step 8: Negative case — no baseline plan**

In a fresh DB (or for a user with no baseline), confirm the button is disabled with the tooltip. If the button is enabled but POST returns 404, the spec's "disabled when no baseline" gate failed — fix in Task 7.

- [ ] **Step 9: Document the verification**

Add a one-line note to commit log or session handover indicating live verification passed:

```bash
git commit --allow-empty -m "$(cat <<'EOF'
chore: verify plan-tab synthesis button works end-to-end in browser

Manual smoke per binding pref (no UI test runner). Confirmed:
- Button visible/disabled correctly based on baseline presence
- Click fires synthesis; cascade streams ~15-20 agents over the run
- decision_audit_token filter scopes cascade to this run only
- plan.draft.completed event collapses the cascade + shows draft link
- /proposals link lands on the new draft row

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-review (post-write check)

### Spec coverage

| Spec section | Task |
|---|---|
| §1 `/check-in` async | Task 3 |
| §2 decision_id to all 5 phases | Task 2 |
| §3 finished event decision_id | Task 1 |
| §4 AgentCascadePanel decisionId prop | Task 5 (panel) + Task 4 (hook) |
| §5 /plan page wiring | Task 7 |
| §6 api.ts type | Task 6 |
| Tests | Tasks 1, 2, 3 (backend); Task 8 (frontend live) |

All covered. The spec said "useDecisionStream itself is not modified" but Task 4 modifies it additively — see Task 4's "Why" section for the deviation reason. Codex will see this in the Task 4 commit.

### Placeholder scan

No "TBD" / "TODO" / "fill in details". One acknowledged in Task 3 step 6: `_log` variable name and `get_active_baseline` import path noted for verification at edit time — these are real-code lookups, not unresolved ambiguity.

### Type consistency

- Backend: `decision_id: str` throughout (phase helpers pass the string audit token; AgentReport schema is String column).
- Frontend: `decisionId?: string | null` on hook + panel; state `synthesisDecisionToken: string | null` on page.
- API response: `decision_audit_token: string` matching the panel's `decisionId` prop.

Consistent across tasks.
