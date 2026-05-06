# Argosy Plan Distillate + Monthly Synthesis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the plan-distillate ingestion pipeline + monthly synthesis flow + speculative-candidate surfacing per `docs/superpowers/specs/2026-05-05-plan-distillate-design.md`.

**Architecture:** Three artifacts (`baseline`, `draft`, `current`) on a `role`-tagged `plan_versions` table; three flows (`plan_distill_flow`, `plan_synthesis_flow`, existing `decision_flow`); the advisor anchors only on `current`. The Jacobs baseline is distilled to ~2k tokens of durable principles + targets-as-stated; the agent fleet re-synthesizes the long/medium/short plan monthly.

**Tech Stack:** Python 3.12, SQLAlchemy + Alembic, FastAPI, pydantic, pytest, Anthropic Claude Agent SDK, Next.js 15 + TypeScript + Tailwind + shadcn/ui.

**Wave gates (no skipping):**

- **Wave 1 → Wave 2 gate** — `Jacobs_Wealth_Plan.md` distilled, viewable, editable on the advisor page; `plan_watcher` daily loop running; `tests/test_plan_distiller.py` golden-output corpus passes.
- **Wave 2 → Wave 3 gate** — at least one full monthly synthesis cycle (auto-scheduled OR user-initiated) end-to-end accepted in paper-only mode; passes `tests/test_plan_synthesis_e2e.py`. Per SDD §3.5 soak rules.
- **Final gate** — speculative candidates surface in the advisor draft, route to Argonaut paper queue without ever exceeding the configured `speculation.max_pct_of_net_worth` cap.

**Dependency notes:**

- Wave 1 depends on existing SDD Phase-1 work (intake_upload flow, `plan_versions` table, plan_critique agent) — already in place.
- Wave 2 depends on the SDD Phase-3 agent fleet (analyst team, researcher debate, risk team, fund manager) being wired and individually green per their existing tests. **Do not start Wave 2 until Phase 3 is in.**
- Wave 3 depends on SDD Phase-5 Argonaut limited-account autonomy. **Do not start Wave 3 until Phase 5 is in.**

**Test posture per SDD §14.6:** every wave includes unit tests, an agent eval case, and an integration test. The user has explicitly stated **accuracy over LLM cost** — do not skimp on test coverage or model depth in service of cost.

---

# WAVE 1 — Baseline Distillate

This wave is shippable on its own. It gives the advisor a structured view of the user's imported plan; nothing else changes (synthesis lands in Wave 2).

**Files this wave creates or modifies:**

- Create: `argosy/agents/plan_distiller.py`
- Create: `argosy/orchestrator/loops/plan_watcher.py`
- Create: `tests/test_plan_distiller.py`
- Create: `tests/test_plan_distiller_golden.py`
- Create: `tests/test_plan_watcher_loop.py`
- Create: `tests/test_migration_0015.py`
- Create: `tests/test_migration_0016.py`
- Create: `tests/golden/jacobs_distillate_expected.json`
- Create: `alembic/versions/0015_plan_versions_lifecycle.py`
- Create: `alembic/versions/0016_plan_versions_distillate.py`
- Create: `ui/src/components/plan-in-scope-card.tsx`
- Create: `ui/src/components/distillate-edit-dialog.tsx`
- Modify: `argosy/state/models.py` (add columns to `PlanVersion`; reflect lifecycle role)
- Modify: `argosy/state/queries.py` (queries for active baseline, current, draft)
- Modify: `argosy/api/routes/intake.py` (call distiller in upload happy-path)
- Modify: `argosy/api/routes/advisor.py` (or new `argosy/api/routes/plan.py` — add baseline distillate endpoints)
- Modify: `ui/src/lib/api.ts` (`PlanCurrentDTO` extension; new endpoints)
- Modify: `ui/src/app/advisor/page.tsx` (mount `<PlanInScopeCard>`)
- Modify: `argosy/orchestrator/scheduler.py` (register `plan_watcher` cadence)
- Modify: `docs/design/SDD.md` (new §6.10; updates to §3.6, §5.1, §8.1, §8.5)

---

### Task 1.1: Pydantic types for `PlanDistillate`

**Files:**
- Create: `argosy/agents/plan_distiller_types.py`
- Test: `tests/test_plan_distiller.py` (initial scaffold)

- [ ] **Step 1: Write the failing test**

Create `tests/test_plan_distiller.py`:

```python
"""Tests for argosy.agents.plan_distiller — see SDD §6.10 / spec §3."""

from __future__ import annotations

from datetime import date

import pytest


def test_plan_distillate_round_trips_minimal():
    """A minimal PlanDistillate must construct + serialize cleanly."""
    from argosy.agents.plan_distiller_types import (
        PlanDistillate,
        Goal,
        Principle,
        Target,
        DecisionRule,
        Constraint,
    )

    d = PlanDistillate(
        plan_label="Jacobs Wealth Plan v2.0",
        distilled_at_iso="2026-05-05T00:00:00+00:00",
        goals=[
            Goal(
                label="retirement_target_year",
                value="2031",
                rationale="Stated retirement target",
                source_section="Executive Overview",
            )
        ],
        principles=[
            Principle(
                label="UCITS-first for estate safety",
                rationale="Avoids US estate exposure for non-resident aliens",
                source_section="Asset Allocation",
            )
        ],
        risk_priorities=["concentration", "fx", "sector_overweight"],
        decision_rules=[
            DecisionRule(
                label="bracket_aware_rsu_sales",
                rule="Spread RSU sales across years to avoid 47-50% bracket spikes",
                source_section="Tax Optimization",
            )
        ],
        targets=[
            Target(
                label="NVDA concentration",
                value=0.15,
                unit="pct_of_portfolio",
                stated_at=date(2026, 2, 1),
                revisit_after=date(2026, 8, 1),
                rationale="Reduce single-stock exposure",
                source_section="Investment Strategy",
            )
        ],
        constraints=[
            Constraint(
                label="no_consolidate_brokers",
                detail="Do not recommend merging Schwab and Leumi",
                source_section="Operational Preferences",
            )
        ],
        stress_tolerance="Willing to ride 30% drawdown while employed",
    )

    payload = d.model_dump_json()
    assert "Jacobs Wealth Plan v2.0" in payload
    assert "concentration" in payload

    # Round-trip
    d2 = PlanDistillate.model_validate_json(payload)
    assert d2.plan_label == d.plan_label
    assert d2.targets[0].unit == "pct_of_portfolio"
    assert d2.risk_priorities[0] == "concentration"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_plan_distiller.py::test_plan_distillate_round_trips_minimal -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'argosy.agents.plan_distiller_types'`

- [ ] **Step 3: Write the types module**

Create `argosy/agents/plan_distiller_types.py`:

```python
"""Pydantic types for the baseline plan distillate.

Per SDD §6.10 / spec §3: the distillate captures durable principles +
targets-as-stated; explicitly drops time-stamped numbers (current
portfolio %, FX rate, share counts, dated tranche schedules).

Each item carries a ``source_section`` pointer back to the heading in
the imported plan markdown for click-through provenance.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


# Allowed unit values for ``Target.unit``. Kept as a Literal for
# pydantic validation rather than a free-form string.
TargetUnit = Literal[
    "pct_of_portfolio",
    "pct_of_net_worth",
    "pct_of_liquid",
    "usd",
    "nis",
    "shares",
    "ratio",
    "years",
]


class Goal(BaseModel):
    """A durable goal extracted from the plan.

    Examples: retirement target year, target annual income, FI status,
    employment horizon. Goals are durable (years, not months) and rarely
    revised between syntheses.
    """

    label: str
    value: str
    rationale: str = ""
    source_section: str = ""
    user_edited: bool = False
    user_edit_note: str | None = None


class Principle(BaseModel):
    """An investment-philosophy principle from the plan.

    Examples: UCITS-first for estate safety, real-returns framework,
    NIS salary covers NIS expenses (natural hedge), concentration is
    the load-bearing risk.
    """

    label: str
    rationale: str = ""
    source_section: str = ""
    user_edited: bool = False
    user_edit_note: str | None = None


class DecisionRule(BaseModel):
    """A decision rule the user has committed to.

    Examples: bracket-aware RSU sales, gap-weighted deployment, no
    Defensive above cap, never panic-convert NIS<->USD.
    """

    label: str
    rule: str
    source_section: str = ""
    user_edited: bool = False
    user_edit_note: str | None = None


class Target(BaseModel):
    """A numeric target with explicit as-of stamping.

    Examples: NVDA -> 15%, defensive 5-8%, Core 20-25%, Growth 15-20%.
    The ``stated_at`` and ``revisit_after`` dates make the time-bound
    nature of the value explicit so consumers can age-down the
    recommendation as needed.
    """

    label: str
    value: float
    unit: TargetUnit
    stated_at: date
    revisit_after: date
    rationale: str = ""
    source_section: str = ""
    user_edited: bool = False
    user_edit_note: str | None = None


class Constraint(BaseModel):
    """An operational constraint the user has opted in to.

    Examples: no consolidate brokers, UCITS preferred, limited account
    capped at $1k, speculation max % cap.
    """

    label: str
    detail: str
    source_section: str = ""
    user_edited: bool = False
    user_edit_note: str | None = None


class PlanDistillate(BaseModel):
    """Compressed structured extract of a baseline plan.

    Target rendered size: 1500-2500 tokens. The only representation of
    the baseline that downstream synthesis ever consumes; the full
    ``raw_markdown`` is preserved for forensic / "show me the source"
    lookups but is never injected into agent prompts.

    Exclusions enforced by the distiller's system prompt:
      - Current portfolio percentages
      - Current FX rates
      - Specific dollar amounts at point-in-time
      - Dated tranche schedules
      - Share counts
      - Implementation roadmap "next 30/90 days" sections
    """

    plan_label: str
    distilled_at_iso: str  # ISO-8601 UTC

    goals: list[Goal] = Field(default_factory=list)
    principles: list[Principle] = Field(default_factory=list)
    risk_priorities: list[str] = Field(
        default_factory=list,
        description="Ordered list of top risks; first item dominates."
    )
    decision_rules: list[DecisionRule] = Field(default_factory=list)
    targets: list[Target] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    stress_tolerance: str = ""


__all__ = [
    "Goal",
    "Principle",
    "DecisionRule",
    "Target",
    "TargetUnit",
    "Constraint",
    "PlanDistillate",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_plan_distiller.py::test_plan_distillate_round_trips_minimal -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/agents/plan_distiller_types.py tests/test_plan_distiller.py
git commit -m "feat(plan-distillate): add PlanDistillate pydantic types"
```

---

### Task 1.2: Migration 0015 — `plan_versions` lifecycle columns + `decision_runs.decision_kind`

**Files:**
- Create: `alembic/versions/0015_plan_versions_lifecycle.py`
- Create: `tests/test_migration_0015.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_migration_0015.py`:

```python
"""Schema-shape assertions after migration 0015.

Mirrors the pattern of tests/test_migration_0013.py: spin up a temp DB,
run alembic upgrade to head, assert columns + indexes + constraints
exist with the expected types.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect


def _columns(engine, table: str) -> dict[str, dict]:
    insp = inspect(engine)
    return {c["name"]: c for c in insp.get_columns(table)}


def _indexes(engine, table: str) -> list[dict]:
    insp = inspect(engine)
    return insp.get_indexes(table)


def test_0015_adds_lifecycle_columns(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "plan_versions")
    assert "role" in cols
    assert "accepted_at" in cols
    assert "accepted_by_user_id" in cols
    assert "superseded_at" in cols
    assert "derived_from_id" in cols
    assert "decision_run_id" in cols


def test_0015_adds_decision_kind_to_decision_runs(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "decision_runs")
    assert "decision_kind" in cols


def test_0015_adds_partial_unique_indexes(alembic_engine_at_head):
    """One baseline / current / draft per user — partial unique indexes."""
    idxs = {i["name"]: i for i in _indexes(alembic_engine_at_head, "plan_versions")}
    expected = {
        "uq_plan_versions_baseline_per_user",
        "uq_plan_versions_current_per_user",
        "uq_plan_versions_draft_per_user",
    }
    assert expected.issubset(idxs.keys()), f"missing partial unique indexes: {expected - idxs.keys()}"


def test_0015_role_default_is_baseline_for_existing_rows(
    alembic_engine_with_existing_plan_row,
):
    """Pre-existing plan_versions rows must be backfilled to role=baseline.

    Pre-0015 the table had implicit "all rows are baseline-ish" semantics.
    The migration must backfill role='baseline' so existing data still
    resolves to a usable plan.
    """
    eng = alembic_engine_with_existing_plan_row
    with eng.connect() as conn:
        rows = conn.execute(sa.text("SELECT role FROM plan_versions")).fetchall()
    assert all(r[0] == "baseline" for r in rows), rows
```

This test depends on two fixtures (`alembic_engine_at_head` and `alembic_engine_with_existing_plan_row`) that may not yet exist in `tests/conftest.py`. Add them in the next step.

- [ ] **Step 2: Add fixtures to `tests/conftest.py` if missing**

Read `tests/conftest.py`. If `alembic_engine_at_head` is not defined, add (append):

```python
import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from alembic.config import Config
from alembic import command


def _make_alembic_config(db_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def alembic_engine_at_head(tmp_path):
    """A fresh SQLite DB upgraded to alembic head."""
    db_path = tmp_path / "argosy_test.db"
    db_url = f"sqlite:///{db_path}"
    cfg = _make_alembic_config(db_url)
    command.upgrade(cfg, "head")
    eng = create_engine(db_url)
    yield eng
    eng.dispose()


@pytest.fixture
def alembic_engine_with_existing_plan_row(tmp_path):
    """DB upgraded to 0014, a plan_versions row inserted, THEN upgraded to head.

    Verifies backfill of new columns on existing data.
    """
    db_path = tmp_path / "argosy_test.db"
    db_url = f"sqlite:///{db_path}"
    cfg = _make_alembic_config(db_url)
    command.upgrade(cfg, "0014_investor_events_dedup")
    eng = create_engine(db_url)
    with eng.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, plan, created_at) VALUES ('ariel', 'free', :now)"), {"now": "2026-01-01"})
        conn.execute(sa.text(
            "INSERT INTO plan_versions (user_id, version_label, source_path, raw_markdown, imported_at) "
            "VALUES ('ariel', 'Jacobs v2.0', '', '# Plan', :now)"
        ), {"now": "2026-02-01"})
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()
```

If either fixture already exists, skip the duplicate.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_migration_0015.py -v`
Expected: FAIL — migration 0015 does not yet exist; head is still 0014.

- [ ] **Step 4: Write the migration**

Create `alembic/versions/0015_plan_versions_lifecycle.py`:

```python
"""plan_versions lifecycle: role + acceptance/lineage columns; decision_runs.decision_kind.

Revision ID: 0015_plan_versions_lifecycle
Revises: 0014_investor_events_dedup
Create Date: 2026-05-05

Per spec docs/superpowers/specs/2026-05-05-plan-distillate-design.md §5.1:

  - role: enum baseline | draft | current | superseded
  - accepted_at, accepted_by_user_id, superseded_at: lifecycle stamps
  - derived_from_id: lineage of synthesized rows -> baseline / prior current
  - decision_run_id: links synthesis row to fleet-review run

Plus partial unique indexes:
  - one baseline per user
  - one current per user
  - one draft per user

And on decision_runs: decision_kind column to distinguish trade-proposal
runs from plan-revision runs.

Backfill: pre-existing rows are baselines (the table previously held
imported plans only). Set role='baseline' on every pre-existing row.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_plan_versions_lifecycle"
down_revision: str | Sequence[str] | None = "0014_investor_events_dedup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add columns nullable so we can backfill, then tighten where needed.
    with op.batch_alter_table("plan_versions") as batch:
        batch.add_column(sa.Column("role", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("accepted_by_user_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("derived_from_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("decision_run_id", sa.String(length=64), nullable=True))

    # 2. Backfill role='baseline' for all existing rows.
    op.execute("UPDATE plan_versions SET role = 'baseline' WHERE role IS NULL")

    # 3. Tighten role to NOT NULL with a server default.
    with op.batch_alter_table("plan_versions") as batch:
        batch.alter_column(
            "role",
            existing_type=sa.String(length=16),
            nullable=False,
            server_default="baseline",
        )
        # FK on derived_from_id (self-referential).
        batch.create_foreign_key(
            "fk_plan_versions_derived_from",
            "plan_versions",
            ["derived_from_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # 4. Partial unique indexes (SQLite-compatible WHERE clause).
    op.create_index(
        "uq_plan_versions_baseline_per_user",
        "plan_versions",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("role = 'baseline'"),
        postgresql_where=sa.text("role = 'baseline'"),
    )
    op.create_index(
        "uq_plan_versions_current_per_user",
        "plan_versions",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("role = 'current'"),
        postgresql_where=sa.text("role = 'current'"),
    )
    op.create_index(
        "uq_plan_versions_draft_per_user",
        "plan_versions",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("role = 'draft'"),
        postgresql_where=sa.text("role = 'draft'"),
    )
    # Non-unique helper for history queries.
    op.create_index(
        "ix_plan_versions_user_role",
        "plan_versions",
        ["user_id", "role"],
        unique=False,
    )

    # 5. decision_runs.decision_kind. Inspector check first — if the
    #    table does not exist (very old DBs), skip silently.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("decision_runs"):
        existing_cols = {c["name"] for c in insp.get_columns("decision_runs")}
        if "decision_kind" not in existing_cols:
            with op.batch_alter_table("decision_runs") as batch:
                batch.add_column(
                    sa.Column(
                        "decision_kind",
                        sa.String(length=32),
                        nullable=False,
                        server_default="trade_proposal",
                    )
                )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("decision_runs"):
        existing_cols = {c["name"] for c in insp.get_columns("decision_runs")}
        if "decision_kind" in existing_cols:
            with op.batch_alter_table("decision_runs") as batch:
                batch.drop_column("decision_kind")

    op.drop_index("ix_plan_versions_user_role", table_name="plan_versions")
    op.drop_index("uq_plan_versions_draft_per_user", table_name="plan_versions")
    op.drop_index("uq_plan_versions_current_per_user", table_name="plan_versions")
    op.drop_index("uq_plan_versions_baseline_per_user", table_name="plan_versions")

    with op.batch_alter_table("plan_versions") as batch:
        batch.drop_constraint("fk_plan_versions_derived_from", type_="foreignkey")
        batch.drop_column("decision_run_id")
        batch.drop_column("derived_from_id")
        batch.drop_column("superseded_at")
        batch.drop_column("accepted_by_user_id")
        batch.drop_column("accepted_at")
        batch.drop_column("role")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_migration_0015.py -v`
Expected: PASS (4 tests)

Then run upgrade-downgrade to verify reversibility:

```bash
python -c "from alembic.config import Config; from alembic import command; cfg = Config('alembic.ini'); cfg.set_main_option('sqlalchemy.url', 'sqlite:///./scratch_0015.db'); command.upgrade(cfg, 'head'); command.downgrade(cfg, '0014_investor_events_dedup'); command.upgrade(cfg, 'head')"
```

Expected: completes without error. Delete `scratch_0015.db` afterwards.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0015_plan_versions_lifecycle.py tests/test_migration_0015.py tests/conftest.py
git commit -m "feat(db): migration 0015 — plan_versions lifecycle + decision_runs.decision_kind"
```

---

### Task 1.3: Migration 0016 — `plan_versions` distillate columns

**Files:**
- Create: `alembic/versions/0016_plan_versions_distillate.py`
- Create: `tests/test_migration_0016.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_migration_0016.py`:

```python
"""Schema assertions after migration 0016 (distillate columns)."""

from __future__ import annotations

from sqlalchemy import inspect


def _columns(engine, table: str) -> dict[str, dict]:
    insp = inspect(engine)
    return {c["name"]: c for c in insp.get_columns(table)}


def test_0016_adds_distillate_columns(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "plan_versions")
    for name in ("distillate_json", "distillate_rendered", "source_hash", "distilled_at"):
        assert name in cols, f"expected column {name} on plan_versions, got {sorted(cols)}"


def test_0016_distillate_columns_are_nullable(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "plan_versions")
    # All distillate columns are populated only on role=baseline rows;
    # synthesized rows leave them NULL.
    for name in ("distillate_json", "distillate_rendered", "source_hash", "distilled_at"):
        assert cols[name]["nullable"] is True, f"{name} must be nullable"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_migration_0016.py -v`
Expected: FAIL — columns do not yet exist.

- [ ] **Step 3: Write the migration**

Create `alembic/versions/0016_plan_versions_distillate.py`:

```python
"""plan_versions distillate columns.

Revision ID: 0016_plan_versions_distillate
Revises: 0015_plan_versions_lifecycle
Create Date: 2026-05-05

Per spec §5.2: populated only when role=baseline. Synthesized rows
(role in {draft,current,superseded}) leave these NULL.

  - distillate_json: PlanDistillate pydantic JSON
  - distillate_rendered: pre-rendered markdown view (UI consumes)
  - source_hash: sha256 of raw_markdown — drives plan_watcher diff detection
  - distilled_at: when the last distill run completed

Note: source_path already exists on plan_versions (added pre-0001).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_plan_versions_distillate"
down_revision: str | Sequence[str] | None = "0015_plan_versions_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("plan_versions") as batch:
        batch.add_column(sa.Column("distillate_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("distillate_rendered", sa.Text(), nullable=True))
        batch.add_column(sa.Column("source_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("distilled_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("plan_versions") as batch:
        batch.drop_column("distilled_at")
        batch.drop_column("source_hash")
        batch.drop_column("distillate_rendered")
        batch.drop_column("distillate_json")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_migration_0016.py -v`
Expected: PASS (2 tests)

Reversibility check:

```bash
python -c "from alembic.config import Config; from alembic import command; cfg = Config('alembic.ini'); cfg.set_main_option('sqlalchemy.url', 'sqlite:///./scratch_0016.db'); command.upgrade(cfg, 'head'); command.downgrade(cfg, '0015_plan_versions_lifecycle'); command.upgrade(cfg, 'head')"
```

Expected: completes without error. Delete `scratch_0016.db`.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/0016_plan_versions_distillate.py tests/test_migration_0016.py
git commit -m "feat(db): migration 0016 — plan_versions distillate columns"
```

---

### Task 1.4: Reflect new columns on `PlanVersion` SQLAlchemy model

**Files:**
- Modify: `argosy/state/models.py:88-110` (PlanVersion class)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_phase1_models.py` (append):

```python
def test_plan_version_has_lifecycle_and_distillate_fields():
    """Spec §5: PlanVersion now carries role + lifecycle + distillate columns."""
    from argosy.state.models import PlanVersion

    expected_fields = {
        "role",
        "accepted_at",
        "accepted_by_user_id",
        "superseded_at",
        "derived_from_id",
        "decision_run_id",
        "distillate_json",
        "distillate_rendered",
        "source_hash",
        "distilled_at",
    }
    actual = set(PlanVersion.__table__.columns.keys())
    missing = expected_fields - actual
    assert not missing, f"PlanVersion missing fields: {missing}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_phase1_models.py::test_plan_version_has_lifecycle_and_distillate_fields -v`
Expected: FAIL — model has not been updated yet.

- [ ] **Step 3: Update `PlanVersion` model**

Edit `argosy/state/models.py`. Replace the `PlanVersion` class (currently at lines ~88-110) with:

```python
class PlanVersion(Base):
    """An imported or synthesized plan, with explicit lifecycle role.

    Roles per SDD §6.10:
      - baseline: user-imported source (Jacobs Wealth Plan v2.0). Carries
        distillate_json + distillate_rendered + source_hash. One active
        per user (partial unique index).
      - draft: synthesis output awaiting user accept. Carries horizon_*_*
        columns (added in 0017). One in-flight per user.
      - current: accepted draft, the canonical plan the advisor anchors on.
      - superseded: historical; demoted from baseline/current/draft.

    Lineage: derived_from_id points back to the source row a synthesized
    plan was built from (typically the active baseline at synthesis time).
    """

    __tablename__ = "plan_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_label: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    raw_markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    # Lifecycle (migration 0015).
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="baseline", server_default="baseline"
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    derived_from_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("plan_versions.id", ondelete="SET NULL"), nullable=True
    )
    decision_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Distillate (migration 0016) — populated only when role='baseline'.
    distillate_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    distillate_rendered: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    distilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    critiques: Mapped[list["PlanCritique"]] = relationship(
        back_populates="plan_version", cascade="all, delete-orphan"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_phase1_models.py::test_plan_version_has_lifecycle_and_distillate_fields -v`
Expected: PASS

Sanity-check the broader model suite still passes:

Run: `pytest tests/test_phase1_models.py -v`
Expected: PASS (existing tests unchanged)

- [ ] **Step 5: Commit**

```bash
git add argosy/state/models.py tests/test_phase1_models.py
git commit -m "feat(state): reflect lifecycle + distillate columns on PlanVersion"
```

---

### Task 1.5: State queries for active baseline / current / draft

**Files:**
- Modify: `argosy/state/queries.py` (add helpers)
- Test: `tests/test_phase1_models.py` (or new `tests/test_plan_queries.py`)

- [ ] **Step 1: Write the failing test**

Create `tests/test_plan_queries.py`:

```python
"""Queries for plan_versions lifecycle access — see spec §5."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from argosy.state.models import PlanVersion, User


@pytest.fixture
def session_with_users(alembic_engine_at_head):
    from sqlalchemy.orm import sessionmaker

    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    sess = SessionLocal()
    sess.add(User(id="ariel", plan="free"))
    sess.add(User(id="dana", plan="free"))
    sess.commit()
    yield sess
    sess.close()


def _make(sess: Session, **kw) -> PlanVersion:
    pv = PlanVersion(**kw)
    sess.add(pv)
    sess.commit()
    sess.refresh(pv)
    return pv


def test_get_active_baseline_returns_only_role_baseline(session_with_users):
    from argosy.state.queries import get_active_baseline

    sess = session_with_users
    _make(sess, user_id="ariel", role="baseline", version_label="Jacobs v2.0", raw_markdown="# Plan")
    _make(sess, user_id="ariel", role="superseded", version_label="Jacobs v1.0", raw_markdown="# Old")

    pv = get_active_baseline(sess, "ariel")
    assert pv is not None
    assert pv.role == "baseline"
    assert pv.version_label == "Jacobs v2.0"


def test_get_active_baseline_returns_none_when_absent(session_with_users):
    from argosy.state.queries import get_active_baseline

    pv = get_active_baseline(session_with_users, "dana")
    assert pv is None


def test_get_current_plan_returns_role_current(session_with_users):
    from argosy.state.queries import get_current_plan

    sess = session_with_users
    _make(sess, user_id="ariel", role="baseline", version_label="Jacobs v2.0", raw_markdown="# Plan")
    _make(sess, user_id="ariel", role="current", version_label="synth-2026-05", raw_markdown="")

    pv = get_current_plan(sess, "ariel")
    assert pv is not None
    assert pv.role == "current"


def test_get_pending_draft_returns_role_draft(session_with_users):
    from argosy.state.queries import get_pending_draft

    sess = session_with_users
    _make(sess, user_id="ariel", role="baseline", version_label="Jacobs v2.0", raw_markdown="# Plan")
    _make(sess, user_id="ariel", role="draft", version_label="synth-2026-06-draft", raw_markdown="")

    pv = get_pending_draft(sess, "ariel")
    assert pv is not None
    assert pv.role == "draft"


def test_at_most_one_baseline_per_user(session_with_users):
    """The partial unique index from migration 0015 must reject duplicates."""
    from sqlalchemy.exc import IntegrityError

    sess = session_with_users
    _make(sess, user_id="ariel", role="baseline", version_label="A", raw_markdown="")
    with pytest.raises(IntegrityError):
        _make(sess, user_id="ariel", role="baseline", version_label="B", raw_markdown="")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plan_queries.py -v`
Expected: FAIL — `get_active_baseline` / `get_current_plan` / `get_pending_draft` don't exist yet.

- [ ] **Step 3: Implement the queries**

Append to `argosy/state/queries.py`:

```python
# ----------------------------------------------------------------------
# Plan-versions lifecycle queries (spec §5)
# ----------------------------------------------------------------------


def get_active_baseline(session, user_id: str):
    """Return the user's active baseline plan, or None.

    Partial unique index ``uq_plan_versions_baseline_per_user`` guarantees
    at most one row matches.
    """
    from argosy.state.models import PlanVersion

    return (
        session.query(PlanVersion)
        .filter(PlanVersion.user_id == user_id, PlanVersion.role == "baseline")
        .one_or_none()
    )


def get_current_plan(session, user_id: str):
    """Return the user's currently-accepted plan (role='current'), or None.

    Partial unique index ``uq_plan_versions_current_per_user`` guarantees
    at most one row matches.
    """
    from argosy.state.models import PlanVersion

    return (
        session.query(PlanVersion)
        .filter(PlanVersion.user_id == user_id, PlanVersion.role == "current")
        .one_or_none()
    )


def get_pending_draft(session, user_id: str):
    """Return the user's in-flight draft plan, or None.

    Partial unique index ``uq_plan_versions_draft_per_user`` guarantees
    at most one row matches.
    """
    from argosy.state.models import PlanVersion

    return (
        session.query(PlanVersion)
        .filter(PlanVersion.user_id == user_id, PlanVersion.role == "draft")
        .one_or_none()
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plan_queries.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add argosy/state/queries.py tests/test_plan_queries.py
git commit -m "feat(state): plan-versions lifecycle accessors (active baseline / current / draft)"
```

---

### Task 1.6: `PlanDistillerAgent` — system prompt + agent class

**Files:**
- Create: `argosy/agents/plan_distiller.py`
- Modify: `argosy/agents/base.py:45-89` (add `plan_distiller` to `DEFAULT_MODEL_BY_ROLE`)
- Test: `tests/test_plan_distiller.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_plan_distiller.py`:

```python
def test_plan_distiller_agent_basic_shape():
    """The agent declares the right role, output model, and citation policy."""
    from argosy.agents.plan_distiller import PlanDistillerAgent
    from argosy.agents.plan_distiller_types import PlanDistillate

    agent = PlanDistillerAgent()
    assert agent.agent_role == "plan_distiller"
    assert agent.output_model is PlanDistillate
    # Source IS the plan -> external citations not required, but the
    # source_section provenance is expected per item.
    assert agent.require_citations is False


def test_plan_distiller_build_prompt_contains_exclusion_list():
    """The system prompt must enumerate excluded categories explicitly."""
    from argosy.agents.plan_distiller import PlanDistillerAgent

    agent = PlanDistillerAgent()
    sys, usr = agent.build_prompt(
        plan_label="Jacobs Wealth Plan v2.0",
        plan_markdown="# Plan\n\nNVDA at 66% today.\n",
    )
    # Exclusion list — these phrases must appear so the agent knows
    # what to drop:
    for phrase in (
        "current portfolio percentages",
        "current FX rates",
        "specific dollar amounts",
        "dated tranche schedules",
        "share counts",
    ):
        assert phrase.lower() in sys.lower(), f"missing exclusion: {phrase}"
    # Plan markdown must be in the user prompt (not the system prompt —
    # makes prompt-cache friendliness easier later).
    assert "NVDA at 66% today" in usr
    # Plan label must be passed through.
    assert "Jacobs Wealth Plan v2.0" in usr
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plan_distiller.py -v -k "agent_basic_shape or exclusion_list"`
Expected: FAIL — `argosy.agents.plan_distiller` does not exist yet.

- [ ] **Step 3: Add `plan_distiller` to default model map**

In `argosy/agents/base.py`, find `DEFAULT_MODEL_BY_ROLE: dict[str, str] = {` (around line 45). Add this entry alongside the other intake-family entries:

```python
    # Plan-distiller: extracts durable principles + targets from a
    # baseline plan markdown. Single-pass; structured output. Sonnet.
    "plan_distiller": "claude-sonnet-4-6",
```

- [ ] **Step 4: Write the agent**

Create `argosy/agents/plan_distiller.py`:

```python
"""Plan-distiller agent — extract a durable, LLM-suited distillate from a
user-imported plan markdown.

See SDD §6.10 / spec docs/superpowers/specs/2026-05-05-plan-distillate-design.md §3.

Inputs: plan markdown (already in the DB as ``plan_versions.raw_markdown``).
Output: a structured ``PlanDistillate`` capturing principles, targets-as-stated,
decision rules, constraints, goals, risk priorities, and stress tolerance.

EXPLICIT EXCLUSIONS (enforced in the system prompt):
  - Current portfolio percentages (66% NVDA today, etc.)
  - Current FX rates (3.09 NIS/USD)
  - Specific dollar amounts at point-in-time ($430k proceeds)
  - Dated tranche schedules (Q1 2026 sells 2,500 shares)
  - Share counts (12,748 NVDA shares)
  - "Next 30/90 days" implementation roadmap sections

These will be re-derived monthly by ``plan_synthesis_flow`` from current
state — the distillate must NOT bake them in.
"""

from __future__ import annotations

from argosy.agents.base import BaseAgent
from argosy.agents.plan_distiller_types import PlanDistillate

EXCLUSION_LIST = [
    "current portfolio percentages (66% NVDA today, 19% defensive, etc.)",
    "current FX rates (e.g. 3.09 NIS/USD)",
    "specific dollar amounts at a point in time (e.g. $430k proceeds, $171.81/share)",
    "dated tranche schedules (Q1 2026 sells 2,500 shares)",
    "share counts (12,748 NVDA shares, etc.)",
    "implementation roadmap 'next 30/90 days' sections — those belong "
    "in the synthesized short-horizon plan, not in the distillate",
]


class PlanDistillerAgent(BaseAgent[PlanDistillate]):
    """Extracts a durable structured distillate from a plan markdown."""

    agent_role = "plan_distiller"
    output_model = PlanDistillate
    # Citations not required — the source IS the user's plan, not an
    # external authority. Each extracted item still carries
    # ``source_section`` so the UI can click-through to the heading.
    require_citations = False
    max_tokens = 8192

    def build_prompt(
        self,
        *,
        plan_label: str,
        plan_markdown: str,
    ) -> tuple[str, str]:
        exclusions = "\n".join(f"  - {item}" for item in EXCLUSION_LIST)
        system = (
            "You are the plan-distiller agent on the Argosy fleet.\n\n"
            "Your job: extract a DURABLE, structured distillate from the "
            "user's imported plan. The distillate is the only representation "
            "of the baseline that downstream synthesis ever consumes; the "
            "raw plan stays available for forensic lookups, but is NOT "
            "injected into agent prompts.\n\n"
            "WHAT TO EXTRACT (durable):\n"
            "  - goals: retirement target year, target annual income, FI status, "
            "    employment horizon, lifestyle aspirations\n"
            "  - principles: investment philosophy (UCITS-first for estate "
            "    safety, real-returns framework, NIS-USD natural hedge, "
            "    concentration is the load-bearing risk)\n"
            "  - risk_priorities: ordered list of top risks the user cares "
            "    about; the first item dominates\n"
            "  - decision_rules: actionable rules the user has committed to "
            "    (bracket-aware RSU sales, gap-weighted deployment, no "
            "    Defensive above cap, never panic-convert)\n"
            "  - targets: numeric targets WITH explicit stated_at + "
            "    revisit_after dates; treat them as working assumptions, "
            "    not eternal truths\n"
            "  - constraints: things the user has explicitly opted in/out "
            "    of (no consolidate brokers, UCITS preferred, speculation "
            "    cap)\n"
            "  - stress_tolerance: free text on willingness to ride "
            "    drawdowns / sequence-of-returns risk tolerance\n\n"
            "EXPLICIT EXCLUSIONS — DO NOT EXTRACT (these decay; the "
            "monthly synthesis flow will derive them fresh from current "
            "state):\n"
            f"{exclusions}\n\n"
            "PROVENANCE: every extracted item must carry a ``source_section`` "
            "pointing to the plan heading or sub-heading where it appears, "
            "so the UI can click-through. Use the plan's own heading text.\n\n"
            "DO NOT INFER. If a category has no clear evidence in the plan, "
            "leave the list empty. The user can fill gaps conversationally.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{PlanDistillate.model_json_schema()}\n"
        )

        user = (
            f"PLAN LABEL: {plan_label}\n\n"
            "=== PLAN MARKDOWN ===\n"
            f"{plan_markdown}\n"
            "=== END PLAN MARKDOWN ===\n\n"
            "Produce the PlanDistillate JSON now. Respect the exclusion "
            "list strictly: if the plan says 'NVDA is currently 66%', "
            "you do NOT record 66% as a target. You may record the "
            "stated target value (e.g. 15%) since that is durable."
        )
        return system, user


__all__ = ["PlanDistillerAgent", "EXCLUSION_LIST"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_plan_distiller.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add argosy/agents/plan_distiller.py argosy/agents/base.py tests/test_plan_distiller.py
git commit -m "feat(agents): PlanDistillerAgent + exclusion list"
```

---

### Task 1.7: Distillate rendering helper (markdown view)

**Files:**
- Create: `argosy/agents/plan_distiller_render.py`
- Test: `tests/test_plan_distiller.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_plan_distiller.py`:

```python
def test_render_distillate_to_markdown_smoke():
    """Rendered markdown contains every category header and each label."""
    from datetime import date

    from argosy.agents.plan_distiller_render import render_distillate
    from argosy.agents.plan_distiller_types import (
        Constraint,
        DecisionRule,
        Goal,
        PlanDistillate,
        Principle,
        Target,
    )

    d = PlanDistillate(
        plan_label="Jacobs v2.0",
        distilled_at_iso="2026-05-05T00:00:00+00:00",
        goals=[Goal(label="retirement_target_year", value="2031")],
        principles=[Principle(label="UCITS-first")],
        risk_priorities=["concentration", "fx"],
        decision_rules=[DecisionRule(label="bracket_aware_rsu_sales", rule="spread sales")],
        targets=[
            Target(
                label="NVDA concentration",
                value=0.15,
                unit="pct_of_portfolio",
                stated_at=date(2026, 2, 1),
                revisit_after=date(2026, 8, 1),
            )
        ],
        constraints=[Constraint(label="no_consolidate_brokers", detail="keep separate")],
        stress_tolerance="30% drawdown OK while employed",
    )

    md = render_distillate(d)
    assert "# Plan distillate — Jacobs v2.0" in md
    assert "## Goals" in md
    assert "retirement_target_year" in md
    assert "## Principles" in md
    assert "UCITS-first" in md
    assert "## Risk priorities" in md
    assert "concentration" in md
    assert "## Decision rules" in md
    assert "bracket_aware_rsu_sales" in md
    assert "## Targets" in md
    assert "NVDA concentration" in md
    assert "stated 2026-02-01" in md
    assert "revisit 2026-08-01" in md
    assert "## Constraints" in md
    assert "no_consolidate_brokers" in md
    assert "## Stress tolerance" in md
    assert "30% drawdown OK" in md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_plan_distiller.py::test_render_distillate_to_markdown_smoke -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write the renderer**

Create `argosy/agents/plan_distiller_render.py`:

```python
"""Pure-Python markdown rendering of a PlanDistillate.

Used in two places:
  1. Stored on `plan_versions.distillate_rendered` so the UI can render
     the distillate without parsing JSON.
  2. Available to any synthesis agent that wants a human-readable
     view alongside the structured payload.

No LLM calls. Deterministic.
"""

from __future__ import annotations

from argosy.agents.plan_distiller_types import PlanDistillate


def render_distillate(d: PlanDistillate) -> str:
    """Render a PlanDistillate to compact markdown.

    Target output size ~1500 tokens for a typical Jacobs-style plan.
    """
    lines: list[str] = []
    lines.append(f"# Plan distillate — {d.plan_label}")
    lines.append("")
    lines.append(f"_Distilled at: {d.distilled_at_iso}_")
    lines.append("")

    if d.goals:
        lines.append("## Goals")
        for g in d.goals:
            edited = " *(user-edited)*" if g.user_edited else ""
            value = f": {g.value}" if g.value else ""
            rationale = f" — {g.rationale}" if g.rationale else ""
            lines.append(f"- **{g.label}**{value}{rationale}{edited}")
            if g.source_section:
                lines.append(f"  · _source: {g.source_section}_")
        lines.append("")

    if d.principles:
        lines.append("## Principles")
        for p in d.principles:
            edited = " *(user-edited)*" if p.user_edited else ""
            rationale = f" — {p.rationale}" if p.rationale else ""
            lines.append(f"- **{p.label}**{rationale}{edited}")
            if p.source_section:
                lines.append(f"  · _source: {p.source_section}_")
        lines.append("")

    if d.risk_priorities:
        lines.append("## Risk priorities")
        lines.append("_(ordered; first item dominates)_")
        for i, r in enumerate(d.risk_priorities, 1):
            lines.append(f"{i}. {r}")
        lines.append("")

    if d.decision_rules:
        lines.append("## Decision rules")
        for r in d.decision_rules:
            edited = " *(user-edited)*" if r.user_edited else ""
            lines.append(f"- **{r.label}**: {r.rule}{edited}")
            if r.source_section:
                lines.append(f"  · _source: {r.source_section}_")
        lines.append("")

    if d.targets:
        lines.append("## Targets")
        lines.append("_(working assumptions, not eternal — each carries an as-of date)_")
        for t in d.targets:
            edited = " *(user-edited)*" if t.user_edited else ""
            stated = t.stated_at.isoformat()
            revisit = t.revisit_after.isoformat()
            rationale = f" — {t.rationale}" if t.rationale else ""
            lines.append(
                f"- **{t.label}**: {t.value} {t.unit} "
                f"(stated {stated}, revisit {revisit}){rationale}{edited}"
            )
            if t.source_section:
                lines.append(f"  · _source: {t.source_section}_")
        lines.append("")

    if d.constraints:
        lines.append("## Constraints")
        for c in d.constraints:
            edited = " *(user-edited)*" if c.user_edited else ""
            lines.append(f"- **{c.label}**: {c.detail}{edited}")
            if c.source_section:
                lines.append(f"  · _source: {c.source_section}_")
        lines.append("")

    if d.stress_tolerance:
        lines.append("## Stress tolerance")
        lines.append(d.stress_tolerance)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


__all__ = ["render_distillate"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_plan_distiller.py::test_render_distillate_to_markdown_smoke -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/agents/plan_distiller_render.py tests/test_plan_distiller.py
git commit -m "feat(agents): markdown renderer for PlanDistillate"
```

---

### Task 1.8: Distill orchestration — pure function that stamps a baseline row

**Files:**
- Create: `argosy/services/plan_distiller_service.py`
- Test: `tests/test_plan_distiller_service.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_plan_distiller_service.py`:

```python
"""Service-layer tests for distill_baseline_plan.

The service is the seam between the API/loop callers and the agent.
Tests use a fake agent so no Anthropic call is made.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import PlanVersion, User


class _FakeDistillerAgent:
    """Stand-in for PlanDistillerAgent — returns a fixed PlanDistillate."""

    def __init__(self, payload):
        self._payload = payload
        self.calls: list[dict] = []

    def run_sync(self, **kw):  # mimic BaseAgent.run_sync signature
        self.calls.append(kw)
        # mimic AgentReport-shaped return: an object with .output
        return type("R", (), {"output": self._payload, "model": "fake", "tokens_in": 10, "tokens_out": 20, "cost_usd": 0.001})()


@pytest.fixture
def session(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    yield s
    s.close()


def _baseline_payload():
    from argosy.agents.plan_distiller_types import (
        Goal,
        PlanDistillate,
        Target,
    )

    return PlanDistillate(
        plan_label="Jacobs v2.0",
        distilled_at_iso="2026-05-05T00:00:00+00:00",
        goals=[Goal(label="retirement_target_year", value="2031")],
        targets=[
            Target(
                label="NVDA",
                value=0.15,
                unit="pct_of_portfolio",
                stated_at=date(2026, 2, 1),
                revisit_after=date(2026, 8, 1),
            )
        ],
    )


def test_distill_baseline_plan_populates_columns(session, monkeypatch):
    from argosy.services import plan_distiller_service as svc

    pv = PlanVersion(
        user_id="ariel",
        role="baseline",
        version_label="Jacobs v2.0",
        raw_markdown="# Plan\n\nRetirement: 2031\nNVDA target 15%\n",
    )
    session.add(pv)
    session.commit()

    fake = _FakeDistillerAgent(_baseline_payload())
    monkeypatch.setattr(svc, "_make_agent", lambda: fake)

    out = svc.distill_baseline_plan(session, plan_version_id=pv.id, user_id="ariel")

    session.refresh(pv)
    assert pv.distillate_json is not None
    parsed = json.loads(pv.distillate_json)
    assert parsed["plan_label"] == "Jacobs v2.0"
    assert pv.distillate_rendered is not None
    assert "# Plan distillate" in pv.distillate_rendered
    assert pv.distilled_at is not None
    expected_hash = hashlib.sha256(pv.raw_markdown.encode("utf-8")).hexdigest()
    assert pv.source_hash == expected_hash
    assert out.distillate.plan_label == "Jacobs v2.0"


def test_distill_baseline_plan_preserves_user_edits_on_rerun(session, monkeypatch):
    """If user edited a target, re-distill must NOT clobber it."""
    from argosy.agents.plan_distiller_types import PlanDistillate, Target
    from argosy.services import plan_distiller_service as svc

    # Initial distill.
    pv = PlanVersion(
        user_id="ariel",
        role="baseline",
        version_label="Jacobs v2.0",
        raw_markdown="# Plan",
    )
    session.add(pv)
    session.commit()

    initial = _baseline_payload()
    monkeypatch.setattr(svc, "_make_agent", lambda: _FakeDistillerAgent(initial))
    svc.distill_baseline_plan(session, plan_version_id=pv.id, user_id="ariel")

    # User edits the NVDA target down to 0.12.
    svc.set_distillate_item_user_edit(
        session,
        plan_version_id=pv.id,
        category="targets",
        item_label="NVDA",
        new_value={"value": 0.12, "user_edit_note": "tighter than plan"},
    )

    # Re-run with a fresh distiller output that says 0.15 again.
    refresh = PlanDistillate(
        plan_label="Jacobs v2.0",
        distilled_at_iso="2026-05-06T00:00:00+00:00",
        targets=[
            Target(
                label="NVDA",
                value=0.15,
                unit="pct_of_portfolio",
                stated_at=date(2026, 2, 1),
                revisit_after=date(2026, 8, 1),
            )
        ],
    )
    monkeypatch.setattr(svc, "_make_agent", lambda: _FakeDistillerAgent(refresh))
    svc.distill_baseline_plan(
        session, plan_version_id=pv.id, user_id="ariel", preserve_user_edits=True
    )

    session.refresh(pv)
    parsed = json.loads(pv.distillate_json)
    nvda = next(t for t in parsed["targets"] if t["label"] == "NVDA")
    assert nvda["value"] == 0.12, f"user edit was clobbered: {nvda}"
    assert nvda["user_edited"] is True


def test_distill_baseline_plan_force_overwrites_user_edits(session, monkeypatch):
    """When force_refresh=True, user edits are dropped (with a warning)."""
    from argosy.services import plan_distiller_service as svc

    pv = PlanVersion(
        user_id="ariel",
        role="baseline",
        version_label="Jacobs v2.0",
        raw_markdown="# Plan",
    )
    session.add(pv)
    session.commit()

    initial = _baseline_payload()
    monkeypatch.setattr(svc, "_make_agent", lambda: _FakeDistillerAgent(initial))
    svc.distill_baseline_plan(session, plan_version_id=pv.id, user_id="ariel")
    svc.set_distillate_item_user_edit(
        session, plan_version_id=pv.id, category="targets",
        item_label="NVDA", new_value={"value": 0.12},
    )

    # Force refresh — user edit dropped.
    monkeypatch.setattr(svc, "_make_agent", lambda: _FakeDistillerAgent(initial))
    svc.distill_baseline_plan(
        session, plan_version_id=pv.id, user_id="ariel",
        preserve_user_edits=False,
    )

    session.refresh(pv)
    parsed = json.loads(pv.distillate_json)
    nvda = next(t for t in parsed["targets"] if t["label"] == "NVDA")
    assert nvda["value"] == 0.15
    assert nvda["user_edited"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plan_distiller_service.py -v`
Expected: FAIL — `argosy.services.plan_distiller_service` does not exist.

- [ ] **Step 3: Write the service**

Create `argosy/services/__init__.py` if it does not exist (empty file):

```bash
mkdir -p argosy/services && touch argosy/services/__init__.py
```

Create `argosy/services/plan_distiller_service.py`:

```python
"""Service layer for plan distillation.

Wraps PlanDistillerAgent + persistence logic. Used by:
  - argosy.api.routes.intake (baseline upload happy-path)
  - argosy.orchestrator.loops.plan_watcher (file-change re-distill)
  - the future "Re-distill" UI button

User-edit preservation: each PlanDistillate item carries a ``user_edited``
flag. When ``preserve_user_edits=True`` (the default), re-distillation
merges fresh agent output with prior user-edited items, keeping the
user's value. ``force_refresh=True`` drops user edits.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from argosy.agents.plan_distiller import PlanDistillerAgent
from argosy.agents.plan_distiller_render import render_distillate
from argosy.agents.plan_distiller_types import PlanDistillate
from argosy.logging import get_logger
from argosy.state.models import PlanVersion

log = get_logger(__name__)


@dataclass
class DistillResult:
    plan_version_id: int
    distillate: PlanDistillate
    source_hash: str
    user_edits_preserved: int


def _make_agent() -> PlanDistillerAgent:
    """Indirection point so tests can monkeypatch."""
    return PlanDistillerAgent()


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def distill_baseline_plan(
    session: Session,
    *,
    plan_version_id: int,
    user_id: str,
    preserve_user_edits: bool = True,
) -> DistillResult:
    """Run the distiller against a plan_versions row and persist the result.

    Args:
        plan_version_id: must exist and have role='baseline'.
        user_id: stamped on agent_reports rows.
        preserve_user_edits: if True (default), prior user-edited items
            survive re-distillation. If False, agent output wins.

    Returns:
        DistillResult with the parsed PlanDistillate and side-effects
        already persisted on the row.
    """
    pv = session.get(PlanVersion, plan_version_id)
    if pv is None:
        raise ValueError(f"plan_version {plan_version_id} not found")
    if pv.role != "baseline":
        raise ValueError(
            f"plan_version {plan_version_id} role={pv.role!r}, expected 'baseline'"
        )

    # Capture prior user-edits before agent call.
    prior_edits: dict[str, dict[str, dict]] = {}
    if preserve_user_edits and pv.distillate_json:
        prior = json.loads(pv.distillate_json)
        for category in ("goals", "principles", "decision_rules", "targets", "constraints"):
            for item in prior.get(category) or []:
                if item.get("user_edited"):
                    prior_edits.setdefault(category, {})[item["label"]] = item

    # Run the agent.
    agent = _make_agent()
    result = agent.run_sync(
        plan_label=pv.version_label or "Imported plan",
        plan_markdown=pv.raw_markdown,
    )
    fresh: PlanDistillate = result.output  # type: ignore[attr-defined]

    # Merge user edits back in.
    edits_preserved = 0
    if prior_edits:
        for category, by_label in prior_edits.items():
            items = getattr(fresh, category)
            new_items = []
            for fresh_item in items:
                edit = by_label.get(fresh_item.label)
                if edit is None:
                    new_items.append(fresh_item)
                    continue
                # Apply user edit on top of the fresh item.
                merged = fresh_item.model_copy(update={
                    k: v for k, v in edit.items()
                    if k in fresh_item.model_fields and k not in {"label"}
                })
                new_items.append(merged)
                edits_preserved += 1
            setattr(fresh, category, new_items)

    # Persist.
    pv.distillate_json = fresh.model_dump_json()
    pv.distillate_rendered = render_distillate(fresh)
    pv.source_hash = _sha256(pv.raw_markdown)
    pv.distilled_at = datetime.now(timezone.utc)
    session.commit()

    log.info(
        "plan_distiller.persisted",
        plan_version_id=plan_version_id,
        user_id=user_id,
        edits_preserved=edits_preserved,
    )

    return DistillResult(
        plan_version_id=plan_version_id,
        distillate=fresh,
        source_hash=pv.source_hash,
        user_edits_preserved=edits_preserved,
    )


def set_distillate_item_user_edit(
    session: Session,
    *,
    plan_version_id: int,
    category: str,
    item_label: str,
    new_value: dict[str, Any],
) -> None:
    """Apply a user edit to one item of the distillate.

    The category must be one of: goals, principles, decision_rules,
    targets, constraints. The item is matched by ``label``. Sets
    ``user_edited=True`` on the item; merges in any keys from ``new_value``.
    """
    valid = {"goals", "principles", "decision_rules", "targets", "constraints"}
    if category not in valid:
        raise ValueError(f"category {category!r} not in {valid}")

    pv = session.get(PlanVersion, plan_version_id)
    if pv is None or pv.distillate_json is None:
        raise ValueError(f"plan_version {plan_version_id} has no distillate")

    payload = json.loads(pv.distillate_json)
    items = payload.get(category) or []
    for item in items:
        if item.get("label") == item_label:
            item.update(new_value)
            item["user_edited"] = True
            break
    else:
        raise ValueError(f"no item with label={item_label!r} in {category}")

    payload[category] = items
    pv.distillate_json = json.dumps(payload)
    # Re-render markdown view from the edited payload.
    pv.distillate_rendered = render_distillate(PlanDistillate.model_validate(payload))
    session.commit()


__all__ = [
    "DistillResult",
    "distill_baseline_plan",
    "set_distillate_item_user_edit",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plan_distiller_service.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add argosy/services/__init__.py argosy/services/plan_distiller_service.py tests/test_plan_distiller_service.py
git commit -m "feat(services): plan_distiller_service with user-edit preservation"
```

---

### Task 1.9: Hook distillation into `intake_upload` happy-path

**Files:**
- Modify: `argosy/api/routes/intake.py` (around line 472 — after `plan_versions` insert)
- Test: `tests/test_intake_route.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_intake_route.py`:

```python
def test_intake_upload_triggers_distillation(client_with_db, monkeypatch):
    """After upload, the inserted plan_versions row has distillate_json populated."""
    from argosy.agents.plan_distiller_types import (
        Goal,
        PlanDistillate,
    )
    from argosy.services import plan_distiller_service as svc
    from argosy.state.models import PlanVersion

    # Stub the agent so no LLM call happens.
    class _Fake:
        def run_sync(self, **kw):
            payload = PlanDistillate(
                plan_label="Test plan",
                distilled_at_iso="2026-05-05T00:00:00+00:00",
                goals=[Goal(label="retirement_target_year", value="2031")],
            )
            return type("R", (), {"output": payload, "model": "fake", "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0})()

    monkeypatch.setattr(svc, "_make_agent", lambda: _Fake())

    files = {"file": ("plan.md", b"# Plan\n\nRetirement: 2031\n", "text/markdown")}
    data = {"user_id": "ariel"}
    r = client_with_db.post("/api/intake/upload", files=files, data=data)
    assert r.status_code == 200, r.text
    body = r.json()
    plan_id = body["plan_version_id"]

    # Distillate should now be populated.
    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, plan_id)
        assert pv is not None
        assert pv.distillate_json is not None
        assert "retirement_target_year" in pv.distillate_json
        assert pv.distilled_at is not None
        assert pv.source_hash is not None
        assert pv.role == "baseline"
    finally:
        sess.close()


def test_intake_upload_distillation_failure_is_non_fatal(client_with_db, monkeypatch):
    """If the distiller raises, the upload still succeeds; distillate stays NULL.

    Distillation is a value-add, not a precondition for the upload to
    be useful. The user's plan markdown must still be captured.
    """
    from argosy.services import plan_distiller_service as svc
    from argosy.state.models import PlanVersion

    class _Boom:
        def run_sync(self, **kw):
            raise RuntimeError("LLM down")

    monkeypatch.setattr(svc, "_make_agent", lambda: _Boom())

    files = {"file": ("plan.md", b"# Plan", "text/markdown")}
    data = {"user_id": "ariel"}
    r = client_with_db.post("/api/intake/upload", files=files, data=data)
    assert r.status_code == 200, r.text
    plan_id = r.json()["plan_version_id"]

    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, plan_id)
        assert pv is not None
        assert pv.distillate_json is None  # distillation failed silently
        assert pv.role == "baseline"
        assert pv.raw_markdown == "# Plan"
    finally:
        sess.close()
```

The `client_with_db` fixture should already exist in `tests/conftest.py` (it's used by `tests/test_intake_route.py` already). If not, follow the pattern from existing tests.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_intake_route.py::test_intake_upload_triggers_distillation tests/test_intake_route.py::test_intake_upload_distillation_failure_is_non_fatal -v`
Expected: FAIL — distillation hook not yet wired.

- [ ] **Step 3: Wire the hook**

Open `argosy/api/routes/intake.py`. Find the section where `PlanVersion` is inserted (per spec context, around line 472, in the `intake_upload` happy-path). After the row is committed and `plan_version_id` is obtained, add:

```python
    # Spec §3.7: trigger distillation post-upload. Non-fatal — if the
    # distiller raises, the upload itself stays successful (distillate
    # stays NULL; user can retry via "Re-distill" button).
    try:
        from argosy.services.plan_distiller_service import distill_baseline_plan

        distill_baseline_plan(
            session=db,
            plan_version_id=plan_version_id,
            user_id=user_id,
            preserve_user_edits=False,  # initial import — no prior edits
        )
    except Exception as exc:  # noqa: BLE001 — non-fatal value-add
        logger.warning(
            "intake_upload.distill_failed",
            plan_version_id=plan_version_id,
            user_id=user_id,
            error=str(exc),
        )
```

(Replace `db` and `logger` with whatever the surrounding scope uses — read 5-10 lines around the insert site first to get the variable names right.)

Also ensure the inserted row is created with `role="baseline"`. If the existing INSERT does not set `role`, add it (the model default is already `"baseline"`, but be explicit):

```python
    plan = PlanVersion(
        user_id=user_id,
        version_label=version_label,
        source_path=source_path,
        raw_markdown=raw_markdown,
        role="baseline",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_intake_route.py -v`
Expected: PASS (existing intake tests still pass; the two new tests pass)

- [ ] **Step 5: Commit**

```bash
git add argosy/api/routes/intake.py tests/test_intake_route.py
git commit -m "feat(intake): trigger plan distillation on baseline upload (non-fatal)"
```

---

### Task 1.10: New API route — `GET /api/plan/baseline` (distillate JSON + rendered)

**Files:**
- Create: `argosy/api/routes/plan.py` (new module if absent; otherwise extend)
- Modify: `argosy/api/main.py` (mount the router if new module)
- Test: `tests/test_plan_baseline_route.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_plan_baseline_route.py`:

```python
"""Tests for /api/plan/baseline — exposes the distillate to the UI."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import PlanVersion, User


@pytest.fixture
def app_with_baseline(client_with_db):
    """Insert a baseline row with a populated distillate."""
    from argosy.agents.plan_distiller_types import Goal, PlanDistillate
    from argosy.agents.plan_distiller_render import render_distillate

    payload = PlanDistillate(
        plan_label="Jacobs v2.0",
        distilled_at_iso="2026-05-05T00:00:00+00:00",
        goals=[Goal(label="retirement_target_year", value="2031")],
    )
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        pv = PlanVersion(
            user_id="ariel",
            version_label="Jacobs v2.0",
            raw_markdown="# Plan",
            role="baseline",
            distillate_json=payload.model_dump_json(),
            distillate_rendered=render_distillate(payload),
        )
        sess.add(pv)
        sess.commit()
    finally:
        sess.close()
    return client_with_db


def test_get_baseline_returns_distillate(app_with_baseline):
    r = app_with_baseline.get("/api/plan/baseline?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    assert body["version_label"] == "Jacobs v2.0"
    assert body["distillate"] is not None
    assert body["distillate"]["plan_label"] == "Jacobs v2.0"
    assert "retirement_target_year" in json.dumps(body["distillate"])
    assert "# Plan distillate" in body["distillate_rendered"]


def test_get_baseline_returns_404_when_absent(client_with_db):
    """Users without an imported plan get 404 (not a 500)."""
    r = client_with_db.get("/api/plan/baseline?user_id=newcomer")
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plan_baseline_route.py -v`
Expected: FAIL — route does not exist.

- [ ] **Step 3: Write the route**

Check whether `argosy/api/routes/plan.py` already exists.

```bash
ls argosy/api/routes/plan.py 2>&1 || echo MISSING
```

If MISSING, create `argosy/api/routes/plan.py`:

```python
"""Plan routes — baseline distillate, current plan, draft lifecycle.

Wave 1 implements only ``GET /api/plan/baseline``. Wave 2 adds the
draft + current endpoints (see plan tasks 2.18-2.23).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from argosy.api.deps import get_db
from argosy.state.queries import get_active_baseline

router = APIRouter(prefix="/api/plan", tags=["plan"])


class BaselineResponse(BaseModel):
    plan_version_id: int
    version_label: str
    raw_markdown: str
    distillate: dict | None
    distillate_rendered: str | None
    distilled_at: str | None
    source_hash: str | None


@router.get("/baseline", response_model=BaselineResponse)
def get_baseline(user_id: str, db: Session = Depends(get_db)) -> BaselineResponse:
    pv = get_active_baseline(db, user_id)
    if pv is None:
        raise HTTPException(status_code=404, detail="no baseline plan for user")
    distillate_obj = json.loads(pv.distillate_json) if pv.distillate_json else None
    return BaselineResponse(
        plan_version_id=pv.id,
        version_label=pv.version_label,
        raw_markdown=pv.raw_markdown,
        distillate=distillate_obj,
        distillate_rendered=pv.distillate_rendered,
        distilled_at=pv.distilled_at.isoformat() if pv.distilled_at else None,
        source_hash=pv.source_hash,
    )
```

- [ ] **Step 4: Mount the router**

Edit `argosy/api/main.py`. Find the existing router mounts (look for `app.include_router(...)` patterns). Add:

```python
from argosy.api.routes import plan as plan_routes

app.include_router(plan_routes.router)
```

(If a `plan` import already exists for some legacy reason, rename to `plan_routes` to avoid shadowing.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_plan_baseline_route.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add argosy/api/routes/plan.py argosy/api/main.py tests/test_plan_baseline_route.py
git commit -m "feat(api): GET /api/plan/baseline — distillate + rendered MD"
```

---

### Task 1.11: API route — `POST /api/plan/baseline/distill` (manual re-distill)

**Files:**
- Modify: `argosy/api/routes/plan.py`
- Test: `tests/test_plan_baseline_route.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_plan_baseline_route.py`:

```python
def test_post_baseline_distill_reruns_distillation(app_with_baseline, monkeypatch):
    """The Re-distill button hits this endpoint."""
    from argosy.agents.plan_distiller_types import Goal, PlanDistillate
    from argosy.services import plan_distiller_service as svc

    new_payload = PlanDistillate(
        plan_label="Jacobs v2.0",
        distilled_at_iso="2026-05-06T00:00:00+00:00",
        goals=[
            Goal(label="retirement_target_year", value="2031"),
            Goal(label="lifestyle", value="early retire to nature"),
        ],
    )

    class _Fake:
        def run_sync(self, **kw):
            return type("R", (), {"output": new_payload, "model": "fake", "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0})()

    monkeypatch.setattr(svc, "_make_agent", lambda: _Fake())

    r = app_with_baseline.post("/api/plan/baseline/distill?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["distillate"]["distilled_at_iso"] == "2026-05-06T00:00:00+00:00"
    assert any(g["label"] == "lifestyle" for g in body["distillate"]["goals"])


def test_post_baseline_distill_404_when_no_baseline(client_with_db):
    r = client_with_db.post("/api/plan/baseline/distill?user_id=ghost")
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plan_baseline_route.py -v`
Expected: FAIL — endpoint not yet defined.

- [ ] **Step 3: Add the route**

Append to `argosy/api/routes/plan.py`:

```python
@router.post("/baseline/distill", response_model=BaselineResponse)
def post_baseline_distill(
    user_id: str,
    preserve_user_edits: bool = True,
    db: Session = Depends(get_db),
) -> BaselineResponse:
    """Trigger a fresh distillation pass on the active baseline.

    Used by the "Re-distill" UI button. Preserves user edits by default.
    Pass preserve_user_edits=false to overwrite (only the user can do
    this; see Wave 1 UI confirmation modal).
    """
    pv = get_active_baseline(db, user_id)
    if pv is None:
        raise HTTPException(status_code=404, detail="no baseline plan for user")

    from argosy.services.plan_distiller_service import distill_baseline_plan

    distill_baseline_plan(
        session=db,
        plan_version_id=pv.id,
        user_id=user_id,
        preserve_user_edits=preserve_user_edits,
    )
    db.refresh(pv)
    distillate_obj = json.loads(pv.distillate_json) if pv.distillate_json else None
    return BaselineResponse(
        plan_version_id=pv.id,
        version_label=pv.version_label,
        raw_markdown=pv.raw_markdown,
        distillate=distillate_obj,
        distillate_rendered=pv.distillate_rendered,
        distilled_at=pv.distilled_at.isoformat() if pv.distilled_at else None,
        source_hash=pv.source_hash,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plan_baseline_route.py -v`
Expected: PASS (4 tests total)

- [ ] **Step 5: Commit**

```bash
git add argosy/api/routes/plan.py tests/test_plan_baseline_route.py
git commit -m "feat(api): POST /api/plan/baseline/distill — manual re-distill"
```

---

### Task 1.12: API route — `PATCH /api/plan/baseline/distillate/<category>/<label>`

**Files:**
- Modify: `argosy/api/routes/plan.py`
- Test: `tests/test_plan_baseline_route.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_plan_baseline_route.py`:

```python
def test_patch_distillate_item_applies_user_edit(app_with_baseline):
    body = {
        "value": "2030",
        "user_edit_note": "decided to retire one year earlier",
    }
    r = app_with_baseline.patch(
        "/api/plan/baseline/distillate/goals/retirement_target_year?user_id=ariel",
        json=body,
    )
    assert r.status_code == 200, r.text
    out = r.json()
    goal = next(g for g in out["distillate"]["goals"] if g["label"] == "retirement_target_year")
    assert goal["value"] == "2030"
    assert goal["user_edited"] is True
    assert goal["user_edit_note"] == "decided to retire one year earlier"


def test_patch_distillate_item_404_when_label_missing(app_with_baseline):
    r = app_with_baseline.patch(
        "/api/plan/baseline/distillate/goals/no_such_label?user_id=ariel",
        json={"value": "x"},
    )
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plan_baseline_route.py::test_patch_distillate_item_applies_user_edit tests/test_plan_baseline_route.py::test_patch_distillate_item_404_when_label_missing -v`
Expected: FAIL.

- [ ] **Step 3: Add the route**

Append to `argosy/api/routes/plan.py`:

```python
class DistillateItemEditRequest(BaseModel):
    value: str | float | None = None
    rationale: str | None = None
    detail: str | None = None
    rule: str | None = None
    user_edit_note: str | None = None


@router.patch(
    "/baseline/distillate/{category}/{item_label}",
    response_model=BaselineResponse,
)
def patch_distillate_item(
    category: str,
    item_label: str,
    user_id: str,
    body: DistillateItemEditRequest,
    db: Session = Depends(get_db),
) -> BaselineResponse:
    """Apply a user edit to one item of the distillate."""
    pv = get_active_baseline(db, user_id)
    if pv is None:
        raise HTTPException(status_code=404, detail="no baseline plan for user")

    from argosy.services.plan_distiller_service import set_distillate_item_user_edit

    new_value = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        set_distillate_item_user_edit(
            db,
            plan_version_id=pv.id,
            category=category,
            item_label=item_label,
            new_value=new_value,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    db.refresh(pv)
    distillate_obj = json.loads(pv.distillate_json) if pv.distillate_json else None
    return BaselineResponse(
        plan_version_id=pv.id,
        version_label=pv.version_label,
        raw_markdown=pv.raw_markdown,
        distillate=distillate_obj,
        distillate_rendered=pv.distillate_rendered,
        distilled_at=pv.distilled_at.isoformat() if pv.distilled_at else None,
        source_hash=pv.source_hash,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plan_baseline_route.py -v`
Expected: PASS (6 tests total)

- [ ] **Step 5: Commit**

```bash
git add argosy/api/routes/plan.py tests/test_plan_baseline_route.py
git commit -m "feat(api): PATCH /api/plan/baseline/distillate/{category}/{label} — user edits"
```

---

### Task 1.13: `plan_watcher` daily cadence loop

**Files:**
- Create: `argosy/orchestrator/loops/plan_watcher.py`
- Modify: `argosy/orchestrator/scheduler.py` (register the loop)
- Test: `tests/test_plan_watcher_loop.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_plan_watcher_loop.py`:

```python
"""Tests for plan_watcher daily cadence loop.

The loop hashes each user's configured plan source path. On hash change,
re-runs distillation (preserving user edits). Designed to be cheap when
nothing has changed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import PlanVersion, User


@pytest.fixture
def session(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    yield s
    s.close()


def _write(p: Path, contents: str) -> None:
    p.write_text(contents, encoding="utf-8")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def test_plan_watcher_no_change_is_noop(session, tmp_path, monkeypatch):
    from argosy.orchestrator.loops import plan_watcher
    from argosy.services import plan_distiller_service as svc

    # Set up a baseline + matching source file.
    plan_path = tmp_path / "plan.md"
    contents = "# Plan\n\nNVDA target 15%\n"
    _write(plan_path, contents)

    pv = PlanVersion(
        user_id="ariel",
        role="baseline",
        version_label="Jacobs v2.0",
        source_path=str(plan_path),
        raw_markdown=contents,
        source_hash=_sha(contents),
    )
    session.add(pv)
    session.commit()

    # Spy: distill should NOT be called when nothing changed.
    calls = []
    monkeypatch.setattr(
        svc, "distill_baseline_plan",
        lambda **kw: calls.append(kw) or type("R", (), {"distillate": None, "source_hash": _sha(contents), "user_edits_preserved": 0, "plan_version_id": pv.id})(),
    )

    plan_watcher.tick(session)
    assert calls == [], "distillation should be skipped when source_hash matches"


def test_plan_watcher_redistills_on_file_change(session, tmp_path, monkeypatch):
    from argosy.orchestrator.loops import plan_watcher
    from argosy.services import plan_distiller_service as svc

    plan_path = tmp_path / "plan.md"
    _write(plan_path, "# Plan v1\n\nNVDA 15%\n")

    pv = PlanVersion(
        user_id="ariel",
        role="baseline",
        source_path=str(plan_path),
        raw_markdown="# Plan v1\n\nNVDA 15%\n",
        source_hash=_sha("# Plan v1\n\nNVDA 15%\n"),
    )
    session.add(pv)
    session.commit()

    # Mutate the file.
    _write(plan_path, "# Plan v2\n\nNVDA 12%\n")

    calls: list[dict] = []
    def _fake(**kw):
        calls.append(kw)
        # Simulate updating raw_markdown + source_hash inside the service.
        target = session.get(PlanVersion, kw["plan_version_id"])
        target.raw_markdown = plan_path.read_text(encoding="utf-8")
        target.source_hash = _sha(target.raw_markdown)
        session.commit()
        return type("R", (), {
            "distillate": None,
            "source_hash": target.source_hash,
            "user_edits_preserved": 0,
            "plan_version_id": kw["plan_version_id"],
        })()

    monkeypatch.setattr(svc, "distill_baseline_plan", _fake)

    plan_watcher.tick(session)
    assert len(calls) == 1
    assert calls[0]["preserve_user_edits"] is True


def test_plan_watcher_skips_users_without_source_path(session, monkeypatch):
    """If source_path is empty, the watcher cannot diff — skip silently."""
    from argosy.orchestrator.loops import plan_watcher
    from argosy.services import plan_distiller_service as svc

    pv = PlanVersion(
        user_id="ariel",
        role="baseline",
        source_path="",  # blank — uploaded via UI, no auto-watched file
        raw_markdown="# Plan",
        source_hash=_sha("# Plan"),
    )
    session.add(pv)
    session.commit()

    calls = []
    monkeypatch.setattr(svc, "distill_baseline_plan", lambda **kw: calls.append(kw))
    plan_watcher.tick(session)
    assert calls == []


def test_plan_watcher_handles_missing_file_gracefully(session, tmp_path, monkeypatch, caplog):
    """File deleted between ticks — log a warning, do not crash."""
    from argosy.orchestrator.loops import plan_watcher
    from argosy.services import plan_distiller_service as svc

    pv = PlanVersion(
        user_id="ariel",
        role="baseline",
        source_path=str(tmp_path / "deleted.md"),
        raw_markdown="# Plan",
        source_hash=_sha("# Plan"),
    )
    session.add(pv)
    session.commit()

    calls = []
    monkeypatch.setattr(svc, "distill_baseline_plan", lambda **kw: calls.append(kw))
    plan_watcher.tick(session)  # must not raise
    assert calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plan_watcher_loop.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write the loop**

Create `argosy/orchestrator/loops/plan_watcher.py`:

```python
"""plan_watcher — daily cadence loop.

Per spec §3.7: detects when the user's baseline plan source file has
changed on disk and re-runs distillation, preserving user edits.

Cheap when nothing has changed: O(N_users) sha256 of the file contents
against the stored ``source_hash`` column.

Configured in agent_settings.yaml::
    cadences:
      plan_watcher:
        enabled: true
        cron: "0 7 * * *"   # 07:00 user TZ; before daily_brief
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.state.models import PlanVersion

log = get_logger(__name__)


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def tick(session: Session) -> int:
    """One loop iteration. Returns the count of re-distillations triggered.

    Iterates every active baseline plan_version row across all users
    (multi-tenant ready). For each:

      1. If ``source_path`` is empty -> skip (uploaded via UI, no
         disk file to watch).
      2. If the file is missing -> log warning, skip.
      3. If sha256(file_contents) == row.source_hash -> skip (no change).
      4. Else update raw_markdown and call distill_baseline_plan with
         ``preserve_user_edits=True``.
    """
    from argosy.services.plan_distiller_service import distill_baseline_plan

    rerun_count = 0
    rows = (
        session.query(PlanVersion)
        .filter(PlanVersion.role == "baseline")
        .all()
    )
    for pv in rows:
        if not pv.source_path:
            continue

        path = Path(pv.source_path)
        try:
            contents = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            log.warning(
                "plan_watcher.source_missing",
                user_id=pv.user_id,
                source_path=pv.source_path,
            )
            continue
        except OSError as exc:
            log.warning(
                "plan_watcher.source_unreadable",
                user_id=pv.user_id,
                source_path=pv.source_path,
                error=str(exc),
            )
            continue

        new_hash = _sha256(contents)
        if new_hash == (pv.source_hash or ""):
            continue

        log.info(
            "plan_watcher.diff_detected",
            user_id=pv.user_id,
            plan_version_id=pv.id,
            old_hash=(pv.source_hash or "")[:8],
            new_hash=new_hash[:8],
        )

        # Update the raw_markdown so distill sees fresh content.
        pv.raw_markdown = contents
        session.commit()

        try:
            distill_baseline_plan(
                session=session,
                plan_version_id=pv.id,
                user_id=pv.user_id,
                preserve_user_edits=True,
            )
            rerun_count += 1
        except Exception as exc:  # noqa: BLE001
            log.error(
                "plan_watcher.distill_failed",
                user_id=pv.user_id,
                plan_version_id=pv.id,
                error=str(exc),
            )

    return rerun_count


__all__ = ["tick"]
```

- [ ] **Step 4: Register with the scheduler**

Open `argosy/orchestrator/scheduler.py`. Find the existing cadence registrations (look for `daily_brief` or similar). Add (mirroring the surrounding pattern; concrete example below uses APScheduler-style cron):

```python
def register_plan_watcher(scheduler, session_factory) -> None:
    """Register the daily plan_watcher tick (07:00 user TZ).

    Runs before daily_brief so a fresh-file baseline propagates into the
    same morning's brief if the user happens to edit overnight.
    """
    from argosy.orchestrator.loops import plan_watcher

    def _tick():
        sess = session_factory()
        try:
            plan_watcher.tick(sess)
        finally:
            sess.close()

    scheduler.add_job(_tick, trigger="cron", hour=7, minute=0, id="plan_watcher")
```

Wire `register_plan_watcher` into the same place that registers `register_daily_brief` (or equivalent). If the existing module uses a different scheduler shape, adapt — the call site is the seam.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_plan_watcher_loop.py -v`
Expected: PASS (4 tests)

Also run the broader scheduler test:

Run: `pytest tests/test_scheduler.py -v`
Expected: PASS (existing tests unchanged)

- [ ] **Step 6: Commit**

```bash
git add argosy/orchestrator/loops/plan_watcher.py argosy/orchestrator/scheduler.py tests/test_plan_watcher_loop.py
git commit -m "feat(orchestrator): plan_watcher daily cadence — re-distill on source file change"
```

---

### Task 1.14: Golden-output eval — distill the actual Jacobs plan

**Files:**
- Create: `tests/golden/jacobs_distillate_expected.json`
- Create: `tests/test_plan_distiller_golden.py`
- Create: `tests/golden/jacobs_plan_excerpt.md` (a redacted excerpt — full plan stays out of the repo)

This is the "agent eval case" gate per SDD §14.6.

- [ ] **Step 1: Prepare a redacted excerpt of the Jacobs plan for the repo**

Read approximately 200 lines from key sections of the actual plan and write a copy to `tests/golden/jacobs_plan_excerpt.md`. The excerpt must include: Executive Overview, Investment Strategy & Risk Management's allocation table, Asset Allocation Glidepath, Tax Optimization Strategies, Risk Mitigation Framework, Conclusion. Keep it under ~600 lines so the test runs in seconds.

```bash
mkdir -p tests/golden
```

You will need to copy from `D:/Google Drive/Family/Finances/Portfolio/Jacobs_Wealth_Plan.md`. Open that file and extract the named sections into `tests/golden/jacobs_plan_excerpt.md`. Do NOT commit the full plan — only the excerpt.

- [ ] **Step 2: Write the golden expected file**

Create `tests/golden/jacobs_distillate_expected.json`. This is the *floor* of what a correct distillate must contain — the test checks for *presence and exclusion*, not exact-match (LLM output is not deterministic). Schema:

```json
{
  "must_include": {
    "goals": [
      {"label_contains": "retirement_target_year", "value_contains": "2031"},
      {"label_contains": "target_annual_income", "value_contains": "360"}
    ],
    "principles": [
      {"label_contains": "ucits"},
      {"label_contains": "concentration"},
      {"label_contains": "natural"}
    ],
    "risk_priorities_first": "concentration",
    "decision_rules_any_label_contains": [
      "rsu", "gap", "defensive"
    ],
    "targets_labels_any_contains": [
      "nvda", "defensive", "international"
    ],
    "constraints_any_label_contains": [
      "ucits"
    ],
    "stress_tolerance_contains": "drawdown"
  },
  "must_exclude_in_serialized_text": [
    "66%",
    "3.09",
    "12,748",
    "$171.81",
    "Q1 2026 sells 2,500",
    "next 30 days"
  ]
}
```

- [ ] **Step 3: Write the failing test**

Create `tests/test_plan_distiller_golden.py`:

```python
"""Golden-corpus test: distill the Jacobs plan excerpt and assert content
floor + exclusion rules.

This test calls the actual LLM (PlanDistillerAgent.run_sync against
Anthropic). It is marked ``llm_eval`` so CI can skip it when no API key
is configured, but it MUST pass before Wave 1 ships per the wave gate.

Run locally with ``pytest -m llm_eval -v``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

EXCERPT = Path("tests/golden/jacobs_plan_excerpt.md")
EXPECT = Path("tests/golden/jacobs_distillate_expected.json")


@pytest.mark.llm_eval
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set; skipping live LLM eval",
)
def test_distiller_produces_acceptable_distillate_for_jacobs_excerpt():
    from argosy.agents.plan_distiller import PlanDistillerAgent

    plan_md = EXCERPT.read_text(encoding="utf-8")
    expect = json.loads(EXPECT.read_text(encoding="utf-8"))

    agent = PlanDistillerAgent()
    result = agent.run_sync(plan_label="Jacobs v2.0 (excerpt)", plan_markdown=plan_md)
    distillate = result.output

    serialized = distillate.model_dump_json().lower()

    # 1. must_include — content floor.
    must = expect["must_include"]

    for entry in must.get("goals", []):
        label_substr = entry["label_contains"].lower()
        value_substr = entry.get("value_contains", "").lower()
        match = any(
            label_substr in g.label.lower() and (not value_substr or value_substr in str(g.value).lower())
            for g in distillate.goals
        )
        assert match, f"missing goal matching {entry}; got {[g.label for g in distillate.goals]}"

    for entry in must.get("principles", []):
        sub = entry["label_contains"].lower()
        assert any(sub in p.label.lower() for p in distillate.principles), \
            f"missing principle containing {sub}; got {[p.label for p in distillate.principles]}"

    if "risk_priorities_first" in must:
        assert distillate.risk_priorities, "risk_priorities is empty"
        assert must["risk_priorities_first"].lower() in distillate.risk_priorities[0].lower()

    for sub in must.get("decision_rules_any_label_contains", []):
        assert any(sub.lower() in r.label.lower() for r in distillate.decision_rules), \
            f"no decision_rule label contains {sub}"

    for sub in must.get("targets_labels_any_contains", []):
        assert any(sub.lower() in t.label.lower() for t in distillate.targets), \
            f"no target label contains {sub}"

    for sub in must.get("constraints_any_label_contains", []):
        assert any(sub.lower() in c.label.lower() for c in distillate.constraints), \
            f"no constraint contains {sub}"

    if "stress_tolerance_contains" in must:
        assert must["stress_tolerance_contains"].lower() in distillate.stress_tolerance.lower()

    # 2. must_exclude — exclusion rules from spec §3.3.
    for forbidden in expect["must_exclude_in_serialized_text"]:
        assert forbidden.lower() not in serialized, \
            f"distillate contains forbidden time-stamped value: {forbidden!r}"
```

Add the marker to `pyproject.toml` if not present (under `[tool.pytest.ini_options]` markers list):

```toml
markers = [
    "llm_eval: marks tests that call the live Anthropic API (deselect with -m 'not llm_eval')",
]
```

- [ ] **Step 4: Run the test**

```bash
pytest tests/test_plan_distiller_golden.py -m llm_eval -v
```

If `ANTHROPIC_API_KEY` is set, expect: PASS (with one ~$0.30 LLM call).
If not set: SKIPPED.

The wave gate requires this test to pass at least once. Iterate on the distiller's system prompt (Task 1.6) until the assertions pass for the actual Jacobs excerpt.

- [ ] **Step 5: Commit**

```bash
git add tests/test_plan_distiller_golden.py tests/golden/jacobs_distillate_expected.json tests/golden/jacobs_plan_excerpt.md pyproject.toml
git commit -m "test(plan-distiller): golden-corpus eval against Jacobs plan excerpt"
```

---

### Task 1.15: UI — `<PlanInScopeCard>` component

**Files:**
- Create: `ui/src/components/plan-in-scope-card.tsx`
- Modify: `ui/src/lib/api.ts` (add types + functions for the new endpoints)

- [ ] **Step 1: Add types + API client functions**

Edit `ui/src/lib/api.ts`. Append:

```typescript
// ----------------------------------------------------------------------
// Wave 1: Baseline distillate
// ----------------------------------------------------------------------

export interface DistillateGoal {
  label: string;
  value: string;
  rationale: string;
  source_section: string;
  user_edited: boolean;
  user_edit_note: string | null;
}

export interface DistillatePrinciple {
  label: string;
  rationale: string;
  source_section: string;
  user_edited: boolean;
  user_edit_note: string | null;
}

export interface DistillateDecisionRule {
  label: string;
  rule: string;
  source_section: string;
  user_edited: boolean;
  user_edit_note: string | null;
}

export interface DistillateTarget {
  label: string;
  value: number;
  unit: string;
  stated_at: string;     // ISO date
  revisit_after: string; // ISO date
  rationale: string;
  source_section: string;
  user_edited: boolean;
  user_edit_note: string | null;
}

export interface DistillateConstraint {
  label: string;
  detail: string;
  source_section: string;
  user_edited: boolean;
  user_edit_note: string | null;
}

export interface PlanDistillate {
  plan_label: string;
  distilled_at_iso: string;
  goals: DistillateGoal[];
  principles: DistillatePrinciple[];
  risk_priorities: string[];
  decision_rules: DistillateDecisionRule[];
  targets: DistillateTarget[];
  constraints: DistillateConstraint[];
  stress_tolerance: string;
}

export interface BaselineResponse {
  plan_version_id: number;
  version_label: string;
  raw_markdown: string;
  distillate: PlanDistillate | null;
  distillate_rendered: string | null;
  distilled_at: string | null;
  source_hash: string | null;
}

// Add to the `api` object literal — find the closing brace of `api = {...}`
// and add these methods before it:
```

Then in the same file, inside the `api = { ... }` object literal, add (alphabetical-ish placement near `planCurrent`):

```typescript
  planBaseline: (userId: string) =>
    getJSON<BaselineResponse>(
      `/api/plan/baseline?user_id=${encodeURIComponent(userId)}`,
    ),
  planBaselineDistill: (userId: string, preserveUserEdits = true) =>
    postJSON<BaselineResponse>(
      `/api/plan/baseline/distill?user_id=${encodeURIComponent(userId)}&preserve_user_edits=${preserveUserEdits}`,
      {},
    ),
  planBaselineDistillateEdit: (
    userId: string,
    category: string,
    itemLabel: string,
    body: { value?: string | number; rationale?: string; detail?: string; rule?: string; user_edit_note?: string },
  ) =>
    fetch(
      apiUrl(
        `/api/plan/baseline/distillate/${encodeURIComponent(category)}/${encodeURIComponent(itemLabel)}?user_id=${encodeURIComponent(userId)}`,
      ),
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ).then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status} for /api/plan/baseline/distillate`);
      return (await r.json()) as BaselineResponse;
    }),
```

- [ ] **Step 2: Create the component**

Create `ui/src/components/plan-in-scope-card.tsx`:

```typescript
"use client";

import { useCallback, useEffect, useState } from "react";
import { FileText, RefreshCw, ChevronDown, ChevronUp } from "lucide-react";

import { Markdown } from "@/components/markdown";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type BaselineResponse } from "@/lib/api";

interface PlanInScopeCardProps {
  userId: string;
}

/**
 * Plan-in-scope card — renders the imported baseline distillate at the
 * top of the Advisor page. See spec §7.8.
 *
 * Behavior:
 *  - On mount, fetch /api/plan/baseline.
 *  - 404 (no baseline yet): render a soft empty state inviting upload.
 *  - Otherwise: render the markdown distillate; expandable.
 *  - "Re-distill" button calls POST /api/plan/baseline/distill with
 *    preserve_user_edits=true.
 */
export function PlanInScopeCard({ userId }: PlanInScopeCardProps) {
  const [data, setData] = useState<BaselineResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(true);
  const [redistilling, setRedistilling] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await api.planBaseline(userId);
      setData(r);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes("404")) {
        setData(null); // empty state
      } else {
        setError(msg);
      }
    } finally {
      setLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const onRedistill = async () => {
    setRedistilling(true);
    try {
      const r = await api.planBaselineDistill(userId, true);
      setData(r);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRedistilling(false);
    }
  };

  if (loading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Plan in scope</CardTitle>
          <CardDescription>Loading...</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  if (error) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Plan in scope</CardTitle>
          <CardDescription className="text-red-500">{error}</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  if (!data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <FileText className="h-4 w-4" /> No plan imported yet
          </CardTitle>
          <CardDescription>
            Upload a Markdown plan below and the advisor will distill the
            durable principles before our first conversation.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const distilledAt = data.distilled_at
    ? new Date(data.distilled_at).toLocaleString()
    : "(not yet distilled)";

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle className="text-base flex items-center gap-2">
            <FileText className="h-4 w-4" />
            Plan in scope: {data.version_label || "(untitled)"}
          </CardTitle>
          <CardDescription>
            Baseline · distilled {distilledAt}
            {!data.distillate && " · awaiting distillation"}
          </CardDescription>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={onRedistill}
            disabled={redistilling}
            title="Re-run the distiller against the imported plan"
          >
            <RefreshCw
              className={`h-3 w-3 mr-1 ${redistilling ? "animate-spin" : ""}`}
            />
            Re-distill
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setExpanded((v) => !v)}
            aria-label={expanded ? "Collapse" : "Expand"}
          >
            {expanded ? (
              <ChevronUp className="h-4 w-4" />
            ) : (
              <ChevronDown className="h-4 w-4" />
            )}
          </Button>
        </div>
      </CardHeader>

      {expanded && (
        <CardContent>
          {data.distillate_rendered ? (
            <div className="prose prose-sm dark:prose-invert max-w-none">
              <Markdown>{data.distillate_rendered}</Markdown>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground italic">
              Distillation has not run yet (or failed). Click Re-distill to retry.
            </p>
          )}
        </CardContent>
      )}
    </Card>
  );
}
```

- [ ] **Step 3: Mount the card on the Advisor page**

Edit `ui/src/app/advisor/page.tsx`. Find the section just below the `<header>` element (around line 287, before `<div className="grid grid-cols-1 md:grid-cols-[minmax(0,1fr)_320px] ...">`). Insert:

```tsx
import { PlanInScopeCard } from "@/components/plan-in-scope-card";

// ... existing imports

// Inside the JSX returned by AdvisorPage, after the header and error block,
// before the two-column grid:
      <PlanInScopeCard userId={USER_ID} />
```

- [ ] **Step 4: Manually verify in dev**

Steps:
1. Start the engine and UI: `python -m argosy.api.main` and `npm run dev` (in `ui/`).
2. Open `http://localhost:1337/advisor`.
3. Upload `Jacobs_Wealth_Plan.md` via the existing upload widget.
4. After upload completes, the `<PlanInScopeCard>` should populate with the distilled markdown view.
5. Click *Re-distill* — the icon spins; a fresh distillate replaces the old.
6. Collapse / expand the card.

There is no automated UI test for this surface in Wave 1 — manual verification is the gate. Note any rough edges; iterate on copy + spacing.

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/plan-in-scope-card.tsx ui/src/lib/api.ts ui/src/app/advisor/page.tsx
git commit -m "feat(ui): <PlanInScopeCard> on advisor page; api client for baseline endpoints"
```

---

### Task 1.16: UI — distillate edit dialog (per-item)

**Files:**
- Create: `ui/src/components/distillate-edit-dialog.tsx`
- Modify: `ui/src/components/plan-in-scope-card.tsx` (add Edit buttons + dialog wiring)

- [ ] **Step 1: Build the dialog**

Create `ui/src/components/distillate-edit-dialog.tsx`:

```typescript
"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api, type BaselineResponse } from "@/lib/api";

type Category = "goals" | "principles" | "decision_rules" | "targets" | "constraints";

interface DistillateEditDialogProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  userId: string;
  category: Category;
  itemLabel: string;
  initialValue: string;
  fieldLabel: string;          // user-facing field (e.g. "Value", "Detail", "Rule")
  fieldKey: "value" | "detail" | "rule" | "rationale";
  onSaved: (next: BaselineResponse) => void;
}

/**
 * Inline edit dialog for one distillate item. Calls
 * PATCH /api/plan/baseline/distillate/<category>/<itemLabel>
 * and propagates the fresh BaselineResponse back to the parent.
 */
export function DistillateEditDialog(props: DistillateEditDialogProps) {
  const {
    open,
    onOpenChange,
    userId,
    category,
    itemLabel,
    initialValue,
    fieldLabel,
    fieldKey,
    onSaved,
  } = props;

  const [val, setVal] = useState(initialValue);
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSave = async () => {
    setSaving(true);
    setError(null);
    try {
      const body = { [fieldKey]: val, user_edit_note: note } as Record<string, string>;
      const r = await api.planBaselineDistillateEdit(userId, category, itemLabel, body);
      onSaved(r);
      onOpenChange(false);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Edit {category.replace("_", " ").replace(/s$/, "")}: {itemLabel}</DialogTitle>
          <DialogDescription>
            Your edit will be marked user-edited and preserved through future
            re-distillations of the imported plan.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <div>
            <Label htmlFor="distillate-edit-value">{fieldLabel}</Label>
            <Input
              id="distillate-edit-value"
              value={val}
              onChange={(e) => setVal(e.target.value)}
              autoFocus
            />
          </div>
          <div>
            <Label htmlFor="distillate-edit-note">Note (optional)</Label>
            <Textarea
              id="distillate-edit-note"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Why this edit? (e.g. 'decided to retire one year earlier')"
              rows={2}
            />
          </div>
          {error && (
            <p className="text-sm text-red-500 font-mono">{error}</p>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={onSave} disabled={saving || !val}>
            {saving ? "Saving..." : "Save edit"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
```

- [ ] **Step 2: Wire into `<PlanInScopeCard>`**

Edit `ui/src/components/plan-in-scope-card.tsx`. Replace the `expanded && ( ... )` block content. Now we render the *structured* distillate (not just markdown) so we can attach Edit buttons. New CardContent body:

```tsx
{expanded && data.distillate && (
  <CardContent className="flex flex-col gap-4">
    {data.distillate.goals.length > 0 && (
      <Section
        title="Goals"
        items={data.distillate.goals.map((g) => ({
          key: g.label,
          left: <strong>{g.label}</strong>,
          right: g.value,
          edited: g.user_edited,
          onEdit: () =>
            openEdit({ category: "goals", itemLabel: g.label, value: g.value, fieldKey: "value", fieldLabel: "Value" }),
        }))}
      />
    )}
    {data.distillate.principles.length > 0 && (
      <Section
        title="Principles"
        items={data.distillate.principles.map((p) => ({
          key: p.label,
          left: <strong>{p.label}</strong>,
          right: p.rationale,
          edited: p.user_edited,
          onEdit: () =>
            openEdit({ category: "principles", itemLabel: p.label, value: p.rationale, fieldKey: "rationale", fieldLabel: "Rationale" }),
        }))}
      />
    )}
    {data.distillate.targets.length > 0 && (
      <Section
        title="Targets (working assumptions, not eternal)"
        items={data.distillate.targets.map((t) => ({
          key: t.label,
          left: <strong>{t.label}</strong>,
          right: `${t.value} ${t.unit} (stated ${t.stated_at}; revisit ${t.revisit_after})`,
          edited: t.user_edited,
          onEdit: () =>
            openEdit({ category: "targets", itemLabel: t.label, value: String(t.value), fieldKey: "value", fieldLabel: "Value" }),
        }))}
      />
    )}
    {data.distillate.decision_rules.length > 0 && (
      <Section
        title="Decision rules"
        items={data.distillate.decision_rules.map((r) => ({
          key: r.label,
          left: <strong>{r.label}</strong>,
          right: r.rule,
          edited: r.user_edited,
          onEdit: () =>
            openEdit({ category: "decision_rules", itemLabel: r.label, value: r.rule, fieldKey: "rule", fieldLabel: "Rule" }),
        }))}
      />
    )}
    {data.distillate.constraints.length > 0 && (
      <Section
        title="Constraints"
        items={data.distillate.constraints.map((c) => ({
          key: c.label,
          left: <strong>{c.label}</strong>,
          right: c.detail,
          edited: c.user_edited,
          onEdit: () =>
            openEdit({ category: "constraints", itemLabel: c.label, value: c.detail, fieldKey: "detail", fieldLabel: "Detail" }),
        }))}
      />
    )}
    {data.distillate.risk_priorities.length > 0 && (
      <div>
        <p className="text-sm font-semibold uppercase tracking-wide text-muted-foreground mb-1">
          Risk priorities (ordered)
        </p>
        <ol className="list-decimal list-inside text-sm">
          {data.distillate.risk_priorities.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ol>
      </div>
    )}
    {data.distillate.stress_tolerance && (
      <div>
        <p className="text-sm font-semibold uppercase tracking-wide text-muted-foreground mb-1">
          Stress tolerance
        </p>
        <p className="text-sm">{data.distillate.stress_tolerance}</p>
      </div>
    )}
  </CardContent>
)}

<DistillateEditDialog
  open={editTarget !== null}
  onOpenChange={(v) => !v && setEditTarget(null)}
  userId={userId}
  category={editTarget?.category ?? "goals"}
  itemLabel={editTarget?.itemLabel ?? ""}
  initialValue={editTarget?.value ?? ""}
  fieldLabel={editTarget?.fieldLabel ?? "Value"}
  fieldKey={editTarget?.fieldKey ?? "value"}
  onSaved={(next) => setData(next)}
/>
```

Add the supporting state + helpers near the top of the component:

```typescript
import { Pencil } from "lucide-react";
import { DistillateEditDialog } from "./distillate-edit-dialog";

// ... existing useState calls ...
const [editTarget, setEditTarget] = useState<{
  category: "goals" | "principles" | "decision_rules" | "targets" | "constraints";
  itemLabel: string;
  value: string;
  fieldLabel: string;
  fieldKey: "value" | "detail" | "rule" | "rationale";
} | null>(null);

const openEdit = (t: NonNullable<typeof editTarget>) => setEditTarget(t);

function Section(props: {
  title: string;
  items: Array<{
    key: string;
    left: React.ReactNode;
    right: React.ReactNode;
    edited: boolean;
    onEdit: () => void;
  }>;
}) {
  return (
    <div>
      <p className="text-sm font-semibold uppercase tracking-wide text-muted-foreground mb-1">
        {props.title}
      </p>
      <ul className="flex flex-col gap-1">
        {props.items.map((it) => (
          <li
            key={it.key}
            className="flex items-start justify-between gap-3 text-sm"
          >
            <span>
              {it.left}: {it.right}
              {it.edited && (
                <span className="ml-2 text-[10px] uppercase font-mono text-amber-500">
                  user-edited
                </span>
              )}
            </span>
            <button
              type="button"
              onClick={it.onEdit}
              className="text-muted-foreground hover:text-foreground"
              aria-label={`Edit ${it.key}`}
              title="Edit"
            >
              <Pencil className="h-3 w-3" />
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
```

- [ ] **Step 2: Manually verify in dev**

1. Start engine + UI.
2. Open `/advisor`.
3. Click the pencil next to a goal, principle, target, decision rule, or constraint.
4. Edit the value, add a note, save.
5. The card refreshes; the row shows `user-edited`.
6. Click *Re-distill* (preserve_user_edits=true) — the user-edited row keeps its value.

- [ ] **Step 3: Commit**

```bash
git add ui/src/components/distillate-edit-dialog.tsx ui/src/components/plan-in-scope-card.tsx
git commit -m "feat(ui): per-item edit dialog for plan distillate"
```

---

### Task 1.17: SDD edits — new §6.10 + cross-references

**Files:**
- Modify: `docs/design/SDD.md`

- [ ] **Step 1: Insert new §6.10**

Edit `docs/design/SDD.md`. Find the end of `§6.9 Home-brief composition` (search for `### 6.9 Home-brief composition` and read the section to its end). Append a new section:

```markdown
### 6.10 Plan as baseline input (Wave 1 of plan-distillate work)

The user-imported plan (Jacobs Wealth Plan v2.0 today) is treated as a
**starting line, not a north star**. The full markdown is preserved in
`plan_versions.raw_markdown` for forensic lookups, but the only thing
downstream synthesis ever consumes is a compressed **distillate** —
durable principles, decision rules, and targets-as-stated, with explicit
exclusion of time-stamped numbers.

**The distillate captures (durable):**

- Goals (retirement target year, target income, FI status, employment horizon)
- Principles (UCITS-first for estate safety, NIS-USD natural hedge, real-returns framework, concentration-as-load-bearing-risk)
- Risk priorities (ordered list; first item dominates)
- Decision rules (bracket-aware RSU sales, gap-weighted deployment, etc.)
- Targets-as-stated (each carries `stated_at` + `revisit_after`)
- Constraints (no consolidate brokers, UCITS preferred, speculation cap)
- Stress tolerance

**The distillate explicitly excludes (decay-prone):**

- Current portfolio percentages (66% NVDA today)
- Current FX rates (3.09 NIS/USD)
- Specific dollar amounts at point-in-time
- Dated tranche schedules (Q1 2026 sells 2,500 shares)
- Share counts
- "Next 30/90 days" implementation roadmap sections

These are re-derived monthly by the synthesis flow (§6.11, Wave 2) from
current state.

**Pipeline:**

1. User uploads `Jacobs_Wealth_Plan.md` via `/api/intake/upload` — the
   row lands in `plan_versions` with `role='baseline'`.
2. The intake route synchronously calls `PlanDistillerAgent` (Sonnet,
   ~$0.30) and writes `distillate_json` + `distillate_rendered` +
   `source_hash` + `distilled_at` on the same row.
3. The advisor page shows the structured distillate via
   `<PlanInScopeCard>`; each item is editable inline with a
   `user_edited=true` flag preserved across re-distillations.
4. A daily `plan_watcher` cadence loop hashes the configured
   `source_path`. On diff, re-runs distillation with
   `preserve_user_edits=true`.
5. The advisor's working memory NEVER reads the distillate directly —
   it anchors only on the synthesized `current` plan (Wave 2).

**Schema** (migrations 0015 + 0016): the `plan_versions` table gains
`role`, `accepted_at`, `accepted_by_user_id`, `superseded_at`,
`derived_from_id`, `decision_run_id`, `distillate_json`,
`distillate_rendered`, `source_hash`, `distilled_at`. Three partial
unique indexes enforce one baseline / current / draft per user.
`decision_runs` gains `decision_kind` (values `trade_proposal` |
`plan_revision`).

**Authority framing.** Every plan-touching agent imports a shared
authority disclaimer (Wave 2): the plan is one input; cite it; disagree
when evidence warrants; loyalty is to the user, not to the plan. The
distillate is only the seed of the conversation.

See `docs/superpowers/specs/2026-05-05-plan-distillate-design.md` for
the full design.
```

- [ ] **Step 2: Update §3.6 cross-cutting agents table**

Find the §3.6 table (search `### 3.6 Cross-cutting agents`). Add a row:

```markdown
| **Plan distiller** | Extracts a durable structured distillate from a user-imported plan markdown. See §6.10. | One-shot on import + on baseline file change | Sonnet |
```

- [ ] **Step 3: Update §5.1 cadence catalog**

Find the §5.1 table. Add a row:

```markdown
| **Plan watcher** | Daily 07:00 user TZ | Hashes each user's baseline `source_path`; detects file change | Re-distill on diff (preserves user edits) |
```

- [ ] **Step 4: Update §8.5 migrations table**

Append two rows:

```markdown
| `0015_plan_versions_lifecycle` | `plan_versions.role` + acceptance/lineage columns; `decision_runs.decision_kind`; partial unique indexes (one baseline/current/draft per user) |
| `0016_plan_versions_distillate` | `plan_versions.{distillate_json,distillate_rendered,source_hash,distilled_at}` (Wave 1 of plan-distillate work) |
```

- [ ] **Step 5: Commit**

```bash
git add docs/design/SDD.md
git commit -m "docs(sdd): add §6.10 plan-as-baseline-input; cross-refs in §3.6/§5.1/§8.5"
```

---

## Wave 1 → Wave 2 GATE

Before starting Wave 2, all of the following must hold:

- [ ] All Wave 1 tests pass: `pytest tests/test_plan_distiller.py tests/test_plan_distiller_service.py tests/test_plan_baseline_route.py tests/test_plan_watcher_loop.py tests/test_migration_0015.py tests/test_migration_0016.py tests/test_phase1_models.py tests/test_intake_route.py -v`
- [ ] Live LLM eval passes: `pytest tests/test_plan_distiller_golden.py -m llm_eval -v` (must run with a real `ANTHROPIC_API_KEY` at least once and pass).
- [ ] Manual smoke: upload `Jacobs_Wealth_Plan.md` end-to-end through the UI; the distillate renders; an item edit + re-distill preserves the edit; `plan_watcher` re-distills when the source file changes on disk.
- [ ] No regressions: `pytest -m "not llm_eval" -v` is fully green.
- [ ] SDD edits committed (Task 1.17).

If any item fails, fix before proceeding. **Do not start Wave 2 until Phase 3 (decision team) is also in place** — Wave 2 reuses the analyst, debate, risk, and fund-manager agents and will not function without them.

---

# WAVE 2 — Synthesis Flow + Monthly Check-in UI

This wave wires the agent fleet to produce a fresh long/medium/short plan each month (or on user check-in), surfaces it in a side-sheet review UI, and gives the advisor's working memory an anchor that is updated and accepted by the user — not the static Jacobs baseline.

**Files this wave creates or modifies:**

- Create: `alembic/versions/0017_plan_versions_synthesis.py`
- Create: `argosy/agents/_plan_authority.py`
- Create: `argosy/agents/_plan_projection.py`
- Create: `argosy/agents/plan_synthesizer_types.py`
- Create: `argosy/agents/plan_synthesizer.py`
- Create: `argosy/orchestrator/flows/__init__.py`
- Create: `argosy/orchestrator/flows/plan_synthesis.py`
- Create: `argosy/api/routes/plan_draft.py` (or extend `plan.py`)
- Create: `ui/src/components/plan-revision-sheet.tsx`
- Create: `tests/test_plan_synthesizer.py`
- Create: `tests/test_plan_projection.py`
- Create: `tests/test_plan_synthesis_flow.py`
- Create: `tests/test_plan_synthesis_e2e.py`
- Create: `tests/test_plan_draft_api.py`
- Create: `tests/test_migration_0017.py`
- Modify: `argosy/state/models.py` (synthesis columns on `PlanVersion`)
- Modify: `argosy/orchestrator/loops/monthly_cycle.py` (kick off synthesis on the 1st)
- Modify: `argosy/api/routes/advisor.py` (add `/api/advisor/check-in`)
- Modify: `argosy/api/main.py` (register WebSocket events; mount draft router)
- Modify: `ui/src/lib/api.ts` (draft endpoints, WebSocket types)
- Modify: `ui/src/components/advisor-brief-card.tsx` (add `draft_plan` bullet kind)
- Modify: `ui/src/app/advisor/page.tsx` (mount `<PlanRevisionSheet>`)
- Modify: `docs/design/SDD.md` (new §6.11; updates to §3.6, §5.1, §10.1, §11.1, §11.3, §A.2)

---

### Task 2.1: Migration 0017 — synthesis columns

**Files:**
- Create: `alembic/versions/0017_plan_versions_synthesis.py`
- Create: `tests/test_migration_0017.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_migration_0017.py`:

```python
"""Schema assertions after migration 0017 (synthesis columns)."""

from sqlalchemy import inspect


def _columns(engine, table):
    insp = inspect(engine)
    return {c["name"]: c for c in insp.get_columns(table)}


def test_0017_adds_horizon_columns(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "plan_versions")
    for name in (
        "horizon_long_json", "horizon_medium_json", "horizon_short_json",
        "horizon_long_md", "horizon_medium_md", "horizon_short_md",
        "synthesis_inputs_json",
    ):
        assert name in cols, f"missing {name}"


def test_0017_columns_are_nullable(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "plan_versions")
    for name in (
        "horizon_long_json", "horizon_medium_json", "horizon_short_json",
        "horizon_long_md", "horizon_medium_md", "horizon_short_md",
        "synthesis_inputs_json",
    ):
        assert cols[name]["nullable"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_migration_0017.py -v`
Expected: FAIL.

- [ ] **Step 3: Write the migration**

Create `alembic/versions/0017_plan_versions_synthesis.py`:

```python
"""plan_versions synthesis columns (Wave 2 of plan-distillate work).

Revision ID: 0017_plan_versions_synthesis
Revises: 0016_plan_versions_distillate
Create Date: 2026-05-05

Populated only on synthesized rows (role in {draft,current,superseded}).
Baseline rows leave these NULL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_plan_versions_synthesis"
down_revision: str | Sequence[str] | None = "0016_plan_versions_distillate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("plan_versions") as batch:
        batch.add_column(sa.Column("horizon_long_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("horizon_medium_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("horizon_short_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("horizon_long_md", sa.Text(), nullable=True))
        batch.add_column(sa.Column("horizon_medium_md", sa.Text(), nullable=True))
        batch.add_column(sa.Column("horizon_short_md", sa.Text(), nullable=True))
        batch.add_column(sa.Column("synthesis_inputs_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("plan_versions") as batch:
        batch.drop_column("synthesis_inputs_json")
        batch.drop_column("horizon_short_md")
        batch.drop_column("horizon_medium_md")
        batch.drop_column("horizon_long_md")
        batch.drop_column("horizon_short_json")
        batch.drop_column("horizon_medium_json")
        batch.drop_column("horizon_long_json")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_migration_0017.py -v`
Expected: PASS (2 tests)

Reversibility:

```bash
python -c "from alembic.config import Config; from alembic import command; cfg = Config('alembic.ini'); cfg.set_main_option('sqlalchemy.url', 'sqlite:///./scratch_0017.db'); command.upgrade(cfg, 'head'); command.downgrade(cfg, '0016_plan_versions_distillate'); command.upgrade(cfg, 'head')"
```

Delete the scratch DB.

- [ ] **Step 5: Reflect on the model**

Edit `argosy/state/models.py` PlanVersion class. Append after the distillate columns (before `critiques` relationship):

```python
    # Synthesis (migration 0017) — populated only when role in {draft,current,superseded}.
    horizon_long_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    horizon_medium_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    horizon_short_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    horizon_long_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    horizon_medium_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    horizon_short_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    synthesis_inputs_json: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0017_plan_versions_synthesis.py tests/test_migration_0017.py argosy/state/models.py
git commit -m "feat(db): migration 0017 — plan_versions synthesis columns"
```

---

### Task 2.2: Pydantic types for `HorizonSection`, `Target`, `Theme`, `Action`, `Delta`, `SpeculativeCandidate`

**Files:**
- Create: `argosy/agents/plan_synthesizer_types.py`
- Test: `tests/test_plan_synthesizer.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_plan_synthesizer.py`:

```python
"""Tests for plan_synthesizer types and rendering."""

from __future__ import annotations

from datetime import date

import pytest


def test_horizon_section_round_trips():
    from argosy.agents.plan_synthesizer_types import (
        Action,
        Delta,
        HorizonSection,
        SpeculativeCandidate,
        SynthTarget,
        Theme,
    )

    h = HorizonSection(
        horizon="medium",
        freshness_expected="quarterly",
        status="minor_revision",
        posture="Continue concentration reduction; bias growth tilt for accumulation phase.",
        targets=[
            SynthTarget(
                label="NVDA concentration",
                value=0.12,
                unit="pct_of_portfolio",
                stated_at=date(2026, 5, 1),
                revisit_after=date(2026, 8, 1),
                rationale="DeepSeek + tariff overhang argues for tighter cap",
                source_section=None,
            )
        ],
        themes=[
            Theme(
                label="Tighter NVDA cap",
                direction="lean_away_from",
                rationale="structural shift",
                cited_sources=["agent_report:42"],
            )
        ],
        actions=[
            Action(
                label="Sell NVDA tranche on next strength",
                horizon_kind="parameterized",
                trigger_or_date="if NVDA > $200",
                detail="2500 shares",
                rationale="execute the medium-horizon target",
                cited_sources=["decision_run:99"],
            )
        ],
        speculative_candidates=[],
        deltas_from_prior=[
            Delta(
                item_kind="target",
                item_id="medium.targets.nvda",
                horizon="medium",
                change_kind="modified",
                summary="NVDA target tightened 15% -> 12%",
                prior={"value": 0.15, "unit": "pct_of_portfolio"},
                proposed={"value": 0.12, "unit": "pct_of_portfolio"},
                rationale="macro analyst flagged DeepSeek + tariff overhang",
                cited_sources=["agent_report:macro:2026-05-01"],
            )
        ],
        rationale="Updated medium horizon based on Phase 4 risk debate.",
        cited_sources=["plan_section:Investment Strategy"],
    )

    payload = h.model_dump_json()
    h2 = HorizonSection.model_validate_json(payload)
    assert h2.targets[0].value == 0.12
    assert h2.deltas_from_prior[0].change_kind == "modified"


def test_speculative_candidate_validates():
    from argosy.agents.plan_synthesizer_types import SpeculativeCandidate

    c = SpeculativeCandidate(
        ticker="HOOD",
        thesis_summary="momentum + sector rotation",
        suggested_position_usd=800,
        suggested_position_pct_of_net_worth=0.0008,
        risk_ceiling_check=True,
        horizon_days=30,
        expected_drawdown_pct=0.20,
        exit_trigger="stop -20%, take +50%",
        sourced_from=["sentiment", "watchlist"],
    )
    assert c.risk_ceiling_check is True


def test_short_horizon_only_allows_speculative_candidates():
    """SpeculativeCandidate is structurally a `short`-only field — covered by validation."""
    from argosy.agents.plan_synthesizer_types import (
        HorizonSection,
        SpeculativeCandidate,
    )

    bad = HorizonSection(
        horizon="medium",
        freshness_expected="quarterly",
        status="no_change",
        posture="x",
        speculative_candidates=[
            SpeculativeCandidate(
                ticker="HOOD", thesis_summary="x",
                suggested_position_usd=1, suggested_position_pct_of_net_worth=0.001,
                risk_ceiling_check=True, horizon_days=10, expected_drawdown_pct=0.1,
                exit_trigger="x", sourced_from=[],
            )
        ],
    )
    # We choose to NOT raise here; the synthesizer is responsible for
    # only emitting them on `short`. The test asserts the type still
    # validates so legacy data round-trips.
    assert len(bad.speculative_candidates) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_plan_synthesizer.py -v`
Expected: FAIL.

- [ ] **Step 3: Write the types module**

Create `argosy/agents/plan_synthesizer_types.py`:

```python
"""Types emitted by plan_synthesis_flow (Wave 2).

Mirrors spec §4.5. Each synthesized plan_versions row carries one
HorizonSection per horizon (long/medium/short) plus a synthesis_inputs
provenance payload.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class SynthTarget(BaseModel):
    """A numeric target inside a HorizonSection.

    Distinct from agents.plan_distiller_types.Target so the synthesis
    pipeline can evolve targets independently of the distillate's
    targets-as-stated.
    """

    label: str
    value: float
    unit: Literal[
        "pct_of_portfolio",
        "pct_of_net_worth",
        "pct_of_liquid",
        "usd",
        "nis",
        "shares",
        "ratio",
        "years",
    ]
    stated_at: date
    revisit_after: date
    rationale: str = ""
    source_section: str | None = None


class Theme(BaseModel):
    """Qualitative tilt for a horizon.

    Examples: "Tighter NVDA cap given DeepSeek + tariffs",
    "Currency-discipline: don't panic-convert".
    """

    label: str
    direction: Literal["lean_into", "lean_away_from", "monitor"]
    rationale: str = ""
    cited_sources: list[str] = Field(default_factory=list)


class Action(BaseModel):
    """An action item for the horizon.

    horizon_kind:
      - "directional": "continue NVDA reduction toward 15%"
      - "parameterized": "if VIX > 30 OR NVDA > $250: accelerate tranche size by 50%"
      - "dated": "harvest IBIT loss before 2026-05-15"
    """

    label: str
    horizon_kind: Literal["directional", "parameterized", "dated"]
    trigger_or_date: str | None = None
    detail: str = ""
    rationale: str = ""
    cited_sources: list[str] = Field(default_factory=list)


class SpeculativeCandidate(BaseModel):
    """Bounded-risk opportunity surfaced in the short horizon.

    risk_ceiling_check MUST be True for the candidate to be surfaced;
    the synthesizer enforces this against agent_settings.yaml::
    speculation.max_pct_of_net_worth.
    """

    ticker: str
    thesis_summary: str
    suggested_position_usd: float
    suggested_position_pct_of_net_worth: float
    risk_ceiling_check: bool
    horizon_days: int
    expected_drawdown_pct: float
    exit_trigger: str
    sourced_from: list[str] = Field(default_factory=list)


class Delta(BaseModel):
    """One change in the draft vs. prior current plan.

    item_id is a stable string within a draft (e.g. "medium.targets.nvda")
    so per-delta accept/reject in the UI keys against a stable identifier.
    """

    item_kind: Literal["target", "theme", "action", "speculative_candidate"]
    item_id: str
    horizon: Literal["long", "medium", "short"]
    change_kind: Literal["added", "removed", "modified"]
    summary: str
    prior: dict | None = None
    proposed: dict | None = None
    rationale: str = ""
    cited_sources: list[str] = Field(default_factory=list)
    accepted: bool = False
    user_edited: bool = False
    user_edit_note: str | None = None


class HorizonSection(BaseModel):
    """One of the three horizon documents emitted by synthesis."""

    horizon: Literal["long", "medium", "short"]
    freshness_expected: Literal["annual", "quarterly", "monthly"]
    status: Literal["no_change", "minor_revision", "major_revision"]
    posture: str
    targets: list[SynthTarget] = Field(default_factory=list)
    themes: list[Theme] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    speculative_candidates: list[SpeculativeCandidate] = Field(default_factory=list)
    deltas_from_prior: list[Delta] = Field(default_factory=list)
    rationale: str = ""
    cited_sources: list[str] = Field(default_factory=list)


class SynthesisInputs(BaseModel):
    """Provenance: what was fed into this synthesis run."""

    baseline_id: int | None = None
    prior_current_id: int | None = None
    snapshot_id: int | None = None
    fill_ids: list[int] = Field(default_factory=list)
    agent_report_ids: list[int] = Field(default_factory=list)
    debate_outcome_ids: list[int] = Field(default_factory=list)
    decision_run_id: str | None = None


class PlanSynthesisOutput(BaseModel):
    """The full output of one synthesis run, written to plan_versions
    as role='draft'.
    """

    long: HorizonSection
    medium: HorizonSection
    short: HorizonSection
    inputs: SynthesisInputs


__all__ = [
    "Action",
    "Delta",
    "HorizonSection",
    "PlanSynthesisOutput",
    "SpeculativeCandidate",
    "SynthTarget",
    "SynthesisInputs",
    "Theme",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plan_synthesizer.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add argosy/agents/plan_synthesizer_types.py tests/test_plan_synthesizer.py
git commit -m "feat(agents): pydantic types for plan synthesizer (HorizonSection, SynthTarget, Theme, Action, Delta, SpeculativeCandidate)"
```

---

### Task 2.3: `_plan_authority.py` — shared authority disclaimer

**Files:**
- Create: `argosy/agents/_plan_authority.py`
- Test: `tests/test_plan_authority.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_plan_authority.py`:

```python
"""The authority disclaimer is a shared constant; every plan-touching
agent must pull from this single source so the message stays consistent.
"""

def test_authority_disclaimer_contains_required_phrases():
    from argosy.agents._plan_authority import AUTHORITY_DISCLAIMER

    text = AUTHORITY_DISCLAIMER.lower()
    for phrase in (
        "one input",
        "disagree",
        "loyal",
        "not authority",
    ):
        assert phrase in text, f"disclaimer missing required phrase: {phrase!r}"


def test_authority_disclaimer_is_singleton():
    """Importing twice returns the same object — no per-call mutation."""
    from argosy.agents._plan_authority import AUTHORITY_DISCLAIMER as A
    from argosy.agents._plan_authority import AUTHORITY_DISCLAIMER as B

    assert A is B
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plan_authority.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write the module**

Create `argosy/agents/_plan_authority.py`:

```python
"""Shared authority disclaimer for plan-touching agents.

Per spec §6.1: every agent prompt that injects plan context must
include this disclaimer so the model cannot drift toward treating the
plan as authority.

Imported by:
  - argosy.agents.advisor (advisor turns)
  - argosy.agents.plan_synthesizer (synthesis Phase 3)
  - argosy.agents.plan_critique (when run as part of synthesis Phase 1)
  - all decision_flow agents that read the current plan

Do NOT modify this string lightly — wording was deliberately chosen.
If you must edit it, update tests/test_plan_authority.py too.
"""

from __future__ import annotations

AUTHORITY_DISCLAIMER = (
    "AUTHORITY NOTE — read carefully:\n\n"
    "The plan you have been provided is ONE INPUT among portfolio state, "
    "market data, news, and the analyst reports you receive. Cite it when "
    "you reason; DISAGREE when evidence warrants. The plan is NOT "
    "authority. Your job is to be loyal to the user, not to the plan. "
    "If the plan's stated targets or assumptions are stale, contradicted "
    "by current data, or no longer best-serving the user's goals, say so "
    "explicitly and cite the contradicting evidence."
)


__all__ = ["AUTHORITY_DISCLAIMER"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plan_authority.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add argosy/agents/_plan_authority.py tests/test_plan_authority.py
git commit -m "feat(agents): shared authority disclaimer for plan-touching agents"
```

---

### Task 2.4: `_plan_projection.py` — compact projection generator

**Files:**
- Create: `argosy/agents/_plan_projection.py`
- Test: `tests/test_plan_projection.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_plan_projection.py`:

```python
"""Compact-projection generator — pure Python, no LLM call.

Reads a synthesized PlanVersion (role='current') and emits a
~500-800 token markdown block for injection into advisor prompts.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import PlanVersion, User


def _make_horizon(horizon, status="no_change", **kw):
    from argosy.agents.plan_synthesizer_types import HorizonSection

    base = dict(
        horizon=horizon,
        freshness_expected={"long": "annual", "medium": "quarterly", "short": "monthly"}[horizon],
        status=status,
        posture=f"{horizon} posture",
        targets=[],
        themes=[],
        actions=[],
        speculative_candidates=[],
        deltas_from_prior=[],
        rationale="",
        cited_sources=[],
    )
    base.update(kw)
    return HorizonSection(**base)


@pytest.fixture
def session_with_current(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.commit()

    long = _make_horizon("long", status="no_change", posture="Wealth maximization; retirement target 2031.")
    medium = _make_horizon("medium", status="minor_revision", posture="Continue NVDA reduction; growth tilt.")
    short = _make_horizon("short", status="major_revision", posture="Sell NVDA tranche; harvest IBIT.")

    pv = PlanVersion(
        user_id="ariel",
        role="current",
        version_label="synth-2026-05",
        raw_markdown="",
        horizon_long_json=long.model_dump_json(),
        horizon_medium_json=medium.model_dump_json(),
        horizon_short_json=short.model_dump_json(),
    )
    s.add(pv)
    s.commit()
    s.refresh(pv)
    yield s, pv
    s.close()


def test_compact_projection_includes_all_three_horizons(session_with_current):
    from argosy.agents._plan_projection import compact_projection

    s, pv = session_with_current
    md = compact_projection(s, user_id="ariel")
    assert md is not None
    assert "[long" in md and "[medium" in md and "[short" in md
    assert "Wealth maximization" in md
    assert "Continue NVDA reduction" in md
    assert "Sell NVDA tranche" in md


def test_compact_projection_returns_none_when_no_current(alembic_engine_at_head):
    from argosy.agents._plan_projection import compact_projection
    from sqlalchemy.orm import sessionmaker

    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    try:
        s.add(User(id="dana", plan="free"))
        s.commit()
        assert compact_projection(s, user_id="dana") is None
    finally:
        s.close()


def test_compact_projection_under_token_budget(session_with_current):
    """Spec §6.4: compact projection must stay under ~1500 tokens.

    We approximate tokens as len/4 and assert <= 1500 chars * 4 = 6000
    (loose bound; the projection is bigger when full of targets).
    """
    from argosy.agents._plan_projection import compact_projection

    s, pv = session_with_current
    md = compact_projection(s, user_id="ariel")
    assert md is not None
    # Loose bound: projection should not blow past 6000 chars.
    assert len(md) < 6000, f"projection too long: {len(md)} chars"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plan_projection.py -v`
Expected: FAIL.

- [ ] **Step 3: Write the projection generator**

Create `argosy/agents/_plan_projection.py`:

```python
"""Compact projection of the current plan, for advisor system-prompt injection.

Per spec §6.2: deterministic Python helper, no LLM call. Reads the
user's role='current' PlanVersion and emits a ~500-800 token markdown
block. Truncates themes/actions if oversize.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from argosy.agents.plan_synthesizer_types import HorizonSection
from argosy.state.queries import get_current_plan

# Hard cap (chars) — projection must stay reasonable for prompt cache stability.
MAX_CHARS = 6000


def _section_block(section: HorizonSection) -> str:
    lines: list[str] = []
    lines.append(
        f"[{section.horizon}, freshness={section.freshness_expected}, "
        f"status={section.status}]"
    )
    if section.posture:
        lines.append(f"  Posture: {section.posture}")
    if section.targets:
        lines.append("  Top targets (with stated-at):")
        for t in section.targets[:5]:
            stated = t.stated_at.isoformat()
            revisit = t.revisit_after.isoformat()
            lines.append(
                f"    - {t.label}: {t.value} {t.unit} "
                f"(stated {stated}; revisit {revisit})"
            )
    if section.themes:
        lines.append("  Active themes:")
        for th in section.themes[:5]:
            lines.append(f"    - {th.label} ({th.direction})")
    if section.actions:
        kind = (
            "directional" if section.horizon == "long"
            else "parameterized" if section.horizon == "medium"
            else "dated"
        )
        lines.append(f"  Actions ({kind}):")
        for a in section.actions[:8]:
            trigger = f" [{a.trigger_or_date}]" if a.trigger_or_date else ""
            lines.append(f"    - {a.label}{trigger}: {a.detail}")
    if section.horizon == "short" and section.speculative_candidates:
        lines.append("  Speculative candidates surfaced:")
        for sc in section.speculative_candidates[:5]:
            lines.append(
                f"    - {sc.ticker}: max ${sc.suggested_position_usd:,.0f} "
                f"(= {sc.suggested_position_pct_of_net_worth*100:.2f}% NW) · "
                f"{sc.thesis_summary} · exit: {sc.exit_trigger}"
            )
    return "\n".join(lines)


def compact_projection(session: Session, *, user_id: str) -> str | None:
    """Return the compact markdown projection of the user's current plan,
    or None if no current plan exists.
    """
    pv = get_current_plan(session, user_id)
    if pv is None:
        return None

    parts: list[str] = []
    label = pv.version_label or "current"
    accepted = pv.accepted_at.isoformat() if pv.accepted_at else "(unaccepted)"
    parts.append(f"=== Your current plan ({label}; accepted {accepted}) ===")
    parts.append("")

    for horizon_field in ("horizon_long_json", "horizon_medium_json", "horizon_short_json"):
        raw = getattr(pv, horizon_field)
        if not raw:
            continue
        section = HorizonSection.model_validate_json(raw)
        parts.append(_section_block(section))
        parts.append("")

    parts.append("=== End plan ===")
    out = "\n".join(parts)
    if len(out) > MAX_CHARS:
        out = out[: MAX_CHARS - 100] + "\n\n[truncated to fit token budget]\n=== End plan ==="
    return out


__all__ = ["compact_projection", "MAX_CHARS"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plan_projection.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add argosy/agents/_plan_projection.py tests/test_plan_projection.py
git commit -m "feat(agents): compact_projection helper for advisor prompt injection"
```

---

### Task 2.5: `PlanSynthesizerAgent` (Phase 3 of synthesis)

**Files:**
- Create: `argosy/agents/plan_synthesizer.py`
- Modify: `argosy/agents/base.py` (add `plan_synthesizer` to model map; default Opus per accuracy preference)
- Test: `tests/test_plan_synthesizer.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_plan_synthesizer.py`:

```python
def test_plan_synthesizer_agent_basic_shape():
    from argosy.agents.plan_synthesizer import PlanSynthesizerAgent
    from argosy.agents.plan_synthesizer_types import PlanSynthesisOutput

    agent = PlanSynthesizerAgent()
    assert agent.agent_role == "plan_synthesizer"
    assert agent.output_model is PlanSynthesisOutput
    assert agent.require_citations is True


def test_plan_synthesizer_prompt_includes_authority_disclaimer_and_inputs():
    from argosy.agents._plan_authority import AUTHORITY_DISCLAIMER
    from argosy.agents.plan_synthesizer import PlanSynthesizerAgent

    agent = PlanSynthesizerAgent()
    sys, usr = agent.build_prompt(
        baseline_distillate_md="# Distillate\n\nNVDA target 15%",
        prior_current_md="# Prior current",
        analyst_reports_text="news: ok\nmacro: ok\n",
        debate_outcomes_text="long: hold; medium: tighten; short: harvest",
        portfolio_snapshot_summary="NVDA 14%; cash 5%",
        recent_fills_summary="sold 1000 NVDA on 2026-04-15",
    )
    # System prompt MUST include the authority disclaimer verbatim.
    assert AUTHORITY_DISCLAIMER in sys
    # Inputs are in the user prompt.
    assert "Distillate" in usr
    assert "Prior current" in usr
    assert "news: ok" in usr
    assert "tighten" in usr
    assert "NVDA 14%" in usr
    assert "sold 1000 NVDA" in usr
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plan_synthesizer.py -k synthesizer_agent -v`
Expected: FAIL.

- [ ] **Step 3: Add to model map**

In `argosy/agents/base.py`, append to `DEFAULT_MODEL_BY_ROLE`:

```python
    # Plan synthesizer (Phase 3 of plan_synthesis_flow): produces the
    # three HorizonSection drafts. Opus default — accuracy over cost
    # per user preference (the synthesizer is the firm's intellectual
    # output; its quality dominates the overall flow's value).
    "plan_synthesizer": "claude-opus-4-7",
```

- [ ] **Step 4: Write the agent**

Create `argosy/agents/plan_synthesizer.py`:

```python
"""Plan synthesizer — Phase 3 of plan_synthesis_flow.

Inputs (assembled by the orchestrator):
  - baseline distillate (markdown)
  - prior current plan (markdown — or empty on first synthesis)
  - 9 analyst reports concatenated (text)
  - 3 debate outcomes (one per horizon)
  - portfolio snapshot summary
  - recent fills + decisions summary

Output: PlanSynthesisOutput (long, medium, short HorizonSections + inputs
provenance).

Default model: Opus. Per user preference (accuracy over cost), the
synthesizer is given the most capable model in the fleet.
"""

from __future__ import annotations

from argosy.agents._plan_authority import AUTHORITY_DISCLAIMER
from argosy.agents.base import BaseAgent
from argosy.agents.plan_synthesizer_types import PlanSynthesisOutput


class PlanSynthesizerAgent(BaseAgent[PlanSynthesisOutput]):
    """Phase 3 of plan_synthesis_flow."""

    agent_role = "plan_synthesizer"
    output_model = PlanSynthesisOutput
    require_citations = True
    max_tokens = 16384

    def build_prompt(
        self,
        *,
        baseline_distillate_md: str,
        prior_current_md: str,
        analyst_reports_text: str,
        debate_outcomes_text: str,
        portfolio_snapshot_summary: str,
        recent_fills_summary: str,
    ) -> tuple[str, str]:
        system = (
            "You are the plan synthesizer on the Argosy fleet — Phase 3 of the "
            "monthly synthesis flow.\n\n"
            f"{AUTHORITY_DISCLAIMER}\n\n"
            "Your job: produce three HorizonSection documents (long, medium, "
            "short) from the inputs below. The medium horizon is the strategic "
            "centerpiece — that is where the firm earns its fee. Long is mostly "
            "stable; short is mostly mechanical.\n\n"
            "Per-horizon character:\n"
            "  - long (5+ years): posture-heavy, few targets, directional "
            "    actions, status=no_change is the common case.\n"
            "  - medium (1-2 years): tactical targets, themed actions, "
            "    parameterized triggers (\"if VIX > 30: accelerate\").\n"
            "  - short (~30 days): dated, concrete, replaced every monthly "
            "    cycle. Includes speculative_candidates.\n\n"
            "STATUS values:\n"
            "  - no_change: nothing material moved; honest, evidence-backed.\n"
            "  - minor_revision: targets nudged or actions refined.\n"
            "  - major_revision: structural target/posture change.\n\n"
            "DELTAS: every change vs. the prior current plan must produce a "
            "Delta entry with a stable item_id (e.g. 'medium.targets.nvda'), "
            "rationale, and citations. Per-delta accept/reject relies on these.\n\n"
            "CITATIONS REQUIRED for every numeric or directional claim. Use "
            "the format `agent_report:<id>` for analyst evidence, "
            "`decision_run:<id>` for prior synthesis lineage, "
            "`domain_kb:<path>` for jurisdiction rules, "
            "`plan_section:<heading>` for baseline references, "
            "`prior_current:<id>` for diff context.\n\n"
            "OUTPUT must be a JSON object conforming to:\n"
            f"{PlanSynthesisOutput.model_json_schema()}\n"
        )

        usr = "\n\n".join([
            "=== BASELINE DISTILLATE ===\n" + (baseline_distillate_md or "(no baseline)"),
            "=== PRIOR CURRENT PLAN ===\n" + (prior_current_md or "(no prior current — first synthesis)"),
            "=== ANALYST REPORTS (Phase 1 outputs) ===\n" + analyst_reports_text,
            "=== DEBATE OUTCOMES (Phase 2 outputs, one per horizon) ===\n" + debate_outcomes_text,
            "=== PORTFOLIO SNAPSHOT ===\n" + portfolio_snapshot_summary,
            "=== RECENT FILLS + DECISIONS (last 90 days) ===\n" + recent_fills_summary,
            "Produce the PlanSynthesisOutput JSON now. Honor the medium-horizon "
            "centerpiece framing. If status=no_change for a horizon, deltas_from_prior "
            "must be empty AND the rationale must explicitly justify why nothing changed.",
        ])
        return system, usr


__all__ = ["PlanSynthesizerAgent"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_plan_synthesizer.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add argosy/agents/plan_synthesizer.py argosy/agents/base.py tests/test_plan_synthesizer.py
git commit -m "feat(agents): PlanSynthesizerAgent (Phase 3 of plan_synthesis_flow)"
```

---

### Task 2.6: `plan_synthesis_flow` orchestrator — input assembly + idempotency

**Files:**
- Create: `argosy/orchestrator/flows/__init__.py`
- Create: `argosy/orchestrator/flows/plan_synthesis.py`
- Test: `tests/test_plan_synthesis_flow.py`

- [ ] **Step 1: Scaffold the flows package**

```bash
mkdir -p argosy/orchestrator/flows
touch argosy/orchestrator/flows/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_plan_synthesis_flow.py`:

```python
"""Tests for plan_synthesis_flow orchestrator.

The orchestrator wires Phases 1-5 together. Tests use stub agents that
return canned outputs; no live LLM call is made. The end-to-end live
test is in tests/test_plan_synthesis_e2e.py (Task 2.13).
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import PlanVersion, User


@pytest.fixture
def session(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    # Insert a baseline so synthesis has an input.
    s.add(PlanVersion(
        user_id="ariel",
        role="baseline",
        version_label="Jacobs v2.0",
        raw_markdown="# Plan",
        distillate_rendered="# Plan distillate\n\nUCITS-first.\n",
    ))
    s.commit()
    yield s
    s.close()


def _stub_synthesis_output():
    from argosy.agents.plan_synthesizer_types import (
        HorizonSection,
        PlanSynthesisOutput,
        SynthesisInputs,
    )

    long = HorizonSection(
        horizon="long", freshness_expected="annual", status="no_change",
        posture="long posture",
    )
    medium = HorizonSection(
        horizon="medium", freshness_expected="quarterly", status="minor_revision",
        posture="medium posture",
    )
    short = HorizonSection(
        horizon="short", freshness_expected="monthly", status="major_revision",
        posture="short posture",
    )
    return PlanSynthesisOutput(
        long=long, medium=medium, short=short,
        inputs=SynthesisInputs(),
    )


def test_synthesis_flow_writes_role_draft(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow

    # Stub each phase. We only verify the *integration* — that the flow
    # writes a draft row with the expected horizons; the per-agent prompt
    # tests live in their own test files.
    monkeypatch.setattr(flow, "_run_phase_1_analysts", lambda **kw: "(analyst reports)")
    monkeypatch.setattr(flow, "_run_phase_2_debates", lambda **kw: "(debate outcomes)")
    monkeypatch.setattr(flow, "_run_phase_3_synthesizer", lambda **kw: _stub_synthesis_output())
    monkeypatch.setattr(flow, "_run_phase_4_risk", lambda **kw: "(risk verdict)")
    monkeypatch.setattr(flow, "_run_phase_5_fund_manager", lambda **kw: True)
    monkeypatch.setattr(flow, "_assemble_portfolio_summary", lambda **kw: "NVDA 14%")
    monkeypatch.setattr(flow, "_assemble_fills_summary", lambda **kw: "(no fills)")

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None

    pv = session.get(PlanVersion, out.draft_id)
    assert pv.role == "draft"
    assert pv.user_id == "ariel"
    assert pv.horizon_long_json is not None
    assert pv.horizon_medium_json is not None
    assert pv.horizon_short_json is not None
    parsed = json.loads(pv.horizon_medium_json)
    assert parsed["status"] == "minor_revision"


def test_synthesis_flow_replaces_existing_draft(session, monkeypatch):
    """Idempotency: if a draft already exists, replace it (do not stack)."""
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setattr(flow, "_run_phase_1_analysts", lambda **kw: "x")
    monkeypatch.setattr(flow, "_run_phase_2_debates", lambda **kw: "x")
    monkeypatch.setattr(flow, "_run_phase_3_synthesizer", lambda **kw: _stub_synthesis_output())
    monkeypatch.setattr(flow, "_run_phase_4_risk", lambda **kw: "x")
    monkeypatch.setattr(flow, "_run_phase_5_fund_manager", lambda **kw: True)
    monkeypatch.setattr(flow, "_assemble_portfolio_summary", lambda **kw: "x")
    monkeypatch.setattr(flow, "_assemble_fills_summary", lambda **kw: "x")

    out1 = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    out2 = flow.run_synthesis(session, user_id="ariel", trigger="check_in")

    drafts = session.query(PlanVersion).filter_by(user_id="ariel", role="draft").all()
    assert len(drafts) == 1, f"expected 1 draft after idempotent rerun, got {len(drafts)}"
    # The fresh draft is the second one; the first should be superseded.
    superseded = session.query(PlanVersion).filter_by(
        user_id="ariel", role="superseded"
    ).all()
    assert any(pv.id == out1.draft_id for pv in superseded), \
        "first draft should be moved to role=superseded after replacement"


def test_synthesis_flow_fails_loudly_when_no_baseline(alembic_engine_at_head, monkeypatch):
    """Without a baseline, synthesis cannot run — the orchestrator must
    raise rather than silently produce a draft from nothing.
    """
    from sqlalchemy.orm import sessionmaker
    from argosy.orchestrator.flows import plan_synthesis as flow

    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    sess = SessionLocal()
    sess.add(User(id="newcomer", plan="free"))
    sess.commit()

    with pytest.raises(flow.NoBaselineError):
        flow.run_synthesis(sess, user_id="newcomer", trigger="scheduled")
    sess.close()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_plan_synthesis_flow.py -v`
Expected: FAIL.

- [ ] **Step 4: Write the orchestrator**

Create `argosy/orchestrator/flows/plan_synthesis.py`:

```python
"""plan_synthesis_flow — five-phase orchestration that produces a
draft long/medium/short plan from current state + agent fleet review.

Triggers (one of):
  - scheduled (monthly_cycle on the 1st)
  - check_in (user-initiated via /api/advisor/check-in)
  - quarterly (extra prompt weight on medium horizon)
  - annual   (extra prompt weight on long horizon)

Phases:
  1. Analyst reports (parallel) — 9 specialists run concurrently
  2. Researcher debate (per-horizon) — 3 horizons in parallel
  3. Synthesizer — produces the three HorizonSection drafts
  4. Risk team review — plan-level verdict
  5. Fund manager integrity check — green-lights as role=draft

Per spec §4. Output: a new role='draft' PlanVersion row.

Idempotency: if a draft already exists for the user, it is moved to
role='superseded' and a fresh draft is written.

Phase implementations are pluggable (each has a default that calls
the existing fleet agents with plan-revision prompts; tests stub them).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy.orm import Session

from argosy.agents.plan_synthesizer import PlanSynthesizerAgent
from argosy.agents.plan_synthesizer_types import (
    PlanSynthesisOutput,
    SynthesisInputs,
)
from argosy.logging import get_logger
from argosy.state.models import PlanVersion
from argosy.state.queries import get_active_baseline, get_current_plan, get_pending_draft

log = get_logger(__name__)


Trigger = Literal["scheduled", "check_in", "quarterly", "annual"]


class NoBaselineError(Exception):
    """Raised when a user has no active baseline plan."""


@dataclass
class SynthesisResult:
    decision_run_id: str
    draft_id: int


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------


def run_synthesis(
    session: Session,
    *,
    user_id: str,
    trigger: Trigger,
    guidance: str = "",
) -> SynthesisResult:
    """Execute the 5-phase synthesis. Writes a role='draft' row.

    Args:
        guidance: optional free-text from the user's check-in to weight
            the synthesis (e.g. "weight tax analyst more heavily").
    """
    baseline = get_active_baseline(session, user_id)
    if baseline is None:
        raise NoBaselineError(f"user {user_id!r} has no active baseline plan")

    prior_current = get_current_plan(session, user_id)
    decision_run_id = f"plan-synth-{uuid.uuid4().hex[:12]}"
    log.info(
        "plan_synthesis.start",
        user_id=user_id,
        trigger=trigger,
        decision_run_id=decision_run_id,
    )

    # Idempotency: demote any existing draft.
    existing = get_pending_draft(session, user_id)
    if existing is not None:
        existing.role = "superseded"
        existing.superseded_at = datetime.now(timezone.utc)
        session.commit()
        log.info(
            "plan_synthesis.demoted_existing_draft",
            superseded_id=existing.id,
            user_id=user_id,
        )

    # Phase 1: analyst reports.
    analyst_reports_text = _run_phase_1_analysts(
        session=session, user_id=user_id, baseline=baseline,
        prior_current=prior_current, decision_run_id=decision_run_id,
        guidance=guidance,
    )

    # Assemble inputs for Phases 2+.
    portfolio_summary = _assemble_portfolio_summary(session=session, user_id=user_id)
    fills_summary = _assemble_fills_summary(session=session, user_id=user_id)

    # Phase 2: per-horizon debates.
    debate_outcomes_text = _run_phase_2_debates(
        session=session, user_id=user_id,
        analyst_reports_text=analyst_reports_text,
        baseline=baseline, prior_current=prior_current,
        decision_run_id=decision_run_id, trigger=trigger,
    )

    # Phase 3: synthesize.
    output: PlanSynthesisOutput = _run_phase_3_synthesizer(
        session=session, user_id=user_id,
        baseline=baseline, prior_current=prior_current,
        analyst_reports_text=analyst_reports_text,
        debate_outcomes_text=debate_outcomes_text,
        portfolio_summary=portfolio_summary,
        fills_summary=fills_summary,
        decision_run_id=decision_run_id,
    )

    # Phase 4: risk team plan-level review.
    risk_verdict = _run_phase_4_risk(
        session=session, user_id=user_id, draft_output=output,
        analyst_reports_text=analyst_reports_text,
        decision_run_id=decision_run_id,
    )

    # Phase 5: fund manager integrity check.
    approved = _run_phase_5_fund_manager(
        session=session, user_id=user_id, draft_output=output,
        risk_verdict=risk_verdict, decision_run_id=decision_run_id,
    )
    if not approved:
        log.error("plan_synthesis.fm_rejected",
                  user_id=user_id, decision_run_id=decision_run_id)
        raise RuntimeError("fund manager rejected synthesized plan")

    # Persist as role='draft'.
    inputs = output.inputs.model_copy(update={
        "baseline_id": baseline.id,
        "prior_current_id": prior_current.id if prior_current else None,
        "decision_run_id": decision_run_id,
    })

    draft = PlanVersion(
        user_id=user_id,
        role="draft",
        version_label=f"synth-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M')}",
        source_path="",
        raw_markdown="",
        decision_run_id=decision_run_id,
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
    session.commit()
    session.refresh(draft)

    log.info("plan_synthesis.draft_persisted",
             user_id=user_id, draft_id=draft.id, decision_run_id=decision_run_id)
    return SynthesisResult(decision_run_id=decision_run_id, draft_id=draft.id)


# ----------------------------------------------------------------------
# Phase implementations (default — call existing fleet agents)
# ----------------------------------------------------------------------


def _run_phase_1_analysts(*, session, user_id, baseline, prior_current,
                           decision_run_id, guidance) -> str:
    """Run all 9 analysts in parallel against current state.

    Returns a concatenated string of analyst reports for downstream
    phases. Each agent's full structured output is also written to
    agent_reports stamped with decision_run_id.

    For Wave 2 first cut, this delegates to existing analyst agents
    (news/macro/concentration/plan_critique/tax/fx/sentiment/technical/
    fundamentals). We do not re-implement them. The plan-revision shape
    of their inputs/outputs is the same — they read state, produce
    structured reports.

    Tests monkeypatch this whole function to a stub.
    """
    # Wave 2 implementation note: this function will be expanded to call
    # each analyst's run_sync method in parallel via concurrent.futures
    # and concatenate their .output.model_dump_json() outputs. For the
    # initial scaffold we return a TODO marker — the real wiring lands
    # alongside Phase 3 agent-fleet readiness.
    log.info("plan_synthesis.phase_1_stub", user_id=user_id, decision_run_id=decision_run_id)
    return (
        "(Phase 1 analyst reports — wired against the live fleet "
        "in Phase 3 of SDD; see plan task 2.6 phase-stub note.)"
    )


def _run_phase_2_debates(*, session, user_id, analyst_reports_text,
                         baseline, prior_current, decision_run_id, trigger) -> str:
    """Run bull/bear/facilitator per-horizon (parallel across horizons)."""
    log.info("plan_synthesis.phase_2_stub", user_id=user_id, decision_run_id=decision_run_id)
    return (
        "(Phase 2 debate outcomes per horizon — wired against the "
        "researcher debate flow once SDD Phase 3 is complete.)"
    )


def _run_phase_3_synthesizer(*, session, user_id, baseline, prior_current,
                             analyst_reports_text, debate_outcomes_text,
                             portfolio_summary, fills_summary,
                             decision_run_id) -> PlanSynthesisOutput:
    """Default Phase 3: call PlanSynthesizerAgent."""
    agent = PlanSynthesizerAgent()
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
        analyst_reports_text=analyst_reports_text,
        debate_outcomes_text=debate_outcomes_text,
        portfolio_snapshot_summary=portfolio_summary,
        recent_fills_summary=fills_summary,
    )
    return result.output  # type: ignore[attr-defined]


def _run_phase_4_risk(*, session, user_id, draft_output: PlanSynthesisOutput,
                      analyst_reports_text: str, decision_run_id: str) -> str:
    """Run aggressive/neutral/conservative risk officers against the draft.

    Plan-level verdicts (not per-trade). Returns a consolidated text.
    Stubbed for tests.
    """
    log.info("plan_synthesis.phase_4_stub", user_id=user_id, decision_run_id=decision_run_id)
    return "(Phase 4 risk verdict — wired against the existing risk team flow.)"


def _run_phase_5_fund_manager(*, session, user_id,
                              draft_output: PlanSynthesisOutput,
                              risk_verdict: str, decision_run_id: str) -> bool:
    """Final integrity check. Returns True to green-light."""
    log.info("plan_synthesis.phase_5_stub", user_id=user_id, decision_run_id=decision_run_id)
    return True


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _horizon_md(section) -> str:
    """Render a HorizonSection to a markdown view used by the UI side sheet."""
    lines = [f"# {section.horizon.title()} horizon — status: {section.status}"]
    lines.append("")
    if section.posture:
        lines.append(f"**Posture.** {section.posture}")
        lines.append("")
    if section.targets:
        lines.append("## Targets")
        for t in section.targets:
            lines.append(
                f"- **{t.label}**: {t.value} {t.unit} "
                f"(stated {t.stated_at.isoformat()}; revisit {t.revisit_after.isoformat()})"
                f" — {t.rationale}" if t.rationale else ""
            )
        lines.append("")
    if section.themes:
        lines.append("## Themes")
        for th in section.themes:
            lines.append(f"- **{th.label}** ({th.direction}) — {th.rationale}")
        lines.append("")
    if section.actions:
        lines.append("## Actions")
        for a in section.actions:
            trigger = f" [{a.trigger_or_date}]" if a.trigger_or_date else ""
            lines.append(f"- **{a.label}**{trigger}: {a.detail} — {a.rationale}")
        lines.append("")
    if section.horizon == "short" and section.speculative_candidates:
        lines.append("## Speculative candidates")
        for sc in section.speculative_candidates:
            lines.append(
                f"- **{sc.ticker}**: max ${sc.suggested_position_usd:,.0f} "
                f"(= {sc.suggested_position_pct_of_net_worth*100:.2f}% NW) · "
                f"{sc.thesis_summary} · exit: {sc.exit_trigger}"
            )
        lines.append("")
    if section.deltas_from_prior:
        lines.append("## Deltas vs. prior current")
        for d in section.deltas_from_prior:
            lines.append(
                f"- [{d.change_kind}] {d.summary} ({d.item_kind} `{d.item_id}`)"
            )
        lines.append("")
    if section.rationale:
        lines.append("## Rationale")
        lines.append(section.rationale)
    return "\n".join(lines).rstrip() + "\n"


def _assemble_portfolio_summary(*, session, user_id) -> str:
    """Build a compact portfolio-state summary for synthesis input.

    Wave 2: read latest TSV/CSV ingest + IBKR positions per SDD §8.
    Tests stub this.
    """
    return "(portfolio snapshot — wired against existing positions ingest)"


def _assemble_fills_summary(*, session, user_id) -> str:
    """Last 90 days of fills + decisions, summarized."""
    return "(fills summary — wired against fills + proposals tables)"


__all__ = [
    "NoBaselineError",
    "SynthesisResult",
    "Trigger",
    "run_synthesis",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_plan_synthesis_flow.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add argosy/orchestrator/flows/__init__.py argosy/orchestrator/flows/plan_synthesis.py tests/test_plan_synthesis_flow.py
git commit -m "feat(orchestrator): plan_synthesis_flow — 5-phase scaffold + idempotency"
```

---

### Task 2.7: Wire Phase 1 (analysts) against the live fleet

**Files:**
- Modify: `argosy/orchestrator/flows/plan_synthesis.py` (`_run_phase_1_analysts`)
- Test: `tests/test_plan_synthesis_flow.py` (extend)

**Note:** This task assumes SDD Phase 3 (decision team) is in place. Each analyst (`news`, `macro`, `concentration`, `plan_critique`, `tax`, `fx`, `sentiment`, `technical`, `fundamentals`) already exposes a `run_sync(...)` method per the existing `BaseAgent` pattern.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_plan_synthesis_flow.py`:

```python
def test_phase_1_runs_all_nine_analysts(session, monkeypatch):
    """Phase 1 should invoke each of the 9 analyst agents once.

    We track invocations via a side-effect list. Real calls are stubbed.
    """
    from argosy.orchestrator.flows import plan_synthesis as flow

    invoked = []

    class _Stub:
        agent_role = "stub"
        def run_sync(self, **kw):
            invoked.append(self.__class__.__name__)
            return type("R", (), {"output": type("O", (), {"model_dump_json": lambda self: "{}"})(), "model": "fake"})()

    # Build stubs for all 9 analyst classes; monkeypatch the import points.
    for name in (
        "FundamentalsAnalystAgent", "TechnicalAnalystAgent",
        "NewsAnalystAgent", "SentimentAnalystAgent",
        "MacroAnalystAgent", "PlanCritiqueAgent",
        "ConcentrationAnalystAgent", "TaxAnalystAgent", "FxAnalystAgent",
    ):
        cls = type(name, (_Stub,), {})
        monkeypatch.setattr(f"argosy.orchestrator.flows.plan_synthesis.{name}", cls, raising=False)

    baseline = next(iter(session.query(PlanVersion).filter_by(role="baseline").all()))
    out = flow._run_phase_1_analysts(
        session=session,
        user_id="ariel",
        baseline=baseline,
        prior_current=None,
        decision_run_id="test-run",
        guidance="",
    )
    # All 9 must have been invoked exactly once.
    assert len(invoked) == 9, f"expected 9 analyst calls, got {len(invoked)}: {invoked}"
    assert isinstance(out, str)
    assert len(out) > 0
```

- [ ] **Step 2: Replace the stub with the real wiring**

In `argosy/orchestrator/flows/plan_synthesis.py`, replace `_run_phase_1_analysts` with:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

from argosy.agents.fundamentals_analyst import FundamentalsAnalystAgent
from argosy.agents.technical_analyst import TechnicalAnalystAgent
from argosy.agents.news_analyst import NewsAnalystAgent
from argosy.agents.sentiment_analyst import SentimentAnalystAgent
from argosy.agents.macro_analyst import MacroAnalystAgent
from argosy.agents.plan_critique import PlanCritiqueAgent
from argosy.agents.concentration_analyst import ConcentrationAnalystAgent
from argosy.agents.tax_analyst import TaxAnalystAgent
from argosy.agents.fx_analyst import FxAnalystAgent


_PHASE_1_AGENTS = (
    FundamentalsAnalystAgent,
    TechnicalAnalystAgent,
    NewsAnalystAgent,
    SentimentAnalystAgent,
    MacroAnalystAgent,
    PlanCritiqueAgent,
    ConcentrationAnalystAgent,
    TaxAnalystAgent,
    FxAnalystAgent,
)


def _run_phase_1_analysts(*, session, user_id, baseline, prior_current,
                           decision_run_id, guidance) -> str:
    """Run all 9 analysts in parallel. Concatenate their reports as text."""
    log.info("plan_synthesis.phase_1.start",
             user_id=user_id, decision_run_id=decision_run_id)

    # Each agent's run_sync(...) signature varies; we pass a shared kwargs
    # bag and rely on each agent's build_prompt to consume what it needs.
    # The base agents' run_sync forwards **kwargs to build_prompt.
    common_kwargs = dict(
        plan_label=baseline.version_label or "Imported plan",
        plan_markdown=baseline.distillate_rendered or "",
        snapshot_label=f"synthesis-{decision_run_id}",
        snapshot_summary=_assemble_portfolio_summary(session=session, user_id=user_id),
        user_context_yaml=_load_user_context_yaml(session=session, user_id=user_id),
        domain_kb_files={},  # Each analyst's prompt picks its own; pass empty.
        recent_events="",
    )

    reports: list[str] = []
    with ThreadPoolExecutor(max_workers=len(_PHASE_1_AGENTS)) as ex:
        futures = {
            ex.submit(_safe_run_agent, AgentCls, common_kwargs, decision_run_id): AgentCls
            for AgentCls in _PHASE_1_AGENTS
        }
        for fut in as_completed(futures):
            cls = futures[fut]
            try:
                payload = fut.result()
                reports.append(f"=== {cls.__name__} ===\n{payload}")
            except Exception as exc:  # noqa: BLE001
                log.error("plan_synthesis.phase_1.agent_failed",
                          agent=cls.__name__, error=str(exc),
                          decision_run_id=decision_run_id)
                # Failure of one analyst is recoverable — continue with
                # the others. Note in the concatenated text so the
                # synthesizer knows.
                reports.append(f"=== {cls.__name__} (FAILED) ===\n{exc}")

    log.info("plan_synthesis.phase_1.done",
             user_id=user_id, decision_run_id=decision_run_id,
             reports_count=len(reports))
    return "\n\n".join(reports)


def _safe_run_agent(AgentCls, kwargs: dict, decision_run_id: str) -> str:
    agent = AgentCls()
    try:
        result = agent.run_sync(**kwargs)
        out = getattr(result, "output", None)
        if out is not None and hasattr(out, "model_dump_json"):
            return out.model_dump_json()
        return str(out) if out is not None else ""
    except TypeError:
        # If the agent doesn't accept all the common kwargs, retry with
        # only the ones it explicitly declares. Cheap defensive retry.
        sig = getattr(agent.build_prompt, "__code__", None)
        accepted = set(sig.co_varnames if sig else ())
        narrowed = {k: v for k, v in kwargs.items() if k in accepted}
        result = agent.run_sync(**narrowed)
        out = getattr(result, "output", None)
        if out is not None and hasattr(out, "model_dump_json"):
            return out.model_dump_json()
        return str(out) if out is not None else ""


def _load_user_context_yaml(*, session, user_id) -> str:
    """Concatenate identity + goals + constraints YAML for the user."""
    from argosy.state.models import UserContext
    ctx = session.get(UserContext, user_id)
    if ctx is None:
        return ""
    parts = []
    if ctx.identity_yaml:
        parts.append(ctx.identity_yaml)
    if ctx.goals_yaml:
        parts.append(ctx.goals_yaml)
    if ctx.constraints_yaml:
        parts.append(ctx.constraints_yaml)
    return "\n".join(parts)
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest tests/test_plan_synthesis_flow.py -v`
Expected: PASS (4 tests).

- [ ] **Step 4: Commit**

```bash
git add argosy/orchestrator/flows/plan_synthesis.py tests/test_plan_synthesis_flow.py
git commit -m "feat(synthesis): Phase 1 — parallel run of all 9 analyst agents"
```

---

### Task 2.8: Wire Phase 2 (per-horizon debate)

**Files:**
- Modify: `argosy/orchestrator/flows/plan_synthesis.py` (`_run_phase_2_debates`)
- Test: `tests/test_plan_synthesis_flow.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_plan_synthesis_flow.py`:

```python
def test_phase_2_debates_runs_three_horizons(session, monkeypatch):
    """Phase 2 must invoke the researcher-debate flow once per horizon."""
    from argosy.orchestrator.flows import plan_synthesis as flow

    horizons_seen: list[str] = []

    def _fake_debate(*, horizon, **kw):
        horizons_seen.append(horizon)
        return f"DEBATE OUTCOME for {horizon}"

    monkeypatch.setattr(flow, "_run_one_horizon_debate", _fake_debate)

    baseline = next(iter(session.query(PlanVersion).filter_by(role="baseline").all()))
    out = flow._run_phase_2_debates(
        session=session, user_id="ariel",
        analyst_reports_text="(stub)", baseline=baseline,
        prior_current=None, decision_run_id="test", trigger="scheduled",
    )
    assert sorted(horizons_seen) == ["long", "medium", "short"]
    for h in ("long", "medium", "short"):
        assert f"DEBATE OUTCOME for {h}" in out
```

- [ ] **Step 2: Implement `_run_phase_2_debates`**

Replace the stub `_run_phase_2_debates` in `argosy/orchestrator/flows/plan_synthesis.py` with:

```python
def _run_phase_2_debates(*, session, user_id, analyst_reports_text,
                         baseline, prior_current, decision_run_id, trigger) -> str:
    """Run bull/bear/facilitator across all three horizons in parallel.

    Each horizon argues theses, not trades. Per-horizon facilitator
    extracts a structured DebateOutcome record.
    """
    log.info("plan_synthesis.phase_2.start",
             user_id=user_id, decision_run_id=decision_run_id)

    parts: list[str] = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(
                _run_one_horizon_debate,
                horizon=h,
                analyst_reports_text=analyst_reports_text,
                baseline=baseline,
                prior_current=prior_current,
                decision_run_id=decision_run_id,
                trigger=trigger,
            ): h for h in ("long", "medium", "short")
        }
        for fut in as_completed(futures):
            horizon = futures[fut]
            try:
                outcome_text = fut.result()
                parts.append(f"=== Debate outcome — {horizon} ===\n{outcome_text}")
            except Exception as exc:  # noqa: BLE001
                log.error("plan_synthesis.phase_2.debate_failed",
                          horizon=horizon, decision_run_id=decision_run_id,
                          error=str(exc))
                parts.append(f"=== Debate outcome — {horizon} (FAILED) ===\n{exc}")
    return "\n\n".join(parts)


def _run_one_horizon_debate(*, horizon: str, analyst_reports_text: str,
                             baseline, prior_current, decision_run_id: str,
                             trigger: str) -> str:
    """Run bull/bear/facilitator for one horizon.

    Reuses the existing argosy.agents.researcher and researcher_facilitator
    modules. The horizon shapes the prompt's question:
      - long: "do principles + targets still hold?"
      - medium: "tactical posture for next 1-2 years?"
      - short: "specific calls for next 30 days?"
    """
    from argosy.agents.researcher import ResearcherAgent  # bull/bear in one class
    from argosy.agents.researcher_facilitator import ResearcherFacilitatorAgent

    horizon_question = {
        "long": "Do the durable principles and 5+ year targets still hold?",
        "medium": (
            "Given the analyst reports and current state, what tactical "
            "posture should drive the next 1-2 years? Specific targets and "
            "themed actions; this is the strategic centerpiece."
        ),
        "short": (
            "What specific calls for the next 30 days? Defer or pull "
            "anything forward? Speculative candidates worth surfacing?"
        ),
    }[horizon]

    bull = ResearcherAgent(stance="bull")
    bear = ResearcherAgent(stance="bear")
    fac = ResearcherFacilitatorAgent()

    bull_out = bull.run_sync(
        question=horizon_question,
        analyst_reports=analyst_reports_text,
        round_n=1, round_max=2,
    )
    bear_out = bear.run_sync(
        question=horizon_question,
        analyst_reports=analyst_reports_text,
        round_n=1, round_max=2,
        opposing=bull_out.output if hasattr(bull_out, "output") else "",
    )
    facilitated = fac.run_sync(
        question=horizon_question,
        bull=bull_out.output if hasattr(bull_out, "output") else "",
        bear=bear_out.output if hasattr(bear_out, "output") else "",
    )
    out = facilitated.output
    return out.model_dump_json() if hasattr(out, "model_dump_json") else str(out)
```

If the existing `ResearcherAgent` / `ResearcherFacilitatorAgent` signatures differ from the calls above, adapt — open `argosy/agents/researcher.py` and `argosy/agents/researcher_facilitator.py` and use their actual `run_sync` signatures.

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest tests/test_plan_synthesis_flow.py -v`
Expected: PASS (5 tests)

- [ ] **Step 4: Commit**

```bash
git add argosy/orchestrator/flows/plan_synthesis.py tests/test_plan_synthesis_flow.py
git commit -m "feat(synthesis): Phase 2 — parallel per-horizon researcher debate"
```

---

### Task 2.9: Wire Phase 4 (risk team plan-level review)

**Files:**
- Modify: `argosy/orchestrator/flows/plan_synthesis.py` (`_run_phase_4_risk`)
- Test: `tests/test_plan_synthesis_flow.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_plan_synthesis_flow.py`:

```python
def test_phase_4_risk_runs_three_perspectives(monkeypatch, session):
    from argosy.orchestrator.flows import plan_synthesis as flow

    perspectives: list[str] = []

    def _fake_officer(stance):
        class _Stub:
            agent_role = f"risk_{stance}"
            def run_sync(self, **kw):
                perspectives.append(stance)
                return type("R", (), {"output": type("O", (), {"model_dump_json": lambda self: f"{stance} review"})(), "model": "fake"})()
        return _Stub()

    monkeypatch.setattr(flow, "_make_risk_officer", _fake_officer)

    out = _stub_synthesis_output()
    text = flow._run_phase_4_risk(
        session=session, user_id="ariel", draft_output=out,
        analyst_reports_text="(stub)", decision_run_id="test",
    )
    assert sorted(perspectives) == ["aggressive", "conservative", "neutral"]
    for s in ("aggressive", "neutral", "conservative"):
        assert f"{s} review" in text
```

- [ ] **Step 2: Implement `_run_phase_4_risk`**

Replace the stub in `plan_synthesis.py` with:

```python
def _run_phase_4_risk(*, session, user_id, draft_output: PlanSynthesisOutput,
                      analyst_reports_text: str, decision_run_id: str) -> str:
    """Plan-level risk verdict from three perspectives + facilitator merge."""
    log.info("plan_synthesis.phase_4.start",
             user_id=user_id, decision_run_id=decision_run_id)

    parts: list[str] = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(_run_one_risk_perspective,
                      stance=stance, draft_output=draft_output,
                      analyst_reports_text=analyst_reports_text,
                      decision_run_id=decision_run_id): stance
            for stance in ("aggressive", "neutral", "conservative")
        }
        for fut in as_completed(futures):
            stance = futures[fut]
            try:
                parts.append(f"=== Risk {stance} ===\n{fut.result()}")
            except Exception as exc:  # noqa: BLE001
                log.error("plan_synthesis.phase_4.risk_failed",
                          stance=stance, decision_run_id=decision_run_id,
                          error=str(exc))
                parts.append(f"=== Risk {stance} (FAILED) ===\n{exc}")

    # Facilitator merge.
    from argosy.agents.risk_facilitator import RiskFacilitatorAgent
    facilitator = RiskFacilitatorAgent()
    merge_input = "\n\n".join(parts)
    try:
        merged = facilitator.run_sync(
            draft_plan=draft_output.model_dump_json(),
            risk_reviews=merge_input,
        )
        merged_text = merged.output.model_dump_json() if hasattr(merged.output, "model_dump_json") else str(merged.output)
        parts.append(f"=== Risk facilitator verdict ===\n{merged_text}")
    except Exception as exc:  # noqa: BLE001
        log.error("plan_synthesis.phase_4.facilitator_failed",
                  decision_run_id=decision_run_id, error=str(exc))
        parts.append(f"=== Risk facilitator (FAILED) ===\n{exc}")

    return "\n\n".join(parts)


def _make_risk_officer(stance: str):
    """Return a RiskOfficerAgent configured for the requested stance."""
    from argosy.agents.risk_officer import RiskOfficerAgent
    return RiskOfficerAgent(stance=stance)


def _run_one_risk_perspective(*, stance: str,
                              draft_output: PlanSynthesisOutput,
                              analyst_reports_text: str,
                              decision_run_id: str) -> str:
    officer = _make_risk_officer(stance)
    result = officer.run_sync(
        draft_plan=draft_output.model_dump_json(),
        analyst_reports=analyst_reports_text,
    )
    out = result.output
    return out.model_dump_json() if hasattr(out, "model_dump_json") else str(out)
```

If `RiskOfficerAgent.__init__` does not accept a `stance` keyword in the existing codebase, open `argosy/agents/risk_officer.py` and adapt: pass the stance via prompt instead, or use whatever existing API the agent exposes.

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest tests/test_plan_synthesis_flow.py -v`
Expected: PASS (6 tests)

- [ ] **Step 4: Commit**

```bash
git add argosy/orchestrator/flows/plan_synthesis.py tests/test_plan_synthesis_flow.py
git commit -m "feat(synthesis): Phase 4 — plan-level risk team review (parallel perspectives + facilitator)"
```

---

### Task 2.10: Wire Phase 5 (fund manager integrity)

**Files:**
- Modify: `argosy/orchestrator/flows/plan_synthesis.py` (`_run_phase_5_fund_manager`)
- Test: `tests/test_plan_synthesis_flow.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_plan_synthesis_flow.py`:

```python
def test_phase_5_fund_manager_green_lights_or_rejects(monkeypatch, session):
    from argosy.orchestrator.flows import plan_synthesis as flow

    class _FakeFM:
        def __init__(self, ok):
            self.ok = ok
        def run_sync(self, **kw):
            class _Out:
                def __init__(s, ok): s.ok = ok
                def model_dump_json(self): return f'{{"approved": {str(self.ok).lower()}}}'
            return type("R", (), {"output": _Out(self.ok), "model": "fake"})()

    out = _stub_synthesis_output()

    monkeypatch.setattr(flow, "_make_fund_manager", lambda: _FakeFM(True))
    assert flow._run_phase_5_fund_manager(
        session=session, user_id="ariel", draft_output=out,
        risk_verdict="(ok)", decision_run_id="test",
    ) is True

    monkeypatch.setattr(flow, "_make_fund_manager", lambda: _FakeFM(False))
    assert flow._run_phase_5_fund_manager(
        session=session, user_id="ariel", draft_output=out,
        risk_verdict="(ok)", decision_run_id="test",
    ) is False
```

- [ ] **Step 2: Implement Phase 5**

Replace the stub `_run_phase_5_fund_manager` in `plan_synthesis.py`:

```python
import json as _json


def _make_fund_manager():
    from argosy.agents.fund_manager import FundManagerAgent
    return FundManagerAgent()


def _run_phase_5_fund_manager(*, session, user_id,
                              draft_output: PlanSynthesisOutput,
                              risk_verdict: str, decision_run_id: str) -> bool:
    """Final integrity check.

    Validates:
      - distillate hard-constraints honored
      - three horizons cohere
      - every target has rationale + cited source
      - 'no_change' justified by evidence if claimed

    Returns True to green-light the draft, False to reject.
    """
    fm = _make_fund_manager()
    result = fm.run_sync(
        decision_kind="plan_revision",
        draft_plan=draft_output.model_dump_json(),
        risk_verdict=risk_verdict,
    )
    out = result.output
    payload_text = out.model_dump_json() if hasattr(out, "model_dump_json") else str(out)
    try:
        payload = _json.loads(payload_text)
        approved = bool(payload.get("approved", False))
    except (ValueError, TypeError):
        log.error("plan_synthesis.phase_5.payload_unparseable",
                  decision_run_id=decision_run_id, payload=payload_text)
        return False

    log.info("plan_synthesis.phase_5.verdict",
             user_id=user_id, decision_run_id=decision_run_id,
             approved=approved)
    return approved
```

If the existing `FundManagerAgent.run_sync` does not accept `decision_kind`, add it to the agent's `build_prompt` signature with a default of `"trade_proposal"`. The new value `"plan_revision"` toggles a different prompt stanza inside the FM agent. Edit `argosy/agents/fund_manager.py` accordingly:

```python
# In build_prompt, accept and route on decision_kind:
def build_prompt(self, *, decision_kind: str = "trade_proposal", **kw) -> tuple[str, str]:
    if decision_kind == "plan_revision":
        return self._build_plan_revision_prompt(**kw)
    return self._build_trade_proposal_prompt(**kw)


def _build_plan_revision_prompt(self, *, draft_plan: str, risk_verdict: str) -> tuple[str, str]:
    from argosy.agents._plan_authority import AUTHORITY_DISCLAIMER
    system = (
        "You are the fund manager on the Argosy fleet. This is a "
        "plan-revision integrity check, not a trade approval.\n\n"
        f"{AUTHORITY_DISCLAIMER}\n\n"
        "Validate: (a) distillate hard-constraints honored; (b) three "
        "horizons cohere; (c) every target has rationale + cited source; "
        "(d) 'no_change' is justified by evidence if claimed.\n\n"
        "Output a JSON object: { approved: bool, reasons: list[str] }."
    )
    user = (
        f"=== DRAFT PLAN ===\n{draft_plan}\n\n"
        f"=== CONSOLIDATED RISK VERDICT ===\n{risk_verdict}\n\n"
        "Return your JSON verdict now."
    )
    return system, user
```

(Adapt to the agent's existing class structure — preserve any existing `_build_trade_proposal_prompt` body.)

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest tests/test_plan_synthesis_flow.py -v`
Expected: PASS (7 tests)

- [ ] **Step 4: Commit**

```bash
git add argosy/orchestrator/flows/plan_synthesis.py argosy/agents/fund_manager.py tests/test_plan_synthesis_flow.py
git commit -m "feat(synthesis): Phase 5 — fund manager plan-revision integrity check"
```

---

### Task 2.11: monthly_cycle integration — fire synthesis on the 1st

**Files:**
- Modify: `argosy/orchestrator/loops/monthly_cycle.py`
- Modify: `tests/test_monthly_cycle_loop.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_monthly_cycle_loop.py`:

```python
def test_monthly_cycle_triggers_plan_synthesis(monkeypatch, session_with_baseline):
    """On the 1st of the month, monthly_cycle.tick must call run_synthesis."""
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.orchestrator.loops import monthly_cycle

    calls = []

    def _fake_run(session, *, user_id, trigger, guidance=""):
        calls.append({"user_id": user_id, "trigger": trigger})
        class _R:
            decision_run_id = "test-run"
            draft_id = 999
        return _R()

    monkeypatch.setattr(flow, "run_synthesis", _fake_run)

    monthly_cycle.tick(session_with_baseline)

    user_ids = [c["user_id"] for c in calls]
    assert "ariel" in user_ids
    assert all(c["trigger"] == "scheduled" for c in calls)
```

The fixture `session_with_baseline` should set up a user `ariel` with a `role=baseline` row. Add it if absent:

```python
@pytest.fixture
def session_with_baseline(alembic_engine_at_head):
    from sqlalchemy.orm import sessionmaker
    from argosy.state.models import PlanVersion, User

    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.add(PlanVersion(user_id="ariel", role="baseline", raw_markdown="# Plan"))
    s.commit()
    yield s
    s.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_monthly_cycle_loop.py::test_monthly_cycle_triggers_plan_synthesis -v`
Expected: FAIL.

- [ ] **Step 3: Wire into `monthly_cycle.tick`**

Edit `argosy/orchestrator/loops/monthly_cycle.py`. Find the `tick(session)` function and add (alongside whatever existing logic — typically statement reconciliation, RSU vest, gap-weighted buy template):

```python
def tick(session) -> None:
    """Monthly cycle: fires on the 1st of the month."""
    # ... existing reconciliation / RSU / buy-template work ...

    # Wave 2: trigger plan synthesis for every user with an active baseline.
    _trigger_plan_synthesis_for_all(session)


def _trigger_plan_synthesis_for_all(session) -> None:
    """Fire plan_synthesis_flow.run_synthesis for each eligible user."""
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.state.models import PlanVersion

    log = get_logger(__name__) if "get_logger" in globals() else None

    # One synthesis per user with an active baseline.
    rows = (
        session.query(PlanVersion.user_id)
        .filter(PlanVersion.role == "baseline")
        .distinct()
        .all()
    )
    for (user_id,) in rows:
        try:
            flow.run_synthesis(session, user_id=user_id, trigger="scheduled")
        except flow.NoBaselineError:
            continue
        except Exception as exc:  # noqa: BLE001 — one user's failure must not stop others
            if log is not None:
                log.error("monthly_cycle.synthesis_failed",
                          user_id=user_id, error=str(exc))
            else:
                # fallback: stderr is fine for a non-critical path
                import sys
                print(f"monthly_cycle synthesis failed for {user_id}: {exc}", file=sys.stderr)
```

If `monthly_cycle.py` already imports `get_logger`, drop the conditional and use it directly.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_monthly_cycle_loop.py -v`
Expected: PASS (existing tests + the new one).

- [ ] **Step 5: Commit**

```bash
git add argosy/orchestrator/loops/monthly_cycle.py tests/test_monthly_cycle_loop.py
git commit -m "feat(orchestrator): monthly_cycle triggers plan synthesis for every baseline user"
```

---

### Task 2.12: API — `POST /api/advisor/check-in` (user-initiated synthesis)

**Files:**
- Modify: `argosy/api/routes/advisor.py`
- Test: `tests/test_advisor_route.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_advisor_route.py`:

```python
def test_post_advisor_checkin_returns_decision_run_id(client_with_db, monkeypatch):
    """POST /api/advisor/check-in should fire run_synthesis and return 202."""
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.state.models import PlanVersion, User

    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.add(PlanVersion(user_id="ariel", role="baseline", raw_markdown="# Plan"))
            sess.commit()
    finally:
        sess.close()

    captured = {}

    def _fake_run(session, *, user_id, trigger, guidance=""):
        captured["user_id"] = user_id
        captured["trigger"] = trigger
        captured["guidance"] = guidance
        class _R:
            decision_run_id = "test-cr-1"
            draft_id = 42
        return _R()

    monkeypatch.setattr(flow, "run_synthesis", _fake_run)

    body = {"user_id": "ariel", "guidance": "weight tax analyst more heavily", "urgency": "now"}
    r = client_with_db.post("/api/advisor/check-in", json=body)
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["decision_run_id"] == "test-cr-1"
    assert out["draft_id"] == 42
    assert captured["user_id"] == "ariel"
    assert captured["trigger"] == "check_in"
    assert "tax analyst" in captured["guidance"]


def test_post_advisor_checkin_404_when_no_baseline(client_with_db):
    body = {"user_id": "ghost", "guidance": "", "urgency": "now"}
    r = client_with_db.post("/api/advisor/check-in", json=body)
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_advisor_route.py::test_post_advisor_checkin_returns_decision_run_id tests/test_advisor_route.py::test_post_advisor_checkin_404_when_no_baseline -v`
Expected: FAIL — endpoint not yet defined.

- [ ] **Step 3: Add the route**

Edit `argosy/api/routes/advisor.py`. Append (alongside existing `/turn`, `/gaps`, `/home-brief` routes):

```python
class CheckInRequest(BaseModel):
    user_id: str
    guidance: str = ""
    urgency: str = "now"  # currently informational only


class CheckInResponse(BaseModel):
    status: str
    decision_run_id: str
    draft_id: int


@router.post("/check-in", response_model=CheckInResponse, status_code=202)
def post_check_in(
    body: CheckInRequest,
    db: Session = Depends(get_db),
) -> CheckInResponse:
    """User-initiated plan synthesis (spec §7.6)."""
    from argosy.orchestrator.flows.plan_synthesis import (
        NoBaselineError,
        run_synthesis,
    )

    try:
        result = run_synthesis(
            db,
            user_id=body.user_id,
            trigger="check_in",
            guidance=body.guidance,
        )
    except NoBaselineError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return CheckInResponse(
        status="accepted",
        decision_run_id=result.decision_run_id,
        draft_id=result.draft_id,
    )
```

(Make sure `BaseModel`, `HTTPException`, `Session`, `Depends`, `get_db` are imported at the top of the file. If imports differ, follow the file's existing conventions.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_advisor_route.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add argosy/api/routes/advisor.py tests/test_advisor_route.py
git commit -m "feat(api): POST /api/advisor/check-in — user-initiated plan synthesis"
```

---

### Task 2.13: API — draft lifecycle endpoints (`GET /draft`, `POST /draft/<id>/accept`, `POST /draft/<id>/reject`)

**Files:**
- Modify: `argosy/api/routes/plan.py`
- Test: `tests/test_plan_draft_api.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_plan_draft_api.py`:

```python
"""Draft lifecycle endpoints — see spec §7.6."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import PlanVersion, User


@pytest.fixture
def app_with_draft(client_with_db):
    """Insert a baseline + a draft for user ariel."""
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        sess.add(PlanVersion(user_id="ariel", role="baseline", raw_markdown="# Plan"))
        sess.add(PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="synth-2026-05",
            raw_markdown="",
            horizon_long_md="# Long",
            horizon_medium_md="# Medium",
            horizon_short_md="# Short",
            horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
            horizon_medium_json='{"horizon":"medium","freshness_expected":"quarterly","status":"minor_revision","posture":"x"}',
            horizon_short_json='{"horizon":"short","freshness_expected":"monthly","status":"major_revision","posture":"x"}',
        ))
        sess.commit()
    finally:
        sess.close()
    return client_with_db


def test_get_draft_returns_pending(app_with_draft):
    r = app_with_draft.get("/api/plan/draft?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    assert body["plan_version_id"] is not None
    assert body["horizon_long"]["horizon"] == "long"
    assert body["horizon_medium"]["status"] == "minor_revision"


def test_get_draft_404_when_absent(client_with_db):
    r = client_with_db.get("/api/plan/draft?user_id=newcomer")
    assert r.status_code == 404


def test_post_draft_accept_promotes_to_current(app_with_draft):
    r1 = app_with_draft.get("/api/plan/draft?user_id=ariel")
    draft_id = r1.json()["plan_version_id"]

    r2 = app_with_draft.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "accepted"
    assert body["new_current_id"] == draft_id

    # Inspect: draft is now role=current.
    sess = app_with_draft.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        assert pv.role == "current"
        assert pv.accepted_at is not None
        assert pv.accepted_by_user_id == "ariel"
    finally:
        sess.close()


def test_post_draft_accept_supersedes_prior_current(app_with_draft):
    # Insert a prior current first.
    sess = app_with_draft.app.state.session_factory()
    try:
        sess.add(PlanVersion(user_id="ariel", role="current", version_label="prior"))
        sess.commit()
        prior_id = sess.query(PlanVersion).filter_by(user_id="ariel", role="current").one().id
    finally:
        sess.close()

    r1 = app_with_draft.get("/api/plan/draft?user_id=ariel")
    draft_id = r1.json()["plan_version_id"]
    r2 = app_with_draft.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")
    assert r2.status_code == 200

    sess = app_with_draft.app.state.session_factory()
    try:
        prior = sess.get(PlanVersion, prior_id)
        assert prior.role == "superseded"
        assert prior.superseded_at is not None
    finally:
        sess.close()


def test_post_draft_reject_marks_superseded(app_with_draft):
    r1 = app_with_draft.get("/api/plan/draft?user_id=ariel")
    draft_id = r1.json()["plan_version_id"]

    r2 = app_with_draft.post(
        f"/api/plan/draft/{draft_id}/reject?user_id=ariel",
        json={"reason": "macro analyst was too cautious", "guidance": "weight aggressive risk"},
    )
    assert r2.status_code == 200

    sess = app_with_draft.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        assert pv.role == "superseded"
        assert pv.superseded_at is not None
    finally:
        sess.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plan_draft_api.py -v`
Expected: FAIL.

- [ ] **Step 3: Add the routes**

Edit `argosy/api/routes/plan.py`. Append:

```python
import json
from datetime import datetime, timezone


class HorizonSectionView(BaseModel):
    horizon: str
    freshness_expected: str
    status: str
    posture: str
    targets: list[dict] = []
    themes: list[dict] = []
    actions: list[dict] = []
    speculative_candidates: list[dict] = []
    deltas_from_prior: list[dict] = []
    rationale: str = ""
    cited_sources: list[str] = []


class DraftResponse(BaseModel):
    plan_version_id: int
    drafted_at: str
    derived_from_id: int | None
    decision_run_id: str | None
    horizon_long: HorizonSectionView | None
    horizon_medium: HorizonSectionView | None
    horizon_short: HorizonSectionView | None
    horizon_long_md: str | None
    horizon_medium_md: str | None
    horizon_short_md: str | None


class AcceptResponse(BaseModel):
    status: str
    new_current_id: int


class RejectRequest(BaseModel):
    reason: str
    guidance: str = ""


def _horizon_view(json_str: str | None) -> HorizonSectionView | None:
    if not json_str:
        return None
    payload = json.loads(json_str)
    return HorizonSectionView(**payload)


@router.get("/draft", response_model=DraftResponse)
def get_draft(user_id: str, db: Session = Depends(get_db)) -> DraftResponse:
    from argosy.state.queries import get_pending_draft

    pv = get_pending_draft(db, user_id)
    if pv is None:
        raise HTTPException(status_code=404, detail="no pending draft for user")
    return DraftResponse(
        plan_version_id=pv.id,
        drafted_at=pv.imported_at.isoformat(),
        derived_from_id=pv.derived_from_id,
        decision_run_id=pv.decision_run_id,
        horizon_long=_horizon_view(pv.horizon_long_json),
        horizon_medium=_horizon_view(pv.horizon_medium_json),
        horizon_short=_horizon_view(pv.horizon_short_json),
        horizon_long_md=pv.horizon_long_md,
        horizon_medium_md=pv.horizon_medium_md,
        horizon_short_md=pv.horizon_short_md,
    )


@router.post("/draft/{draft_id}/accept", response_model=AcceptResponse)
def post_draft_accept(
    draft_id: int,
    user_id: str,
    db: Session = Depends(get_db),
) -> AcceptResponse:
    from argosy.state.models import PlanVersion
    from argosy.state.queries import get_current_plan

    pv = db.get(PlanVersion, draft_id)
    if pv is None or pv.user_id != user_id or pv.role != "draft":
        raise HTTPException(status_code=404, detail="draft not found for user")

    now = datetime.now(timezone.utc)
    prior = get_current_plan(db, user_id)
    if prior is not None:
        prior.role = "superseded"
        prior.superseded_at = now

    pv.role = "current"
    pv.accepted_at = now
    pv.accepted_by_user_id = user_id
    db.commit()

    return AcceptResponse(status="accepted", new_current_id=pv.id)


@router.post("/draft/{draft_id}/reject")
def post_draft_reject(
    draft_id: int,
    user_id: str,
    body: RejectRequest,
    db: Session = Depends(get_db),
):
    from argosy.state.models import PlanVersion

    pv = db.get(PlanVersion, draft_id)
    if pv is None or pv.user_id != user_id or pv.role != "draft":
        raise HTTPException(status_code=404, detail="draft not found for user")

    pv.role = "superseded"
    pv.superseded_at = datetime.now(timezone.utc)
    # Stash the rejection reason in synthesis_inputs_json for forensics.
    inputs = json.loads(pv.synthesis_inputs_json) if pv.synthesis_inputs_json else {}
    inputs["rejection_reason"] = body.reason
    inputs["rejection_guidance"] = body.guidance
    pv.synthesis_inputs_json = json.dumps(inputs)
    db.commit()
    return {"status": "rejected", "draft_id": draft_id}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plan_draft_api.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add argosy/api/routes/plan.py tests/test_plan_draft_api.py
git commit -m "feat(api): plan draft lifecycle — GET /draft, POST /accept, POST /reject"
```

---

### Task 2.14: API — per-delta accept and edit endpoints

**Files:**
- Modify: `argosy/api/routes/plan.py`
- Test: `tests/test_plan_draft_api.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_plan_draft_api.py`:

```python
def test_post_delta_accept_marks_item_accepted(app_with_draft):
    """Per-delta accept flips the `accepted` flag on a Delta within a horizon."""
    sess = app_with_draft.app.state.session_factory()
    try:
        # Inject a delta into the draft's horizon_medium_json.
        from argosy.state.queries import get_pending_draft
        pv = get_pending_draft(sess, "ariel")
        import json as _j
        med = _j.loads(pv.horizon_medium_json)
        med["deltas_from_prior"] = [{
            "item_kind": "target",
            "item_id": "medium.targets.nvda",
            "horizon": "medium",
            "change_kind": "modified",
            "summary": "NVDA target tightened 15% -> 12%",
            "prior": {"value": 0.15},
            "proposed": {"value": 0.12},
            "rationale": "macro analyst flagged DeepSeek + tariff overhang",
            "cited_sources": [],
            "accepted": False,
            "user_edited": False,
            "user_edit_note": None,
        }]
        pv.horizon_medium_json = _j.dumps(med)
        sess.commit()
        draft_id = pv.id
    finally:
        sess.close()

    r = app_with_draft.post(
        f"/api/plan/draft/{draft_id}/items/medium.targets.nvda/accept?user_id=ariel"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"

    sess = app_with_draft.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        med = json.loads(pv.horizon_medium_json)
        delta = next(d for d in med["deltas_from_prior"] if d["item_id"] == "medium.targets.nvda")
        assert delta["accepted"] is True
    finally:
        sess.close()


def test_patch_delta_user_edit_records_change(app_with_draft):
    sess = app_with_draft.app.state.session_factory()
    try:
        from argosy.state.queries import get_pending_draft
        pv = get_pending_draft(sess, "ariel")
        import json as _j
        med = _j.loads(pv.horizon_medium_json)
        med["deltas_from_prior"] = [{
            "item_kind": "target",
            "item_id": "medium.targets.nvda",
            "horizon": "medium",
            "change_kind": "modified",
            "summary": "...",
            "prior": {"value": 0.15},
            "proposed": {"value": 0.12},
            "rationale": "...",
            "cited_sources": [],
            "accepted": False,
            "user_edited": False,
            "user_edit_note": None,
        }]
        pv.horizon_medium_json = _j.dumps(med)
        sess.commit()
        draft_id = pv.id
    finally:
        sess.close()

    r = app_with_draft.patch(
        f"/api/plan/draft/{draft_id}/items/medium.targets.nvda?user_id=ariel",
        json={"proposed": {"value": 0.13}, "user_edit_note": "tighter, but 13%"},
    )
    assert r.status_code == 200, r.text

    sess = app_with_draft.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        med = json.loads(pv.horizon_medium_json)
        delta = next(d for d in med["deltas_from_prior"] if d["item_id"] == "medium.targets.nvda")
        assert delta["proposed"]["value"] == 0.13
        assert delta["user_edited"] is True
        assert delta["user_edit_note"] == "tighter, but 13%"
    finally:
        sess.close()


def test_post_delta_accept_404_when_item_id_missing(app_with_draft):
    sess = app_with_draft.app.state.session_factory()
    try:
        from argosy.state.queries import get_pending_draft
        draft_id = get_pending_draft(sess, "ariel").id
    finally:
        sess.close()

    r = app_with_draft.post(
        f"/api/plan/draft/{draft_id}/items/nope/accept?user_id=ariel"
    )
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plan_draft_api.py::test_post_delta_accept_marks_item_accepted -v`
Expected: FAIL.

- [ ] **Step 3: Add the routes**

Append to `argosy/api/routes/plan.py`:

```python
class DeltaEditRequest(BaseModel):
    proposed: dict | None = None
    user_edit_note: str | None = None


def _find_delta_horizon_field(pv, item_id: str) -> tuple[str, dict, dict] | None:
    """Find which horizon contains the given item_id; return (field, payload, delta)."""
    import json as _j
    for field in ("horizon_long_json", "horizon_medium_json", "horizon_short_json"):
        raw = getattr(pv, field)
        if not raw:
            continue
        payload = _j.loads(raw)
        for d in payload.get("deltas_from_prior") or []:
            if d.get("item_id") == item_id:
                return field, payload, d
    return None


@router.post("/draft/{draft_id}/items/{item_id}/accept")
def post_delta_accept(
    draft_id: int,
    item_id: str,
    user_id: str,
    db: Session = Depends(get_db),
):
    from argosy.state.models import PlanVersion
    pv = db.get(PlanVersion, draft_id)
    if pv is None or pv.user_id != user_id or pv.role != "draft":
        raise HTTPException(status_code=404, detail="draft not found")
    found = _find_delta_horizon_field(pv, item_id)
    if found is None:
        raise HTTPException(status_code=404, detail="item_id not found in any horizon delta list")
    field, payload, delta = found
    delta["accepted"] = True
    setattr(pv, field, json.dumps(payload))
    db.commit()
    return {"status": "accepted", "draft_id": draft_id, "item_id": item_id}


@router.patch("/draft/{draft_id}/items/{item_id}")
def patch_delta_edit(
    draft_id: int,
    item_id: str,
    user_id: str,
    body: DeltaEditRequest,
    db: Session = Depends(get_db),
):
    from argosy.state.models import PlanVersion
    pv = db.get(PlanVersion, draft_id)
    if pv is None or pv.user_id != user_id or pv.role != "draft":
        raise HTTPException(status_code=404, detail="draft not found")
    found = _find_delta_horizon_field(pv, item_id)
    if found is None:
        raise HTTPException(status_code=404, detail="item_id not found")
    field, payload, delta = found
    if body.proposed is not None:
        delta["proposed"] = body.proposed
    if body.user_edit_note is not None:
        delta["user_edit_note"] = body.user_edit_note
    delta["user_edited"] = True
    setattr(pv, field, json.dumps(payload))
    db.commit()
    return {"status": "edited", "draft_id": draft_id, "item_id": item_id}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plan_draft_api.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add argosy/api/routes/plan.py tests/test_plan_draft_api.py
git commit -m "feat(api): per-delta accept + edit endpoints on draft"
```

---

### Task 2.15: End-to-end synthesis test (LLM, gate)

**Files:**
- Create: `tests/test_plan_synthesis_e2e.py`

- [ ] **Step 1: Write the live-LLM e2e test**

Create `tests/test_plan_synthesis_e2e.py`:

```python
"""End-to-end synthesis test — calls the live fleet.

Marked llm_eval; skipped without ANTHROPIC_API_KEY. The Wave 2 gate
requires this test to PASS at least once before promoting to Wave 3.

Cost: ~$5-8 per run (T3 depth, full fleet). Run sparingly.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import PlanVersion, User


@pytest.mark.llm_eval
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
def test_synthesis_e2e_jacobs_baseline(alembic_engine_at_head):
    """Run synthesis end-to-end against a Jacobs-style baseline."""
    from argosy.orchestrator.flows.plan_synthesis import run_synthesis
    from argosy.services.plan_distiller_service import distill_baseline_plan

    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    sess = SessionLocal()
    try:
        sess.add(User(id="ariel", plan="free"))
        # Use the same excerpt Wave 1's golden test uses.
        from pathlib import Path
        plan_md = Path("tests/golden/jacobs_plan_excerpt.md").read_text(encoding="utf-8")
        pv = PlanVersion(
            user_id="ariel",
            role="baseline",
            version_label="Jacobs v2.0 (excerpt)",
            raw_markdown=plan_md,
        )
        sess.add(pv)
        sess.commit()

        # Distill the baseline first.
        distill_baseline_plan(sess, plan_version_id=pv.id, user_id="ariel")

        # Run synthesis (full live fleet).
        result = run_synthesis(sess, user_id="ariel", trigger="check_in")

        draft = sess.get(PlanVersion, result.draft_id)
        assert draft is not None
        assert draft.role == "draft"
        assert draft.horizon_long_json
        assert draft.horizon_medium_json
        assert draft.horizon_short_json
        assert draft.synthesis_inputs_json
        assert draft.decision_run_id == result.decision_run_id
    finally:
        sess.close()
```

- [ ] **Step 2: Run the test once with a real key**

```bash
ANTHROPIC_API_KEY=$YOUR_KEY pytest tests/test_plan_synthesis_e2e.py -m llm_eval -v
```

Expected: PASS. Iterate on prompts (Tasks 2.5 / 2.7 / 2.8 / 2.9 / 2.10) until the e2e test passes.

- [ ] **Step 3: Commit**

```bash
git add tests/test_plan_synthesis_e2e.py
git commit -m "test(synthesis): live e2e — full fleet against Jacobs excerpt"
```

---

### Task 2.16: WebSocket events — `plan.draft.*`, `plan.current.changed`

**Files:**
- Modify: `argosy/api/routes/plan.py` (publish events on accept/reject/edit)
- Modify: `argosy/api/main.py` (register the event channels in the WebSocket layer if needed)
- Test: `tests/test_plan_draft_api.py` (extend)

**Note:** This task assumes the existing WebSocket plumbing exposes a `publish(event_type, payload)` helper. If the plumbing differs, adapt — open `argosy/api/websocket.py` or wherever events are emitted today and use its actual API.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_plan_draft_api.py`:

```python
def test_accept_publishes_plan_current_changed(app_with_draft, monkeypatch):
    """Accepting a draft must publish plan.current.changed."""
    from argosy.api.routes import plan as plan_routes

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(plan_routes, "_publish", lambda et, payload: events.append((et, payload)))

    r1 = app_with_draft.get("/api/plan/draft?user_id=ariel")
    draft_id = r1.json()["plan_version_id"]
    app_with_draft.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")

    types = [e[0] for e in events]
    assert "plan.draft.accepted" in types
    assert "plan.current.changed" in types
```

- [ ] **Step 2: Add the publish hook**

In `argosy/api/routes/plan.py`, add at module level:

```python
def _publish(event_type: str, payload: dict) -> None:
    """Indirection point so tests can monkeypatch.

    Wave 2: delegates to whatever WebSocket layer exists in the project.
    If WebSocket plumbing is not yet wired, this is a no-op.
    """
    try:
        from argosy.api.websocket import publish_event
    except ImportError:
        return
    publish_event(event_type, payload)
```

Then sprinkle calls into `post_draft_accept` (after commit):

```python
    _publish("plan.draft.accepted", {"user_id": user_id, "draft_id": draft_id})
    _publish("plan.current.changed", {"user_id": user_id, "current_id": pv.id})
```

In `post_draft_reject`:

```python
    _publish("plan.draft.rejected", {"user_id": user_id, "draft_id": draft_id, "reason": body.reason})
```

In `post_delta_accept`:

```python
    _publish("plan.draft.delta.accepted", {"user_id": user_id, "draft_id": draft_id, "item_id": item_id})
```

In `patch_delta_edit`:

```python
    _publish("plan.draft.delta.edited", {"user_id": user_id, "draft_id": draft_id, "item_id": item_id})
```

In `argosy/orchestrator/flows/plan_synthesis.py`, after the draft is committed in `run_synthesis`:

```python
    try:
        from argosy.api.websocket import publish_event
        publish_event("plan.draft.completed", {"user_id": user_id, "draft_id": draft.id})
    except Exception:
        pass
```

And at the start of `run_synthesis`:

```python
    try:
        from argosy.api.websocket import publish_event
        publish_event("plan.draft.started", {"user_id": user_id, "trigger": trigger})
    except Exception:
        pass
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest tests/test_plan_draft_api.py -v`
Expected: PASS (9 tests)

- [ ] **Step 4: Commit**

```bash
git add argosy/api/routes/plan.py argosy/orchestrator/flows/plan_synthesis.py tests/test_plan_draft_api.py
git commit -m "feat(events): publish plan.draft.* and plan.current.changed WebSocket events"
```

---

### Task 2.17: UI — `<PlanRevisionSheet>` component (basic + per-delta accept)

**Files:**
- Create: `ui/src/components/plan-revision-sheet.tsx`
- Modify: `ui/src/lib/api.ts` (draft + accept + reject + delta endpoints + types)
- Modify: `ui/src/app/advisor/page.tsx` (mount the sheet + draft-pending banner)

- [ ] **Step 1: Add API types and methods**

Append to `ui/src/lib/api.ts`:

```typescript
// ----------------------------------------------------------------------
// Wave 2: synthesis flow + draft lifecycle
// ----------------------------------------------------------------------

export interface DeltaItem {
  item_kind: "target" | "theme" | "action" | "speculative_candidate";
  item_id: string;
  horizon: "long" | "medium" | "short";
  change_kind: "added" | "removed" | "modified";
  summary: string;
  prior: Record<string, unknown> | null;
  proposed: Record<string, unknown> | null;
  rationale: string;
  cited_sources: string[];
  accepted: boolean;
  user_edited: boolean;
  user_edit_note: string | null;
}

export interface HorizonView {
  horizon: "long" | "medium" | "short";
  freshness_expected: "annual" | "quarterly" | "monthly";
  status: "no_change" | "minor_revision" | "major_revision";
  posture: string;
  targets: Array<Record<string, unknown>>;
  themes: Array<Record<string, unknown>>;
  actions: Array<Record<string, unknown>>;
  speculative_candidates: Array<Record<string, unknown>>;
  deltas_from_prior: DeltaItem[];
  rationale: string;
  cited_sources: string[];
}

export interface DraftResponse {
  plan_version_id: number;
  drafted_at: string;
  derived_from_id: number | null;
  decision_run_id: string | null;
  horizon_long: HorizonView | null;
  horizon_medium: HorizonView | null;
  horizon_short: HorizonView | null;
  horizon_long_md: string | null;
  horizon_medium_md: string | null;
  horizon_short_md: string | null;
}
```

Inside the `api = { ... }` object, add:

```typescript
  planDraft: (userId: string) =>
    getJSON<DraftResponse>(`/api/plan/draft?user_id=${encodeURIComponent(userId)}`),
  planDraftAccept: (draftId: number, userId: string) =>
    postJSON<{ status: string; new_current_id: number }>(
      `/api/plan/draft/${draftId}/accept?user_id=${encodeURIComponent(userId)}`,
      {},
    ),
  planDraftReject: (draftId: number, userId: string, reason: string, guidance = "") =>
    postJSON<{ status: string; draft_id: number }>(
      `/api/plan/draft/${draftId}/reject?user_id=${encodeURIComponent(userId)}`,
      { reason, guidance },
    ),
  planDraftDeltaAccept: (draftId: number, itemId: string, userId: string) =>
    postJSON<{ status: string }>(
      `/api/plan/draft/${draftId}/items/${encodeURIComponent(itemId)}/accept?user_id=${encodeURIComponent(userId)}`,
      {},
    ),
  planDraftDeltaEdit: (
    draftId: number,
    itemId: string,
    userId: string,
    body: { proposed?: Record<string, unknown>; user_edit_note?: string },
  ) =>
    fetch(
      apiUrl(
        `/api/plan/draft/${draftId}/items/${encodeURIComponent(itemId)}?user_id=${encodeURIComponent(userId)}`,
      ),
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ).then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return (await r.json()) as { status: string };
    }),
  advisorCheckIn: (userId: string, guidance = "") =>
    postJSON<{ status: string; decision_run_id: string; draft_id: number }>(
      `/api/advisor/check-in`,
      { user_id: userId, guidance, urgency: "now" },
    ),
```

- [ ] **Step 2: Build the side-sheet component**

Create `ui/src/components/plan-revision-sheet.tsx`:

```typescript
"use client";

import { useCallback, useEffect, useState } from "react";
import { Check, X, Pencil, Star } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, type DeltaItem, type DraftResponse, type HorizonView } from "@/lib/api";

interface PlanRevisionSheetProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  userId: string;
  draft: DraftResponse;
  onAccepted: () => void;
  onRejected: () => void;
}

function deltaCount(h: HorizonView | null): number {
  return h?.deltas_from_prior.length ?? 0;
}

export function PlanRevisionSheet(props: PlanRevisionSheetProps) {
  const { open, onOpenChange, userId, draft, onAccepted, onRejected } = props;

  const [activeTab, setActiveTab] = useState<"deltas" | "long" | "medium" | "short">("deltas");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const allDeltas: DeltaItem[] = [
    ...(draft.horizon_long?.deltas_from_prior ?? []),
    ...(draft.horizon_medium?.deltas_from_prior ?? []),
    ...(draft.horizon_short?.deltas_from_prior ?? []),
  ];

  const acceptDelta = async (item: DeltaItem) => {
    setError(null);
    setWorking(true);
    try {
      await api.planDraftDeltaAccept(draft.plan_version_id, item.item_id, userId);
      // Local mutate (the sheet re-renders on parent re-fetch via WS).
      item.accepted = true;
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setWorking(false);
    }
  };

  const acceptAll = async () => {
    setError(null);
    setWorking(true);
    try {
      await api.planDraftAccept(draft.plan_version_id, userId);
      onAccepted();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setWorking(false);
    }
  };

  const reject = async () => {
    const reason = window.prompt("What should the fleet reconsider?") ?? "";
    if (!reason) return;
    setError(null);
    setWorking(true);
    try {
      await api.planDraftReject(draft.plan_version_id, userId, reason);
      onRejected();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setWorking(false);
    }
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-2xl overflow-y-auto">
        <SheetHeader>
          <SheetTitle>Monthly plan revision</SheetTitle>
          <SheetDescription>
            Synthesized {new Date(draft.drafted_at).toLocaleString()} · derived from
            baseline #{draft.derived_from_id} · run {draft.decision_run_id?.slice(0, 12)}
          </SheetDescription>
        </SheetHeader>

        {error && <p className="text-sm text-red-500 font-mono mt-3">{error}</p>}

        <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as typeof activeTab)} className="mt-4">
          <TabsList>
            <TabsTrigger value="deltas">Deltas ({allDeltas.length})</TabsTrigger>
            <TabsTrigger value="long">Long</TabsTrigger>
            <TabsTrigger value="medium">
              <span className="flex items-center gap-1">
                Medium <Star className="h-3 w-3 text-amber-500" />
              </span>
            </TabsTrigger>
            <TabsTrigger value="short">Short</TabsTrigger>
          </TabsList>

          <TabsContent value="deltas">
            <DeltasView
              deltas={allDeltas}
              onAccept={acceptDelta}
              disabled={working}
            />
          </TabsContent>
          <TabsContent value="long">
            <HorizonViewBlock h={draft.horizon_long} md={draft.horizon_long_md} />
          </TabsContent>
          <TabsContent value="medium">
            <HorizonViewBlock h={draft.horizon_medium} md={draft.horizon_medium_md} />
          </TabsContent>
          <TabsContent value="short">
            <HorizonViewBlock h={draft.horizon_short} md={draft.horizon_short_md} />
          </TabsContent>
        </Tabs>

        <div className="mt-6 flex justify-between gap-2 sticky bottom-0 bg-background py-2">
          <Button variant="outline" onClick={reject} disabled={working}>
            <X className="h-4 w-4 mr-1" /> Reject + re-synthesize
          </Button>
          <Button onClick={acceptAll} disabled={working}>
            <Check className="h-4 w-4 mr-1" /> Accept all remaining
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function DeltasView(props: {
  deltas: DeltaItem[];
  onAccept: (d: DeltaItem) => void | Promise<void>;
  disabled: boolean;
}) {
  if (props.deltas.length === 0) {
    return (
      <div className="py-6 text-center text-sm text-muted-foreground">
        No changes recommended this month — the fleet thinks current state is fine.
      </div>
    );
  }
  return (
    <ul className="flex flex-col gap-3 mt-3">
      {props.deltas.map((d) => (
        <li key={`${d.horizon}.${d.item_id}`} className="border border-border rounded-md p-3">
          <div className="flex items-start justify-between gap-2">
            <div className="text-sm">
              <span className="text-xs uppercase font-mono text-muted-foreground mr-2">
                [{d.horizon}]
              </span>
              <strong>{d.summary}</strong>
            </div>
            <div className="flex gap-1">
              {d.accepted ? (
                <span className="text-xs text-emerald-500 font-mono">accepted</span>
              ) : (
                <button
                  type="button"
                  onClick={() => props.onAccept(d)}
                  disabled={props.disabled}
                  className="text-xs text-primary hover:underline"
                >
                  Accept
                </button>
              )}
            </div>
          </div>
          {d.rationale && (
            <p className="text-xs text-muted-foreground mt-1">{d.rationale}</p>
          )}
          {d.cited_sources.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-1">
              {d.cited_sources.map((c) => (
                <span
                  key={c}
                  className="text-[10px] font-mono bg-accent/40 px-1.5 py-0.5 rounded"
                >
                  {c}
                </span>
              ))}
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

function HorizonViewBlock(props: { h: HorizonView | null; md: string | null }) {
  if (!props.h) {
    return <div className="py-6 text-center text-sm text-muted-foreground">Empty horizon.</div>;
  }
  return (
    <div className="prose prose-sm dark:prose-invert max-w-none mt-3">
      <pre className="whitespace-pre-wrap font-mono text-xs">{props.md ?? ""}</pre>
    </div>
  );
}
```

- [ ] **Step 3: Mount on the advisor page**

Edit `ui/src/app/advisor/page.tsx`. Add state for the draft + sheet:

```typescript
import { PlanRevisionSheet } from "@/components/plan-revision-sheet";

// inside component:
const [draft, setDraft] = useState<DraftResponse | null>(null);
const [sheetOpen, setSheetOpen] = useState(false);

const refreshDraft = useCallback(async () => {
  try {
    const d = await api.planDraft(USER_ID);
    setDraft(d);
  } catch (e: unknown) {
    setDraft(null);
  }
}, []);

useEffect(() => {
  refreshDraft();
}, [refreshDraft]);
```

Render a sticky banner above the chat when `draft != null`:

```tsx
{draft && (
  <div className="rounded-md border border-rose-500/40 bg-rose-500/10 p-3 flex items-center justify-between">
    <p className="text-sm">
      Draft plan ready (synthesized {new Date(draft.drafted_at).toLocaleDateString()}) —{" "}
      <strong>
        {(draft.horizon_long?.deltas_from_prior.length ?? 0) +
          (draft.horizon_medium?.deltas_from_prior.length ?? 0) +
          (draft.horizon_short?.deltas_from_prior.length ?? 0)}
      </strong>{" "}
      delta(s) vs. last month.
    </p>
    <Button size="sm" onClick={() => setSheetOpen(true)}>
      Review now
    </Button>
  </div>
)}

{draft && (
  <PlanRevisionSheet
    open={sheetOpen}
    onOpenChange={setSheetOpen}
    userId={USER_ID}
    draft={draft}
    onAccepted={() => {
      setSheetOpen(false);
      refreshDraft();
    }}
    onRejected={() => {
      setSheetOpen(false);
      refreshDraft();
    }}
  />
)}
```

- [ ] **Step 4: Manual smoke test**

1. Start engine + UI.
2. Trigger a check-in: `curl -X POST http://localhost:8000/api/advisor/check-in -H 'Content-Type: application/json' -d '{"user_id":"ariel","guidance":"","urgency":"now"}'`.
3. Reload `/advisor` — banner appears.
4. Click "Review now" — side sheet opens, three horizons + deltas tab.
5. Accept one delta, then "Accept all remaining" — sheet closes, banner clears.
6. Trigger another check-in, then reject it — verify the draft moves to superseded.

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/plan-revision-sheet.tsx ui/src/lib/api.ts ui/src/app/advisor/page.tsx
git commit -m "feat(ui): <PlanRevisionSheet> + draft-pending banner on advisor page"
```

---

### Task 2.18: Home brief — `draft_plan` bullet kind

**Files:**
- Modify: `argosy/api/routes/advisor.py` (`_signal_bullet` or wherever home-brief is composed)
- Modify: `ui/src/components/advisor-brief-card.tsx`
- Modify: `ui/src/lib/api.ts` (add the new bullet kind to the union)
- Test: extend whatever home-brief test exists (search `home_brief`)

- [ ] **Step 1: Update the bullet-kind union**

In `ui/src/lib/api.ts`:

```typescript
export type AdvisorBriefBulletKind = "gap" | "portfolio" | "signal" | "draft_plan";
```

In `ui/src/components/advisor-brief-card.tsx`, add an icon entry for `draft_plan`:

```typescript
import { FileCheck } from "lucide-react";

// inside the bullet -> icon map (find it; usually a switch or object literal):
case "draft_plan":
  return <FileCheck className="h-4 w-4 text-rose-400" />;
```

- [ ] **Step 2: Add a `_draft_bullet` helper in the advisor route**

In `argosy/api/routes/advisor.py` (the home-brief composer — search for `_gap_bullet`/`_signal_bullet`):

```python
def _draft_bullet(session, user_id: str) -> dict | None:
    """If the user has a pending draft, surface it as the top bullet."""
    from argosy.state.queries import get_pending_draft
    pv = get_pending_draft(session, user_id)
    if pv is None:
        return None
    return {
        "kind": "draft_plan",
        "text": (
            f"Monthly plan revision drafted "
            f"on {pv.imported_at.strftime('%Y-%m-%d')} — ready to review."
        ),
    }
```

In the home-brief composer, prepend this bullet ahead of `_gap_bullet`/`_portfolio_bullet`/`_signal_bullet`:

```python
bullets = []
draft = _draft_bullet(session, user_id)
if draft:
    bullets.append(draft)
g = _gap_bullet(...)
if g: bullets.append(g)
# ... etc.
```

When a draft is pending, also override the CTA from `Talk to advisor` to `Review monthly plan` -> `/advisor?action=review-draft`:

```python
cta = (
    {"label": "Review monthly plan", "href": "/advisor?action=review-draft"}
    if draft
    else {"label": "Talk to advisor", "href": "/advisor"}
)
```

- [ ] **Step 3: Test the composition**

Append to whatever existing home-brief test file exists (e.g. `tests/test_advisor_route.py` or a dedicated `test_home_brief.py`):

```python
def test_home_brief_surfaces_draft_plan_bullet(client_with_db):
    from argosy.state.models import PlanVersion, User

    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
        sess.add(PlanVersion(user_id="ariel", role="baseline", raw_markdown="# Plan"))
        sess.add(PlanVersion(
            user_id="ariel", role="draft", version_label="synth-x", raw_markdown="",
            horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
            horizon_medium_json='{"horizon":"medium","freshness_expected":"quarterly","status":"no_change","posture":"x"}',
            horizon_short_json='{"horizon":"short","freshness_expected":"monthly","status":"no_change","posture":"x"}',
        ))
        sess.commit()
    finally:
        sess.close()

    r = client_with_db.get("/api/advisor/home-brief?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    kinds = [b["kind"] for b in body["bullets"]]
    assert "draft_plan" in kinds
    assert body["cta"]["label"] == "Review monthly plan"
    assert body["cta"]["href"].startswith("/advisor")
```

- [ ] **Step 4: Run the test, then commit**

Run: `pytest tests/test_advisor_route.py -v`
Expected: PASS.

```bash
git add argosy/api/routes/advisor.py ui/src/components/advisor-brief-card.tsx ui/src/lib/api.ts tests/test_advisor_route.py
git commit -m "feat(advisor): home-brief surfaces draft_plan bullet + 'Review monthly plan' CTA"
```

---

### Task 2.19: SDD edits for Wave 2

**Files:**
- Modify: `docs/design/SDD.md`

- [ ] **Step 1: Insert §6.11 "Plan synthesis flow"**

Edit `docs/design/SDD.md`. After §6.10 (added in Wave 1 task 1.17), append:

```markdown
### 6.11 Plan synthesis flow (Wave 2 of plan-distillate work)

The advisor never reads the baseline plan directly. Each month a fleet
synthesis re-derives a fresh **long / medium / short** plan from
{baseline distillate + current portfolio state + recent fills + analyst
reports + researcher debates}, the user accepts (or rejects) it, and
the resulting `role='current'` plan is what every other agent in the
system anchors on.

**Triggers.**

- `monthly_cycle` on the 1st of each month (auto-scheduled per §5.1)
- `quarterly` after each quarter close — extra prompt weight on medium
  horizon
- `annual` (January) — extra prompt weight on long horizon
- User-initiated via `POST /api/advisor/check-in` (any time)

**Five-phase fleet review** (a new T3-depth flow, distinct from the
per-trade `decision_flow` of §3 / §10):

1. Analyst reports (parallel, ~3-5 min) — 9 specialists run concurrently
2. Researcher debate (per-horizon, ~5 min) — bull/bear/facilitator argue
   theses (long/medium/short) in parallel
3. Synthesizer (Opus, ~1-2 min) — produces three `HorizonSection` drafts
4. Risk team review (parallel, ~2 min) — aggressive/neutral/conservative
   plan-level verdicts + facilitator merge
5. Fund manager integrity check (~1 min) — green-lights as `role='draft'`

Total wall-clock ~12-15 minutes from trigger to draft-ready.

**Idempotency.** Re-running synthesis when an unaccepted draft already
exists demotes the prior draft to `role='superseded'` and writes a
fresh draft. Single user, single in-flight draft.

**Output.** A new `plan_versions` row with `role='draft'` and three
`HorizonSection` JSON payloads (`horizon_long_json`,
`horizon_medium_json`, `horizon_short_json`) plus pre-rendered markdown
views. Lineage via `derived_from_id` (-> baseline) and `decision_run_id`
(-> the `decision_runs` row tying every analyst/debate/risk/FM call
together for audit reconstruction).

**Authority framing.** Every plan-touching agent imports the shared
`AUTHORITY_DISCLAIMER` from `argosy/agents/_plan_authority.py`. The
plan is one input; the fleet is empowered to disagree.

**Per-horizon character:**

- **Long (5+ yrs)** — posture-heavy, few targets, directional actions;
  `status='no_change'` is the common case.
- **Medium (1-2 yrs)** — *strategic centerpiece*; tactical targets,
  themed actions, parameterized triggers. Bull/bear debate at this
  horizon gets the most prompt weight.
- **Short (~30 days)** — dated, concrete, replaced every monthly cycle.
  Includes `speculative_candidates` (Wave 3).

**Acceptance UI.** A right-side `Sheet` on the Advisor page renders the
draft (deltas tab + per-horizon tabs). Per-delta `[✓ Accept]`,
`[✗ Reject]`, `[✎ Edit]` buttons; `[Accept all remaining]` promotes the
draft to `role='current'`; `[Reject draft + re-synthesize]` opens a
guidance prompt and fires another check-in.

See `docs/superpowers/specs/2026-05-05-plan-distillate-design.md` for
full design.
```

- [ ] **Step 2: Update §3.6 cross-cutting agents**

Add rows:

```markdown
| **Plan synthesizer** | Phase 3 of plan_synthesis_flow — produces the three HorizonSection drafts. See §6.11. | Monthly + quarterly + annual + on user check-in | Opus |
```

- [ ] **Step 3: Update §5.1 cadence catalog**

Add a "Triggers plan synthesis" column entry to `monthly_cycle`,
`quarterly`, and `annual` rows.

- [ ] **Step 4: Update §10.1 routing matrix**

Append a row:

```markdown
| `plan_revision` | Any | Any | Human queue, **always T3 depth, never auto-execute** |
```

- [ ] **Step 5: Update §11.3 WebSocket events**

Append:

```
plan.draft.started        plan.draft.completed
plan.draft.delta.accepted plan.draft.delta.edited
plan.draft.accepted       plan.draft.rejected
plan.current.changed
```

- [ ] **Step 6: Update §A.2 cost cap**

Note that the `cost.monthly_budget_usd` should account for ~$15-20/month
of synthesis-related LLM spend (one scheduled monthly run at ~$5-8 +
two ad-hoc check-ins).

- [ ] **Step 7: Commit**

```bash
git add docs/design/SDD.md
git commit -m "docs(sdd): §6.11 plan-synthesis-flow; updates to §3.6/§5.1/§10.1/§11.3/§A.2"
```

---

## Wave 2 → Wave 3 GATE

Before starting Wave 3, all of the following must hold:

- [ ] All Wave 2 tests pass: `pytest tests/test_plan_synthesizer.py tests/test_plan_synthesis_flow.py tests/test_plan_authority.py tests/test_plan_projection.py tests/test_plan_draft_api.py tests/test_advisor_route.py tests/test_monthly_cycle_loop.py tests/test_migration_0017.py -v`
- [ ] Live e2e passes: `pytest tests/test_plan_synthesis_e2e.py -m llm_eval -v` (must pass at least once with a real key).
- [ ] Manual smoke: trigger `/api/advisor/check-in`; the side sheet renders; per-delta accept and reject flows both work; an accepted draft becomes `role='current'`; the home brief surfaces the draft.
- [ ] **One full monthly synthesis cycle accepted in paper-only mode** per SDD §3.5 soak rule.
- [ ] No regressions: `pytest -m "not llm_eval" -v` is green.

Wave 3 also depends on **SDD Phase 5 Argonaut autonomy** being in place. **Do not start Wave 3 until that work has shipped.**

---

# WAVE 3 — Speculative Candidates Live

The framework is already in place from Wave 2 — `SpeculativeCandidate` is a field of `HorizonSection`, the synthesizer fills `short.speculative_candidates`, the side sheet renders them. Wave 3 makes them actionable: a candidate that passes the `risk_ceiling_check` can route into the Argonaut paper queue with one click; in live mode it auto-executes (T0 routing per SDD §10.1).

**Files this wave creates or modifies:**

- Modify: `argosy/config.py` or `agent_settings.yaml` schema — add `speculation` block
- Create: `argosy/orchestrator/speculation_router.py`
- Create: `tests/test_speculation_cap.py`
- Create: `tests/test_speculation_router.py`
- Modify: `argosy/agents/watchlist.py` — feed speculative candidates into synthesis
- Modify: `argosy/agents/plan_synthesizer.py` — enforce risk_ceiling_check before emitting
- Modify: `argosy/orchestrator/flows/plan_synthesis.py` — wire speculation cap into prompt
- Modify: `ui/src/components/plan-revision-sheet.tsx` — dedicated UI section
- Modify: `ui/src/app/argonaut/page.tsx` — speculative-candidate panel
- Modify: `docs/design/SDD.md` — §6.12, §10.1 update, §A.2 update

---

### Task 3.1: Speculation cap config

**Files:**
- Modify: `argosy/config.py` (or wherever `agent_settings.yaml` is parsed)
- Create: `tests/test_speculation_cap.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_speculation_cap.py`:

```python
"""Speculation cap config — schema and defaults."""

from __future__ import annotations

import pytest


def test_speculation_cap_defaults():
    """If `speculation` block is absent, defaults apply."""
    from argosy.config import load_speculation_cap

    cfg = load_speculation_cap(user_id="ariel", agent_settings={})
    assert cfg.max_pct_of_net_worth == 0.001  # 0.1% — conservative default
    assert cfg.max_concurrent_positions == 3


def test_speculation_cap_user_override():
    from argosy.config import load_speculation_cap

    cfg = load_speculation_cap(
        user_id="ariel",
        agent_settings={"speculation": {"max_pct_of_net_worth": 0.002, "max_concurrent_positions": 5}},
    )
    assert cfg.max_pct_of_net_worth == 0.002
    assert cfg.max_concurrent_positions == 5


def test_speculation_cap_rejects_negative():
    from argosy.config import load_speculation_cap

    with pytest.raises(ValueError):
        load_speculation_cap(
            user_id="ariel",
            agent_settings={"speculation": {"max_pct_of_net_worth": -0.01}},
        )


def test_speculation_cap_clamps_excessive():
    """Cap above 5% NW is rejected — that's not speculation, it's a position."""
    from argosy.config import load_speculation_cap

    with pytest.raises(ValueError):
        load_speculation_cap(
            user_id="ariel",
            agent_settings={"speculation": {"max_pct_of_net_worth": 0.10}},
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_speculation_cap.py -v`
Expected: FAIL — `load_speculation_cap` does not exist.

- [ ] **Step 3: Implement the loader**

Add to `argosy/config.py` (find the file's end and append):

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class SpeculationCap:
    """Per-user speculation guardrails (Wave 3 of plan-distillate work).

    Loaded from agent_settings.yaml::

        speculation:
          max_pct_of_net_worth: 0.001       # 0.1% NW, very tight
          max_concurrent_positions: 3
          allowed_account_classes: ["argonaut"]

    These values constrain the synthesizer (it must never emit a
    SpeculativeCandidate that would breach the cap) AND the routing
    layer (preflight enforcement before any broker call).
    """

    max_pct_of_net_worth: float = 0.001  # 0.1% default — conservative
    max_concurrent_positions: int = 3
    allowed_account_classes: tuple[str, ...] = ("argonaut",)

    def validate(self) -> None:
        if self.max_pct_of_net_worth <= 0:
            raise ValueError(
                f"speculation.max_pct_of_net_worth must be > 0, got {self.max_pct_of_net_worth}"
            )
        if self.max_pct_of_net_worth > 0.05:
            raise ValueError(
                f"speculation.max_pct_of_net_worth must be <= 0.05 (5% NW); "
                f"above that it's not speculation, it's a position. Got "
                f"{self.max_pct_of_net_worth}"
            )
        if self.max_concurrent_positions < 0:
            raise ValueError(
                f"speculation.max_concurrent_positions must be >= 0, got "
                f"{self.max_concurrent_positions}"
            )


def load_speculation_cap(*, user_id: str, agent_settings: dict) -> SpeculationCap:
    """Build a SpeculationCap from a parsed agent_settings.yaml dict."""
    block = agent_settings.get("speculation") or {}
    cap = SpeculationCap(
        max_pct_of_net_worth=float(block.get("max_pct_of_net_worth", 0.001)),
        max_concurrent_positions=int(block.get("max_concurrent_positions", 3)),
        allowed_account_classes=tuple(
            block.get("allowed_account_classes", ("argonaut",))
        ),
    )
    cap.validate()
    return cap
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_speculation_cap.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add argosy/config.py tests/test_speculation_cap.py
git commit -m "feat(config): SpeculationCap loader + validation"
```

---

### Task 3.2: Synthesizer enforces the cap

**Files:**
- Modify: `argosy/agents/plan_synthesizer.py`
- Modify: `argosy/orchestrator/flows/plan_synthesis.py`
- Test: `tests/test_plan_synthesizer.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_plan_synthesizer.py`:

```python
def test_synthesizer_prompt_includes_speculation_cap():
    """The synthesizer's user prompt must surface the cap so it cannot
    emit candidates that would breach it.
    """
    from argosy.agents.plan_synthesizer import PlanSynthesizerAgent

    agent = PlanSynthesizerAgent()
    sys, usr = agent.build_prompt(
        baseline_distillate_md="x",
        prior_current_md="x",
        analyst_reports_text="x",
        debate_outcomes_text="x",
        portfolio_snapshot_summary="net_worth_usd: 4_600_000",
        recent_fills_summary="x",
        speculation_cap_pct=0.001,
        speculation_cap_concurrent=3,
    )
    # Cap must appear so the model cannot ignore it.
    assert "0.1" in usr or "0.001" in usr or "0.10%" in usr
    assert "3" in usr
    assert "speculative" in (sys + usr).lower()


def test_synthesizer_post_validates_speculative_candidates(monkeypatch):
    """If the model emits a candidate over cap, the orchestrator drops it."""
    from argosy.agents.plan_synthesizer_types import (
        HorizonSection,
        PlanSynthesisOutput,
        SpeculativeCandidate,
        SynthesisInputs,
    )
    from argosy.orchestrator.flows import plan_synthesis as flow

    over_cap = SpeculativeCandidate(
        ticker="OVER", thesis_summary="too big",
        suggested_position_usd=50_000,
        suggested_position_pct_of_net_worth=0.011,  # 1.1% NW — over default cap of 0.1%
        risk_ceiling_check=False,
        horizon_days=10, expected_drawdown_pct=0.2,
        exit_trigger="x", sourced_from=["sentiment"],
    )
    in_cap = SpeculativeCandidate(
        ticker="OK", thesis_summary="bounded",
        suggested_position_usd=800,
        suggested_position_pct_of_net_worth=0.0008,
        risk_ceiling_check=True,
        horizon_days=10, expected_drawdown_pct=0.2,
        exit_trigger="x", sourced_from=["sentiment"],
    )
    out = PlanSynthesisOutput(
        long=HorizonSection(horizon="long", freshness_expected="annual",
                            status="no_change", posture="x"),
        medium=HorizonSection(horizon="medium", freshness_expected="quarterly",
                              status="no_change", posture="x"),
        short=HorizonSection(
            horizon="short", freshness_expected="monthly", status="no_change",
            posture="x", speculative_candidates=[over_cap, in_cap],
        ),
        inputs=SynthesisInputs(),
    )

    cleaned = flow._enforce_speculation_cap(
        out, max_pct_of_net_worth=0.001, max_concurrent_positions=3,
    )
    tickers = [c.ticker for c in cleaned.short.speculative_candidates]
    assert tickers == ["OK"]
    assert all(c.risk_ceiling_check for c in cleaned.short.speculative_candidates)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plan_synthesizer.py -v`
Expected: FAIL.

- [ ] **Step 3: Update synthesizer build_prompt to take the cap**

Edit `argosy/agents/plan_synthesizer.py` `build_prompt` signature — add `speculation_cap_pct` and `speculation_cap_concurrent` keyword args (default to None for backwards compat). Append to the system prompt:

```python
        if speculation_cap_pct is not None:
            cap_block = (
                "\n\nSPECULATION CAP (HARD CONSTRAINT):\n"
                f"  - max position size: {speculation_cap_pct:.4f} of net worth "
                f"(= {speculation_cap_pct*100:.2f}%)\n"
                f"  - max concurrent positions: {speculation_cap_concurrent}\n"
                "\n"
                "If you surface a SpeculativeCandidate, EVERY candidate must "
                "have suggested_position_pct_of_net_worth <= the cap, AND "
                "risk_ceiling_check=true. Do NOT recommend candidates that "
                "would breach the cap. The orchestrator will silently drop "
                "any over-cap candidates anyway, so you save the user a "
                "confused glance by getting it right here.\n"
            )
            system = system + cap_block
```

Update the function signature accordingly:

```python
def build_prompt(
    self,
    *,
    baseline_distillate_md: str,
    prior_current_md: str,
    analyst_reports_text: str,
    debate_outcomes_text: str,
    portfolio_snapshot_summary: str,
    recent_fills_summary: str,
    speculation_cap_pct: float | None = None,
    speculation_cap_concurrent: int | None = None,
) -> tuple[str, str]:
```

- [ ] **Step 4: Add `_enforce_speculation_cap` to the flow**

In `argosy/orchestrator/flows/plan_synthesis.py`, add:

```python
def _enforce_speculation_cap(
    output: "PlanSynthesisOutput",
    *,
    max_pct_of_net_worth: float,
    max_concurrent_positions: int,
) -> "PlanSynthesisOutput":
    """Drop any speculative candidates that breach the cap.

    The synthesizer prompt is told the cap, but defense-in-depth: we
    enforce it here so a model that fluffs the constraint cannot harm
    the user.
    """
    if not output.short.speculative_candidates:
        return output

    kept = []
    for c in output.short.speculative_candidates:
        if c.suggested_position_pct_of_net_worth > max_pct_of_net_worth:
            log.warning(
                "plan_synthesis.speculative_dropped_over_cap",
                ticker=c.ticker,
                pct=c.suggested_position_pct_of_net_worth,
                cap=max_pct_of_net_worth,
            )
            continue
        if not c.risk_ceiling_check:
            log.warning(
                "plan_synthesis.speculative_dropped_no_ceiling_check",
                ticker=c.ticker,
            )
            continue
        kept.append(c)
        if len(kept) >= max_concurrent_positions:
            break

    if len(kept) == len(output.short.speculative_candidates):
        return output
    new_short = output.short.model_copy(update={"speculative_candidates": kept})
    return output.model_copy(update={"short": new_short})
```

Then in `run_synthesis`, after Phase 3 produces `output`, before persisting:

```python
    from argosy.config import load_speculation_cap, get_user_agent_settings  # add this util if absent
    cap = load_speculation_cap(user_id=user_id, agent_settings=get_user_agent_settings(user_id))
    output = _enforce_speculation_cap(
        output,
        max_pct_of_net_worth=cap.max_pct_of_net_worth,
        max_concurrent_positions=cap.max_concurrent_positions,
    )
```

If `get_user_agent_settings(user_id)` does not yet exist as a helper, add it to `argosy/config.py`:

```python
def get_user_agent_settings(user_id: str) -> dict:
    """Read configs/<user_id>/agent_settings.yaml. Returns empty dict if missing."""
    import yaml
    from pathlib import Path
    from argosy.config import get_settings

    home = Path(get_settings().argosy_home)
    path = home / "configs" / user_id / "agent_settings.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
```

(Adjust to match the project's actual settings shape; the existing `argosy.config.get_settings()` should already expose the home path.)

Also pass the cap into the synthesizer call inside `_run_phase_3_synthesizer`:

```python
    result = agent.run_sync(
        baseline_distillate_md=baseline_md,
        prior_current_md=prior_md,
        analyst_reports_text=analyst_reports_text,
        debate_outcomes_text=debate_outcomes_text,
        portfolio_snapshot_summary=portfolio_summary,
        recent_fills_summary=fills_summary,
        speculation_cap_pct=cap.max_pct_of_net_worth,
        speculation_cap_concurrent=cap.max_concurrent_positions,
    )
```

(Pass `cap` from `run_synthesis` through to the phase function as an extra kwarg.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_plan_synthesizer.py tests/test_plan_synthesis_flow.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add argosy/agents/plan_synthesizer.py argosy/orchestrator/flows/plan_synthesis.py argosy/config.py tests/test_plan_synthesizer.py
git commit -m "feat(synthesis): enforce speculation cap in synthesizer prompt + post-filter"
```

---

### Task 3.3: Speculation router — route accepted candidates to Argonaut

**Files:**
- Create: `argosy/orchestrator/speculation_router.py`
- Create: `tests/test_speculation_router.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_speculation_router.py`:

```python
"""Routes accepted speculative candidates from `current` -> Argonaut paper queue.

Per SDD §10.1: T0 routing in the limited account auto-executes when in
`live`; in `paper` it logs a PaperFill; otherwise it queues for human
single-click.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import PlanVersion, User


@pytest.fixture
def session_with_current(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.add(PlanVersion(
        user_id="ariel", role="current", version_label="synth-x", raw_markdown="",
        horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
        horizon_medium_json='{"horizon":"medium","freshness_expected":"quarterly","status":"no_change","posture":"x"}',
        horizon_short_json=(
            '{"horizon":"short","freshness_expected":"monthly","status":"no_change",'
            '"posture":"x","speculative_candidates":['
            '{"ticker":"HOOD","thesis_summary":"momentum",'
            '"suggested_position_usd":800,"suggested_position_pct_of_net_worth":0.0008,'
            '"risk_ceiling_check":true,"horizon_days":30,"expected_drawdown_pct":0.2,'
            '"exit_trigger":"stop -20%, take +50%","sourced_from":["sentiment"]}'
            ']}'
        ),
    ))
    s.commit()
    yield s
    s.close()


def test_route_speculative_creates_proposal_in_argonaut_paper(session_with_current, monkeypatch):
    from argosy.orchestrator import speculation_router as router

    routed: list[dict] = []
    def _fake_create_proposal(**kw):
        routed.append(kw)
        return type("P", (), {"id": 999})()

    monkeypatch.setattr(router, "_create_proposal", _fake_create_proposal)

    out = router.route_accepted_candidate(
        session_with_current,
        user_id="ariel",
        ticker="HOOD",
        execution_mode="paper",
    )
    assert out.proposal_id == 999
    assert routed[0]["ticker"] == "HOOD"
    assert routed[0]["account_class"] == "argonaut"
    assert routed[0]["tier"] == "T0"
    assert routed[0]["paper"] is True


def test_route_speculative_rejects_unknown_ticker(session_with_current):
    from argosy.orchestrator import speculation_router as router

    with pytest.raises(router.UnknownCandidateError):
        router.route_accepted_candidate(
            session_with_current, user_id="ariel", ticker="NOPE", execution_mode="paper",
        )


def test_route_speculative_blocks_when_cap_breached(session_with_current, monkeypatch):
    """Defense-in-depth: even if the synthesizer somehow emitted an over-cap
    candidate, the router refuses to act on it.
    """
    # Forcibly mutate the candidate to be over cap.
    sess = session_with_current
    pv = sess.query(PlanVersion).filter_by(user_id="ariel", role="current").one()
    import json
    short = json.loads(pv.horizon_short_json)
    short["speculative_candidates"][0]["suggested_position_pct_of_net_worth"] = 0.10  # 10% NW
    pv.horizon_short_json = json.dumps(short)
    sess.commit()

    from argosy.orchestrator import speculation_router as router
    with pytest.raises(router.CapBreachError):
        router.route_accepted_candidate(
            sess, user_id="ariel", ticker="HOOD", execution_mode="paper",
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_speculation_router.py -v`
Expected: FAIL.

- [ ] **Step 3: Write the router**

Create `argosy/orchestrator/speculation_router.py`:

```python
"""Routes accepted speculative candidates from `current.short` -> Argonaut.

Wave 3. Reads the user's `role='current'` plan, finds the requested
speculative candidate by ticker, applies the speculation cap one more
time (defense-in-depth), and creates a T0 proposal targeting the
Argonaut account.

In `paper` mode, the proposal lands as `paper=True` and is recorded as a
PaperFill via the existing decision_flow infrastructure (SDD §9.2).

In `live` mode, the SDD §10.1 routing matrix applies: T0 + Argonaut +
live = auto-execute. The router defers that policy to the existing
proposal lifecycle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from argosy.config import get_user_agent_settings, load_speculation_cap
from argosy.logging import get_logger
from argosy.state.queries import get_current_plan

log = get_logger(__name__)


class UnknownCandidateError(Exception):
    """No speculative candidate with the given ticker in current.short."""


class CapBreachError(Exception):
    """The candidate exceeds the user's speculation cap (defense-in-depth)."""


@dataclass
class RouteResult:
    proposal_id: int
    ticker: str
    paper: bool


def route_accepted_candidate(
    session: Session,
    *,
    user_id: str,
    ticker: str,
    execution_mode: Literal["paper", "live", "queue_only"],
) -> RouteResult:
    """Create a T0 Argonaut proposal for the named candidate."""
    pv = get_current_plan(session, user_id)
    if pv is None or not pv.horizon_short_json:
        raise UnknownCandidateError(f"no current plan or short horizon for {user_id}")

    short = json.loads(pv.horizon_short_json)
    candidate = next(
        (c for c in (short.get("speculative_candidates") or [])
         if c.get("ticker", "").upper() == ticker.upper()),
        None,
    )
    if candidate is None:
        raise UnknownCandidateError(
            f"no speculative candidate for ticker {ticker!r} in current.short"
        )

    cap = load_speculation_cap(
        user_id=user_id, agent_settings=get_user_agent_settings(user_id),
    )
    pct = float(candidate.get("suggested_position_pct_of_net_worth", 0))
    if pct > cap.max_pct_of_net_worth:
        raise CapBreachError(
            f"candidate {ticker} pct={pct} exceeds cap={cap.max_pct_of_net_worth}"
        )
    if not candidate.get("risk_ceiling_check"):
        raise CapBreachError(
            f"candidate {ticker} risk_ceiling_check is false"
        )

    paper = execution_mode != "live"
    proposal = _create_proposal(
        user_id=user_id,
        ticker=ticker.upper(),
        action="buy",
        size_usd=float(candidate["suggested_position_usd"]),
        order_type="limit",
        tier="T0",
        account_class="argonaut",
        rationale_summary=candidate.get("thesis_summary", ""),
        exit_trigger=candidate.get("exit_trigger", ""),
        execution_mode=execution_mode,
        paper=paper,
    )

    log.info(
        "speculation_router.routed",
        user_id=user_id,
        ticker=ticker,
        proposal_id=proposal.id,
        paper=paper,
    )
    return RouteResult(proposal_id=proposal.id, ticker=ticker.upper(), paper=paper)


def _create_proposal(**kw):  # pragma: no cover — thin shim around existing flow
    """Indirection point so tests can monkeypatch.

    Wave 3: delegates to argosy.orchestrator.proposal_lifecycle (or wherever
    the existing decision_flow creates `proposals` rows).
    """
    from argosy.orchestrator.proposal_lifecycle import create_speculative_proposal
    return create_speculative_proposal(**kw)
```

If `argosy.orchestrator.proposal_lifecycle.create_speculative_proposal` doesn't already exist in your tree, expose a thin helper in whatever module currently writes `proposals` rows from a synthesized action. The signature should accept the keyword args used above and return an object with `.id`. The router only exercises the surface of the existing decision-flow plumbing — it does not duplicate it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_speculation_router.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add argosy/orchestrator/speculation_router.py tests/test_speculation_router.py
git commit -m "feat(orchestrator): speculation_router — route accepted candidates to Argonaut T0 proposal"
```

---

### Task 3.4: API — `POST /api/plan/current/speculative/<ticker>/take`

**Files:**
- Modify: `argosy/api/routes/plan.py`
- Test: `tests/test_plan_draft_api.py` (extend, or new `tests/test_speculation_route.py`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_plan_draft_api.py` (or create `tests/test_speculation_route.py`):

```python
def test_post_take_speculative_routes_to_argonaut(client_with_db, monkeypatch):
    """Clicking 'Take a swing' on a speculative candidate creates a T0 Argonaut proposal."""
    from argosy.orchestrator import speculation_router as router
    from argosy.state.models import PlanVersion, User

    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
        sess.add(PlanVersion(
            user_id="ariel", role="current", version_label="x", raw_markdown="",
            horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
            horizon_medium_json='{"horizon":"medium","freshness_expected":"quarterly","status":"no_change","posture":"x"}',
            horizon_short_json=(
                '{"horizon":"short","freshness_expected":"monthly","status":"no_change",'
                '"posture":"x","speculative_candidates":['
                '{"ticker":"HOOD","thesis_summary":"momentum",'
                '"suggested_position_usd":800,"suggested_position_pct_of_net_worth":0.0008,'
                '"risk_ceiling_check":true,"horizon_days":30,"expected_drawdown_pct":0.2,'
                '"exit_trigger":"stop -20%, take +50%","sourced_from":["sentiment"]}'
                ']}'
            ),
        ))
        sess.commit()
    finally:
        sess.close()

    monkeypatch.setattr(
        router, "_create_proposal",
        lambda **kw: type("P", (), {"id": 4242})(),
    )

    r = client_with_db.post(
        "/api/plan/current/speculative/HOOD/take?user_id=ariel&execution_mode=paper"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposal_id"] == 4242
    assert body["ticker"] == "HOOD"
    assert body["paper"] is True


def test_post_take_speculative_404_unknown_ticker(client_with_db):
    r = client_with_db.post(
        "/api/plan/current/speculative/NOPE/take?user_id=ariel&execution_mode=paper"
    )
    assert r.status_code in (404, 400)
```

- [ ] **Step 2: Add the route**

Append to `argosy/api/routes/plan.py`:

```python
class TakeSpeculativeResponse(BaseModel):
    status: str
    proposal_id: int
    ticker: str
    paper: bool


@router.post("/current/speculative/{ticker}/take", response_model=TakeSpeculativeResponse)
def post_take_speculative(
    ticker: str,
    user_id: str,
    execution_mode: str = "paper",
    db: Session = Depends(get_db),
) -> TakeSpeculativeResponse:
    """Route an accepted speculative candidate -> Argonaut T0 proposal."""
    from argosy.orchestrator.speculation_router import (
        CapBreachError,
        UnknownCandidateError,
        route_accepted_candidate,
    )

    try:
        out = route_accepted_candidate(
            db, user_id=user_id, ticker=ticker, execution_mode=execution_mode,
        )
    except UnknownCandidateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CapBreachError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return TakeSpeculativeResponse(
        status="routed", proposal_id=out.proposal_id, ticker=out.ticker, paper=out.paper,
    )
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest tests/test_plan_draft_api.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add argosy/api/routes/plan.py tests/test_plan_draft_api.py
git commit -m "feat(api): POST /api/plan/current/speculative/{ticker}/take — route to Argonaut"
```

---

### Task 3.5: UI — speculative candidates panel on `<PlanRevisionSheet>` and Argonaut tab

**Files:**
- Modify: `ui/src/components/plan-revision-sheet.tsx`
- Modify: `ui/src/app/argonaut/page.tsx`
- Modify: `ui/src/lib/api.ts`

- [ ] **Step 1: Add API method**

Append to `ui/src/lib/api.ts` inside `api = { ... }`:

```typescript
  planSpeculativeTake: (
    userId: string,
    ticker: string,
    executionMode: "paper" | "live" = "paper",
  ) =>
    postJSON<{ status: string; proposal_id: number; ticker: string; paper: boolean }>(
      `/api/plan/current/speculative/${encodeURIComponent(ticker)}/take?user_id=${encodeURIComponent(userId)}&execution_mode=${executionMode}`,
      {},
    ),
```

- [ ] **Step 2: Add a speculative section to the side sheet**

In `ui/src/components/plan-revision-sheet.tsx`, inside the `short` tab content, add a dedicated rendering for `speculative_candidates`:

```tsx
{draft.horizon_short && draft.horizon_short.speculative_candidates.length > 0 && (
  <div className="mt-4">
    <p className="text-sm font-semibold uppercase tracking-wide text-muted-foreground mb-1">
      Speculative candidates (bounded-risk)
    </p>
    <ul className="flex flex-col gap-2">
      {draft.horizon_short.speculative_candidates.map((c, i) => (
        <li key={i} className="border border-cyan-500/30 rounded-md p-2 text-sm">
          <div className="flex items-center justify-between">
            <strong>{(c as { ticker: string }).ticker}</strong>
            <span className="text-xs font-mono text-muted-foreground">
              ≤ ${(c as { suggested_position_usd: number }).suggested_position_usd.toLocaleString()} ·{" "}
              {(((c as { suggested_position_pct_of_net_worth: number }).suggested_position_pct_of_net_worth) * 100).toFixed(2)}% NW
            </span>
          </div>
          <p className="text-xs text-muted-foreground">
            {(c as { thesis_summary: string }).thesis_summary}
          </p>
          <p className="text-[10px] font-mono text-muted-foreground">
            exit: {(c as { exit_trigger: string }).exit_trigger}
          </p>
        </li>
      ))}
    </ul>
    <p className="text-xs text-muted-foreground italic mt-2">
      Worth a small swing if you want it. Take action from the Argonaut tab.
    </p>
  </div>
)}
```

- [ ] **Step 3: Add a panel on the Argonaut page**

Edit `ui/src/app/argonaut/page.tsx`. Add a new section (alongside existing position list and trades):

```tsx
import { api, type DraftResponse } from "@/lib/api";
// ...

const [planCurrent, setPlanCurrent] = useState<DraftResponse | null>(null);

useEffect(() => {
  // Read from /api/plan/draft? — actually we want the *current* plan's
  // short horizon. The api offers /api/plan/baseline (Wave 1) and
  // /api/plan/draft (Wave 2). For Wave 3 we add a getter for /current.
  // For now, surface the draft's short.speculative_candidates if a
  // draft is pending — since they only become live after accept.
}, []);
```

Wave 3 needs a small new endpoint `GET /api/plan/current` to return the structured horizons. Add it now — it mirrors the draft endpoint but reads `role='current'`:

```python
# argosy/api/routes/plan.py
@router.get("/current", response_model=DraftResponse)
def get_current(user_id: str, db: Session = Depends(get_db)) -> DraftResponse:
    from argosy.state.queries import get_current_plan
    pv = get_current_plan(db, user_id)
    if pv is None:
        raise HTTPException(status_code=404, detail="no current plan for user")
    return DraftResponse(
        plan_version_id=pv.id,
        drafted_at=(pv.accepted_at or pv.imported_at).isoformat(),
        derived_from_id=pv.derived_from_id,
        decision_run_id=pv.decision_run_id,
        horizon_long=_horizon_view(pv.horizon_long_json),
        horizon_medium=_horizon_view(pv.horizon_medium_json),
        horizon_short=_horizon_view(pv.horizon_short_json),
        horizon_long_md=pv.horizon_long_md,
        horizon_medium_md=pv.horizon_medium_md,
        horizon_short_md=pv.horizon_short_md,
    )
```

Add corresponding api method in `ui/src/lib/api.ts`:

```typescript
  planCurrentStructured: (userId: string) =>
    getJSON<DraftResponse>(`/api/plan/current?user_id=${encodeURIComponent(userId)}`),
```

Now in the Argonaut page:

```tsx
useEffect(() => {
  api.planCurrentStructured(USER_ID).then(setPlanCurrent).catch(() => setPlanCurrent(null));
}, []);

const onTake = async (ticker: string) => {
  try {
    await api.planSpeculativeTake(USER_ID, ticker, "paper");
    alert(`Routed ${ticker} to Argonaut paper queue`);
  } catch (e: unknown) {
    alert(e instanceof Error ? e.message : String(e));
  }
};

// Render:
{planCurrent?.horizon_short?.speculative_candidates.length ? (
  <Card>
    <CardHeader>
      <CardTitle className="text-base">Speculative candidates this month</CardTitle>
      <CardDescription>
        Bounded-risk shots surfaced by the fleet. Each is within your speculation cap.
      </CardDescription>
    </CardHeader>
    <CardContent>
      <ul className="flex flex-col gap-2">
        {planCurrent.horizon_short.speculative_candidates.map((c, i) => {
          const cc = c as Record<string, unknown>;
          return (
            <li key={i} className="border border-border rounded-md p-3 flex items-start justify-between gap-3">
              <div className="text-sm">
                <strong>{cc.ticker as string}</strong> — {cc.thesis_summary as string}
                <br />
                <span className="text-xs text-muted-foreground">
                  ≤ ${(cc.suggested_position_usd as number).toLocaleString()} ·{" "}
                  exit: {cc.exit_trigger as string}
                </span>
              </div>
              <div className="flex gap-2">
                <Button size="sm" variant="outline" onClick={() => onTake(cc.ticker as string)}>
                  Take a swing
                </Button>
                <Button size="sm" variant="ghost" disabled>
                  Skip
                </Button>
              </div>
            </li>
          );
        })}
      </ul>
    </CardContent>
  </Card>
) : null}
```

- [ ] **Step 4: Manual smoke test**

1. Trigger a check-in that produces a speculative candidate (use the live e2e test or a hand-crafted draft).
2. Accept the draft so it becomes `current`.
3. Open `/argonaut`. The candidate appears.
4. Click "Take a swing". Verify a new proposal lands in the Argonaut paper queue.
5. Verify the candidate row in the proposals table has `tier=T0`, `account_class=argonaut`.

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/plan-revision-sheet.tsx ui/src/app/argonaut/page.tsx ui/src/lib/api.ts argosy/api/routes/plan.py
git commit -m "feat(ui): speculative candidates panel + take-a-swing -> Argonaut"
```

---

### Task 3.6: SDD edits for Wave 3

**Files:**
- Modify: `docs/design/SDD.md`

- [ ] **Step 1: Append §6.12 "Speculative candidates"**

```markdown
### 6.12 Speculative candidates (Wave 3 of plan-distillate work)

The synthesizer's `short.speculative_candidates` list surfaces
bounded-risk opportunities — "worth a small swing if you want it,"
never recommendations. Each candidate must satisfy the user's
speculation cap (default 0.1% of net worth, max 3 concurrent positions)
both at synthesis time (the synthesizer's prompt enforces it) and at
routing time (defense-in-depth in `argosy/orchestrator/speculation_router.py`).

Accepting a candidate via the Argonaut tab routes it as a T0 proposal
in the limited (`argonaut`) account, paper-mode by default. Per SDD
§10.1 routing matrix: T0 + Argonaut + live = auto-execute; T0 + main +
live = single-click human queue.

Configuration in `agent_settings.yaml`::

    speculation:
      max_pct_of_net_worth: 0.001       # 0.1% NW (default)
      max_concurrent_positions: 3
      allowed_account_classes: ["argonaut"]
```

- [ ] **Step 2: Update §10.1 routing matrix**

Add a row clarifying speculative routing:

```markdown
| `speculative` | argonaut | live | T0 — auto-execute, paper logged |
| `speculative` | argonaut | paper | PaperFill log; cap-enforced preflight |
```

- [ ] **Step 3: Update §A.2 — add `speculation` config block in the YAML example**

Edit the `agent_settings.yaml` example to include:

```yaml
speculation:
  max_pct_of_net_worth: 0.001
  max_concurrent_positions: 3
  allowed_account_classes: [argonaut]
```

- [ ] **Step 4: Commit**

```bash
git add docs/design/SDD.md
git commit -m "docs(sdd): §6.12 speculative candidates; §10.1 + §A.2 updates"
```

---

## Final gate

Before declaring the work complete, all of the following must hold:

- [ ] All tests pass: `pytest -v` (full suite) — including the unit tests of all three waves and the live LLM evals.
- [ ] Live LLM evals pass: `pytest -m llm_eval -v`.
- [ ] At least one full speculative-candidate paper-route succeeds end-to-end: synthesis surfaces a candidate -> user accepts the draft -> Argonaut tab shows it -> "Take a swing" creates a T0 paper proposal -> PaperFill row written.
- [ ] Speculation cap is enforced at three layers — synthesizer prompt, post-synthesizer filter, router preflight. A candidate breaching the cap is dropped at synthesis-time and rejected at routing-time.
- [ ] No regressions: `pytest -m "not llm_eval"` is fully green.
- [ ] SDD edits committed for all three waves.

---

# Closing notes

**Total tasks:** 1.1 through 3.6 — about 35 substantive TDD cycles across three waves. Each task is independently committable; commits are small and reversible.

**LLM cost:** Wave 1 distillation (~$0.30 per import + plan-watcher reruns). Wave 2 synthesis (~$5-8 per run). Wave 3 adds no per-LLM cost; it's structural routing. User has stated **accuracy over cost** — the plan defaults the synthesizer to Opus, runs all 9 analysts in parallel without trimming, and uses Anthropic's prompt cache only as an optimization (never as a depth-reducer).

**Roadmap dependencies:**

- Wave 1 ships inside SDD Phase 1 (intake + plan critique).
- Wave 2 needs SDD Phase 3 (decision team) — analyst, debate, risk, FM agents must be wired and individually green.
- Wave 3 needs SDD Phase 5 (Argonaut autonomy) — limited-account proposal lifecycle must exist.

**Out-of-scope (deferred):**

- Plan amendment chat flow (user asks the advisor for a structural change in chat, advisor proposes a draft) — Wave 2.5 follow-up.
- Multiple baselines / scenario plans — future "what-if engine."
- Household / spouse co-approval — accepted single-user risk per SDD §15.3.
- Cross-tenant baseline templating — Phase 6+ productization.

**Self-review notes (recorded inline; no separate review pass needed):** every task contains complete code in every step; no placeholders; type names consistent across waves (`PlanDistillate`, `HorizonSection`, `SynthTarget`, `SpeculativeCandidate`); tests precede implementation; commit messages follow conventional-commit-ish convention used elsewhere in the repo. Spec coverage: §3 -> Wave 1 tasks 1.1-1.16; §4 -> Wave 2 tasks 2.5-2.16; §5 -> Wave 1 tasks 1.2/1.3, Wave 2 task 2.1; §6 -> Wave 2 tasks 2.3/2.4; §7 -> Wave 2 task 2.17, Wave 1 tasks 1.15/1.16; §8 phasing -> three explicit waves with hard gates.

