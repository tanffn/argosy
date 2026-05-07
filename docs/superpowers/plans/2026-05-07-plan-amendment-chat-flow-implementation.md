# Plan Amendment Chat Flow Implementation Plan (Wave 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a tiered amendment path to the advisor chat so users can request structural plan changes ("tighten my NVDA cap", "shift toward growth", "re-evaluate everything") and the advisor classifies the request as Small / Medium / Large and dispatches accordingly — instant inline Delta, ~30s lightweight synth, or ~15min full synth — without blocking the chat UI.

**Architecture:** The advisor's existing structured turn output gains an `amendment` field carrying tier classification + (for Small) a fully-formed Delta. The `POST /api/advisor/turn` route reads that field and dispatches: Small applies inline via existing PATCH endpoints; Medium and Large open a `DecisionRun` row, spawn a worker via `asyncio.to_thread`, and return 202. Workers emit WebSocket events on completion that trigger an in-app banner plus an opt-in browser-level notification. All paths produce a `role=draft` PlanVersion the user reviews via the existing `<PlanRevisionSheet>`.

**Tech Stack:** Python 3.12, SQLAlchemy 2 + alembic, FastAPI, pydantic v2, claude-agent-sdk (Wave 2 backend), Next.js 15, WebSocket events via existing `argosy/api/events.py`, Web Notifications API (browser-native).

---

## Files this wave creates or modifies

- Create: `alembic/versions/0018_decision_runs_amendment.py`
- Create: `argosy/agents/advisor_amendment_types.py`
- Create: `argosy/orchestrator/flows/plan_amendment/__init__.py`
- Create: `argosy/orchestrator/flows/plan_amendment/_types.py`
- Create: `argosy/orchestrator/flows/plan_amendment/classifier.py`
- Create: `argosy/orchestrator/flows/plan_amendment/dispatcher.py`
- Create: `argosy/orchestrator/flows/plan_amendment/workers.py`
- Create: `tests/test_migration_0018.py`
- Create: `tests/test_advisor_amendment_types.py`
- Create: `tests/test_plan_amendment_classifier.py`
- Create: `tests/test_plan_amendment_dispatcher.py`
- Create: `tests/test_plan_amendment_workers.py`
- Create: `tests/test_advisor_amendment_route.py`
- Create: `tests/test_plan_amendment_e2e.py`
- Create: `ui/src/lib/notifications.ts`
- Modify: `argosy/state/models.py` (DecisionRun: `tier`, `notes_json` columns)
- Modify: `argosy/agents/advisor.py` (turn schema + prompt addendum)
- Modify: `argosy/api/routes/advisor.py` (`/turn` reads amendment + new `/amendment/{id}/cancel`)
- Modify: `ui/src/app/advisor/page.tsx` (status pill, system messages, permission flow)
- Modify: `ui/src/lib/api.ts` (new types + `advisorAmendmentCancel` method)
- Modify: `docs/design/SDD.md` (new §6.13; updates to §10.1, §11.3)

---

### Task 4.1: Migration 0018 — `decision_runs` amendment columns + concurrency index

**Files:**
- Create: `alembic/versions/0018_decision_runs_amendment.py`
- Create: `tests/test_migration_0018.py`
- Modify: `argosy/state/models.py` (DecisionRun ORM reflection)

- [ ] **Step 1: Write the failing test**

Create `tests/test_migration_0018.py`:

```python
"""Schema assertions after migration 0018 (decision_runs amendment columns)."""

from sqlalchemy import inspect


def _columns(engine, table):
    insp = inspect(engine)
    return {c["name"]: c for c in insp.get_columns(table)}


def test_0018_adds_tier_column(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "decision_runs")
    assert "tier" in cols
    assert cols["tier"]["nullable"] is True
    assert "VARCHAR" in str(cols["tier"]["type"]).upper() or "TEXT" in str(cols["tier"]["type"]).upper()


def test_0018_adds_notes_json_column(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "decision_runs")
    assert "notes_json" in cols
    assert cols["notes_json"]["nullable"] is True


def test_0018_creates_partial_unique_index(alembic_engine_at_head):
    insp = inspect(alembic_engine_at_head)
    idx_names = {i["name"] for i in insp.get_indexes("decision_runs")}
    assert "ix_decision_runs_one_amendment_running_per_user" in idx_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_migration_0018.py -v`
Expected: FAIL.

- [ ] **Step 3: Write the migration**

Create `alembic/versions/0018_decision_runs_amendment.py`:

```python
"""decision_runs amendment columns + per-user running-amendment index (Wave 4).

Revision ID: 0018_decision_runs_amendment
Revises: 0017_plan_versions_synthesis
Create Date: 2026-05-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_decision_runs_amendment"
down_revision: str | Sequence[str] | None = "0017_plan_versions_synthesis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("decision_runs") as batch:
        batch.add_column(sa.Column("tier", sa.String(8), nullable=True))
        batch.add_column(sa.Column("notes_json", sa.Text(), nullable=True))

    op.create_index(
        "ix_decision_runs_one_amendment_running_per_user",
        "decision_runs",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text(
            "decision_kind='plan_amendment_chat' AND status='running'"
        ),
        sqlite_where=sa.text(
            "decision_kind='plan_amendment_chat' AND status='running'"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_decision_runs_one_amendment_running_per_user",
        table_name="decision_runs",
    )
    with op.batch_alter_table("decision_runs") as batch:
        batch.drop_column("notes_json")
        batch.drop_column("tier")
```

- [ ] **Step 4: Reflect on the model**

Edit `argosy/state/models.py` `DecisionRun` class. Append in the same column block style as the existing `decision_kind` (added in Wave 2 fix C2):

```python
    tier: Mapped[str | None] = mapped_column(String(8), nullable=True)
    notes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_migration_0018.py -v`
Expected: PASS (3 tests).

Reversibility:

```bash
python -c "from alembic.config import Config; from alembic import command; cfg = Config('alembic.ini'); cfg.set_main_option('sqlalchemy.url', 'sqlite:///./scratch_0018.db'); command.upgrade(cfg, 'head'); command.downgrade(cfg, '0017_plan_versions_synthesis'); command.upgrade(cfg, 'head')"
```

Delete the scratch DB.

- [ ] **Step 6: Run regression suite**

Run: `pytest -m "not llm_eval" -q`
Expected: 676 passed (Wave 3 baseline) + 3 new = 679 passed.

- [ ] **Step 7: Commit**

```bash
git add alembic/versions/0018_decision_runs_amendment.py tests/test_migration_0018.py argosy/state/models.py
git commit -m "feat(db): migration 0018 — decision_runs amendment columns + concurrency index"
```

---

### Task 4.2: Pydantic types — `AmendmentIntent`, `AmendmentResultDTO`

**Files:**
- Create: `argosy/agents/advisor_amendment_types.py`
- Create: `tests/test_advisor_amendment_types.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_advisor_amendment_types.py`:

```python
"""Tests for advisor amendment types (Wave 4)."""

from __future__ import annotations

import pytest


def test_amendment_intent_small_tighten_with_delta():
    from argosy.agents.advisor_amendment_types import AmendmentIntent
    from argosy.agents.plan_synthesizer_types import Delta

    delta = Delta(
        item_kind="target", item_id="medium.targets.nvda", horizon="medium",
        change_kind="modified", summary="NVDA cap 15% -> 12%",
        prior={"value": 0.15}, proposed={"value": 0.12},
        rationale="user-initiated tightening",
    )
    intent = AmendmentIntent(
        tier="small",
        direction="tighten",
        proposed_delta=delta,
        rationale="single-target tightening, explicit numbers given",
    )
    payload = intent.model_dump_json()
    intent2 = AmendmentIntent.model_validate_json(payload)
    assert intent2.tier == "small"
    assert intent2.direction == "tighten"
    assert intent2.proposed_delta.item_id == "medium.targets.nvda"


def test_amendment_intent_medium_no_delta():
    from argosy.agents.advisor_amendment_types import AmendmentIntent

    intent = AmendmentIntent(
        tier="medium",
        rationale="theme shift on medium horizon, multi-target reasoning needed",
    )
    assert intent.tier == "medium"
    assert intent.direction is None
    assert intent.proposed_delta is None


def test_amendment_intent_large_no_delta():
    from argosy.agents.advisor_amendment_types import AmendmentIntent

    intent = AmendmentIntent(
        tier="large",
        rationale="user said 're-evaluate everything'; structural rethink",
    )
    assert intent.tier == "large"


def test_amendment_intent_rejects_unknown_tier():
    from argosy.agents.advisor_amendment_types import AmendmentIntent
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AmendmentIntent(tier="huge", rationale="x")


def test_amendment_result_dto_round_trips():
    from argosy.agents.advisor_amendment_types import AmendmentResultDTO

    dto = AmendmentResultDTO(
        tier="small", decision_run_id=42, status="applied", draft_id=99,
    )
    payload = dto.model_dump_json()
    dto2 = AmendmentResultDTO.model_validate_json(payload)
    assert dto2.draft_id == 99
    assert dto2.eta_seconds is None


def test_amendment_result_dto_running_carries_eta():
    from argosy.agents.advisor_amendment_types import AmendmentResultDTO

    dto = AmendmentResultDTO(
        tier="medium", decision_run_id=42, status="running", eta_seconds=30,
    )
    assert dto.eta_seconds == 30
    assert dto.draft_id is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_advisor_amendment_types.py -v`
Expected: FAIL.

- [ ] **Step 3: Write the types module**

Create `argosy/agents/advisor_amendment_types.py`:

```python
"""Pydantic types for the advisor's plan-amendment-chat flow (Wave 4).

The advisor's structured turn output gains an `amendment` field of type
`AmendmentIntent | None`. The API route reads that and emits an
`AmendmentResultDTO` to the chat client.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.plan_synthesizer_types import Delta


class AmendmentIntent(BaseModel):
    """Advisor's classification of a chat-borne plan amendment request.

    The advisor emits this in its structured turn output when it judges
    the latest user message asks for a plan change. The dispatcher
    reads it and routes:
      - tier="small" + direction="tighten" + proposed_delta → apply inline
      - tier="small" + direction in {"loosen","ambiguous"} → escalate to medium
      - tier="medium" → dispatch lightweight synth worker
      - tier="large" → dispatch full synth worker
    """

    tier: Literal["small", "medium", "large"]
    direction: Literal["tighten", "loosen", "ambiguous"] | None = None
    proposed_delta: Delta | None = None
    rationale: str
    requires_confirmation: bool = False


class AmendmentResultDTO(BaseModel):
    """API surface emitted on `POST /api/advisor/turn` when the turn
    classified an amendment.

    Status semantics:
      - "applied": Small Delta was applied; draft_id points at the affected draft.
      - "running": Medium/Large worker dispatched; decision_run_id and eta_seconds populated.
      - "needs_confirmation": concurrency conflict or ambiguous direction;
        advisor's turn text asks the user to clarify.
      - "cancelled_existing": user said "cancel and restart"; the prior
        run is cancelled, this turn confirms.
    """

    tier: Literal["small", "medium", "large"]
    decision_run_id: int
    status: Literal["applied", "running", "needs_confirmation", "cancelled_existing"]
    draft_id: int | None = None
    eta_seconds: int | None = None


__all__ = ["AmendmentIntent", "AmendmentResultDTO"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_advisor_amendment_types.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add argosy/agents/advisor_amendment_types.py tests/test_advisor_amendment_types.py
git commit -m "feat(agents): pydantic types for advisor amendment intent + result DTO"
```

---

### Task 4.3: AdvisorAgent — amendment field in turn schema + prompt addendum

**Files:**
- Modify: `argosy/agents/advisor.py`
- Modify: existing tests for AdvisorAgent (find via `grep`); add new test cases

- [ ] **Step 1: Discover the advisor turn schema location**

Run: `grep -rn "class AdvisorTurn" argosy/`
Expected: locates the existing turn output pydantic model. Note the file path and current fields.

- [ ] **Step 2: Write the failing test**

Append to whichever test file exercises advisor agent prompts (likely `tests/test_advisor_agent.py` — verify with `ls tests/ | grep advisor`). If absent, create it. Append:

```python
def test_advisor_turn_carries_optional_amendment_field():
    """AdvisorTurn now has an optional amendment: AmendmentIntent | None field."""
    from argosy.agents.advisor import AdvisorTurn  # adjust import if elsewhere
    from argosy.agents.advisor_amendment_types import AmendmentIntent

    turn = AdvisorTurn(
        text="I'll apply this as a small tightening.",
        amendment=AmendmentIntent(
            tier="small",
            direction="tighten",
            rationale="single target, explicit numbers",
        ),
    )
    payload = turn.model_dump_json()
    turn2 = AdvisorTurn.model_validate_json(payload)
    assert turn2.amendment.tier == "small"


def test_advisor_turn_amendment_optional_is_none_by_default():
    from argosy.agents.advisor import AdvisorTurn

    turn = AdvisorTurn(text="just chatting")
    assert turn.amendment is None


def test_advisor_prompt_includes_amendment_classification_block():
    """When the user has a current plan, the system prompt must include the
    amendment classification instructions."""
    from argosy.agents.advisor import AdvisorAgent

    agent = AdvisorAgent(user_id="ariel")
    sys, _usr = agent.build_prompt(
        current_stage="post_intake",
        mode="open",
        last_user_message="tighten NVDA cap to 12%",
        chat_history=[],
        has_current_plan=True,
    )
    assert "AMENDMENT INTENT DETECTION" in sys
    assert "small" in sys.lower() and "medium" in sys.lower() and "large" in sys.lower()


def test_advisor_prompt_omits_amendment_block_without_current_plan():
    """If the user has no current plan, the amendment block is omitted."""
    from argosy.agents.advisor import AdvisorAgent

    agent = AdvisorAgent(user_id="ariel")
    sys, _usr = agent.build_prompt(
        current_stage="post_intake",
        mode="open",
        last_user_message="hello",
        chat_history=[],
        has_current_plan=False,
    )
    assert "AMENDMENT INTENT DETECTION" not in sys
```

The exact `build_prompt` signature may differ; the test reveals what kwargs the actual implementation already takes. If `has_current_plan` doesn't exist yet, add it as a kwarg-only parameter with default `False` in step 4.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_advisor_agent.py -v -k "amendment or current_plan"`
Expected: FAIL.

- [ ] **Step 4: Add the optional field to `AdvisorTurn`**

Find the `AdvisorTurn` class in `argosy/agents/advisor.py` (or wherever step 1 located it). Add the import and the field:

```python
from argosy.agents.advisor_amendment_types import AmendmentIntent


class AdvisorTurn(BaseModel):
    # ... existing fields ...
    amendment: AmendmentIntent | None = None
```

Place the field after the existing optional fields, before the closing.

- [ ] **Step 5: Add `has_current_plan` parameter + amendment prompt block**

Edit `AdvisorAgent.build_prompt`. Add `has_current_plan: bool = False` as a keyword-only parameter. After the existing system-prompt construction and before returning, append the amendment block when `has_current_plan` is True:

```python
        if has_current_plan:
            system = system + (
                "\n\nAMENDMENT INTENT DETECTION\n\n"
                "If the user's latest message asks to change something about their current "
                "plan (a target, theme, action, or speculative candidate), classify it:\n"
                "  small  - strict tightening of one specific target/action they reference\n"
                "           directly. Direction must be \"tighten\" (lowers risk surface);\n"
                "           \"loosen\" or \"ambiguous\" — use medium instead.\n"
                "  medium - theme shift on one horizon, multi-target tweak, loosening, or\n"
                "           any change that involves cross-target reasoning.\n"
                "  large  - structural rethink, cross-horizon, \"re-evaluate everything\",\n"
                "           \"run synthesis\", or any request that asks the fleet to\n"
                "           reconsider.\n\n"
                "Emit the classification in the `amendment` field of your structured output.\n"
                "For small with direction=tighten, also emit a fully-formed `proposed_delta`\n"
                "with item_id, item_kind, horizon, change_kind, summary, prior, proposed,\n"
                "rationale, and accepted=true.\n\n"
                "Be conservative: when in doubt, classify as medium. The user can always say\n"
                "\"do a full synthesis\" to escalate to large; they cannot easily reverse a\n"
                "hasty small Delta.\n"
            )
```

- [ ] **Step 6: Update the `AdvisorAgent.run_sync` callers (if any) that pass kwargs to `build_prompt`**

Run: `grep -rn "advisor.*build_prompt\|AdvisorAgent.*run_sync" argosy/ tests/`

For each call site, decide whether to thread `has_current_plan` through. The `/api/advisor/turn` route is the main caller; update Task 4.8 will handle that.

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_advisor_agent.py -v -k "amendment or current_plan"`
Expected: PASS (4 new tests).

Run regression: `pytest tests/test_advisor_agent.py tests/test_advisor_route.py -v`
Expected: all existing tests still pass.

- [ ] **Step 8: Commit**

```bash
git add argosy/agents/advisor.py tests/test_advisor_agent.py
git commit -m "feat(advisor): amendment field in turn schema + classification prompt block"
```

---

### Task 4.4: Classifier — extract + validate `AmendmentIntent`

**Files:**
- Create: `argosy/orchestrator/flows/plan_amendment/__init__.py` (empty placeholder; populated in Task 4.7)
- Create: `argosy/orchestrator/flows/plan_amendment/_types.py`
- Create: `argosy/orchestrator/flows/plan_amendment/classifier.py`
- Create: `tests/test_plan_amendment_classifier.py`

- [ ] **Step 1: Scaffold the package**

```bash
mkdir -p argosy/orchestrator/flows/plan_amendment
touch argosy/orchestrator/flows/plan_amendment/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_plan_amendment_classifier.py`:

```python
"""Tests for plan_amendment classifier (Wave 4)."""

from __future__ import annotations

import pytest


def _make_intent(**kw):
    from argosy.agents.advisor_amendment_types import AmendmentIntent
    base = dict(tier="medium", rationale="x")
    base.update(kw)
    return AmendmentIntent(**base)


def _make_delta():
    from argosy.agents.plan_synthesizer_types import Delta
    return Delta(
        item_kind="target", item_id="medium.targets.nvda", horizon="medium",
        change_kind="modified", summary="x",
        prior={"value": 0.15}, proposed={"value": 0.12},
    )


def test_classify_small_tighten_with_delta_passes_through():
    from argosy.orchestrator.flows.plan_amendment.classifier import classify
    from argosy.orchestrator.flows.plan_amendment._types import EffectiveTier

    intent = _make_intent(tier="small", direction="tighten", proposed_delta=_make_delta())
    out = classify(intent)
    assert out.effective_tier == EffectiveTier.SMALL
    assert out.proposed_delta is not None


def test_classify_small_loosen_escalates_to_medium():
    from argosy.orchestrator.flows.plan_amendment.classifier import classify
    from argosy.orchestrator.flows.plan_amendment._types import EffectiveTier

    intent = _make_intent(tier="small", direction="loosen", proposed_delta=_make_delta())
    out = classify(intent)
    assert out.effective_tier == EffectiveTier.MEDIUM
    assert out.escalation_reason == "small_with_loosen_direction"


def test_classify_small_ambiguous_escalates_to_medium():
    from argosy.orchestrator.flows.plan_amendment.classifier import classify
    from argosy.orchestrator.flows.plan_amendment._types import EffectiveTier

    intent = _make_intent(tier="small", direction="ambiguous", proposed_delta=_make_delta())
    out = classify(intent)
    assert out.effective_tier == EffectiveTier.MEDIUM


def test_classify_small_without_delta_escalates_to_medium():
    from argosy.orchestrator.flows.plan_amendment.classifier import classify
    from argosy.orchestrator.flows.plan_amendment._types import EffectiveTier

    intent = _make_intent(tier="small", direction="tighten", proposed_delta=None)
    out = classify(intent)
    assert out.effective_tier == EffectiveTier.MEDIUM
    assert out.escalation_reason == "small_without_delta"


def test_classify_medium_passes_through():
    from argosy.orchestrator.flows.plan_amendment.classifier import classify
    from argosy.orchestrator.flows.plan_amendment._types import EffectiveTier

    intent = _make_intent(tier="medium")
    out = classify(intent)
    assert out.effective_tier == EffectiveTier.MEDIUM
    assert out.escalation_reason is None


def test_classify_large_passes_through():
    from argosy.orchestrator.flows.plan_amendment.classifier import classify
    from argosy.orchestrator.flows.plan_amendment._types import EffectiveTier

    intent = _make_intent(tier="large")
    out = classify(intent)
    assert out.effective_tier == EffectiveTier.LARGE
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_plan_amendment_classifier.py -v`
Expected: FAIL.

- [ ] **Step 4: Write the types**

Create `argosy/orchestrator/flows/plan_amendment/_types.py`:

```python
"""Internal types for plan_amendment flow (Wave 4).

Public types live in argosy.agents.advisor_amendment_types. These are
the post-classification effective values used internally by the
dispatcher + workers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from argosy.agents.plan_synthesizer_types import Delta


class EffectiveTier(str, Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


@dataclass(frozen=True)
class ClassificationResult:
    """Output of the classifier — what the dispatcher should actually do."""

    effective_tier: EffectiveTier
    proposed_delta: Delta | None
    escalation_reason: str | None  # set when effective_tier != requested tier


__all__ = ["EffectiveTier", "ClassificationResult"]
```

- [ ] **Step 5: Write the classifier**

Create `argosy/orchestrator/flows/plan_amendment/classifier.py`:

```python
"""Classifier — turns an AmendmentIntent (advisor LLM output) into a
ClassificationResult (effective tier + delta + escalation reason).

Pure logic, no LLM call. The advisor's tier choice is honored unless:
  - tier="small" with direction in {"loosen", "ambiguous"} → escalate to medium
  - tier="small" without proposed_delta → escalate to medium (advisor failed to emit)

Escalation is one-way: small → medium. The user can manually re-ask for
large via a follow-up turn.
"""

from __future__ import annotations

from argosy.agents.advisor_amendment_types import AmendmentIntent
from argosy.orchestrator.flows.plan_amendment._types import (
    ClassificationResult,
    EffectiveTier,
)


def classify(intent: AmendmentIntent) -> ClassificationResult:
    """Map an advisor-emitted AmendmentIntent to its effective dispatch tier."""
    if intent.tier == "small":
        if intent.direction != "tighten":
            return ClassificationResult(
                effective_tier=EffectiveTier.MEDIUM,
                proposed_delta=None,
                escalation_reason=f"small_with_{intent.direction}_direction",
            )
        if intent.proposed_delta is None:
            return ClassificationResult(
                effective_tier=EffectiveTier.MEDIUM,
                proposed_delta=None,
                escalation_reason="small_without_delta",
            )
        return ClassificationResult(
            effective_tier=EffectiveTier.SMALL,
            proposed_delta=intent.proposed_delta,
            escalation_reason=None,
        )

    if intent.tier == "medium":
        return ClassificationResult(
            effective_tier=EffectiveTier.MEDIUM,
            proposed_delta=None,
            escalation_reason=None,
        )

    # tier == "large"
    return ClassificationResult(
        effective_tier=EffectiveTier.LARGE,
        proposed_delta=None,
        escalation_reason=None,
    )


__all__ = ["classify"]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_plan_amendment_classifier.py -v`
Expected: PASS (6 tests).

- [ ] **Step 7: Commit**

```bash
git add argosy/orchestrator/flows/plan_amendment/__init__.py argosy/orchestrator/flows/plan_amendment/_types.py argosy/orchestrator/flows/plan_amendment/classifier.py tests/test_plan_amendment_classifier.py
git commit -m "feat(amendment): classifier + internal types — escalation rules"
```

---

### Task 4.5: Workers — `_medium_worker` + `_large_worker`

**Files:**
- Create: `argosy/orchestrator/flows/plan_amendment/workers.py`
- Create: `tests/test_plan_amendment_workers.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_plan_amendment_workers.py`:

```python
"""Tests for plan_amendment workers (Wave 4).

Workers are sync (called via asyncio.to_thread). They:
  - Read the existing current plan and pending draft (if any)
  - Run synthesis (Phase 3 only for medium; full 5-phase for large)
  - Persist a role=draft PlanVersion
  - Update the DecisionRun row with finished_at + status
  - Emit plan.amendment.completed via publish_event_threadsafe
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import DecisionRun, PlanVersion, User


@pytest.fixture
def session_with_baseline_and_run(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.add(PlanVersion(
        user_id="ariel", role="baseline", version_label="x", raw_markdown="# Plan",
        distillate_rendered="# Plan distillate",
    ))
    s.add(PlanVersion(
        user_id="ariel", role="current", version_label="prior",
        raw_markdown="",
        horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
        horizon_medium_json='{"horizon":"medium","freshness_expected":"quarterly","status":"no_change","posture":"x"}',
        horizon_short_json='{"horizon":"short","freshness_expected":"monthly","status":"no_change","posture":"x"}',
    ))
    run = DecisionRun(
        user_id="ariel", ticker="(plan)", tier="medium",
        decision_kind="plan_amendment_chat", status="running",
    )
    s.add(run)
    s.commit()
    s.refresh(run)
    yield s, run
    s.close()


def test_medium_worker_calls_synthesizer_with_guidance_and_prior(
    session_with_baseline_and_run, monkeypatch,
):
    from argosy.orchestrator.flows.plan_amendment import workers

    sess, run = session_with_baseline_and_run
    captured = {}

    def _fake_run_phase_3(**kw):
        captured.update(kw)
        from argosy.agents.plan_synthesizer_types import (
            HorizonSection, PlanSynthesisOutput, SynthesisInputs,
        )
        long_h = HorizonSection(
            horizon="long", freshness_expected="annual", status="no_change", posture="x"
        )
        med = HorizonSection(
            horizon="medium", freshness_expected="quarterly", status="minor_revision", posture="x",
        )
        short = HorizonSection(
            horizon="short", freshness_expected="monthly", status="no_change", posture="x"
        )
        return PlanSynthesisOutput(
            long=long_h, medium=med, short=short, inputs=SynthesisInputs(),
        )

    monkeypatch.setattr(workers, "_run_phase_3_synthesizer", _fake_run_phase_3)

    workers._medium_worker(
        session=sess, user_id="ariel", decision_run=run,
        guidance="tighten NVDA toward 12%",
    )

    sess.refresh(run)
    assert run.status == "completed"
    assert run.finished_at is not None
    assert "tighten NVDA" in captured["guidance"]
    # Should have prior_current_md populated from the existing role=current row
    assert "no_change" in captured["prior_current_md"] or len(captured["prior_current_md"]) > 0


def test_medium_worker_writes_role_draft(session_with_baseline_and_run, monkeypatch):
    from argosy.orchestrator.flows.plan_amendment import workers

    sess, run = session_with_baseline_and_run

    def _fake_run_phase_3(**kw):
        from argosy.agents.plan_synthesizer_types import (
            HorizonSection, PlanSynthesisOutput, SynthesisInputs,
        )
        return PlanSynthesisOutput(
            long=HorizonSection(horizon="long", freshness_expected="annual", status="no_change", posture="x"),
            medium=HorizonSection(horizon="medium", freshness_expected="quarterly", status="minor_revision", posture="x"),
            short=HorizonSection(horizon="short", freshness_expected="monthly", status="no_change", posture="x"),
            inputs=SynthesisInputs(),
        )

    monkeypatch.setattr(workers, "_run_phase_3_synthesizer", _fake_run_phase_3)

    workers._medium_worker(
        session=sess, user_id="ariel", decision_run=run, guidance="x",
    )

    drafts = sess.query(PlanVersion).filter_by(user_id="ariel", role="draft").all()
    assert len(drafts) == 1
    assert drafts[0].decision_run_id == run.id


def test_medium_worker_emits_completed_event(session_with_baseline_and_run, monkeypatch):
    from argosy.orchestrator.flows.plan_amendment import workers

    sess, run = session_with_baseline_and_run
    events = []

    def _fake_publish(name, payload):
        events.append((name, payload))

    monkeypatch.setattr(workers, "publish_event_threadsafe", _fake_publish)
    monkeypatch.setattr(workers, "_run_phase_3_synthesizer", lambda **kw: _stub_output())

    workers._medium_worker(session=sess, user_id="ariel", decision_run=run, guidance="x")

    names = [e[0] for e in events]
    assert "plan.amendment.completed" in names


def test_medium_worker_bails_when_cancelled(session_with_baseline_and_run, monkeypatch):
    from argosy.orchestrator.flows.plan_amendment import workers

    sess, run = session_with_baseline_and_run
    run.status = "cancelled"
    sess.commit()

    monkeypatch.setattr(workers, "_run_phase_3_synthesizer", lambda **kw: _stub_output())

    workers._medium_worker(session=sess, user_id="ariel", decision_run=run, guidance="x")

    drafts = sess.query(PlanVersion).filter_by(user_id="ariel", role="draft").all()
    assert len(drafts) == 0


def test_large_worker_delegates_to_run_synthesis(session_with_baseline_and_run, monkeypatch):
    from argosy.orchestrator.flows.plan_amendment import workers

    sess, run = session_with_baseline_and_run
    captured = {}

    def _fake_run_synthesis(session, **kw):
        captured.update(kw)
        from argosy.orchestrator.flows.plan_synthesis import SynthesisResult
        return SynthesisResult(decision_run_id=run.id, draft_id=12345)

    monkeypatch.setattr(workers, "run_synthesis", _fake_run_synthesis)

    workers._large_worker(
        session=sess, user_id="ariel", decision_run=run, guidance="re-evaluate everything",
    )

    assert captured["trigger"] == "check_in"
    assert captured["guidance"] == "re-evaluate everything"
    sess.refresh(run)
    assert run.status == "completed"


def _stub_output():
    from argosy.agents.plan_synthesizer_types import (
        HorizonSection, PlanSynthesisOutput, SynthesisInputs,
    )
    return PlanSynthesisOutput(
        long=HorizonSection(horizon="long", freshness_expected="annual", status="no_change", posture="x"),
        medium=HorizonSection(horizon="medium", freshness_expected="quarterly", status="minor_revision", posture="x"),
        short=HorizonSection(horizon="short", freshness_expected="monthly", status="no_change", posture="x"),
        inputs=SynthesisInputs(),
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_plan_amendment_workers.py -v`
Expected: FAIL.

- [ ] **Step 3: Write the workers module**

Create `argosy/orchestrator/flows/plan_amendment/workers.py`:

```python
"""Plan amendment workers — Medium (Phase 3 only) + Large (full synthesis).

Both are sync functions; the dispatcher invokes them via asyncio.to_thread
so the event loop stays free during the synthesis run.

Each worker:
  1. Checks the DecisionRun's status — bails if 'cancelled'.
  2. Runs the work (Phase 3 only for medium; run_synthesis for large).
  3. Applies the speculation cap post-filter (Wave 3 layer 2).
  4. Persists role=draft PlanVersion (medium); large persists via run_synthesis itself.
  5. Stamps DecisionRun finished_at + status='completed'.
  6. Emits plan.amendment.completed via publish_event_threadsafe.

On exception: stamps status='failed' + error_message, emits plan.amendment.failed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from argosy.agents.plan_synthesizer import PlanSynthesizerAgent
from argosy.agents.plan_synthesizer_types import (
    PlanSynthesisOutput,
    SynthesisInputs,
)
from argosy.api.events import publish_event_threadsafe
from argosy.config import get_user_agent_settings, load_speculation_cap
from argosy.logging import get_logger
from argosy.orchestrator.flows.plan_synthesis import (
    _enforce_speculation_cap,
    _horizon_md,
    run_synthesis,
)
from argosy.state.models import DecisionRun, PlanVersion
from argosy.state.queries import (
    get_active_baseline,
    get_current_plan,
    get_pending_draft,
)

log = get_logger(__name__)


def _run_phase_3_synthesizer(*, user_id, baseline, prior_current,
                             guidance, portfolio_summary, fills_summary,
                             speculation_cap_pct, speculation_cap_concurrent,
                             ) -> PlanSynthesisOutput:
    """Direct-invoke PlanSynthesizerAgent; skip Phases 1/2/4/5.

    Indirection point so tests can monkeypatch.
    """
    agent = PlanSynthesizerAgent(user_id=user_id)
    baseline_md = baseline.distillate_rendered or "(no distillate available)"
    prior_md = ""
    if prior_current:
        prior_md = "\n\n".join(filter(None, [
            prior_current.horizon_long_md,
            prior_current.horizon_medium_md,
            prior_current.horizon_short_md,
        ]))
    result = agent.run_sync(
        baseline_distillate_md=baseline_md,
        prior_current_md=prior_md,
        analyst_reports_text=f"(amendment guidance: {guidance})",
        debate_outcomes_text="(skipped — medium-tier amendment)",
        portfolio_snapshot_summary=portfolio_summary,
        recent_fills_summary=fills_summary,
        speculation_cap_pct=speculation_cap_pct,
        speculation_cap_concurrent=speculation_cap_concurrent,
    )
    return result.output  # type: ignore[attr-defined]


def _medium_worker(*, session: Session, user_id: str,
                   decision_run: DecisionRun, guidance: str) -> None:
    """Run Phase 3 only with the user's amendment as guidance."""
    # Cancellation pre-check.
    session.refresh(decision_run)
    if decision_run.status == "cancelled":
        log.info("plan_amendment.medium.cancelled_before_start",
                 decision_run_id=decision_run.id)
        return

    publish_event_threadsafe("plan.amendment.started", {
        "user_id": user_id,
        "decision_run_id": decision_run.id,
        "tier": "medium",
        "eta_seconds": 30,
    })

    try:
        baseline = get_active_baseline(session, user_id)
        if baseline is None:
            raise RuntimeError(f"no active baseline for user {user_id!r}")
        prior_current = get_current_plan(session, user_id)

        # Reuse synthesis-flow placeholder helpers; they're documented stubs.
        portfolio_summary = "(amendment-flow placeholder; see plan_synthesis._assemble_portfolio_summary)"
        fills_summary = "(amendment-flow placeholder)"

        # Cap.
        try:
            cap = load_speculation_cap(
                user_id=user_id, agent_settings=get_user_agent_settings(user_id),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("plan_amendment.medium.cap_load_failed",
                        user_id=user_id, error=str(exc))
            from argosy.config import SpeculationCap
            cap = SpeculationCap()

        output = _run_phase_3_synthesizer(
            user_id=user_id, baseline=baseline, prior_current=prior_current,
            guidance=guidance,
            portfolio_summary=portfolio_summary, fills_summary=fills_summary,
            speculation_cap_pct=cap.max_pct_of_net_worth,
            speculation_cap_concurrent=cap.max_concurrent_positions,
        )

        # Layer 2 post-filter.
        output = _enforce_speculation_cap(
            output,
            max_pct_of_net_worth=cap.max_pct_of_net_worth,
            max_concurrent_positions=cap.max_concurrent_positions,
        )

        # Cancellation re-check before persisting.
        session.refresh(decision_run)
        if decision_run.status == "cancelled":
            log.info("plan_amendment.medium.cancelled_before_persist",
                     decision_run_id=decision_run.id)
            return

        # Idempotency: demote any pending draft.
        existing_draft = get_pending_draft(session, user_id)
        if existing_draft is not None:
            existing_draft.role = "superseded"
            existing_draft.superseded_at = datetime.now(timezone.utc)
            session.commit()

        inputs = output.inputs.model_copy(update={
            "baseline_id": baseline.id,
            "prior_current_id": prior_current.id if prior_current else None,
            "decision_run_id": decision_run.id,
        })
        draft = PlanVersion(
            user_id=user_id, role="draft",
            version_label=f"amend-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M')}",
            source_path="", raw_markdown="",
            decision_run_id=decision_run.id,
            derived_from_id=baseline.id,
            horizon_long_json=output.long.model_dump_json(),
            horizon_medium_json=output.medium.model_dump_json(),
            horizon_short_json=output.short.model_dump_json(),
            horizon_long_md=_horizon_md(output.long),
            horizon_medium_md=_horizon_md(output.medium),
            horizon_short_md=_horizon_md(output.short),
            synthesis_inputs_json=inputs.model_dump_json(),
        )
        session.add(draft)
        decision_run.finished_at = datetime.now(timezone.utc)
        decision_run.status = "completed"
        session.commit()
        session.refresh(draft)

        publish_event_threadsafe("plan.amendment.completed", {
            "user_id": user_id,
            "decision_run_id": decision_run.id,
            "tier": "medium",
            "draft_id": draft.id,
        })
        publish_event_threadsafe("plan.draft.completed", {
            "user_id": user_id,
            "draft_id": draft.id,
        })
    except Exception as exc:  # noqa: BLE001
        log.error("plan_amendment.medium.failed",
                  decision_run_id=decision_run.id, error=str(exc))
        session.refresh(decision_run)
        decision_run.status = "failed"
        decision_run.notes_json = json.dumps({"error": str(exc)})
        decision_run.finished_at = datetime.now(timezone.utc)
        session.commit()
        publish_event_threadsafe("plan.amendment.failed", {
            "user_id": user_id,
            "decision_run_id": decision_run.id,
            "tier": "medium",
            "error": str(exc),
        })


def _large_worker(*, session: Session, user_id: str,
                  decision_run: DecisionRun, guidance: str) -> None:
    """Delegate to run_synthesis (full 5-phase) with guidance."""
    session.refresh(decision_run)
    if decision_run.status == "cancelled":
        log.info("plan_amendment.large.cancelled_before_start",
                 decision_run_id=decision_run.id)
        return

    publish_event_threadsafe("plan.amendment.started", {
        "user_id": user_id,
        "decision_run_id": decision_run.id,
        "tier": "large",
        "eta_seconds": 900,  # 15 min nominal
    })

    try:
        result = run_synthesis(
            session, user_id=user_id, trigger="check_in", guidance=guidance,
        )
        # run_synthesis opens its OWN DecisionRun; attribute the amendment row
        # to the same draft.
        draft = session.get(PlanVersion, result.draft_id)
        if draft is not None:
            draft.decision_run_id = decision_run.id
            session.commit()

        decision_run.finished_at = datetime.now(timezone.utc)
        decision_run.status = "completed"
        session.commit()

        publish_event_threadsafe("plan.amendment.completed", {
            "user_id": user_id,
            "decision_run_id": decision_run.id,
            "tier": "large",
            "draft_id": result.draft_id,
        })
    except Exception as exc:  # noqa: BLE001
        log.error("plan_amendment.large.failed",
                  decision_run_id=decision_run.id, error=str(exc))
        session.refresh(decision_run)
        decision_run.status = "failed"
        decision_run.notes_json = json.dumps({"error": str(exc)})
        decision_run.finished_at = datetime.now(timezone.utc)
        session.commit()
        publish_event_threadsafe("plan.amendment.failed", {
            "user_id": user_id,
            "decision_run_id": decision_run.id,
            "tier": "large",
            "error": str(exc),
        })


__all__ = ["_medium_worker", "_large_worker", "_run_phase_3_synthesizer"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plan_amendment_workers.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add argosy/orchestrator/flows/plan_amendment/workers.py tests/test_plan_amendment_workers.py
git commit -m "feat(amendment): medium + large workers with cancellation + events"
```

---

### Task 4.6: Dispatcher — `run_small`, `dispatch_async`, concurrency control

**Files:**
- Create: `argosy/orchestrator/flows/plan_amendment/dispatcher.py`
- Create: `tests/test_plan_amendment_dispatcher.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_plan_amendment_dispatcher.py`:

```python
"""Tests for plan_amendment dispatcher (Wave 4)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import DecisionRun, PlanVersion, User


def _make_delta():
    from argosy.agents.plan_synthesizer_types import Delta
    return Delta(
        item_kind="target", item_id="medium.targets.nvda", horizon="medium",
        change_kind="modified", summary="NVDA cap 15% -> 12%",
        prior={"value": 0.15}, proposed={"value": 0.12},
        rationale="user-initiated tightening",
    )


def _make_intent(**kw):
    from argosy.agents.advisor_amendment_types import AmendmentIntent
    base = dict(tier="medium", rationale="x")
    base.update(kw)
    return AmendmentIntent(**base)


@pytest.fixture
def session_with_current(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.add(PlanVersion(
        user_id="ariel", role="baseline", version_label="x", raw_markdown="# Plan",
        distillate_rendered="# Plan distillate",
    ))
    pv = PlanVersion(
        user_id="ariel", role="draft", version_label="prior-draft",
        raw_markdown="",
        horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
        horizon_medium_json='{"horizon":"medium","freshness_expected":"quarterly","status":"no_change","posture":"x","deltas_from_prior":[]}',
        horizon_short_json='{"horizon":"short","freshness_expected":"monthly","status":"no_change","posture":"x"}',
    )
    s.add(pv)
    s.commit()
    s.refresh(pv)
    yield s, pv
    s.close()


def test_run_small_appends_delta_to_existing_draft(session_with_current):
    from argosy.orchestrator.flows.plan_amendment.dispatcher import run_small

    sess, pv = session_with_current
    intent_with_delta = _make_intent(tier="small", direction="tighten", proposed_delta=_make_delta())

    result = run_small(sess, user_id="ariel", message="tighten NVDA", intent=intent_with_delta)

    assert result.tier == "small"
    assert result.status == "applied"
    assert result.draft_id == pv.id
    sess.refresh(pv)
    import json
    med = json.loads(pv.horizon_medium_json)
    item_ids = [d["item_id"] for d in med["deltas_from_prior"]]
    assert "medium.targets.nvda" in item_ids
    delta = next(d for d in med["deltas_from_prior"] if d["item_id"] == "medium.targets.nvda")
    assert delta["accepted"] is True
    assert delta["user_edited"] is True


def test_run_small_creates_decision_run_row(session_with_current):
    from argosy.orchestrator.flows.plan_amendment.dispatcher import run_small

    sess, _pv = session_with_current
    intent = _make_intent(tier="small", direction="tighten", proposed_delta=_make_delta())

    result = run_small(sess, user_id="ariel", message="tighten NVDA", intent=intent)

    run = sess.get(DecisionRun, result.decision_run_id)
    assert run is not None
    assert run.decision_kind == "plan_amendment_chat"
    assert run.tier == "small"
    assert run.status == "completed"


def test_dispatch_async_blocks_when_amendment_already_running(session_with_current, monkeypatch):
    from argosy.orchestrator.flows.plan_amendment.dispatcher import dispatch_async

    sess, _pv = session_with_current
    # Pre-existing running amendment.
    sess.add(DecisionRun(
        user_id="ariel", ticker="(plan)", tier="medium",
        decision_kind="plan_amendment_chat", status="running",
    ))
    sess.commit()

    intent = _make_intent(tier="medium")

    # Worker should NOT be dispatched.
    spawned = []
    monkeypatch.setattr(
        "argosy.orchestrator.flows.plan_amendment.dispatcher._spawn_worker",
        lambda *a, **kw: spawned.append(kw),
    )

    result = dispatch_async(
        sess, user_id="ariel", message="x", tier="medium", intent=intent,
    )

    assert result.status == "needs_confirmation"
    assert spawned == []


def test_dispatch_async_returns_running_when_no_conflict(session_with_current, monkeypatch):
    from argosy.orchestrator.flows.plan_amendment.dispatcher import dispatch_async

    sess, _pv = session_with_current
    spawned = []
    monkeypatch.setattr(
        "argosy.orchestrator.flows.plan_amendment.dispatcher._spawn_worker",
        lambda **kw: spawned.append(kw),
    )

    intent = _make_intent(tier="medium")
    result = dispatch_async(
        sess, user_id="ariel", message="shift growth", tier="medium", intent=intent,
    )

    assert result.status == "running"
    assert result.tier == "medium"
    assert result.eta_seconds == 30
    assert len(spawned) == 1
    run = sess.get(DecisionRun, result.decision_run_id)
    assert run.status == "running"
    assert run.tier == "medium"


def test_dispatch_async_large_eta_is_900s(session_with_current, monkeypatch):
    from argosy.orchestrator.flows.plan_amendment.dispatcher import dispatch_async

    sess, _pv = session_with_current
    monkeypatch.setattr(
        "argosy.orchestrator.flows.plan_amendment.dispatcher._spawn_worker",
        lambda **kw: None,
    )

    intent = _make_intent(tier="large")
    result = dispatch_async(
        sess, user_id="ariel", message="re-evaluate", tier="large", intent=intent,
    )

    assert result.tier == "large"
    assert result.eta_seconds == 900
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_plan_amendment_dispatcher.py -v`
Expected: FAIL.

- [ ] **Step 3: Write the dispatcher**

Create `argosy/orchestrator/flows/plan_amendment/dispatcher.py`:

```python
"""Dispatcher — routes a classified AmendmentIntent into the right execution path.

Three entry points:
  - run_small(...) — synchronous, applies the Delta inline, returns AmendmentResultDTO.
  - dispatch_async(...) — opens a DecisionRun, spawns the right worker via
    asyncio.to_thread, returns AmendmentResultDTO with status='running'.
  - cancel(...) — flips a running DecisionRun to status='cancelled'.

Concurrency: the partial unique index on decision_runs (migration 0018)
prevents a second running amendment per user. dispatch_async detects this
and returns status='needs_confirmation' so the chat surface can ask the
user to cancel-and-restart vs queue.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from argosy.agents.advisor_amendment_types import (
    AmendmentIntent,
    AmendmentResultDTO,
)
from argosy.api.events import publish_event_threadsafe
from argosy.logging import get_logger
from argosy.orchestrator.flows.plan_amendment import workers as _workers
from argosy.state.models import DecisionRun, PlanVersion
from argosy.state.queries import get_current_plan, get_pending_draft

log = get_logger(__name__)


def run_small(
    session: Session, *, user_id: str, message: str, intent: AmendmentIntent,
) -> AmendmentResultDTO:
    """Apply the advisor-emitted Delta inline. Synchronous; returns immediately."""
    if intent.proposed_delta is None:
        raise ValueError("run_small requires intent.proposed_delta")

    # Open a DecisionRun row for audit lineage even on the inline path.
    run = DecisionRun(
        user_id=user_id, ticker="(plan)", tier="small",
        decision_kind="plan_amendment_chat", status="running",
        notes_json=json.dumps({"message": message, "intent": intent.model_dump()}),
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    # Find target plan: pending draft if exists, else need a fresh minimal draft.
    target = get_pending_draft(session, user_id)
    if target is None:
        # Create a new draft seeded from the current plan + this single delta.
        current = get_current_plan(session, user_id)
        if current is None:
            raise RuntimeError(f"user {user_id!r} has no current plan to amend")
        target = PlanVersion(
            user_id=user_id, role="draft",
            version_label=f"amend-small-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M')}",
            source_path="", raw_markdown="",
            decision_run_id=run.id,
            derived_from_id=current.id,
            horizon_long_json=current.horizon_long_json,
            horizon_medium_json=current.horizon_medium_json,
            horizon_short_json=current.horizon_short_json,
            horizon_long_md=current.horizon_long_md,
            horizon_medium_md=current.horizon_medium_md,
            horizon_short_md=current.horizon_short_md,
        )
        session.add(target)
        session.commit()
        session.refresh(target)

    # Apply the delta into the target draft.
    delta = intent.proposed_delta
    delta_dict = delta.model_dump()
    delta_dict["accepted"] = True
    delta_dict["user_edited"] = True

    horizon_field = f"horizon_{delta.horizon}_json"
    raw = getattr(target, horizon_field) or "{}"
    payload = json.loads(raw)
    deltas = payload.get("deltas_from_prior") or []
    # Replace existing delta with same item_id, else append.
    existing_idx = next(
        (i for i, d in enumerate(deltas) if d.get("item_id") == delta.item_id),
        None,
    )
    if existing_idx is not None:
        deltas[existing_idx] = delta_dict
    else:
        deltas.append(delta_dict)
    payload["deltas_from_prior"] = deltas
    setattr(target, horizon_field, json.dumps(payload))

    run.status = "completed"
    run.finished_at = datetime.now(timezone.utc)
    session.commit()

    publish_event_threadsafe("plan.amendment.completed", {
        "user_id": user_id,
        "decision_run_id": run.id,
        "tier": "small",
        "draft_id": target.id,
    })

    return AmendmentResultDTO(
        tier="small", decision_run_id=run.id,
        status="applied", draft_id=target.id,
    )


def dispatch_async(
    session: Session, *,
    user_id: str, message: str, tier: str, intent: AmendmentIntent,
    cancel_existing: bool = False,
) -> AmendmentResultDTO:
    """Spawn the medium or large worker; return 202-shaped DTO.

    If a running amendment already exists for this user:
      - cancel_existing=False: return status='needs_confirmation'
      - cancel_existing=True: cancel the prior, then dispatch this one.
    """
    if tier not in ("medium", "large"):
        raise ValueError(f"dispatch_async expects tier in (medium, large); got {tier!r}")

    existing = (
        session.query(DecisionRun)
        .filter_by(
            user_id=user_id, decision_kind="plan_amendment_chat", status="running",
        )
        .first()
    )
    if existing is not None:
        if not cancel_existing:
            return AmendmentResultDTO(
                tier=tier,  # type: ignore[arg-type]
                decision_run_id=existing.id,
                status="needs_confirmation",
            )
        existing.status = "cancelled"
        existing.finished_at = datetime.now(timezone.utc)
        session.commit()
        publish_event_threadsafe("plan.amendment.cancelled", {
            "user_id": user_id,
            "decision_run_id": existing.id,
            "tier": existing.tier,
        })

    run = DecisionRun(
        user_id=user_id, ticker="(plan)", tier=tier,
        decision_kind="plan_amendment_chat", status="running",
        notes_json=json.dumps({"message": message, "intent": intent.model_dump()}),
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    _spawn_worker(
        session=session, user_id=user_id, decision_run=run, tier=tier, guidance=message,
    )

    eta = 30 if tier == "medium" else 900
    return AmendmentResultDTO(
        tier=tier,  # type: ignore[arg-type]
        decision_run_id=run.id,
        status="running",
        eta_seconds=eta,
    )


def _spawn_worker(
    *, session: Session, user_id: str, decision_run: DecisionRun,
    tier: str, guidance: str,
) -> None:
    """Spawn the right worker on a background thread.

    Indirection point so tests can monkeypatch.
    """
    worker = _workers._medium_worker if tier == "medium" else _workers._large_worker

    # The session is bound to the calling thread; spawn a fresh session for the
    # worker thread tied to the same engine.
    engine = session.get_bind()
    from sqlalchemy.orm import sessionmaker
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    decision_run_id = decision_run.id

    def _runnable():
        worker_session = SessionLocal()
        try:
            run = worker_session.get(DecisionRun, decision_run_id)
            worker(session=worker_session, user_id=user_id, decision_run=run, guidance=guidance)
        finally:
            worker_session.close()

    threading.Thread(target=_runnable, daemon=True, name=f"amendment-{tier}-{decision_run_id}").start()


def cancel(session: Session, *, user_id: str, decision_run_id: int) -> bool:
    """Flip a running amendment to cancelled. Returns True on success.

    Returns False if the run doesn't exist, isn't owned by the user, or
    is already finished.
    """
    run = session.get(DecisionRun, decision_run_id)
    if run is None or run.user_id != user_id:
        return False
    if run.decision_kind != "plan_amendment_chat":
        return False
    if run.status != "running":
        return False
    run.status = "cancelled"
    run.finished_at = datetime.now(timezone.utc)
    session.commit()
    publish_event_threadsafe("plan.amendment.cancelled", {
        "user_id": user_id,
        "decision_run_id": decision_run_id,
        "tier": run.tier,
    })
    return True


__all__ = ["run_small", "dispatch_async", "cancel", "_spawn_worker"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plan_amendment_dispatcher.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add argosy/orchestrator/flows/plan_amendment/dispatcher.py tests/test_plan_amendment_dispatcher.py
git commit -m "feat(amendment): dispatcher — small inline + async worker spawn + cancellation"
```

---

### Task 4.7: Package `__init__.py` — public API + monkeypatchable re-exports

**Files:**
- Modify: `argosy/orchestrator/flows/plan_amendment/__init__.py`

- [ ] **Step 1: Write the file**

Replace the empty `__init__.py` with:

```python
"""Plan amendment flow — chat-borne plan changes (Wave 4).

Public API:
  - classify(intent) → ClassificationResult
  - run_small(session, *, user_id, message, intent) → AmendmentResultDTO
  - dispatch_async(session, *, user_id, message, tier, intent, cancel_existing) → AmendmentResultDTO
  - cancel(session, *, user_id, decision_run_id) → bool

Re-exports the monkeypatchable internals so tests' `from argosy.orchestrator.flows
import plan_amendment as flow; monkeypatch.setattr(flow, "_spawn_worker", ...)`
patterns work (mirrors Wave 2's plan_synthesis package convention).
"""

from __future__ import annotations

from argosy.orchestrator.flows.plan_amendment._types import (
    ClassificationResult,
    EffectiveTier,
)
from argosy.orchestrator.flows.plan_amendment.classifier import classify
from argosy.orchestrator.flows.plan_amendment.dispatcher import (
    cancel,
    dispatch_async,
    run_small,
    _spawn_worker,
)
from argosy.orchestrator.flows.plan_amendment.workers import (
    _large_worker,
    _medium_worker,
    _run_phase_3_synthesizer,
)

__all__ = [
    "ClassificationResult",
    "EffectiveTier",
    "_large_worker",
    "_medium_worker",
    "_run_phase_3_synthesizer",
    "_spawn_worker",
    "cancel",
    "classify",
    "dispatch_async",
    "run_small",
]
```

- [ ] **Step 2: Verify the package imports cleanly**

Run: `python -c "from argosy.orchestrator.flows import plan_amendment as flow; print(sorted(name for name in dir(flow) if not name.startswith('__')))"`
Expected: lists all public + private symbols.

- [ ] **Step 3: Run full test suite for the package**

Run: `pytest tests/test_plan_amendment_classifier.py tests/test_plan_amendment_dispatcher.py tests/test_plan_amendment_workers.py -v`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add argosy/orchestrator/flows/plan_amendment/__init__.py
git commit -m "feat(amendment): package __init__ — public API + monkeypatch re-exports"
```

---

### Task 4.8: API — `POST /api/advisor/turn` reads `amendment` field

**Files:**
- Modify: `argosy/api/routes/advisor.py`
- Create: `tests/test_advisor_amendment_route.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_advisor_amendment_route.py`:

```python
"""Tests for the amendment surface on POST /api/advisor/turn (Wave 4)."""

from __future__ import annotations

import pytest

from argosy.state.models import DecisionRun, PlanVersion, User


def _seed_user_with_current(client_with_db):
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
        sess.add(PlanVersion(user_id="ariel", role="baseline", raw_markdown="# Plan"))
        sess.add(PlanVersion(
            user_id="ariel", role="current", version_label="x",
            raw_markdown="",
            horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
            horizon_medium_json='{"horizon":"medium","freshness_expected":"quarterly","status":"no_change","posture":"x","deltas_from_prior":[]}',
            horizon_short_json='{"horizon":"short","freshness_expected":"monthly","status":"no_change","posture":"x"}',
        ))
        sess.commit()
    finally:
        sess.close()


def test_turn_with_small_amendment_applies_inline(client_with_db, monkeypatch):
    """An advisor turn that emits tier=small + tighten + delta returns status=applied."""
    from argosy.agents.advisor_amendment_types import AmendmentIntent
    from argosy.agents.plan_synthesizer_types import Delta

    _seed_user_with_current(client_with_db)

    delta = Delta(
        item_kind="target", item_id="medium.targets.nvda", horizon="medium",
        change_kind="modified", summary="x",
        prior={"value": 0.15}, proposed={"value": 0.12},
    )
    intent = AmendmentIntent(
        tier="small", direction="tighten", proposed_delta=delta, rationale="x",
    )

    # Stub the advisor agent to emit our prepared turn with amendment.
    from argosy.api.routes import advisor as advisor_mod

    class _StubTurn:
        text = "Applied as small tightening."
        amendment = intent
        # Other AdvisorTurn fields default-fine when absent

    async def _fake_run_turn(*args, **kwargs):
        return _StubTurn()

    monkeypatch.setattr(advisor_mod, "_run_advisor_turn", _fake_run_turn, raising=False)
    # The exact monkeypatch target depends on the existing route shape; if
    # `_run_advisor_turn` doesn't exist, find the symbol used inside post_turn
    # and patch it. See discovery in step 3.

    r = client_with_db.post(
        "/api/advisor/turn",
        json={"user_id": "ariel", "message": "tighten NVDA cap to 12%"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["amendment"] is not None
    assert body["amendment"]["status"] == "applied"
    assert body["amendment"]["tier"] == "small"


def test_turn_with_medium_amendment_returns_running(client_with_db, monkeypatch):
    from argosy.agents.advisor_amendment_types import AmendmentIntent

    _seed_user_with_current(client_with_db)

    intent = AmendmentIntent(tier="medium", rationale="x")

    from argosy.api.routes import advisor as advisor_mod
    from argosy.orchestrator.flows.plan_amendment import dispatcher as disp_mod

    class _StubTurn:
        text = "Kicking off medium amendment."
        amendment = intent

    async def _fake_run_turn(*args, **kwargs):
        return _StubTurn()

    spawned = []
    monkeypatch.setattr(disp_mod, "_spawn_worker", lambda **kw: spawned.append(kw))
    monkeypatch.setattr(advisor_mod, "_run_advisor_turn", _fake_run_turn, raising=False)

    r = client_with_db.post(
        "/api/advisor/turn",
        json={"user_id": "ariel", "message": "shift toward growth"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["amendment"]["tier"] == "medium"
    assert body["amendment"]["status"] == "running"
    assert body["amendment"]["eta_seconds"] == 30
    assert len(spawned) == 1


def test_post_amendment_cancel_flips_status(client_with_db):
    """POST /api/advisor/amendment/{id}/cancel flips a running run to cancelled."""
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
        run = DecisionRun(
            user_id="ariel", ticker="(plan)", tier="medium",
            decision_kind="plan_amendment_chat", status="running",
        )
        sess.add(run)
        sess.commit()
        sess.refresh(run)
        run_id = run.id
    finally:
        sess.close()

    r = client_with_db.post(
        f"/api/advisor/amendment/{run_id}/cancel?user_id=ariel"
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"

    sess = client_with_db.app.state.session_factory()
    try:
        run = sess.get(DecisionRun, run_id)
        assert run.status == "cancelled"
        assert run.finished_at is not None
    finally:
        sess.close()


def test_post_amendment_cancel_404_for_unknown_run(client_with_db):
    r = client_with_db.post(
        "/api/advisor/amendment/9999/cancel?user_id=ariel"
    )
    assert r.status_code == 404


def test_post_amendment_cancel_409_for_already_finished(client_with_db):
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
        run = DecisionRun(
            user_id="ariel", ticker="(plan)", tier="medium",
            decision_kind="plan_amendment_chat", status="completed",
        )
        sess.add(run)
        sess.commit()
        sess.refresh(run)
        run_id = run.id
    finally:
        sess.close()

    r = client_with_db.post(
        f"/api/advisor/amendment/{run_id}/cancel?user_id=ariel"
    )
    assert r.status_code == 409
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_advisor_amendment_route.py -v`
Expected: FAIL.

- [ ] **Step 3: Discover the existing turn route shape**

Run: `grep -n "post_turn\|/turn" argosy/api/routes/advisor.py | head -20`

Note where the advisor agent is invoked inside `post_turn`. The handler likely calls something like `agent.run(...)` or a helper. Identify the helper symbol so the test's monkeypatch target is right. If the test's `_run_advisor_turn` patch target isn't accurate, update the test to patch the actual symbol.

- [ ] **Step 4: Modify `post_turn` to read `amendment` and dispatch**

Edit `argosy/api/routes/advisor.py` `post_turn`. After the advisor agent produces its `AdvisorTurn` (and after the existing persistence), add the amendment dispatch:

```python
        # Wave 4: amendment dispatch.
        amendment_dto = None
        if turn.amendment is not None:
            from argosy.orchestrator.flows.plan_amendment import (
                classify, dispatch_async, run_small,
            )
            from argosy.orchestrator.flows.plan_amendment._types import EffectiveTier

            classified = classify(turn.amendment)
            try:
                if classified.effective_tier == EffectiveTier.SMALL:
                    # classified.proposed_delta is guaranteed non-None for small
                    small_intent = turn.amendment.model_copy(
                        update={"proposed_delta": classified.proposed_delta},
                    )
                    amendment_dto = run_small(
                        db, user_id=req.user_id, message=req.message, intent=small_intent,
                    )
                else:
                    # classifier may have escalated small->medium; rebuild intent
                    effective_intent = turn.amendment.model_copy(
                        update={"tier": classified.effective_tier.value},
                    )
                    amendment_dto = dispatch_async(
                        db, user_id=req.user_id, message=req.message,
                        tier=classified.effective_tier.value,
                        intent=effective_intent,
                    )
            except Exception as exc:
                # Amendment dispatch failure should not blow up the chat turn.
                log.error("advisor.turn.amendment_dispatch_failed",
                          user_id=req.user_id, error=str(exc))
                amendment_dto = None
```

Then include `amendment_dto` in the response model:

```python
class AdvisorTurnResponse(BaseModel):
    # ... existing fields ...
    amendment: AmendmentResultDTO | None = None
```

(Add the import at the top: `from argosy.agents.advisor_amendment_types import AmendmentResultDTO`.)

In the `return` statement, set `amendment=amendment_dto`.

- [ ] **Step 5: Add the cancel endpoint**

Append to `argosy/api/routes/advisor.py` (alongside existing routes):

```python
class AmendmentCancelResponse(BaseModel):
    status: str
    decision_run_id: int


@router.post("/amendment/{decision_run_id}/cancel", response_model=AmendmentCancelResponse)
def post_amendment_cancel(
    decision_run_id: int,
    user_id: str,
    db: Session = Depends(get_db),
) -> AmendmentCancelResponse:
    """Cancel a running plan-amendment-chat DecisionRun."""
    from argosy.orchestrator.flows.plan_amendment import cancel
    from argosy.state.models import DecisionRun

    run = db.get(DecisionRun, decision_run_id)
    if run is None or run.user_id != user_id or run.decision_kind != "plan_amendment_chat":
        raise HTTPException(status_code=404, detail="amendment not found for user")
    if run.status != "running":
        raise HTTPException(
            status_code=409,
            detail=f"amendment is in status {run.status!r}; cannot cancel",
        )

    ok = cancel(db, user_id=user_id, decision_run_id=decision_run_id)
    if not ok:
        raise HTTPException(status_code=409, detail="cancel failed")

    return AmendmentCancelResponse(
        status="cancelled", decision_run_id=decision_run_id,
    )
```

Update `__all__` if `advisor.py` has one.

- [ ] **Step 6: Update the model that `post_turn` returns to include `amendment`**

Inspect the existing `AdvisorTurnResponse`. Add `amendment: AmendmentResultDTO | None = None` and the import. Update the `return` to thread `amendment=amendment_dto`.

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_advisor_amendment_route.py -v`
Expected: PASS (5 tests).

Run regression: `pytest tests/test_advisor_route.py -v`
Expected: all existing turn / home-brief / check-in tests still pass.

- [ ] **Step 8: Commit**

```bash
git add argosy/api/routes/advisor.py tests/test_advisor_amendment_route.py
git commit -m "feat(api): /turn reads amendment + new /amendment/{id}/cancel route"
```

---

### Task 4.9: Modify `run_small` to handle tighten-direction validation

**Files:**
- Modify: `argosy/orchestrator/flows/plan_amendment/dispatcher.py`
- Modify: `tests/test_plan_amendment_dispatcher.py`

This task hardens the Small path beyond what classifier already does. The classifier escalates Small→Medium when `direction != "tighten"`. We add an additional defensive check inside `run_small` that the proposed Delta's `prior` and `proposed` numeric values, when both present and numeric, actually represent a tightening (i.e., `proposed.value < prior.value` for caps, `proposed.value > prior.value` for floors). The advisor classifies semantically; this ORM-side check catches numeric inconsistency.

- [ ] **Step 1: Append the failing test**

Append to `tests/test_plan_amendment_dispatcher.py`:

```python
def test_run_small_rejects_loosening_numbers(session_with_current):
    """Even if intent claims direction=tighten, if the numbers loosen we refuse."""
    from argosy.orchestrator.flows.plan_amendment.dispatcher import run_small
    from argosy.agents.advisor_amendment_types import AmendmentIntent
    from argosy.agents.plan_synthesizer_types import Delta

    sess, _pv = session_with_current

    # Spec says cap "tightening", but proposed > prior — actually loosening.
    bad_delta = Delta(
        item_kind="target", item_id="medium.targets.nvda", horizon="medium",
        change_kind="modified", summary="x",
        prior={"value": 0.15, "kind": "cap"},
        proposed={"value": 0.18, "kind": "cap"},
        rationale="claims tightening but numbers loosen",
    )
    intent = AmendmentIntent(
        tier="small", direction="tighten", proposed_delta=bad_delta, rationale="x",
    )

    import pytest
    with pytest.raises(ValueError, match="tightening"):
        run_small(sess, user_id="ariel", message="x", intent=intent)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_plan_amendment_dispatcher.py::test_run_small_rejects_loosening_numbers -v`
Expected: FAIL.

- [ ] **Step 3: Add the numeric-tightening check**

Edit `argosy/orchestrator/flows/plan_amendment/dispatcher.py::run_small`. After the existing `if intent.proposed_delta is None` check and before opening the DecisionRun row, add:

```python
    _validate_tightening(intent.proposed_delta)
```

Then define `_validate_tightening` near the bottom of the file:

```python
def _validate_tightening(delta) -> None:
    """Defensive: confirm numeric values move in the tightening direction.

    Tightening rules (aligned with the spec's "lowers risk surface"):
      - kind in {cap, max_*}: proposed.value < prior.value
      - kind in {floor, min_*}: proposed.value > prior.value
      - if kind absent or prior/proposed missing: trust the advisor's classification
    """
    prior = (delta.prior or {})
    proposed = (delta.proposed or {})
    pv = prior.get("value")
    qv = proposed.get("value")
    if pv is None or qv is None:
        return  # not enough info; trust the advisor

    kind = (proposed.get("kind") or prior.get("kind") or "").lower()
    if not isinstance(pv, (int, float)) or not isinstance(qv, (int, float)):
        return

    is_floor_like = any(k in kind for k in ("floor", "min"))
    is_cap_like = any(k in kind for k in ("cap", "max", "ceiling"))

    if is_cap_like:
        if qv >= pv:
            raise ValueError(
                f"intent claims tightening but proposed value {qv} >= prior {pv} on a cap-like target"
            )
    elif is_floor_like:
        if qv <= pv:
            raise ValueError(
                f"intent claims tightening but proposed value {qv} <= prior {pv} on a floor-like target"
            )
    # If kind is unrecognized, no numeric check — but the API still ran the
    # classifier's direction filter, which is the primary gate.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plan_amendment_dispatcher.py -v`
Expected: PASS (6 tests now).

- [ ] **Step 5: Commit**

```bash
git add argosy/orchestrator/flows/plan_amendment/dispatcher.py tests/test_plan_amendment_dispatcher.py
git commit -m "feat(amendment): defensive numeric-tightening check in run_small"
```

---

### Task 4.10: UI — browser notifications module

**Files:**
- Create: `ui/src/lib/notifications.ts`

- [ ] **Step 1: Write the module**

Create `ui/src/lib/notifications.ts`:

```typescript
"use client";

/**
 * Browser notification helper (Wave 4).
 *
 * Wraps the Web Notifications API with a no-op fallback when the API
 * is unavailable (Safari without permission flow, ancient browsers, or
 * permission denied/default). The in-app banner remains the always-on
 * surface — these notifications are an opt-in escalation when the user
 * has navigated away from the tab.
 */

type Permission = "granted" | "denied" | "default";

function isSupported(): boolean {
  return typeof window !== "undefined" && "Notification" in window;
}

export function permission(): Permission {
  if (!isSupported()) return "denied";
  return Notification.permission;
}

export async function ensureNotificationPermission(): Promise<Permission> {
  if (!isSupported()) return "denied";
  if (Notification.permission === "granted") return "granted";
  if (Notification.permission === "denied") return "denied";
  try {
    const result = await Notification.requestPermission();
    return result;
  } catch {
    return "denied";
  }
}

export function notify(title: string, body: string, opts?: NotificationOptions): void {
  if (!isSupported()) return;
  if (Notification.permission !== "granted") return;
  try {
    new Notification(title, { body, ...opts });
  } catch {
    // Silently swallow — falling back to the in-app banner is the contract.
  }
}
```

- [ ] **Step 2: Verify TypeScript compiles**

Run from `ui/`: `npx tsc --noEmit`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add ui/src/lib/notifications.ts
git commit -m "feat(ui): browser notifications helper with Web Notifications API"
```

---

### Task 4.11: UI — advisor page subscribes to amendment events + permission flow

**Files:**
- Modify: `ui/src/app/advisor/page.tsx`
- Modify: `ui/src/lib/api.ts`

- [ ] **Step 1: Add `advisorAmendmentCancel` API method + amendment types**

Append to `ui/src/lib/api.ts`:

```typescript
// ----------------------------------------------------------------------
// Wave 4: plan amendment chat flow
// ----------------------------------------------------------------------

export interface AmendmentResultDTO {
  tier: "small" | "medium" | "large";
  decision_run_id: number;
  status: "applied" | "running" | "needs_confirmation" | "cancelled_existing";
  draft_id: number | null;
  eta_seconds: number | null;
}

export interface AmendmentEventPayload {
  user_id: string;
  decision_run_id: number;
  tier: "small" | "medium" | "large";
  draft_id?: number;
  eta_seconds?: number;
  error?: string;
}
```

Inside the `api = { ... }` object, add:

```typescript
  advisorAmendmentCancel: (userId: string, decisionRunId: number) =>
    postJSON<{ status: string; decision_run_id: number }>(
      `/api/advisor/amendment/${decisionRunId}/cancel?user_id=${encodeURIComponent(userId)}`,
      {},
    ),
```

If `AdvisorTurnResponse` is typed in `api.ts`, extend it with the optional `amendment: AmendmentResultDTO | null` field.

- [ ] **Step 2: Wire WebSocket subscription on advisor page**

Edit `ui/src/app/advisor/page.tsx`. Find the existing WebSocket subscription (Wave 2 added one for `plan.draft.*` events). Add subscriptions for the new amendment events.

Add at the top:

```typescript
import { ensureNotificationPermission, notify } from "@/lib/notifications";
import type { AmendmentEventPayload, AmendmentResultDTO } from "@/lib/api";
```

Inside the component, add state for the active amendment:

```typescript
const [activeAmendment, setActiveAmendment] = useState<{
  decision_run_id: number;
  tier: "medium" | "large";
  eta_seconds: number;
  started_at: number;
} | null>(null);
const [amendmentSystemMessage, setAmendmentSystemMessage] = useState<string | null>(null);
```

Add a WebSocket handler (or extend the existing one) for amendment events:

```typescript
useEffect(() => {
  // Existing WebSocket setup omitted — find the existing subscription block
  // and add these handlers alongside plan.draft.* handlers.
  const onAmendmentStarted = (payload: AmendmentEventPayload) => {
    if (payload.tier === "medium" || payload.tier === "large") {
      setActiveAmendment({
        decision_run_id: payload.decision_run_id,
        tier: payload.tier,
        eta_seconds: payload.eta_seconds ?? (payload.tier === "medium" ? 30 : 900),
        started_at: Date.now(),
      });
    }
  };
  const onAmendmentCompleted = (payload: AmendmentEventPayload) => {
    setActiveAmendment(null);
    setAmendmentSystemMessage(`Plan revision ready — review it now.`);
    refreshDraft();
    notify("Argosy", "Your plan revision is ready — review it now");
  };
  const onAmendmentFailed = (payload: AmendmentEventPayload) => {
    setActiveAmendment(null);
    setAmendmentSystemMessage(
      `Plan amendment failed${payload.error ? `: ${payload.error}` : ""}.`,
    );
  };
  const onAmendmentCancelled = (_payload: AmendmentEventPayload) => {
    setActiveAmendment(null);
    setAmendmentSystemMessage("Plan amendment cancelled.");
  };

  // Bind to the existing event-source / WebSocket subscriber. The exact
  // mechanism depends on how Wave 2's plan.draft.* handlers are wired.
  // Search for "plan.draft.completed" in this file to find the seam.

  // Example using a hypothetical subscribe(name, handler) helper:
  const unsubs = [
    subscribe("plan.amendment.started", onAmendmentStarted),
    subscribe("plan.amendment.completed", onAmendmentCompleted),
    subscribe("plan.amendment.failed", onAmendmentFailed),
    subscribe("plan.amendment.cancelled", onAmendmentCancelled),
  ];
  return () => { unsubs.forEach(u => u()); };
}, [refreshDraft]);
```

The `subscribe` helper / EventSource bind must match what Wave 2 already used. Find the Wave 2 binding (search for `plan.draft.completed`) and mirror it.

- [ ] **Step 3: Render the active-amendment status pill**

Inside the JSX, above the chat input area, render:

```tsx
{activeAmendment && (
  <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 flex items-center justify-between text-sm">
    <span>
      ⏳ Plan amendment in progress (<strong>{activeAmendment.tier}</strong> · ETA{" "}
      {activeAmendment.tier === "medium" ? "~30s" : "~15 min"})
    </span>
    <Button
      size="sm"
      variant="outline"
      onClick={async () => {
        try {
          await api.advisorAmendmentCancel(USER_ID, activeAmendment.decision_run_id);
        } catch (e: unknown) {
          // Server may have completed in the meantime; refresh the surface.
          setActiveAmendment(null);
        }
      }}
    >
      Cancel
    </Button>
  </div>
)}
{amendmentSystemMessage && (
  <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm">
    {amendmentSystemMessage}
    <button
      type="button"
      className="ml-2 text-xs text-muted-foreground hover:underline"
      onClick={() => setAmendmentSystemMessage(null)}
    >
      dismiss
    </button>
  </div>
)}
```

- [ ] **Step 4: Trigger permission prompt on first Medium/Large response**

When `post turn` returns an `amendment` field with `status === "running"` and `tier !== "small"`, fire the permission prompt:

```typescript
useEffect(() => {
  if (activeAmendment && permission() === "default") {
    void ensureNotificationPermission();
  }
}, [activeAmendment]);
```

(Import `permission` alongside `ensureNotificationPermission` in step 2.)

- [ ] **Step 5: Verify TypeScript compiles**

Run from `ui/`: `npx tsc --noEmit`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add ui/src/app/advisor/page.tsx ui/src/lib/api.ts
git commit -m "feat(ui): advisor page subscribes to plan.amendment.* events + browser notify"
```

---

### Task 4.12: Live LLM e2e test (claude_code backend)

**Files:**
- Create: `tests/test_plan_amendment_e2e.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_plan_amendment_e2e.py`:

```python
"""End-to-end live LLM amendment test (Wave 4).

Marked llm_eval. Runs against the claude_code backend by default; falls
back to the api_key backend if ANTHROPIC_API_KEY is set. Skipped when
neither is reachable.

Cost: ~$0.05 per run on api_key backend; free-of-direct-cost on the
claude_code backend (subscription).
"""

from __future__ import annotations

import os
import shutil

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import PlanVersion, User


def _llm_backend_available() -> bool:
    try:
        from argosy.config import get_settings
        backend = get_settings().anthropic.backend
    except Exception:
        backend = "claude_code"

    if backend == "api_key":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if backend == "claude_code":
        return shutil.which("claude") is not None
    return False


@pytest.mark.llm_eval
@pytest.mark.skipif(
    not _llm_backend_available(),
    reason="No LLM backend reachable",
)
def test_advisor_classifies_small_tightening_amendment(alembic_engine_at_head):
    """Send a chat message that should classify as Small + tighten + Delta."""
    from argosy.agents.advisor import AdvisorAgent

    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    sess = SessionLocal()
    try:
        sess.add(User(id="ariel", plan="free"))
        # Seed a current plan with an NVDA cap target.
        sess.add(PlanVersion(
            user_id="ariel", role="current", version_label="x",
            raw_markdown="",
            horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
            horizon_medium_json=(
                '{"horizon":"medium","freshness_expected":"quarterly","status":"no_change","posture":"x",'
                '"targets":[{"label":"NVDA cap","value":0.15,"unit":"pct_of_portfolio",'
                '"stated_at":"2026-01-01","revisit_after":"2026-04-01","rationale":""}]}'
            ),
            horizon_short_json='{"horizon":"short","freshness_expected":"monthly","status":"no_change","posture":"x"}',
        ))
        sess.commit()

        agent = AdvisorAgent(user_id="ariel")
        sys, usr = agent.build_prompt(
            current_stage="post_intake",
            mode="open",
            last_user_message="Tighten my NVDA cap from 15% to 12%.",
            chat_history=[],
            has_current_plan=True,
        )
        result = agent.run_sync(
            current_stage="post_intake",
            mode="open",
            last_user_message="Tighten my NVDA cap from 15% to 12%.",
            chat_history=[],
            has_current_plan=True,
        )
        turn = result.output

        assert turn.amendment is not None, f"no amendment classified; turn text: {turn.text!r}"
        # Either the model picked small+tighten (ideal) or medium (acceptable
        # — the conservative default). Reject only large or missing.
        assert turn.amendment.tier in ("small", "medium")
        if turn.amendment.tier == "small":
            assert turn.amendment.direction == "tighten"
            assert turn.amendment.proposed_delta is not None
            assert "NVDA" in turn.amendment.proposed_delta.summary.upper() or \
                   "nvda" in turn.amendment.proposed_delta.item_id.lower()
    finally:
        sess.close()
```

The exact `build_prompt` / `run_sync` signature depends on what was set in Task 4.3. If kwarg names differ, update the test to match.

- [ ] **Step 2: Verify the test collects + skips correctly without a backend**

Run: `pytest tests/test_plan_amendment_e2e.py -v` (without ANTHROPIC_API_KEY, on a non-claude-code-installed env)
Expected: 1 skipped.

- [ ] **Step 3: (Optional) Run the live test**

When ready, run with the claude_code backend (no key needed if `claude.exe` is on PATH):
`pytest tests/test_plan_amendment_e2e.py -m llm_eval -v`
Expected: PASS. The test is tolerant — it accepts either tier=small or tier=medium since "be conservative" is part of the prompt. Cost: ~$0.05 or one Claude Code session.

- [ ] **Step 4: Commit**

```bash
git add tests/test_plan_amendment_e2e.py
git commit -m "test(amendment): live e2e — Small tightening classification against current plan"
```

---

### Task 4.13: SDD edits for Wave 4

**Files:**
- Modify: `docs/design/SDD.md`

- [ ] **Step 1: Append §6.13 "Plan amendment chat flow"**

Edit `docs/design/SDD.md`. After §6.12 (added in Wave 3 Task 3.6), append:

```markdown
### 6.13 Plan amendment chat flow (Wave 4 of plan-distillate work)

Between scheduled syntheses, the user can ask the advisor in chat for a
structural plan change. The advisor classifies the request as `small`,
`medium`, or `large` and dispatches accordingly.

**Tiers:**

- **small** (~5s, inline) — strict-tightening Delta on one specific target/
  action/theme. Direction must reduce risk surface (lower cap, raise floor,
  shorten horizon, narrower drawdown). The advisor emits a fully-formed
  `Delta` in its turn output; the dispatcher applies it to the existing
  pending draft (or to a new minimal draft seeded from `current`).
- **medium** (~30s, async) — theme shift on one horizon, multi-target
  tweak, loosening, or anything that needs cross-target reasoning. Runs
  Phase 3 of `plan_synthesis_flow` only — the synthesizer with the user's
  message as `guidance`. Skips analysts/debate/risk/FM phases. Cost ~$0.50.
- **large** (~15 min, async) — structural rethink, "re-evaluate everything",
  cross-horizon. Runs the full 5-phase `plan_synthesis_flow.run_synthesis(...)`
  with `trigger="check_in"` and the user's message as `guidance`. Functionally
  equivalent to `POST /api/advisor/check-in`.

**Async UX.** Medium and Large dispatch a worker on `asyncio.to_thread`,
return `202` to the chat with `decision_run_id` + `eta_seconds`, and emit
`plan.amendment.completed` (plus `plan.draft.completed` for Large) when
done. The advisor page shows a status pill while the run is in flight and
fires a browser-level Web Notification on completion (opt-in; in-app
banner is the always-on fallback).

**Concurrency.** One in-flight async amendment per user, enforced by the
partial unique index `ix_decision_runs_one_amendment_running_per_user`
(migration 0018). A second amendment while one is running returns
`needs_confirmation` so the chat asks the user to cancel-and-restart vs
queue.

**Cancellation.** `POST /api/advisor/amendment/{decision_run_id}/cancel`
flips the row to `status='cancelled'`. The worker checks status between
phases and bails. A Large run cancelled past Phase 3 commits the partial
draft as `role='superseded'` (preserved for audit, not surfaced to UI).

**Audit lineage.** Each amendment opens a `decision_runs` row with
`decision_kind='plan_amendment_chat'` and `tier in {small,medium,large}`.
The resulting `plan_versions` row carries `decision_run_id` for end-to-end
traceability — chat-turn → DecisionRun → draft → (after accept) current.

See `docs/superpowers/specs/2026-05-07-plan-amendment-chat-flow-design.md`
for the full design.
```

- [ ] **Step 2: Update §10.1 routing matrix**

Find the routing matrix table (last extended in Wave 2 Task 2.19 with `plan_revision` and Wave 3 Task 3.6 with two `speculative` rows). Append:

```markdown
| `plan_amendment_chat` | Any | Any | T3-equivalent fleet review when tier in {medium, large}; tier=small bypasses fleet (advisor's structured Delta is the audit). |
```

- [ ] **Step 3: Update §11.3 WebSocket events**

Find the WebSocket events fenced block (last extended in Wave 3 Task 3.6 with `plan.speculative.routed`). Append:

```
plan.amendment.started      plan.amendment.completed
plan.amendment.failed       plan.amendment.cancelled
```

- [ ] **Step 4: Commit**

```bash
git add docs/design/SDD.md
git commit -m "docs(sdd): §6.13 plan amendment chat flow; §10.1 + §11.3 updates"
```

---

## Final gate

Before declaring Wave 4 complete, all of the following must hold:

- [ ] All Wave 4 tests pass: `pytest tests/test_migration_0018.py tests/test_advisor_amendment_types.py tests/test_plan_amendment_classifier.py tests/test_plan_amendment_dispatcher.py tests/test_plan_amendment_workers.py tests/test_advisor_amendment_route.py -v`
- [ ] Live e2e passes at least once: `pytest tests/test_plan_amendment_e2e.py -m llm_eval -v`
- [ ] No regressions: `pytest -m "not llm_eval" -q` is green (Wave 3 baseline + new Wave 4 tests).
- [ ] TypeScript compile clean: `cd ui && npx tsc --noEmit`.
- [ ] SDD edits committed.
- [ ] Manual smoke (optional, user's call): "tighten NVDA cap from 15% to 12%" in advisor chat returns inline; "shift toward growth" returns 30s status pill + completes; "re-evaluate everything" returns 15-min status pill + completes; cancel button works on a running amendment; second amendment while one is running asks for confirmation.

---

## Out of scope (deferred)

- **Multi-turn amendment refinement** — each amendment is one turn; "can you reconsider that?" fires fresh classification.
- **Tier escalation surface in chat text** — when the classifier escalates Small→Medium, the chat text from the advisor still says "applying small tightening" because the advisor's prompt doesn't see the post-classification escalation. Future work: surface the escalation in a follow-up turn or a sub-message.
- **Mid-Phase-X cancellation granularity for Large** — worker checks status between phases; in-flight LLM calls finish.
- **Permission revocation UX** — if the user revokes browser notification permission mid-session, the in-app banner still works; system does not detect/reprompt.
- **Multi-user concurrency** — single-user assumption holds; partial unique index is per-user.
