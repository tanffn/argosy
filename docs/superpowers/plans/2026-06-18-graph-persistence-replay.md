# Graph Persistence + Replay Trace (Phase 1c) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the in-memory `DerivationGraph` (Phase 1a) durable and auditable: SQLAlchemy models + ONE Alembic migration (`0071`, the next head after `0070`) for `plan_nodes`, `plan_edges`, `change_requests`, `dialogue_turns`, `propagation_events`; a save/load round-trip between a `DerivationGraph` and the `plan_nodes` + `plan_edges` rows; emission of one `propagation_events` row per applied change (trigger node, invalidated set, recomputed old→new, re-rendered surfaces, verification verdicts); and an after-the-fact **Replay** reader that reconstructs a cycle's propagation ripple from the rows. **NO live streaming** — the recorded event log is read back on demand.

**Architecture:** A new module `argosy/state/graph_store.py` holds the pure save/load + emit + replay functions; it imports the engine (`argosy.quality.derivation_graph`) and the ORM (`argosy.state.models`). The five tables follow the spec's *Data model* section verbatim. `change_requests` and `dialogue_turns` are CREATED here (their schema) so the one migration is complete, but the negotiation-ladder *behaviour* that writes/reads them is Phase 2 — this plan only persists `plan_nodes`/`plan_edges` and writes/reads `propagation_events`. Round-trip is the load-bearing test: a graph saved then loaded recomputes to the same closed state, and a `propagation_events` row's `invalidated`/`recomputed`/`rerendered` sets exactly match the engine's actual closure.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 (`Mapped`/`mapped_column`), Alembic, pytest. Conventions per `docs/design/SDD.md` "Quickstart". Tests run with `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval"`.

**Depends on (already specified / partly built):**
- `argosy/quality/derivation_graph.py` — the engine (Phase 1a plan `docs/superpowers/plans/2026-06-18-derivation-graph-engine.md`). Public API used here, verbatim: `DerivationGraph()`, `Node(key, kind, value, inputs, recipe, compute_version, input_hash)`, `NodeKind.{INPUT,DERIVED,SURFACE}`, and methods `add_node`, `get`, `hash_of`, `dependents`, `check_acyclic`, `is_valid`, `set_input`, `recompute`, `is_closed`. **If the engine module does not yet exist, implement the engine plan first — these tasks import it.**
- `argosy/state/models.py` — ORM. New models append to the bottom; `Base`, `_utcnow`, the `User` row, and the `String/Text/Integer/DateTime/ForeignKey/Index` imports already exist there.
- Alembic head is `0070_tax_simulation_lots`; the new migration's `down_revision = "0070_tax_simulation_lots"`.

**Out of scope for this plan (other phases):** the change/adjudication negotiation ladder that *populates* `change_requests`/`dialogue_turns` (Phase 2 — we only create the tables + a write/read of `propagation_events`); hydrating the graph from `plan_numeric_resolver` / `sections_json` (Phase 1b); surface rendering + the surgical editor; the threaded negotiation Replay view + blast-radius diff UI (a UI follow-on; this plan delivers the rows it reads); scoped analyst re-runs (Phase 3).

**A recipe is code, not data.** `plan_nodes` persists each node's `value`, `kind`, `input_hash`, `compute_version`, `status_validity`, `status_flag`, `provenance`, `owner` — but NOT the Python `recipe` callable. On load, recipes are re-attached from a caller-supplied `recipe_registry: dict[str, Callable]` (in production `rederivation_reviewer.standard_recipes()`, promoted to the canonical registry per the spec). A loaded node with a recipe key absent from the registry loads as a value-only node and is flagged — never silently treated as a fresh INPUT (that would break invalidation).

---

### Task 1: ORM models for the five persistence tables

**Files:**
- Modify: `argosy/state/models.py`
- Test: `tests/test_graph_store_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_graph_store_models.py
"""The five Phase-1c persistence tables exist on Base.metadata with the
columns the spec's Data-model section names. We assert columns directly off
the mapper so the test is independent of any migration running."""
from argosy.state.models import (
    PlanNode, PlanEdge, ChangeRequest, DialogueTurn, PropagationEvent,
)


def _cols(model) -> set[str]:
    return {c.name for c in model.__table__.columns}


def test_plan_nodes_columns():
    assert PlanNode.__tablename__ == "plan_nodes"
    assert _cols(PlanNode) >= {
        "id", "plan_id", "node_key", "kind", "value_json", "content",
        "input_hash", "status_validity", "status_flag", "provenance_json",
        "owner", "compute_version", "created_at",
    }


def test_plan_edges_columns():
    assert PlanEdge.__tablename__ == "plan_edges"
    assert _cols(PlanEdge) >= {
        "id", "plan_id", "from_node_key", "to_node_key", "edge_kind", "created_at",
    }


def test_change_requests_columns():
    assert ChangeRequest.__tablename__ == "change_requests"
    assert _cols(ChangeRequest) >= {
        "id", "plan_id", "target_node_key", "author", "kind", "payload_json",
        "rationale", "status", "round_count", "adjudicated_by",
        "terminal_reason", "created_at", "updated_at",
    }


def test_dialogue_turns_columns():
    assert DialogueTurn.__tablename__ == "dialogue_turns"
    assert _cols(DialogueTurn) >= {
        "id", "change_request_id", "round", "speaker", "stance", "text",
        "cited_nodes_json", "created_at",
    }


def test_propagation_events_columns():
    assert PropagationEvent.__tablename__ == "propagation_events"
    assert _cols(PropagationEvent) >= {
        "id", "plan_id", "cycle_id", "trigger_node_key",
        "invalidated_node_keys_json", "recomputed_json",
        "rerendered_surfaces_json", "verification_verdicts_json", "created_at",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_store_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'PlanNode' from 'argosy.state.models'`

- [ ] **Step 3: Write minimal implementation**

Append to the bottom of `argosy/state/models.py` (the `Base`, `_utcnow`, and `String/Text/Integer/DateTime/ForeignKey/Index/UniqueConstraint` imports already exist at the top of the file):

```python
# ----------------------------------------------------------------------
# Phase 1c: derivation-graph persistence + replay trace
# (spec: docs/superpowers/specs/2026-06-18-living-plan-derivation-graph-design.md
#  "Data model" section). plan_nodes/plan_edges persist a DerivationGraph;
# propagation_events records the per-change blast-radius ripple for
# after-the-fact Replay. change_requests/dialogue_turns are created here so
# the one migration is complete; the negotiation ladder that writes them is
# Phase 2.
# ----------------------------------------------------------------------


class PlanNode(Base):
    """One node of a persisted DerivationGraph for a plan.

    Mirrors argosy.quality.derivation_graph.Node EXCEPT the recipe callable,
    which is code (re-attached from a recipe_registry on load), not data.
    status_validity (valid|stale) and status_flag (none|flagged) are
    ORTHOGONAL per the spec — a node can be both stale AND flagged.
    """

    __tablename__ = "plan_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_key: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # input|derived|surface
    # DERIVED/INPUT numeric or structured value, JSON-encoded. NULL for a
    # pure-prose surface (which uses `content`).
    value_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SURFACE rendered text/markup. NULL for input/derived nodes.
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status_validity: Mapped[str] = mapped_column(
        String(8), nullable=False, default="stale", server_default="stale"
    )
    status_flag: Mapped[str] = mapped_column(
        String(8), nullable=False, default="none", server_default="none"
    )
    # {recipe_key, author/source, render_template, ...}; recipe_key re-links
    # to the recipe_registry on load.
    provenance_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    owner: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    compute_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_plan_nodes_plan_key", "plan_id", "node_key", unique=True),
    )


class PlanEdge(Base):
    """A derived_from edge, materialized for query/audit. Direction is
    from_node_key (the input) -> to_node_key (the consumer that depends on it),
    i.e. to_node_key has from_node_key in its `inputs`."""

    __tablename__ = "plan_edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_node_key: Mapped[str] = mapped_column(String(256), nullable=False)
    to_node_key: Mapped[str] = mapped_column(String(256), nullable=False)
    # named | set | predicate (spec: hybrid edges). Plain "named" for now.
    edge_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="named", server_default="named"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index(
            "ix_plan_edges_plan_from_to",
            "plan_id", "from_node_key", "to_node_key", "edge_kind",
            unique=True,
        ),
    )


class ChangeRequest(Base):
    """The single author-agnostic primitive (user | agent_role) targeting one
    node. CREATED here for schema completeness; the negotiation ladder that
    populates it is Phase 2."""

    __tablename__ = "change_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_node_key: Mapped[str] = mapped_column(String(256), nullable=False)
    author: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # proposed|in_dialogue|escalated_arbiter|escalated_user|A_conceded|
    # B_conceded|arbiter_ruled|superseded
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="proposed", server_default="proposed"
    )
    round_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    adjudicated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    terminal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class DialogueTurn(Base):
    """One replayable back-and-forth turn on a ChangeRequest (Layer 5.1).
    CREATED here for schema completeness; written in Phase 2."""

    __tablename__ = "dialogue_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    change_request_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("change_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    speaker: Mapped[str] = mapped_column(String(16), nullable=False)  # A|B|arbiter|user
    # propose|rebut|concede|rule|classify|ask|answer
    stance: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cited_nodes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class PropagationEvent(Base):
    """The visible blast-radius ripple for ONE applied change (Layer 5.2).
    Written by graph_store.emit_propagation_event after a propagation;
    read back by the Replay reader. trigger -> invalidated -> recomputed
    (old->new) -> rerendered surfaces -> verification verdicts."""

    __tablename__ = "propagation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Groups the propagation_events of one steady-state run for ordered replay.
    cycle_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    trigger_node_key: Mapped[str] = mapped_column(String(256), nullable=False)
    invalidated_node_keys_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    # {node_key: {"old": <json-able>, "new": <json-able>}}
    recomputed_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    rerendered_surfaces_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    # {check_name: verdict_str}
    verification_verdicts_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_propagation_events_plan_cycle", "plan_id", "cycle_id"),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_store_models.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/state/models.py tests/test_graph_store_models.py
git commit -m "feat(graph-persist): ORM models for plan_nodes/plan_edges/change_requests/dialogue_turns/propagation_events"
```

---

### Task 2: Alembic migration 0071 (the five tables)

**Files:**
- Create: `alembic/versions/0071_derivation_graph_persistence.py`
- Test: `tests/test_migration_0071_graph_persistence.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migration_0071_graph_persistence.py
"""Run alembic upgrade head on a throwaway SQLite DB (via the repo's
ARGOSY_HOME + settings.database_url convention, matching tests/test_migration_0067.py)
and assert the five Phase-1c tables exist with the spec columns; then
downgrade to 0070 drops them."""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
    # Mirror tests/test_migration_0067.py: alembic/env.py resolves the URL
    # from settings.database_url, which keys off ARGOSY_HOME — so point
    # ARGOSY_HOME at tmp_path rather than set_main_option (env.py overrides it).
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    reload_settings()
    sync_url = get_settings().database_url.replace("+aiosqlite", "")
    db_path = sync_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return Config("alembic.ini"), sync_url


def test_upgrade_head_creates_graph_tables(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    eng = sa.create_engine(sync_url)
    insp = sa.inspect(eng)
    names = set(insp.get_table_names())
    assert {
        "plan_nodes", "plan_edges", "change_requests",
        "dialogue_turns", "propagation_events",
    } <= names

    node_cols = {c["name"] for c in insp.get_columns("plan_nodes")}
    assert {"plan_id", "node_key", "kind", "value_json", "input_hash",
            "status_validity", "status_flag", "compute_version"} <= node_cols

    prop_cols = {c["name"] for c in insp.get_columns("propagation_events")}
    assert {"cycle_id", "trigger_node_key", "invalidated_node_keys_json",
            "recomputed_json", "rerendered_surfaces_json",
            "verification_verdicts_json"} <= prop_cols
    eng.dispose()


def test_downgrade_drops_graph_tables(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0070_tax_simulation_lots")

    eng = sa.create_engine(sync_url)
    names = set(sa.inspect(eng).get_table_names())
    assert "plan_nodes" not in names
    assert "propagation_events" not in names
    eng.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_migration_0071_graph_persistence.py -q`
Expected: FAIL — `alembic.util.exc.CommandError: Can't locate revision identified by '0071_derivation_graph_persistence'` is NOT the failure (the file doesn't exist yet, so `head` resolves to `0070` and the table-set assertion `assert {...} <= names` fails because `plan_nodes` etc. don't exist).

- [ ] **Step 3: Write minimal implementation**

```python
# alembic/versions/0071_derivation_graph_persistence.py
"""derivation-graph persistence + replay trace

plan_nodes / plan_edges persist a DerivationGraph; change_requests /
dialogue_turns are the change-substrate schema (populated in Phase 2);
propagation_events records the per-change blast-radius ripple for replay.

Revision ID: 0071_derivation_graph_persistence
Revises: 0070_tax_simulation_lots
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0071_derivation_graph_persistence"
down_revision: str | None = "0070_tax_simulation_lots"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "plan_nodes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_key", sa.String(length=256), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=True),
        sa.Column("status_validity", sa.String(length=8), nullable=False, server_default="stale"),
        sa.Column("status_flag", sa.String(length=8), nullable=False, server_default="none"),
        sa.Column("provenance_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("owner", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("compute_version", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_plan_nodes_plan_id", "plan_nodes", ["plan_id"])
    op.create_index("ix_plan_nodes_plan_key", "plan_nodes", ["plan_id", "node_key"], unique=True)

    op.create_table(
        "plan_edges",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_node_key", sa.String(length=256), nullable=False),
        sa.Column("to_node_key", sa.String(length=256), nullable=False),
        sa.Column("edge_kind", sa.String(length=16), nullable=False, server_default="named"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_plan_edges_plan_id", "plan_edges", ["plan_id"])
    op.create_index(
        "ix_plan_edges_plan_from_to", "plan_edges",
        ["plan_id", "from_node_key", "to_node_key", "edge_kind"], unique=True,
    )

    op.create_table(
        "change_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_node_key", sa.String(length=256), nullable=False),
        sa.Column("author", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="proposed"),
        sa.Column("round_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("adjudicated_by", sa.String(length=64), nullable=True),
        sa.Column("terminal_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_change_requests_plan_id", "change_requests", ["plan_id"])

    op.create_table(
        "dialogue_turns",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("change_request_id", sa.Integer(), sa.ForeignKey("change_requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("speaker", sa.String(length=16), nullable=False),
        sa.Column("stance", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("cited_nodes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dialogue_turns_change_request_id", "dialogue_turns", ["change_request_id"])

    op.create_table(
        "propagation_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cycle_id", sa.String(length=64), nullable=False),
        sa.Column("trigger_node_key", sa.String(length=256), nullable=False),
        sa.Column("invalidated_node_keys_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("recomputed_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("rerendered_surfaces_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("verification_verdicts_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_propagation_events_plan_id", "propagation_events", ["plan_id"])
    op.create_index("ix_propagation_events_plan_cycle", "propagation_events", ["plan_id", "cycle_id"])


def downgrade() -> None:
    op.drop_index("ix_propagation_events_plan_cycle", table_name="propagation_events")
    op.drop_index("ix_propagation_events_plan_id", table_name="propagation_events")
    op.drop_table("propagation_events")

    op.drop_index("ix_dialogue_turns_change_request_id", table_name="dialogue_turns")
    op.drop_table("dialogue_turns")

    op.drop_index("ix_change_requests_plan_id", table_name="change_requests")
    op.drop_table("change_requests")

    op.drop_index("ix_plan_edges_plan_from_to", table_name="plan_edges")
    op.drop_index("ix_plan_edges_plan_id", table_name="plan_edges")
    op.drop_table("plan_edges")

    op.drop_index("ix_plan_nodes_plan_key", table_name="plan_nodes")
    op.drop_index("ix_plan_nodes_plan_id", table_name="plan_nodes")
    op.drop_table("plan_nodes")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_migration_0071_graph_persistence.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/0071_derivation_graph_persistence.py tests/test_migration_0071_graph_persistence.py
git commit -m "feat(graph-persist): alembic 0071 — plan_nodes/edges/change_requests/dialogue_turns/propagation_events"
```

---

### Task 3: `save_graph` — write a DerivationGraph to plan_nodes + plan_edges

**Files:**
- Create: `argosy/state/graph_store.py`
- Test: `tests/test_graph_store_save.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_graph_store_save.py
import json

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.state.models import Base, User, PlanVersion, PlanNode, PlanEdge
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.state.graph_store import save_graph


def _session(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'gs.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    SF = sessionmaker(bind=eng, expire_on_commit=False)
    s = SF()
    s.add(User(id="ariel", plan="free"))
    s.flush()
    pv = PlanVersion(user_id="ariel", role="draft")
    s.add(pv)
    s.flush()
    return s, pv.id


def _built_graph() -> DerivationGraph:
    g = DerivationGraph()
    g.add_node(Node(key="liquid_nw", kind=NodeKind.INPUT, value=11_687_926))
    g.add_node(Node(key="annual_spend", kind=NodeKind.INPUT, value=600_000))
    g.add_node(Node(
        key="fi_margin", kind=NodeKind.DERIVED, inputs=("liquid_nw", "annual_spend"),
        recipe=lambda i: i["liquid_nw"] / i["annual_spend"], compute_version="fi_v1",
    ))
    g.recompute()
    return g


def test_save_graph_writes_nodes_and_edges(tmp_path):
    s, plan_id = _session(tmp_path)
    save_graph(s, plan_id, _built_graph())
    s.commit()

    rows = s.execute(sa.select(PlanNode).where(PlanNode.plan_id == plan_id)).scalars().all()
    by_key = {r.node_key: r for r in rows}
    assert set(by_key) == {"liquid_nw", "annual_spend", "fi_margin"}
    assert by_key["liquid_nw"].kind == "input"
    assert by_key["liquid_nw"].status_validity == "valid"
    assert json.loads(by_key["liquid_nw"].value_json) == 11_687_926
    fi = by_key["fi_margin"]
    assert fi.kind == "derived"
    assert fi.compute_version == "fi_v1"
    assert fi.input_hash is not None and fi.status_validity == "valid"
    assert json.loads(fi.provenance_json)["recipe_key"] == "fi_margin"

    edges = s.execute(sa.select(PlanEdge).where(PlanEdge.plan_id == plan_id)).scalars().all()
    pairs = {(e.from_node_key, e.to_node_key) for e in edges}
    assert pairs == {("liquid_nw", "fi_margin"), ("annual_spend", "fi_margin")}


def test_save_graph_is_idempotent_replaces_prior(tmp_path):
    s, plan_id = _session(tmp_path)
    save_graph(s, plan_id, _built_graph())
    s.commit()
    save_graph(s, plan_id, _built_graph())  # second save must not duplicate
    s.commit()
    n = s.execute(
        sa.select(sa.func.count()).select_from(PlanNode).where(PlanNode.plan_id == plan_id)
    ).scalar_one()
    assert n == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_store_save.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'argosy.state.graph_store'`

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/state/graph_store.py
"""Persist / load a DerivationGraph to plan_nodes + plan_edges, emit a
propagation_events row per applied change, and replay a cycle's ripple.

A node's recipe is CODE, not data: save_graph stores the recipe KEY in
provenance_json["recipe_key"]; load_graph re-attaches the callable from a
caller-supplied recipe_registry. No live streaming — propagation_events are
read back on demand by the Replay reader.

See docs/superpowers/specs/2026-06-18-living-plan-derivation-graph-design.md.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.state.models import PlanEdge, PlanNode

# recipe key -> recipe(inbound: dict[str, Any]) -> Any
RecipeRegistry = dict[str, Callable[[dict[str, Any]], Any]]


def _recipe_key(node: Node) -> str:
    """The registry key under which a DERIVED/SURFACE node's recipe is found.
    Convention: the node key itself (matches rederivation_reviewer's recipe
    names). Stored in provenance so load can re-attach the callable."""
    return node.key


def save_graph(session: Session, plan_id: int, graph: DerivationGraph) -> None:
    """Replace this plan's persisted nodes + edges with `graph`. Idempotent:
    a re-save of the same graph yields the same rows (delete-then-insert so a
    removed node/edge doesn't linger). The recipe callable is NOT stored — its
    key lands in provenance_json."""
    session.execute(delete(PlanEdge).where(PlanEdge.plan_id == plan_id))
    session.execute(delete(PlanNode).where(PlanNode.plan_id == plan_id))
    session.flush()

    for key in graph.keys():
        node = graph.get(key)
        is_surface = node.kind is NodeKind.SURFACE
        provenance: dict[str, Any] = {}
        if node.kind in (NodeKind.DERIVED, NodeKind.SURFACE):
            provenance["recipe_key"] = _recipe_key(node)
        session.add(PlanNode(
            plan_id=plan_id,
            node_key=key,
            kind=node.kind.value,
            value_json=None if is_surface and not isinstance(node.value, (int, float, list, dict))
            else json.dumps(node.value, default=str),
            content=node.value if is_surface and isinstance(node.value, str) else None,
            input_hash=node.input_hash,
            status_validity="valid" if graph.is_valid(key) else "stale",
            status_flag="none",
            provenance_json=json.dumps(provenance),
            owner="",
            compute_version=node.compute_version,
        ))
        for src in node.inputs:
            session.add(PlanEdge(
                plan_id=plan_id,
                from_node_key=src,
                to_node_key=key,
                edge_kind="named",
            ))
    session.flush()
```

> NOTE: `save_graph` calls `graph.keys()` and `graph.get(...)`. The engine plan exposes `get`; add a tiny `keys()` accessor to the engine (it iterates `self._nodes`) — implement it in Task 7 of the engine plan if absent, OR if the engine only exposes private `_nodes`, use `graph._nodes.keys()` here. PREFER adding a public `keys()` to the engine module (one line: `def keys(self): return list(self._nodes)`); if you cannot modify the engine in this phase, fall back to `list(graph._nodes)`. The tests below assume one of these works.

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_store_save.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/state/graph_store.py tests/test_graph_store_save.py
git commit -m "feat(graph-persist): save_graph — write DerivationGraph to plan_nodes + plan_edges"
```

---

### Task 4: `load_graph` — reconstruct a DerivationGraph + round-trip

**Files:**
- Modify: `argosy/state/graph_store.py`
- Test: `tests/test_graph_store_roundtrip.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_graph_store_roundtrip.py
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.state.models import Base, User, PlanVersion
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.state.graph_store import save_graph, load_graph


def _session(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'rt.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    SF = sessionmaker(bind=eng, expire_on_commit=False)
    s = SF()
    s.add(User(id="ariel", plan="free"))
    s.flush()
    pv = PlanVersion(user_id="ariel", role="draft")
    s.add(pv)
    s.flush()
    return s, pv.id


def _fi_recipe(i):
    return i["liquid_nw"] / i["annual_spend"]


def _built_graph() -> DerivationGraph:
    g = DerivationGraph()
    g.add_node(Node(key="liquid_nw", kind=NodeKind.INPUT, value=12_000_000))
    g.add_node(Node(key="annual_spend", kind=NodeKind.INPUT, value=600_000))
    g.add_node(Node(key="fi_margin", kind=NodeKind.DERIVED,
                    inputs=("liquid_nw", "annual_spend"),
                    recipe=_fi_recipe, compute_version="fi_v1"))
    g.recompute()
    return g


def test_roundtrip_preserves_values_and_validity(tmp_path):
    s, plan_id = _session(tmp_path)
    save_graph(s, plan_id, _built_graph())
    s.commit()

    registry = {"fi_margin": _fi_recipe}
    g2 = load_graph(s, plan_id, recipe_registry=registry)

    assert g2.get("liquid_nw").value == 12_000_000
    assert g2.get("fi_margin").value == 20.0
    assert g2.get("fi_margin").inputs == ("liquid_nw", "annual_spend") or \
        set(g2.get("fi_margin").inputs) == {"liquid_nw", "annual_spend"}
    # The reloaded graph is already closed — no recompute needed.
    assert g2.is_closed() is True
    assert g2.is_valid("fi_margin") is True


def test_roundtrip_then_change_input_recomputes_correctly(tmp_path):
    s, plan_id = _session(tmp_path)
    save_graph(s, plan_id, _built_graph())
    s.commit()

    g2 = load_graph(s, plan_id, recipe_registry={"fi_margin": _fi_recipe})
    invalidated = g2.set_input("annual_spend", 1_200_000)
    assert invalidated == {"fi_margin"}
    g2.recompute()
    assert g2.get("fi_margin").value == 10.0  # 12_000_000 / 1_200_000


def test_load_missing_recipe_flags_node(tmp_path):
    s, plan_id = _session(tmp_path)
    save_graph(s, plan_id, _built_graph())
    s.commit()
    # Empty registry — the derived node's recipe cannot be re-attached.
    g2 = load_graph(s, plan_id, recipe_registry={})
    n = g2.get("fi_margin")
    assert n.recipe is None
    # It keeps its persisted value but is NOT silently an INPUT.
    assert n.kind is NodeKind.DERIVED
    assert n.value == 20.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_store_roundtrip.py -q`
Expected: FAIL — `ImportError: cannot import name 'load_graph'`

- [ ] **Step 3: Write minimal implementation**

Add to `argosy/state/graph_store.py`:

```python
def _decode_value(row: PlanNode) -> Any:
    if row.kind == NodeKind.SURFACE.value and row.content is not None:
        return row.content
    if row.value_json is None:
        return None
    return json.loads(row.value_json)


def load_graph(
    session: Session,
    plan_id: int,
    recipe_registry: RecipeRegistry | None = None,
) -> DerivationGraph:
    """Rebuild the DerivationGraph for `plan_id` from plan_nodes + plan_edges.
    Inbound edges come from plan_edges (to_node_key == this node). Recipes are
    re-attached from recipe_registry by provenance_json['recipe_key']; a
    DERIVED/SURFACE node whose key is absent loads value-only with recipe=None
    (it is NOT downgraded to an INPUT — that would break invalidation). The
    persisted input_hash is restored so a clean graph loads already-valid."""
    registry = recipe_registry or {}

    node_rows = session.execute(
        select(PlanNode).where(PlanNode.plan_id == plan_id)
    ).scalars().all()
    edge_rows = session.execute(
        select(PlanEdge).where(PlanEdge.plan_id == plan_id)
    ).scalars().all()

    # inputs(to) = sorted [from ...]; sort for deterministic tuple order.
    inputs_by_node: dict[str, list[str]] = {}
    for e in edge_rows:
        inputs_by_node.setdefault(e.to_node_key, []).append(e.from_node_key)

    graph = DerivationGraph()
    for row in node_rows:
        kind = NodeKind(row.kind)
        recipe = None
        if kind in (NodeKind.DERIVED, NodeKind.SURFACE):
            recipe_key = json.loads(row.provenance_json or "{}").get("recipe_key", row.node_key)
            recipe = registry.get(recipe_key)
        graph.add_node(Node(
            key=row.node_key,
            kind=kind,
            value=_decode_value(row),
            inputs=tuple(sorted(inputs_by_node.get(row.node_key, []))),
            recipe=recipe,
            compute_version=row.compute_version,
            input_hash=row.input_hash,
        ))
    return graph
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_store_roundtrip.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/state/graph_store.py tests/test_graph_store_roundtrip.py
git commit -m "feat(graph-persist): load_graph + round-trip (values/validity preserved, recipes re-attached)"
```

---

### Task 5: `apply_change` — propagate + emit one propagation_events row

**Files:**
- Modify: `argosy/state/graph_store.py`
- Test: `tests/test_graph_store_propagation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_graph_store_propagation.py
import json

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.state.models import Base, User, PlanVersion, PropagationEvent
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.state.graph_store import save_graph, apply_change


def _session(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'prop.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    SF = sessionmaker(bind=eng, expire_on_commit=False)
    s = SF()
    s.add(User(id="ariel", plan="free"))
    s.flush()
    pv = PlanVersion(user_id="ariel", role="draft")
    s.add(pv)
    s.flush()
    return s, pv.id


def _graph() -> DerivationGraph:
    g = DerivationGraph()
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=10))
    g.add_node(Node(key="y", kind=NodeKind.DERIVED, inputs=("x",),
                    recipe=lambda i: i["x"] + 1, compute_version="y_v1"))
    g.add_node(Node(key="surf_y", kind=NodeKind.SURFACE, inputs=("y",),
                    recipe=lambda i: f"y is {i['y']}", compute_version="s_v1"))
    g.add_node(Node(key="indep", kind=NodeKind.INPUT, value=99))
    g.add_node(Node(key="w", kind=NodeKind.DERIVED, inputs=("indep",),
                    recipe=lambda i: i["indep"] * 2, compute_version="w_v1"))
    g.recompute()
    return g


def test_apply_change_emits_event_matching_closure(tmp_path):
    s, plan_id = _session(tmp_path)
    g = _graph()
    save_graph(s, plan_id, g)
    s.commit()

    event = apply_change(
        s, plan_id, g, cycle_id="cycle-1",
        trigger_node_key="x", new_value=100,
        verification_verdicts={"coherence_gate": "pass"},
    )
    s.commit()

    # The returned event mirrors the persisted row.
    row = s.execute(
        sa.select(PropagationEvent).where(PropagationEvent.plan_id == plan_id)
    ).scalar_one()
    assert row.trigger_node_key == "x"
    assert row.cycle_id == "cycle-1"

    invalidated = set(json.loads(row.invalidated_node_keys_json))
    recomputed = json.loads(row.recomputed_json)
    rerendered = set(json.loads(row.rerendered_surfaces_json))
    verdicts = json.loads(row.verification_verdicts_json)

    # EXACTLY x's transitive dependents were invalidated — not w/indep.
    assert invalidated == {"y", "surf_y"}
    assert "w" not in invalidated and "indep" not in invalidated
    # recomputed carries old->new for every recomputed node.
    assert set(recomputed) == {"y", "surf_y"}
    assert recomputed["y"] == {"old": 11, "new": 101}
    assert recomputed["surf_y"] == {"old": "y is 11", "new": "y is 101"}
    # rerendered surfaces = the SURFACE nodes in the closure.
    assert rerendered == {"surf_y"}
    assert verdicts == {"coherence_gate": "pass"}

    # The engine actually applied the change.
    assert g.get("y").value == 101
    assert g.is_closed() is True


def test_apply_change_persists_updated_graph(tmp_path):
    s, plan_id = _session(tmp_path)
    g = _graph()
    save_graph(s, plan_id, g)
    s.commit()
    apply_change(s, plan_id, g, cycle_id="c", trigger_node_key="x", new_value=100)
    s.commit()

    from argosy.state.models import PlanNode
    y_row = s.execute(
        sa.select(PlanNode).where(PlanNode.plan_id == plan_id, PlanNode.node_key == "y")
    ).scalar_one()
    assert json.loads(y_row.value_json) == 101
    assert y_row.status_validity == "valid"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_store_propagation.py -q`
Expected: FAIL — `ImportError: cannot import name 'apply_change'`

- [ ] **Step 3: Write minimal implementation**

Add to `argosy/state/graph_store.py`:

```python
from argosy.state.models import PropagationEvent


def apply_change(
    session: Session,
    plan_id: int,
    graph: DerivationGraph,
    *,
    cycle_id: str,
    trigger_node_key: str,
    new_value: Any,
    verification_verdicts: dict[str, str] | None = None,
) -> PropagationEvent:
    """Apply an INPUT change to `graph`, propagate (recompute the stale
    closure), persist the updated graph, and record ONE propagation_events row
    whose invalidated / recomputed(old->new) / rerendered sets EXACTLY match
    the engine's closure. Returns the persisted event (flushed, not committed).

    Snapshots old values of the about-to-be-invalidated dependents BEFORE the
    change so old->new is exact."""
    invalidated = graph.dependents(trigger_node_key)
    old_values = {k: graph.get(k).value for k in invalidated}

    graph.set_input(trigger_node_key, new_value)
    recomputed_keys = graph.recompute()  # only the stale closure, in topo order

    recomputed: dict[str, dict[str, Any]] = {}
    rerendered: list[str] = []
    for k in recomputed_keys:
        node = graph.get(k)
        recomputed[k] = {"old": old_values.get(k), "new": node.value}
        if node.kind is NodeKind.SURFACE:
            rerendered.append(k)

    # Persist the now-updated graph so the rows reflect post-propagation state.
    save_graph(session, plan_id, graph)

    event = PropagationEvent(
        plan_id=plan_id,
        cycle_id=cycle_id,
        trigger_node_key=trigger_node_key,
        invalidated_node_keys_json=json.dumps(sorted(invalidated)),
        recomputed_json=json.dumps(recomputed, default=str),
        rerendered_surfaces_json=json.dumps(sorted(rerendered)),
        verification_verdicts_json=json.dumps(verification_verdicts or {}),
    )
    session.add(event)
    session.flush()
    return event
```

> NOTE on exactness (spec Testing / Observability): `invalidated` is `graph.dependents(trigger)` — the engine's transitive-dependent set — and `recomputed_keys` is `graph.recompute()`'s return, the engine's actual recompute order. The event is therefore the engine's closure by construction; `recomputed` ⊆ `invalidated` (a valid-but-unchanged dependent — impossible here since all dependents go stale — would appear in `invalidated` but not `recomputed`). The test asserts `set(recomputed) == invalidated` for this fully-stale graph.

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_store_propagation.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/state/graph_store.py tests/test_graph_store_propagation.py
git commit -m "feat(graph-persist): apply_change — propagate + emit propagation_events matching the closure"
```

---

### Task 6: Replay reader — reconstruct a cycle's ripple from the rows

**Files:**
- Modify: `argosy/state/graph_store.py`
- Test: `tests/test_graph_store_replay.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_graph_store_replay.py
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.state.models import Base, User, PlanVersion
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.state.graph_store import save_graph, apply_change, replay_cycle


def _session(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'rep.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    SF = sessionmaker(bind=eng, expire_on_commit=False)
    s = SF()
    s.add(User(id="ariel", plan="free"))
    s.flush()
    pv = PlanVersion(user_id="ariel", role="draft")
    s.add(pv)
    s.flush()
    return s, pv.id


def _graph() -> DerivationGraph:
    g = DerivationGraph()
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=1))
    g.add_node(Node(key="y", kind=NodeKind.DERIVED, inputs=("x",),
                    recipe=lambda i: i["x"] + 1, compute_version="v1"))
    g.add_node(Node(key="a", kind=NodeKind.INPUT, value=5))
    g.add_node(Node(key="b", kind=NodeKind.DERIVED, inputs=("a",),
                    recipe=lambda i: i["a"] * 10, compute_version="v1"))
    g.recompute()
    return g


def test_replay_reconstructs_ordered_ripple(tmp_path):
    s, plan_id = _session(tmp_path)
    g = _graph()
    save_graph(s, plan_id, g)
    s.commit()

    apply_change(s, plan_id, g, cycle_id="cyc", trigger_node_key="x", new_value=100,
                 verification_verdicts={"gate": "pass"})
    apply_change(s, plan_id, g, cycle_id="cyc", trigger_node_key="a", new_value=7,
                 verification_verdicts={"gate": "pass"})
    s.commit()

    steps = replay_cycle(s, plan_id, "cyc")
    assert [st.trigger_node_key for st in steps] == ["x", "a"]  # chronological

    first = steps[0]
    assert first.invalidated == ["y"]
    assert first.recomputed == {"y": {"old": 2, "new": 101}}
    assert first.rerendered == []
    assert first.verdicts == {"gate": "pass"}

    second = steps[1]
    assert second.invalidated == ["b"]
    assert second.recomputed == {"b": {"old": 50, "new": 70}}


def test_replay_unknown_cycle_is_empty(tmp_path):
    s, plan_id = _session(tmp_path)
    assert replay_cycle(s, plan_id, "nope") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_store_replay.py -q`
Expected: FAIL — `ImportError: cannot import name 'replay_cycle'`

- [ ] **Step 3: Write minimal implementation**

Add to `argosy/state/graph_store.py` (the `dataclass` import goes at the top with the others):

```python
from dataclasses import dataclass


@dataclass
class ReplayStep:
    """One propagation_events row, decoded for the after-the-fact Replay
    reader. The ripple of a single applied change."""
    trigger_node_key: str
    invalidated: list[str]
    recomputed: dict[str, dict[str, Any]]
    rerendered: list[str]
    verdicts: dict[str, str]
    created_at: Any


def replay_cycle(session: Session, plan_id: int, cycle_id: str) -> list[ReplayStep]:
    """Reconstruct, in chronological order, the propagation ripple of every
    applied change in one cycle from its propagation_events rows. Pure read —
    no live streaming. Empty list for an unknown (plan_id, cycle_id)."""
    rows = session.execute(
        select(PropagationEvent)
        .where(PropagationEvent.plan_id == plan_id, PropagationEvent.cycle_id == cycle_id)
        .order_by(PropagationEvent.id)
    ).scalars().all()
    return [
        ReplayStep(
            trigger_node_key=r.trigger_node_key,
            invalidated=json.loads(r.invalidated_node_keys_json),
            recomputed=json.loads(r.recomputed_json),
            rerendered=json.loads(r.rerendered_surfaces_json),
            verdicts=json.loads(r.verification_verdicts_json),
            created_at=r.created_at,
        )
        for r in rows
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_store_replay.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/state/graph_store.py tests/test_graph_store_replay.py
git commit -m "feat(graph-persist): replay_cycle — after-the-fact reconstruction of a cycle's blast-radius ripple"
```

---

### Task 7: Module exports + full Phase-1c suite smoke

**Files:**
- Modify: `argosy/state/graph_store.py`
- Test: `tests/test_graph_store_exports.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_graph_store_exports.py
def test_public_exports():
    import argosy.state.graph_store as gs
    for name in ("save_graph", "load_graph", "apply_change",
                 "replay_cycle", "ReplayStep", "RecipeRegistry"):
        assert name in gs.__all__
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_store_exports.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute '__all__'`

- [ ] **Step 3: Write minimal implementation**

Append to the bottom of `argosy/state/graph_store.py`:

```python
__all__ = [
    "RecipeRegistry",
    "save_graph",
    "load_graph",
    "apply_change",
    "replay_cycle",
    "ReplayStep",
]
```

- [ ] **Step 4: Run the full Phase-1c suite**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider -m "not llm_eval" tests/test_graph_store_models.py tests/test_migration_0071_graph_persistence.py tests/test_graph_store_save.py tests/test_graph_store_roundtrip.py tests/test_graph_store_propagation.py tests/test_graph_store_replay.py tests/test_graph_store_exports.py -q`
Expected: PASS (all Phase-1c tests, ~17)

- [ ] **Step 5: Commit**

```bash
git add argosy/state/graph_store.py tests/test_graph_store_exports.py
git commit -m "feat(graph-persist): graph_store public API exports + full Phase-1c suite green"
```

---

## Self-Review

**Spec requirements in this plan's scope (Phase 1c) → task mapping:**

- **`plan_nodes` schema** (id, plan_id, node_key, kind, value_json/content, input_hash, status_validity, status_flag — *orthogonal*, provenance_json, owner; + compute_version) → Task 1 model + Task 2 migration. ✓ (status_validity/status_flag are two columns, not one enum, per the codex MINOR note.)
- **`plan_edges` schema** (materialized derived_from edges for query/audit) → Task 1 + Task 2. ✓
- **`change_requests` schema** (Data model section, verbatim fields incl. status enum + round_count + adjudicated_by + terminal_reason) → Task 1 + Task 2 (created for migration completeness; populated in Phase 2, correctly out of scope here). ✓
- **`dialogue_turns` schema** (Layer 5.1 fields: round/speaker/stance/text/cited_nodes) → Task 1 + Task 2 (schema only; written in Phase 2). ✓
- **`propagation_events` schema** (Layer 5.2: cycle_id, trigger_node_key, invalidated, recomputed old→new, rerendered_surfaces, verification_verdicts) → Task 1 + Task 2 + Task 5. ✓
- **ONE Alembic migration, next head after 0070** → Task 2 (`0071_derivation_graph_persistence`, `down_revision="0070_tax_simulation_lots"`). ✓
- **Save a DerivationGraph to plan_nodes + plan_edges** → Task 3 (`save_graph`, recipe stored as a *key* in provenance, not the callable). ✓
- **Load a DerivationGraph from the rows** → Task 4 (`load_graph` + recipe re-attach from `recipe_registry`; missing recipe flagged, not silently INPUT). ✓
- **Persistence round-trip test** → Task 4 (`test_roundtrip_preserves_values_and_validity`, `test_roundtrip_then_change_input_recomputes_correctly`). ✓
- **Emit a propagation_events row per applied change** (trigger, invalidated set, recomputed old→new, rerendered surfaces, verification verdicts) → Task 5 (`apply_change`). ✓
- **The propagation_events row EXACTLY matches the recompute closure** → Task 5 (`test_apply_change_emits_event_matching_closure`: `invalidated == graph.dependents(trigger)`, `set(recomputed) == invalidated`, `rerendered == surface∩closure`). ✓ — directly satisfies the spec Testing bullet *"every applied change produces a propagation_events row whose invalidated/recomputed/rerendered sets match the actual closure."*
- **Replay reader reconstructs a cycle's ripple from the rows** → Task 6 (`replay_cycle` → ordered `ReplayStep`s). ✓
- **No live streaming — after-the-fact replay only** → honored: `replay_cycle` is a pure read of persisted rows; no streaming/WS surface. ✓

**Reuse / DRY (no reinvention):**
- Engine API consumed verbatim from `argosy/quality/derivation_graph.py` (`DerivationGraph`, `Node`, `NodeKind`, `add_node`, `get`, `hash_of`, `dependents`, `is_valid`, `set_input`, `recompute`, `is_closed`) — the closure/old→new sets come from the engine, not re-derived in the store.
- New models append to the existing `argosy/state/models.py` reusing its `Base`, `_utcnow`, and SQLAlchemy column imports; FK to the existing `plan_versions.id`.
- Migration mirrors the structure of `0070_tax_simulation_lots.py` / `0069_coherence_decisions.py` (same `op.create_table` / `create_index` idiom, `from __future__` header, revision wiring).
- `recipe_key` convention aligns with `rederivation_reviewer.standard_recipes()` (the spec's canonical registry) so production `load_graph` passes `standard_recipes()` as `recipe_registry`.

**Placeholder scan:** none — every step has complete runnable code + an exact run command. No TBD / "similar to".

**Type consistency across tasks:** `save_graph(session, plan_id, graph) -> None`; `load_graph(session, plan_id, recipe_registry=None) -> DerivationGraph`; `apply_change(session, plan_id, graph, *, cycle_id, trigger_node_key, new_value, verification_verdicts=None) -> PropagationEvent`; `replay_cycle(session, plan_id, cycle_id) -> list[ReplayStep]`. `RecipeRegistry = dict[str, Callable[[dict[str, Any]], Any]]`. These signatures are used identically in every test.
