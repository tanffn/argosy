# Change / Adjudication Substrate + Negotiation Ladder (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task is a bite-sized TDD loop: write a failing test → run it (RED) → minimal implementation → run it (GREEN) → commit.

**Goal:** Build Layer 2 of the living-plan redesign — the **author-agnostic change/adjudication substrate** and the **bounded negotiation ladder**. A `ChangeRequest` is the single primitive any author (user OR agent_role) writes against a graph node. Adjudication is ownership- and authority-specific and fail-closed: a request to set a `DerivedValue` is rejected ("change the inputs or the recipe"); an `InputFact` / policy change that flips a hard verdict is itself an adjudicated, audited, contestable change-request (anti-laundering); a recipe/policy change routes through the ladder. The ladder generalizes the existing `converge_fm_objections` FM↔analyst dialogue: A files "change X because Y" → **B may rebut Y, not just the value** → bounded peer rounds `n=3` → escalate to the **arbiter (FM)** which CLASSIFIES *resolvable-by-evidence* vs *genuine-decision* → escalate to the **user** ONLY for a certified real decision, as a single boxed choice. Typed terminal states are recorded on `dialogue_turns`. The existing `argosy/quality/promote_gate.py` is wired as the PUBLISH gate: no plan publishes while any open hard/coherence `status_flag` remains.

**Architecture:** This plan sits on top of the Phase-1a engine (`argosy/quality/derivation_graph.py`: `DerivationGraph`/`Node`/`NodeKind` with `add_node/get/hash_of/dependents/check_acyclic/is_valid/set_input/recompute/is_closed`). It adds:
1. A pure **ownership map + adjudication** module `argosy/quality/change_adjudication.py` — author-agnostic `ChangeRequest`, `adjudicate()` (fail-closed authority clearance), and the hard-node guard. No DB, no LLM.
2. A pure **negotiation ladder** module `argosy/orchestrator/flows/negotiation_ladder.py` — `run_ladder()` driving propose → rebut → ≤3 peer rounds → arbiter classify/rule → escalate-to-user, recording typed `LadderTurn`s and a typed terminal state. The peer/arbiter step functions are injected (a `LadderParticipants` protocol) so the engine is unit-testable with deterministic fakes, and wired in production to the same agents `fm_objection_dialogue` uses.
3. **Persistence** for `change_requests` + `dialogue_turns` (SQLAlchemy models + Alembic migration `0071`, head is `0070_tax_simulation_lots`).
4. The **publish gate adapter** `argosy/quality/publish_gate.py::can_publish_plan()` — folds open hard/coherence flags into `promote_gate.evaluate_promotion`.

**Tech Stack:** Python 3.12, stdlib + pydantic dataclasses where the codebase already uses them, SQLAlchemy 2.0 typed ORM, Alembic, pytest. Tests run with `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval"`. New code lives next to its peers: adjudication under `argosy/quality/` (with `derivation_graph.py`, `promote_gate.py`, `rederivation_reviewer.py`); the ladder under `argosy/orchestrator/flows/` (with `fm_objection_dialogue.py`).

**Out of scope for this plan (other phases):** the graph engine itself (Phase 1a — done); resolver/`sections_json` hydration + `propagation_events` ripple trace (Phase 1b–1d); scoped analyst re-runs (Phase 3); the threaded change-request *UI view* (the Replay UI projects the `dialogue_turns` rows we write here — building the React view is a follow-on). This plan delivers the data model + adjudication logic + ladder + publish-gate wiring, all unit-tested with deterministic fakes (no live LLM calls in the test path — that is the `converge_fm_objections` gotcha called out in MEMORY).

**Convention notes (from `docs/design/SDD.md` Quickstart + observed in the codebase):**
- Models import names from `sqlalchemy` (`Integer`, `String`, `Text`, `DateTime`, `ForeignKey`, `Index`) and use `Mapped[...]` / `mapped_column(...)`; `_utcnow()` is the timestamp default. JSON columns are stored as `Text` holding `json.dumps(...)` (see `DecisionRun.notes_json`, `DecisionPhase.participants_json`), NOT a JSON type.
- Migrations: `revision` / `down_revision` strings, `upgrade()` / `downgrade()`. Current head `0070_tax_simulation_lots`; this plan adds `0071_change_requests_dialogue_turns`.
- The DB string for a derived-value author rejection etc. is plain text; no enums in the DB — store as `String`.
- The FM↔analyst convergence already maps `(resolution, stance) → terminal state` in `fm_objection_dialogue._terminal_state`; we reuse the *shape* (typed terminal states) but generalize the speakers from FM/analyst to A/B/arbiter/user.

---

### Task 1: `ChangeRequest` + author-agnostic primitive (pure dataclass)

**Files:**
- Create: `argosy/quality/change_adjudication.py`
- Test: `tests/test_change_adjudication.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_change_adjudication.py
import pytest
from argosy.quality.change_adjudication import (
    ChangeRequest, ChangeKind, Author, AuthorKind,
)


def test_change_request_construction_user_author():
    cr = ChangeRequest(
        target_node_key="swr_pct",
        author=Author(kind=AuthorKind.USER, role="user"),
        kind=ChangeKind.SET_INPUT,
        payload={"value": 0.035},
        rationale="I want a more conservative withdrawal rate.",
    )
    assert cr.target_node_key == "swr_pct"
    assert cr.author.kind is AuthorKind.USER
    assert cr.kind is ChangeKind.SET_INPUT
    assert cr.payload["value"] == 0.035


def test_change_request_construction_agent_author():
    cr = ChangeRequest(
        target_node_key="fi_margin_liquid_nis",
        author=Author(kind=AuthorKind.AGENT, role="fund_manager"),
        kind=ChangeKind.OBJECTION,
        payload={},
        rationale="FI margin looks too thin under the bear track.",
    )
    assert cr.author.kind is AuthorKind.AGENT
    assert cr.author.role == "fund_manager"
    assert cr.kind is ChangeKind.OBJECTION
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_change_adjudication.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'argosy.quality.change_adjudication'`

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/quality/change_adjudication.py
"""Layer 2 — the author-agnostic change / adjudication substrate.

A ChangeRequest is the single primitive any author (user OR agent_role)
writes against a derivation-graph node (argosy/quality/derivation_graph.py).
Adjudication is ownership- and authority-specific and fail-closed:

  * a DerivedValue target is REJECTED ("change the inputs or the recipe");
  * an InputFact / policy change that flips a HARD verdict is itself an
    adjudicated, audited, contestable change-request (anti-laundering);
  * a recipe / policy change routes through the negotiation ladder.

Pure logic — no DB, no LLM, no I/O. See docs/superpowers/specs/
2026-06-18-living-plan-derivation-graph-design.md (Layer 2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AuthorKind(str, Enum):
    USER = "user"
    AGENT = "agent"


class ChangeKind(str, Enum):
    SET_INPUT = "set_input"        # propose a new value for an InputFact
    SET_RECIPE = "set_recipe"      # propose a new recipe/policy on a recipe node
    SET_DERIVED = "set_derived"    # propose to hand-set a DerivedValue (always rejected)
    OBJECTION = "objection"        # a reader/FM/codex finding against a node


@dataclass(frozen=True)
class Author:
    kind: AuthorKind
    role: str  # "user", "fund_manager", "plan_critique", "codex", ...


@dataclass
class ChangeRequest:
    target_node_key: str
    author: Author
    kind: ChangeKind
    payload: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_change_adjudication.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/change_adjudication.py tests/test_change_adjudication.py
git commit -m "feat(adjudication): author-agnostic ChangeRequest primitive"
```

---

### Task 2: Ownership map + node classification (hard vs soft, input vs derived vs recipe)

**Files:**
- Modify: `argosy/quality/change_adjudication.py`
- Test: `tests/test_change_adjudication.py`

- [ ] **Step 1: Write the failing test**

```python
from argosy.quality.change_adjudication import (
    OwnershipMap, NodeClass,
)
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind


def _graph():
    g = DerivationGraph()
    g.add_node(Node(key="liquid_nw_nis", kind=NodeKind.INPUT, value=11_687_926))
    g.add_node(Node(key="swr_pct", kind=NodeKind.INPUT, value=0.035))
    g.add_node(Node(key="fi_margin_liquid_nis", kind=NodeKind.DERIVED,
                    inputs=("liquid_nw_nis", "swr_pct"),
                    recipe=lambda i: i["liquid_nw_nis"]))
    return g


def test_owner_of_default_for_input_is_user():
    g = _graph()
    om = OwnershipMap(g)
    assert om.owner_of("liquid_nw_nis") == "user"


def test_explicit_owner_overrides_default():
    g = _graph()
    om = OwnershipMap(g, owners={"swr_pct": "fund_manager"})
    assert om.owner_of("swr_pct") == "fund_manager"


def test_node_class_derived_is_not_editable():
    g = _graph()
    om = OwnershipMap(g)
    assert om.classify("fi_margin_liquid_nis") is NodeClass.DERIVED
    assert om.classify("liquid_nw_nis") is NodeClass.INPUT


def test_hard_node_flagged_by_registry():
    g = _graph()
    om = OwnershipMap(g, hard_node_keys={"fi_margin_liquid_nis"})
    assert om.is_hard("fi_margin_liquid_nis") is True
    assert om.is_hard("swr_pct") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_change_adjudication.py -q`
Expected: FAIL — `ImportError: cannot import name 'OwnershipMap'`

- [ ] **Step 3: Write minimal implementation**

Add to `argosy/quality/change_adjudication.py`:

```python
from argosy.quality.derivation_graph import DerivationGraph, NodeKind


class NodeClass(str, Enum):
    INPUT = "input"        # an InputFact — adjudicated, editable by its owner
    DERIVED = "derived"    # never hand-editable; change inputs or recipe
    RECIPE = "recipe"      # a recipe/policy node — routes through the ladder
    SURFACE = "surface"    # a rendered consumer


class OwnershipMap:
    """Who owns each node + node classification. The owner is the authority
    that may accept (or reject) a change against the node. INPUT nodes default
    to the user; everything else can be overridden via ``owners``.

    ``hard_node_keys`` marks nodes whose value is math/derivation/coherence and
    can NEVER be "agreed away" — only an (adjudicated) input change or a recipe
    fix moves them. ``recipe_node_keys`` marks nodes whose VALUE is itself a
    policy/recipe (SWR, NVDA cap, a phase assumption) routed through the ladder.
    """

    def __init__(
        self,
        graph: DerivationGraph,
        *,
        owners: dict[str, str] | None = None,
        hard_node_keys: set[str] | None = None,
        recipe_node_keys: set[str] | None = None,
    ) -> None:
        self._graph = graph
        self._owners = dict(owners or {})
        self._hard = set(hard_node_keys or ())
        self._recipe = set(recipe_node_keys or ())

    def owner_of(self, key: str) -> str:
        if key in self._owners:
            return self._owners[key]
        node = self._graph.get(key)
        # InputFacts default to the user/ingest; derived/surface have no
        # human owner (their owner is "the recipe") — but a caller asking for
        # an owner of a derived node still gets a deterministic answer.
        if node.kind is NodeKind.INPUT:
            return "user"
        return "system"

    def classify(self, key: str) -> NodeClass:
        node = self._graph.get(key)
        if key in self._recipe:
            return NodeClass.RECIPE
        if node.kind is NodeKind.INPUT:
            return NodeClass.INPUT
        if node.kind is NodeKind.SURFACE:
            return NodeClass.SURFACE
        return NodeClass.DERIVED

    def is_hard(self, key: str) -> bool:
        return key in self._hard
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_change_adjudication.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/change_adjudication.py tests/test_change_adjudication.py
git commit -m "feat(adjudication): ownership map + node classification (hard/input/derived/recipe)"
```

---

### Task 3: `adjudicate()` rejects a DerivedValue target

**Files:**
- Modify: `argosy/quality/change_adjudication.py`
- Test: `tests/test_change_adjudication.py`

- [ ] **Step 1: Write the failing test**

```python
from argosy.quality.change_adjudication import (
    adjudicate, AdjudicationOutcome, Disposition,
)


def test_setting_a_derived_value_is_rejected():
    g = _graph()
    om = OwnershipMap(g)
    cr = ChangeRequest(
        target_node_key="fi_margin_liquid_nis",
        author=Author(kind=AuthorKind.AGENT, role="codex"),
        kind=ChangeKind.SET_DERIVED,
        payload={"value": 999_999},
        rationale="just make the margin bigger",
    )
    out = adjudicate(cr, om)
    assert out.disposition is Disposition.REJECTED
    assert "change the inputs or the recipe" in out.reason


def test_set_input_on_a_derived_node_is_also_rejected():
    # A SET_INPUT kind aimed at a DERIVED node is the same derive-don't-inherit
    # violation, regardless of the declared kind.
    g = _graph()
    om = OwnershipMap(g)
    cr = ChangeRequest(
        target_node_key="fi_margin_liquid_nis",
        author=Author(kind=AuthorKind.USER, role="user"),
        kind=ChangeKind.SET_INPUT,
        payload={"value": 5},
        rationale="",
    )
    out = adjudicate(cr, om)
    assert out.disposition is Disposition.REJECTED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_change_adjudication.py -q`
Expected: FAIL — `ImportError: cannot import name 'adjudicate'`

- [ ] **Step 3: Write minimal implementation**

Add to `argosy/quality/change_adjudication.py`:

```python
class Disposition(str, Enum):
    ACCEPTED = "accepted"            # apply directly (input change, owner-clear)
    REJECTED = "rejected"            # fail-closed; cannot proceed
    NEEDS_LADDER = "needs_ladder"    # route to the negotiation ladder
    NEEDS_AUDIT = "needs_audit"      # verdict-flipping input change — audited + contestable


@dataclass
class AdjudicationOutcome:
    disposition: Disposition
    reason: str = ""


def adjudicate(cr: ChangeRequest, owners: OwnershipMap) -> AdjudicationOutcome:
    """Route a ChangeRequest by ownership + node class, fail-closed.

    A DerivedValue is never hand-editable: any attempt to set it (by ANY
    author, regardless of declared kind) is rejected with the canonical
    "change the inputs or the recipe" message. Other dispositions land in
    later tasks (recipe → ladder; verdict-flipping input → audit).
    """
    node_class = owners.classify(cr.target_node_key)
    if node_class is NodeClass.DERIVED or cr.kind is ChangeKind.SET_DERIVED:
        return AdjudicationOutcome(
            Disposition.REJECTED,
            "a DerivedValue is not directly editable — change the inputs or the recipe",
        )
    return AdjudicationOutcome(Disposition.ACCEPTED)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_change_adjudication.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/change_adjudication.py tests/test_change_adjudication.py
git commit -m "feat(adjudication): reject any hand-set of a DerivedValue (derive-don't-inherit)"
```

---

### Task 4: Recipe/policy change routes to the ladder; verdict-flipping input change needs audit

**Files:**
- Modify: `argosy/quality/change_adjudication.py`
- Test: `tests/test_change_adjudication.py`

- [ ] **Step 1: Write the failing test**

```python
def test_recipe_change_routes_to_ladder():
    g = _graph()
    om = OwnershipMap(g, recipe_node_keys={"swr_pct"})
    cr = ChangeRequest(
        target_node_key="swr_pct",
        author=Author(kind=AuthorKind.AGENT, role="plan_critique"),
        kind=ChangeKind.SET_RECIPE,
        payload={"value": 0.04},
        rationale="3.5% SWR is over-conservative for a 30y horizon.",
    )
    out = adjudicate(cr, om)
    assert out.disposition is Disposition.NEEDS_LADDER


def test_plain_input_change_is_accepted():
    g = _graph()
    om = OwnershipMap(g)
    cr = ChangeRequest(
        target_node_key="liquid_nw_nis",
        author=Author(kind=AuthorKind.AGENT, role="portfolio_ingest"),
        kind=ChangeKind.SET_INPUT,
        payload={"value": 11_900_000},
        rationale="refreshed holdings snapshot",
    )
    out = adjudicate(cr, om)
    assert out.disposition is Disposition.ACCEPTED


def test_verdict_flipping_input_change_needs_audit():
    # An input change that would flip a HARD verdict downstream is itself an
    # adjudicated, audited, contestable change — anti-laundering.
    g = _graph()
    om = OwnershipMap(
        g, hard_node_keys={"fi_margin_liquid_nis"},
    )
    cr = ChangeRequest(
        target_node_key="liquid_nw_nis",
        author=Author(kind=AuthorKind.AGENT, role="plan_critique"),
        kind=ChangeKind.SET_INPUT,
        payload={"value": 50_000_000},  # would flip FI sufficiency
        rationale="",                    # no evidence supplied
    )
    out = adjudicate(cr, om, flips_hard_verdict=True)
    assert out.disposition is Disposition.NEEDS_AUDIT
    assert "audited" in out.reason


def test_verdict_flipping_input_with_evidence_still_audited_not_accepted():
    # Even WITH a rationale, a verdict-flipping premise edit is on the record
    # and contestable — it never silently auto-applies.
    g = _graph()
    om = OwnershipMap(g, hard_node_keys={"fi_margin_liquid_nis"})
    cr = ChangeRequest(
        target_node_key="liquid_nw_nis",
        author=Author(kind=AuthorKind.USER, role="user"),
        kind=ChangeKind.SET_INPUT,
        payload={"value": 50_000_000},
        rationale="inheritance received, see bank statement",
    )
    out = adjudicate(cr, om, flips_hard_verdict=True)
    assert out.disposition is Disposition.NEEDS_AUDIT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_change_adjudication.py -q`
Expected: FAIL — `test_recipe_change_routes_to_ladder` gets `ACCEPTED`; `adjudicate()` has no `flips_hard_verdict` kwarg (`TypeError`).

- [ ] **Step 3: Write minimal implementation**

Replace the body of `adjudicate` in `argosy/quality/change_adjudication.py`:

```python
def adjudicate(
    cr: ChangeRequest,
    owners: OwnershipMap,
    *,
    flips_hard_verdict: bool = False,
) -> AdjudicationOutcome:
    """Route a ChangeRequest by ownership + node class, fail-closed.

    Order of checks:
      1. DerivedValue / SET_DERIVED  -> REJECTED (derive-don't-inherit).
      2. Recipe/policy node          -> NEEDS_LADDER (negotiated, not commanded).
      3. Input change that flips a   -> NEEDS_AUDIT (anti-laundering: on the
         hard verdict downstream         record + contestable by owner/arbiter).
      4. Plain input change          -> ACCEPTED.

    ``flips_hard_verdict`` is supplied by the caller (the propagation layer
    knows the blast radius); when an InputFact edit's transitive dependents
    include a hard node whose verdict would change sign, the premise edit is
    not silently accepted.
    """
    node_class = owners.classify(cr.target_node_key)
    if node_class is NodeClass.DERIVED or cr.kind is ChangeKind.SET_DERIVED:
        return AdjudicationOutcome(
            Disposition.REJECTED,
            "a DerivedValue is not directly editable — change the inputs or the recipe",
        )
    if node_class is NodeClass.RECIPE:
        return AdjudicationOutcome(
            Disposition.NEEDS_LADDER,
            "recipe/policy change is negotiated through the ladder, not commanded",
        )
    if node_class is NodeClass.INPUT and flips_hard_verdict:
        return AdjudicationOutcome(
            Disposition.NEEDS_AUDIT,
            "premise edit flips a hard verdict — audited + contestable by the owner/arbiter",
        )
    return AdjudicationOutcome(Disposition.ACCEPTED)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_change_adjudication.py -q`
Expected: PASS (12 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/change_adjudication.py tests/test_change_adjudication.py
git commit -m "feat(adjudication): recipe->ladder routing + anti-laundering audit on verdict-flipping inputs"
```

---

### Task 5: Hard-node guard — a hard node cannot be "agreed away"

**Files:**
- Modify: `argosy/quality/change_adjudication.py`
- Test: `tests/test_change_adjudication.py`

- [ ] **Step 1: Write the failing test**

```python
from argosy.quality.change_adjudication import HardNodeError, assert_resolvable


def test_hard_node_cannot_be_agreed_away_by_concession():
    g = _graph()
    om = OwnershipMap(g, hard_node_keys={"fi_margin_liquid_nis"})
    # An OBJECTION directly against a hard derived node can only be resolved by
    # fixing an input or the recipe — never by a peer simply conceding the value.
    with pytest.raises(HardNodeError):
        assert_resolvable(
            target_node_key="fi_margin_liquid_nis",
            owners=om,
            resolution_kind="concede_value",
        )


def test_hard_node_resolvable_by_input_fix():
    g = _graph()
    om = OwnershipMap(g, hard_node_keys={"fi_margin_liquid_nis"})
    # No raise — fixing an input is a legitimate resolution for a hard node.
    assert_resolvable(
        target_node_key="fi_margin_liquid_nis",
        owners=om,
        resolution_kind="fix_input",
    )


def test_soft_node_can_be_conceded():
    g = _graph()
    om = OwnershipMap(g)  # nothing marked hard
    assert_resolvable(
        target_node_key="swr_pct",
        owners=om,
        resolution_kind="concede_value",
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_change_adjudication.py -q`
Expected: FAIL — `ImportError: cannot import name 'HardNodeError'`

- [ ] **Step 3: Write minimal implementation**

Add to `argosy/quality/change_adjudication.py`:

```python
class HardNodeError(Exception):
    """A hard (math/derivation/coherence) node cannot be 'agreed away' — the
    only resolutions are fixing an input (adjudicated) or fixing the recipe."""


# Resolution kinds the ladder may apply to a node.
_VALUE_CONCESSION_KINDS = {"concede_value", "agree_value"}
_STRUCTURAL_FIX_KINDS = {"fix_input", "fix_recipe", "rederive"}


def assert_resolvable(
    *, target_node_key: str, owners: OwnershipMap, resolution_kind: str,
) -> None:
    """Guard before a ladder applies a resolution. Raises HardNodeError if a
    HARD node is being resolved by simply conceding/agreeing a value rather
    than by a structural fix (input or recipe)."""
    if owners.is_hard(target_node_key) and resolution_kind in _VALUE_CONCESSION_KINDS:
        raise HardNodeError(
            f"{target_node_key} is a hard node — resolve by fixing an input or "
            f"the recipe, not by conceding the value ({resolution_kind!r})"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_change_adjudication.py -q`
Expected: PASS (15 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/change_adjudication.py tests/test_change_adjudication.py
git commit -m "feat(adjudication): hard-node guard — never agreed away, only input/recipe fix"
```

---

### Task 6: Negotiation ladder — typed turns + terminal states (propose/rebut/round model)

**Files:**
- Create: `argosy/orchestrator/flows/negotiation_ladder.py`
- Test: `tests/test_negotiation_ladder.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_negotiation_ladder.py
import pytest
from argosy.orchestrator.flows.negotiation_ladder import (
    LadderTurn, TerminalState, Speaker, Stance,
)


def test_ladder_turn_fields():
    t = LadderTurn(
        round=1, speaker=Speaker.A, stance=Stance.PROPOSE,
        text="change swr_pct to 0.04 because the horizon is 30y",
        cited_nodes=["swr_pct"],
    )
    assert t.round == 1
    assert t.speaker is Speaker.A
    assert t.stance is Stance.PROPOSE
    assert t.cited_nodes == ["swr_pct"]


def test_terminal_states_enumerated():
    # The five typed terminal states from the spec data model.
    assert {s.value for s in TerminalState} >= {
        "A_conceded", "B_conceded", "arbiter_ruled",
        "escalated_to_user", "superseded",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_negotiation_ladder.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'argosy.orchestrator.flows.negotiation_ladder'`

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/orchestrator/flows/negotiation_ladder.py
"""The bounded negotiation ladder (Layer 2).

Generalizes argosy/orchestrator/flows/fm_objection_dialogue.py
(converge_fm_objections) from the fixed FM<->analyst pair to ANY author pair:

  1. A files "change X because Y" against a node B owns.
  2. B may REBUT the rationale Y itself (not just the value). The burden is on
     A to defend Y; "A said so" never wins.
  3. Bounded peer rounds A<->B, n = MAX_PEER_ROUNDS (3).
  4. Unresolved -> escalate to the ARBITER (FM), which CLASSIFIES the conflict:
     resolvable-by-evidence -> it rules + applies (stays in the fleet);
     genuine judgment call    -> escalate up.
  5. Escalate to the USER -- last rung, ONLY for a certified real decision,
     surfaced as a single boxed choice.

Every step is recorded as a typed LadderTurn; the ladder ends in exactly one
typed TerminalState. The peer/arbiter step functions are INJECTED (the
LadderParticipants protocol) so the engine is deterministic + unit-testable;
production wires them to the same agents fm_objection_dialogue uses.

Pure orchestration — no DB writes here; the caller persists the returned turns
+ terminal state via change_request_store (Task 9/10). No direct LLM calls in
this module (that is the converge_fm_objections gotcha: a real claude.exe call
in the synthesis-flow path hangs the tests). Participants are the LLM seam.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


MAX_PEER_ROUNDS = 3  # spec: bounded peer rounds n = 3


class Speaker(str, Enum):
    A = "A"
    B = "B"
    ARBITER = "arbiter"
    USER = "user"


class Stance(str, Enum):
    PROPOSE = "propose"
    REBUT = "rebut"
    CONCEDE = "concede"
    RULE = "rule"
    CLASSIFY = "classify"
    ASK = "ask"
    ANSWER = "answer"


class TerminalState(str, Enum):
    A_CONCEDED = "A_conceded"
    B_CONCEDED = "B_conceded"
    ARBITER_RULED = "arbiter_ruled"
    ESCALATED_TO_USER = "escalated_to_user"
    SUPERSEDED = "superseded"


@dataclass
class LadderTurn:
    round: int
    speaker: Speaker
    stance: Stance
    text: str
    cited_nodes: list[str] = field(default_factory=list)


class PeerVerdict(str, Enum):
    """Outcome of one peer (B) reply to A's standing proposal/defense."""
    B_CONCEDES = "b_concedes"      # B accepts A's change
    A_CONCEDES = "a_concedes"      # B's rebuttal lands; A withdraws
    UNRESOLVED = "unresolved"      # neither side yields this round


class ArbiterClass(str, Enum):
    EVIDENCE_RESOLVABLE = "evidence_resolvable"  # arbiter rules + applies
    GENUINE_DECISION = "genuine_decision"        # escalate to the user
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_negotiation_ladder.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/orchestrator/flows/negotiation_ladder.py tests/test_negotiation_ladder.py
git commit -m "feat(ladder): typed LadderTurn + Speaker/Stance/TerminalState + arbiter classes"
```

---

### Task 7: `run_ladder()` — peer round resolution (B concedes / A concedes within n=3)

**Files:**
- Modify: `argosy/orchestrator/flows/negotiation_ladder.py`
- Test: `tests/test_negotiation_ladder.py`

- [ ] **Step 1: Write the failing test**

```python
from argosy.orchestrator.flows.negotiation_ladder import (
    run_ladder, LadderResult, LadderParticipants, PeerVerdict, ArbiterClass,
)


class _FakeParticipants:
    """Deterministic test double for the LLM seam."""
    def __init__(self, peer_sequence, arbiter_class=None, arbiter_ruling=""):
        self._peer = list(peer_sequence)
        self._arbiter_class = arbiter_class
        self._arbiter_ruling = arbiter_ruling
        self.peer_calls = 0
        self.arbiter_calls = 0

    def peer_round(self, *, change, prior_turns, round):
        self.peer_calls += 1
        verdict, text = self._peer[round - 1]
        return verdict, text

    def arbiter(self, *, change, prior_turns):
        self.arbiter_calls += 1
        return self._arbiter_class, self._arbiter_ruling


def _change():
    from argosy.quality.change_adjudication import (
        ChangeRequest, ChangeKind, Author, AuthorKind,
    )
    return ChangeRequest(
        target_node_key="swr_pct",
        author=Author(kind=AuthorKind.AGENT, role="plan_critique"),
        kind=ChangeKind.SET_RECIPE,
        payload={"value": 0.04},
        rationale="3.5% is over-conservative for a 30y horizon",
    )


def test_b_concedes_in_round_one():
    parts = _FakeParticipants([(PeerVerdict.B_CONCEDES, "you're right, 0.04 is defensible")])
    res = run_ladder(_change(), parts)
    assert res.terminal_state.value == "B_conceded"
    assert parts.peer_calls == 1
    assert parts.arbiter_calls == 0
    # The first turn is A's PROPOSE; the second is B's CONCEDE.
    assert res.turns[0].stance.value == "propose"
    assert res.turns[-1].stance.value == "concede"


def test_a_concedes_when_rebuttal_lands():
    parts = _FakeParticipants([(PeerVerdict.A_CONCEDES, "fair, the horizon assumption was wrong")])
    res = run_ladder(_change(), parts)
    assert res.terminal_state.value == "A_conceded"
    assert parts.arbiter_calls == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_negotiation_ladder.py -q`
Expected: FAIL — `ImportError: cannot import name 'run_ladder'`

- [ ] **Step 3: Write minimal implementation**

Add to `argosy/orchestrator/flows/negotiation_ladder.py`:

```python
from argosy.quality.change_adjudication import ChangeRequest


class LadderParticipants(Protocol):
    """The LLM seam. Production wires these to the analyst-responder /
    FM-verdict agents; tests inject a deterministic double."""

    def peer_round(
        self, *, change: ChangeRequest, prior_turns: list[LadderTurn], round: int,
    ) -> tuple[PeerVerdict, str]:
        ...

    def arbiter(
        self, *, change: ChangeRequest, prior_turns: list[LadderTurn],
    ) -> tuple[ArbiterClass, str]:
        ...


@dataclass
class LadderResult:
    terminal_state: TerminalState
    turns: list[LadderTurn]
    arbiter_class: ArbiterClass | None = None
    user_question: str | None = None  # the boxed choice, when escalated_to_user


def run_ladder(change: ChangeRequest, participants: LadderParticipants) -> LadderResult:
    """Drive the bounded ladder. Records every turn; ends in one TerminalState.

    A opens with PROPOSE ("change X because Y"). Then up to MAX_PEER_ROUNDS
    peer rounds: B may CONCEDE (-> B_conceded), B's rebuttal may land and A
    CONCEDE (-> A_conceded), or the round is UNRESOLVED and we continue.
    Arbiter + user rungs land in Task 8.
    """
    turns: list[LadderTurn] = [
        LadderTurn(
            round=0, speaker=Speaker.A, stance=Stance.PROPOSE,
            text=f"change {change.target_node_key} because {change.rationale}",
            cited_nodes=[change.target_node_key],
        )
    ]

    for rnd in range(1, MAX_PEER_ROUNDS + 1):
        verdict, text = participants.peer_round(
            change=change, prior_turns=turns, round=rnd,
        )
        if verdict is PeerVerdict.B_CONCEDES:
            turns.append(LadderTurn(rnd, Speaker.B, Stance.CONCEDE, text,
                                    [change.target_node_key]))
            return LadderResult(TerminalState.B_CONCEDED, turns)
        if verdict is PeerVerdict.A_CONCEDES:
            turns.append(LadderTurn(rnd, Speaker.B, Stance.REBUT, text,
                                    [change.target_node_key]))
            turns.append(LadderTurn(rnd, Speaker.A, Stance.CONCEDE,
                                    "rebuttal accepted; withdrawing",
                                    [change.target_node_key]))
            return LadderResult(TerminalState.A_CONCEDED, turns)
        # UNRESOLVED — record B's rebuttal and continue.
        turns.append(LadderTurn(rnd, Speaker.B, Stance.REBUT, text,
                                [change.target_node_key]))

    # Falls through to arbiter in Task 8.
    raise NotImplementedError("arbiter escalation lands in Task 8")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_negotiation_ladder.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/orchestrator/flows/negotiation_ladder.py tests/test_negotiation_ladder.py
git commit -m "feat(ladder): run_ladder peer rounds — A/B concession within n=3"
```

---

### Task 8: `run_ladder()` — n=3 escalation to arbiter; arbiter routing (evidence vs user)

**Files:**
- Modify: `argosy/orchestrator/flows/negotiation_ladder.py`
- Test: `tests/test_negotiation_ladder.py`

- [ ] **Step 1: Write the failing test**

```python
def test_escalates_to_arbiter_after_three_unresolved_rounds():
    parts = _FakeParticipants(
        [(PeerVerdict.UNRESOLVED, "no"),
         (PeerVerdict.UNRESOLVED, "still no"),
         (PeerVerdict.UNRESOLVED, "we disagree")],
        arbiter_class=ArbiterClass.EVIDENCE_RESOLVABLE,
        arbiter_ruling="re-derive from the 30y horizon table; A's 0.04 is supported",
    )
    res = run_ladder(_change(), parts)
    assert parts.peer_calls == 3          # exactly n=3 peer rounds
    assert parts.arbiter_calls == 1       # then escalate
    assert res.terminal_state.value == "arbiter_ruled"
    assert res.arbiter_class is ArbiterClass.EVIDENCE_RESOLVABLE
    assert "re-derive" in res.turns[-1].text
    assert res.turns[-1].speaker.value == "arbiter"


def test_arbiter_routes_genuine_decision_to_user():
    parts = _FakeParticipants(
        [(PeerVerdict.UNRESOLVED, "no")] * 3,
        arbiter_class=ArbiterClass.GENUINE_DECISION,
        arbiter_ruling="this is a risk-tolerance call only the client can make",
    )
    res = run_ladder(_change(), parts)
    assert res.terminal_state.value == "escalated_to_user"
    assert res.arbiter_class is ArbiterClass.GENUINE_DECISION
    assert res.user_question  # a single boxed choice was produced
    # The transcript shows the arbiter CLASSIFY turn then the USER ASK turn.
    stances = [t.stance.value for t in res.turns]
    assert "classify" in stances
    assert res.turns[-1].speaker.value == "user"
    assert res.turns[-1].stance.value == "ask"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_negotiation_ladder.py -q`
Expected: FAIL — `NotImplementedError: arbiter escalation lands in Task 8`

- [ ] **Step 3: Write minimal implementation**

Replace the trailing `raise NotImplementedError(...)` in `run_ladder` with:

```python
    # Unresolved after n=3 — escalate to the arbiter (FM).
    arbiter_class, ruling = participants.arbiter(change=change, prior_turns=turns)
    turns.append(LadderTurn(
        round=MAX_PEER_ROUNDS + 1, speaker=Speaker.ARBITER, stance=Stance.CLASSIFY,
        text=f"classification: {arbiter_class.value}",
        cited_nodes=[change.target_node_key],
    ))
    if arbiter_class is ArbiterClass.EVIDENCE_RESOLVABLE:
        turns.append(LadderTurn(
            round=MAX_PEER_ROUNDS + 1, speaker=Speaker.ARBITER, stance=Stance.RULE,
            text=ruling, cited_nodes=[change.target_node_key],
        ))
        return LadderResult(TerminalState.ARBITER_RULED, turns,
                            arbiter_class=arbiter_class)

    # Genuine decision — escalate to the user as a single boxed choice.
    question = (
        f"Decision needed on {change.target_node_key}: {ruling} "
        f"(proposed: {change.payload.get('value')!r}). How would you like to proceed?"
    )
    turns.append(LadderTurn(
        round=MAX_PEER_ROUNDS + 2, speaker=Speaker.USER, stance=Stance.ASK,
        text=question, cited_nodes=[change.target_node_key],
    ))
    return LadderResult(TerminalState.ESCALATED_TO_USER, turns,
                        arbiter_class=arbiter_class, user_question=question)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_negotiation_ladder.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/orchestrator/flows/negotiation_ladder.py tests/test_negotiation_ladder.py
git commit -m "feat(ladder): n=3 -> arbiter classify/rule; genuine-decision -> single boxed user choice"
```

---

### Task 9: Persistence model + migration 0071 (`change_requests` + `dialogue_turns`)

**Files:**
- Modify: `argosy/state/models.py`
- Create: `alembic/versions/0071_change_requests_dialogue_turns.py`
- Test: `tests/test_change_request_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_change_request_models.py
import json
from datetime import datetime, timezone

from argosy.state.models import ChangeRequestRow, DialogueTurnRow

# tests/conftest.py provides `db_session_with_seeded_user` — a SQLAlchemy
# Session over a file-backed SQLite built with Base.metadata.create_all,
# seeded with User(id="test"). It yields the session; the user id is the
# literal "test". We alias it locally so the tests read cleanly.
SEED_USER = "test"


def test_change_request_row_roundtrip(db_session_with_seeded_user):
    s = db_session_with_seeded_user
    row = ChangeRequestRow(
        user_id=SEED_USER,
        plan_version_id=None,
        target_node_key="swr_pct",
        author="agent:plan_critique",
        kind="set_recipe",
        payload_json=json.dumps({"value": 0.04}),
        rationale="over-conservative",
        status="proposed",
        round_count=0,
        created_at=datetime.now(timezone.utc),
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    assert row.id is not None
    assert row.status == "proposed"


def test_dialogue_turn_row_links_to_change_request(db_session_with_seeded_user):
    s = db_session_with_seeded_user
    cr = ChangeRequestRow(
        user_id=SEED_USER, target_node_key="swr_pct", author="user",
        kind="set_recipe", payload_json="{}", rationale="", status="in_dialogue",
        round_count=1, created_at=datetime.now(timezone.utc),
    )
    s.add(cr)
    s.commit()
    s.refresh(cr)
    turn = DialogueTurnRow(
        change_request_id=cr.id, round=1, speaker="A", stance="propose",
        text="change swr_pct", cited_nodes_json=json.dumps(["swr_pct"]),
        created_at=datetime.now(timezone.utc),
    )
    s.add(turn)
    s.commit()
    s.refresh(turn)
    assert turn.change_request_id == cr.id
    assert turn.speaker == "A"
```

> NOTE: `db_session_with_seeded_user` is the canonical session fixture in `tests/conftest.py` (line ~241) — it builds the schema via `Base.metadata.create_all(engine)`, so the new `change_requests`/`dialogue_turns` tables appear automatically once their ORM models are defined; no `alembic upgrade` is needed in the unit-test path. The seeded user id is the literal `"test"`.

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_change_request_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'ChangeRequestRow'`

- [ ] **Step 3: Write minimal implementation**

Add to `argosy/state/models.py` (after the existing model classes, before any trailing `__all__` if present):

```python
class ChangeRequestRow(Base):
    """A persisted ChangeRequest (Layer 2). Author-agnostic: a user OR an
    agent_role proposes a change against a derivation-graph node. ``status``
    walks the negotiation ladder and ends in a typed terminal state. See
    argosy/quality/change_adjudication.py + orchestrator/flows/negotiation_ladder.py.
    """

    __tablename__ = "change_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_version_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("plan_versions.id", ondelete="SET NULL"), nullable=True
    )
    target_node_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # "user" or "agent:<role>".
    author: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # proposed|in_dialogue|escalated_arbiter|escalated_user|A_conceded|
    # B_conceded|arbiter_ruled|superseded
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="proposed", server_default="proposed"
    )
    round_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    adjudicated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    terminal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DialogueTurnRow(Base):
    """One turn in a change-request's negotiation thread (Layer 5 replay).
    speaker ∈ {A,B,arbiter,user}; stance ∈ {propose,rebut,concede,rule,
    classify,ask,answer}."""

    __tablename__ = "dialogue_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    change_request_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("change_requests.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    speaker: Mapped[str] = mapped_column(String(16), nullable=False)
    stance: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cited_nodes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
```

Then create `alembic/versions/0071_change_requests_dialogue_turns.py`:

```python
"""change_requests + dialogue_turns — Layer-2 adjudication substrate

Revision ID: 0071_change_requests_dialogue_turns
Revises: 0070_tax_simulation_lots
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0071_change_requests_dialogue_turns"
down_revision: str | None = "0070_tax_simulation_lots"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "change_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_version_id", sa.Integer(),
                  sa.ForeignKey("plan_versions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("target_node_key", sa.String(length=128), nullable=False),
        sa.Column("author", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="proposed"),
        sa.Column("round_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("adjudicated_by", sa.String(length=64), nullable=True),
        sa.Column("terminal_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_change_requests_user_id", "change_requests", ["user_id"])
    op.create_table(
        "dialogue_turns",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("change_request_id", sa.Integer(),
                  sa.ForeignKey("change_requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("speaker", sa.String(length=16), nullable=False),
        sa.Column("stance", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("cited_nodes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dialogue_turns_change_request_id", "dialogue_turns",
                    ["change_request_id"])


def downgrade() -> None:
    op.drop_index("ix_dialogue_turns_change_request_id", table_name="dialogue_turns")
    op.drop_table("dialogue_turns")
    op.drop_index("ix_change_requests_user_id", table_name="change_requests")
    op.drop_table("change_requests")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_change_request_models.py -q`
Expected: PASS (2 passed). If the test fixture builds the schema via `Base.metadata.create_all` it is green immediately; if it migrates, first run `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m alembic upgrade head` and confirm it lands on `0071_change_requests_dialogue_turns`.

- [ ] **Step 5: Commit**

```bash
git add argosy/state/models.py alembic/versions/0071_change_requests_dialogue_turns.py tests/test_change_request_models.py
git commit -m "feat(adjudication): persist change_requests + dialogue_turns (migration 0071)"
```

---

### Task 10: Change-request store — persist a ladder result + its turns (typed terminal state recorded)

**Files:**
- Create: `argosy/quality/change_request_store.py`
- Test: `tests/test_change_request_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_change_request_store.py
import json

from argosy.quality.change_adjudication import (
    ChangeRequest, ChangeKind, Author, AuthorKind,
)
from argosy.quality.change_request_store import (
    open_change_request, record_ladder_result, load_thread,
)
from argosy.orchestrator.flows.negotiation_ladder import (
    LadderResult, LadderTurn, Speaker, Stance, TerminalState,
)


SEED_USER = "test"  # db_session_with_seeded_user seeds User(id="test")


def _cr():
    return ChangeRequest(
        target_node_key="swr_pct",
        author=Author(kind=AuthorKind.AGENT, role="plan_critique"),
        kind=ChangeKind.SET_RECIPE,
        payload={"value": 0.04},
        rationale="over-conservative",
    )


def test_open_then_record_persists_terminal_state_and_turns(db_session_with_seeded_user):
    s = db_session_with_seeded_user
    row_id = open_change_request(s, user_id=SEED_USER, plan_version_id=None, cr=_cr())
    result = LadderResult(
        terminal_state=TerminalState.B_CONCEDED,
        turns=[
            LadderTurn(0, Speaker.A, Stance.PROPOSE, "change swr_pct", ["swr_pct"]),
            LadderTurn(1, Speaker.B, Stance.CONCEDE, "agreed", ["swr_pct"]),
        ],
    )
    record_ladder_result(s, change_request_id=row_id, result=result)

    thread = load_thread(s, change_request_id=row_id)
    assert thread["status"] == "B_conceded"
    assert [t["speaker"] for t in thread["turns"]] == ["A", "B"]
    assert thread["turns"][0]["cited_nodes"] == ["swr_pct"]


def test_recorded_author_encodes_role(db_session_with_seeded_user):
    s = db_session_with_seeded_user
    row_id = open_change_request(s, user_id=SEED_USER, plan_version_id=None, cr=_cr())
    thread = load_thread(s, change_request_id=row_id)
    assert thread["author"] == "agent:plan_critique"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_change_request_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'argosy.quality.change_request_store'`

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/quality/change_request_store.py
"""Persistence helpers for Layer-2 change-requests + their negotiation threads.

Maps the in-memory ChangeRequest / LadderResult onto the ChangeRequestRow /
DialogueTurnRow tables (migration 0071), and reloads a thread for the Replay
view. The terminal TerminalState is written onto the change_request's status
so a settled dispute cannot silently reopen.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.orchestrator.flows.negotiation_ladder import LadderResult
from argosy.quality.change_adjudication import (
    Author, AuthorKind, ChangeRequest,
)
from argosy.state.models import ChangeRequestRow, DialogueTurnRow


def _encode_author(author: Author) -> str:
    if author.kind is AuthorKind.USER:
        return "user"
    return f"agent:{author.role}"


def open_change_request(
    session: Session, *, user_id: str, plan_version_id: int | None, cr: ChangeRequest,
) -> int:
    row = ChangeRequestRow(
        user_id=user_id,
        plan_version_id=plan_version_id,
        target_node_key=cr.target_node_key,
        author=_encode_author(cr.author),
        kind=cr.kind.value,
        payload_json=json.dumps(cr.payload, default=str),
        rationale=cr.rationale or "",
        status="proposed",
        round_count=0,
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.id


def record_ladder_result(
    session: Session, *, change_request_id: int, result: LadderResult,
) -> None:
    """Persist every LadderTurn and stamp the typed terminal state on the row."""
    for t in result.turns:
        session.add(DialogueTurnRow(
            change_request_id=change_request_id,
            round=t.round,
            speaker=t.speaker.value,
            stance=t.stance.value,
            text=t.text,
            cited_nodes_json=json.dumps(t.cited_nodes, default=str),
            created_at=datetime.now(timezone.utc),
        ))
    row = session.get(ChangeRequestRow, change_request_id)
    if row is not None:
        row.status = result.terminal_state.value
        row.round_count = max((t.round for t in result.turns), default=0)
        row.terminal_reason = result.user_question or (
            result.arbiter_class.value if result.arbiter_class else None
        )
        row.updated_at = datetime.now(timezone.utc)
    session.commit()


def load_thread(session: Session, *, change_request_id: int) -> dict:
    """Reconstruct the full replayable thread for a change-request."""
    row = session.get(ChangeRequestRow, change_request_id)
    if row is None:
        raise KeyError(f"change_request {change_request_id} not found")
    turns = session.execute(
        select(DialogueTurnRow)
        .where(DialogueTurnRow.change_request_id == change_request_id)
        .order_by(DialogueTurnRow.id)
    ).scalars().all()
    return {
        "id": row.id,
        "target_node_key": row.target_node_key,
        "author": row.author,
        "kind": row.kind,
        "status": row.status,
        "terminal_reason": row.terminal_reason,
        "turns": [
            {
                "round": t.round,
                "speaker": t.speaker,
                "stance": t.stance,
                "text": t.text,
                "cited_nodes": json.loads(t.cited_nodes_json or "[]"),
            }
            for t in turns
        ],
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_change_request_store.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/change_request_store.py tests/test_change_request_store.py
git commit -m "feat(adjudication): change_request_store — persist ladder turns + typed terminal state"
```

---

### Task 11: Superseded objection cannot reopen

**Files:**
- Modify: `argosy/quality/change_request_store.py`
- Test: `tests/test_change_request_store.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest
from argosy.quality.change_request_store import (
    supersede_change_request, ReopenError, assert_reopenable,
)


def test_superseded_request_cannot_reopen(db_session_with_seeded_user):
    s = db_session_with_seeded_user
    row_id = open_change_request(s, user_id=SEED_USER, plan_version_id=None, cr=_cr())
    supersede_change_request(s, change_request_id=row_id,
                             reason="resolved by a later input refresh")
    thread = load_thread(s, change_request_id=row_id)
    assert thread["status"] == "superseded"
    with pytest.raises(ReopenError):
        assert_reopenable(s, change_request_id=row_id)


def test_concluded_request_cannot_reopen(db_session_with_seeded_user):
    from argosy.orchestrator.flows.negotiation_ladder import (
        LadderResult, LadderTurn, Speaker, Stance, TerminalState,
    )
    s = db_session_with_seeded_user
    row_id = open_change_request(s, user_id=SEED_USER, plan_version_id=None, cr=_cr())
    record_ladder_result(s, change_request_id=row_id, result=LadderResult(
        terminal_state=TerminalState.ARBITER_RULED,
        turns=[LadderTurn(0, Speaker.A, Stance.PROPOSE, "x", ["swr_pct"])],
    ))
    with pytest.raises(ReopenError):
        assert_reopenable(s, change_request_id=row_id)


def test_in_dialogue_request_is_reopenable(db_session_with_seeded_user):
    s = db_session_with_seeded_user
    row_id = open_change_request(s, user_id=SEED_USER, plan_version_id=None, cr=_cr())
    # Default status is "proposed" — not terminal, so reopen is allowed.
    assert_reopenable(s, change_request_id=row_id)  # no raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_change_request_store.py -q`
Expected: FAIL — `ImportError: cannot import name 'supersede_change_request'`

- [ ] **Step 3: Write minimal implementation**

Add to `argosy/quality/change_request_store.py`:

```python
class ReopenError(Exception):
    """A settled (terminal) change-request cannot silently reopen."""


# Statuses past which a change-request is settled and must not reopen.
_TERMINAL_STATUSES = {
    "A_conceded", "B_conceded", "arbiter_ruled", "superseded",
}


def supersede_change_request(
    session: Session, *, change_request_id: int, reason: str = "",
) -> None:
    row = session.get(ChangeRequestRow, change_request_id)
    if row is None:
        raise KeyError(f"change_request {change_request_id} not found")
    row.status = "superseded"
    row.terminal_reason = reason or row.terminal_reason
    row.updated_at = datetime.now(timezone.utc)
    session.commit()


def assert_reopenable(session: Session, *, change_request_id: int) -> None:
    """Raise ReopenError if the change-request is already in a terminal state."""
    row = session.get(ChangeRequestRow, change_request_id)
    if row is None:
        raise KeyError(f"change_request {change_request_id} not found")
    if row.status in _TERMINAL_STATUSES:
        raise ReopenError(
            f"change_request {change_request_id} is terminal ({row.status}); "
            "a settled dispute cannot reopen"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_change_request_store.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/change_request_store.py tests/test_change_request_store.py
git commit -m "feat(adjudication): terminal change-requests cannot reopen (no silent re-litigation)"
```

---

### Task 12: Publish gate — no open hard/coherence flag may publish

**Files:**
- Create: `argosy/quality/publish_gate.py`
- Test: `tests/test_publish_gate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_publish_gate.py
from argosy.quality.publish_gate import can_publish_plan, OpenFlag


_CLEAR_AUTHORITIES = {
    "codex": "APPROVE",
    "deterministic_gate": "pass",
    "fund_manager": "approve",
    "whole_artifact_reader": "approve",
    "rederivation": "ok",
}


def test_publishable_when_all_clear_and_no_open_flag():
    decision = can_publish_plan(authorities=_CLEAR_AUTHORITIES, open_flags=[])
    assert decision.can_promote is True
    assert decision.blocking_authorities == []


def test_open_hard_flag_blocks_even_when_authorities_clear():
    flags = [OpenFlag(node_key="fi_margin_liquid_nis", kind="hard")]
    decision = can_publish_plan(authorities=_CLEAR_AUTHORITIES, open_flags=flags)
    assert decision.can_promote is False
    assert any("fi_margin_liquid_nis" in r for r in decision.reasons)


def test_open_coherence_flag_blocks():
    flags = [OpenFlag(node_key="wealth_dashboard", kind="coherence")]
    decision = can_publish_plan(authorities=_CLEAR_AUTHORITIES, open_flags=flags)
    assert decision.can_promote is False


def test_non_hard_non_coherence_flag_does_not_block():
    # An informational/cosmetic flag is not a publish blocker.
    flags = [OpenFlag(node_key="appendix_note", kind="info")]
    decision = can_publish_plan(authorities=_CLEAR_AUTHORITIES, open_flags=flags)
    assert decision.can_promote is True


def test_missing_authority_still_blocks_via_promote_gate():
    partial = dict(_CLEAR_AUTHORITIES)
    del partial["rederivation"]
    decision = can_publish_plan(authorities=partial, open_flags=[])
    assert decision.can_promote is False
    assert "rederivation" in decision.blocking_authorities
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_publish_gate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'argosy.quality.publish_gate'`

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/quality/publish_gate.py
"""Publish gate for the living plan (Layer 3).

A plan is promotable ONLY when (1) every promote_gate authority clears AND
(2) no node carries an open HARD or COHERENCE status_flag. Hash-validity
alone never authorizes publication. This wraps the existing
argosy/quality/promote_gate.py::evaluate_promotion authority set and folds the
open-flag check in front of it, fail-closed.
"""
from __future__ import annotations

from dataclasses import dataclass

from argosy.quality.promote_gate import (
    PromoteDecision, REQUIRED_AUTHORITIES, evaluate_promotion,
)

# Flag kinds that block publication. Anything else (info/cosmetic) does not.
_BLOCKING_FLAG_KINDS = {"hard", "coherence"}


@dataclass
class OpenFlag:
    node_key: str
    kind: str  # "hard" | "coherence" | "info" | ...


def can_publish_plan(
    *,
    authorities: dict[str, object],
    open_flags: list[OpenFlag],
    required: tuple[str, ...] = REQUIRED_AUTHORITIES,
) -> PromoteDecision:
    """Fail-closed publish decision. An open hard/coherence flag blocks even
    when every authority clears; a missing authority blocks via promote_gate."""
    base = evaluate_promotion(authorities, required=required)
    blocking = list(base.blocking_authorities)
    reasons = list(base.reasons)
    for flag in open_flags:
        if flag.kind in _BLOCKING_FLAG_KINDS:
            tag = f"open-{flag.kind}-flag:{flag.node_key}"
            blocking.append(tag)
            reasons.append(f"{tag}: node carries an open {flag.kind} flag -> fail-closed")
    return PromoteDecision(not blocking, blocking, reasons)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_publish_gate.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/publish_gate.py tests/test_publish_gate.py
git commit -m "feat(publish): wire promote_gate + open hard/coherence flag block (fail-closed)"
```

---

### Task 13: End-to-end adjudication→ladder→persist wiring + module exports + full suite smoke

**Files:**
- Modify: `argosy/quality/change_adjudication.py`, `argosy/orchestrator/flows/negotiation_ladder.py`, `argosy/quality/change_request_store.py`, `argosy/quality/publish_gate.py`
- Test: `tests/test_adjudication_e2e.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_adjudication_e2e.py
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.change_adjudication import (
    ChangeRequest, ChangeKind, Author, AuthorKind,
    OwnershipMap, adjudicate, Disposition, HardNodeError, assert_resolvable,
)
from argosy.orchestrator.flows.negotiation_ladder import (
    run_ladder, PeerVerdict, ArbiterClass, TerminalState,
)
from argosy.quality.change_request_store import (
    open_change_request, record_ladder_result, load_thread,
)


class _Parts:
    def __init__(self, peer, ac=None, ruling=""):
        self._peer, self._ac, self._ruling = list(peer), ac, ruling
    def peer_round(self, *, change, prior_turns, round):
        return self._peer[round - 1]
    def arbiter(self, *, change, prior_turns):
        return self._ac, self._ruling


SEED_USER = "test"  # db_session_with_seeded_user seeds User(id="test")


def test_recipe_change_full_path_to_persisted_arbiter_ruling(db_session_with_seeded_user):
    s = db_session_with_seeded_user
    g = DerivationGraph()
    g.add_node(Node(key="swr_pct", kind=NodeKind.INPUT, value=0.035))
    om = OwnershipMap(g, recipe_node_keys={"swr_pct"})
    cr = ChangeRequest(
        target_node_key="swr_pct",
        author=Author(kind=AuthorKind.AGENT, role="plan_critique"),
        kind=ChangeKind.SET_RECIPE, payload={"value": 0.04},
        rationale="over-conservative",
    )
    assert adjudicate(cr, om).disposition is Disposition.NEEDS_LADDER

    parts = _Parts(
        [(PeerVerdict.UNRESOLVED, "no")] * 3,
        ac=ArbiterClass.EVIDENCE_RESOLVABLE, ruling="re-derive; 0.04 supported",
    )
    result = run_ladder(cr, parts)
    assert result.terminal_state is TerminalState.ARBITER_RULED

    row_id = open_change_request(s, user_id=SEED_USER, plan_version_id=None, cr=cr)
    record_ladder_result(s, change_request_id=row_id, result=result)
    thread = load_thread(s, change_request_id=row_id)
    assert thread["status"] == "arbiter_ruled"
    # Full replayable transcript: A propose -> 3 B rebuts -> arbiter classify+rule.
    speakers = [t["speaker"] for t in thread["turns"]]
    assert speakers[0] == "A"
    assert speakers.count("B") == 3
    assert "arbiter" in speakers


def test_public_exports():
    import argosy.quality.change_adjudication as ca
    import argosy.orchestrator.flows.negotiation_ladder as nl
    import argosy.quality.change_request_store as crs
    import argosy.quality.publish_gate as pg
    for name in ("ChangeRequest", "adjudicate", "OwnershipMap", "Disposition",
                 "HardNodeError", "assert_resolvable"):
        assert name in ca.__all__
    for name in ("run_ladder", "LadderResult", "TerminalState", "LadderParticipants"):
        assert name in nl.__all__
    for name in ("open_change_request", "record_ladder_result", "load_thread",
                 "supersede_change_request", "assert_reopenable", "ReopenError"):
        assert name in crs.__all__
    assert "can_publish_plan" in pg.__all__
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_adjudication_e2e.py -q`
Expected: FAIL — `test_public_exports` errors with `AttributeError: module ... has no attribute '__all__'`

- [ ] **Step 3: Write minimal implementation**

Append `__all__` to each module.

`argosy/quality/change_adjudication.py`:

```python
__all__ = [
    "AuthorKind", "ChangeKind", "Author", "ChangeRequest",
    "NodeClass", "OwnershipMap",
    "Disposition", "AdjudicationOutcome", "adjudicate",
    "HardNodeError", "assert_resolvable",
]
```

`argosy/orchestrator/flows/negotiation_ladder.py`:

```python
__all__ = [
    "MAX_PEER_ROUNDS", "Speaker", "Stance", "TerminalState",
    "PeerVerdict", "ArbiterClass",
    "LadderTurn", "LadderParticipants", "LadderResult", "run_ladder",
]
```

`argosy/quality/change_request_store.py`:

```python
__all__ = [
    "open_change_request", "record_ladder_result", "load_thread",
    "supersede_change_request", "assert_reopenable", "ReopenError",
]
```

`argosy/quality/publish_gate.py`:

```python
__all__ = ["OpenFlag", "can_publish_plan"]
```

- [ ] **Step 4: Run the full Phase-2 suite**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_change_adjudication.py tests/test_negotiation_ladder.py tests/test_change_request_models.py tests/test_change_request_store.py tests/test_publish_gate.py tests/test_adjudication_e2e.py -q`
Expected: PASS (all Phase-2 tests). Then run the broader marker-filtered suite to confirm no regression: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" -q tests/test_change_adjudication.py tests/test_negotiation_ladder.py tests/test_change_request_models.py tests/test_change_request_store.py tests/test_publish_gate.py tests/test_adjudication_e2e.py`

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/change_adjudication.py argosy/orchestrator/flows/negotiation_ladder.py argosy/quality/change_request_store.py argosy/quality/publish_gate.py tests/test_adjudication_e2e.py
git commit -m "feat(adjudication): e2e adjudicate->ladder->persist path + public API exports"
```

---

## Self-Review — spec requirement → task mapping

Scope of this plan is Phase 2 (the spec's "Phasing" item 2). Each in-scope spec requirement maps to a task:

| Spec requirement (Layer 2 + testing) | Task |
|---|---|
| `ChangeRequest` is author-agnostic (`user | agent_role`) targeting a node | Task 1 |
| Ownership map (one authority owns each node) | Task 2 |
| Node classification hard vs input vs derived vs recipe | Task 2 |
| **DerivedValue target rejected** ("change inputs or recipe") | Task 3 (`test_setting_a_derived_value_is_rejected`) |
| Recipe/policy change routes through the ladder | Task 4 |
| **InputFact/policy change adjudicated + audited (anti-laundering)**; verdict-flip is on the record + contestable | Task 4 (`test_verdict_flipping_input_change_needs_audit`) |
| **A hard node cannot be agreed away** — only input/recipe fix | Task 5 (`test_hard_node_cannot_be_agreed_away_by_concession`) |
| Ladder: A files "change X because Y" | Task 7 (PROPOSE turn) |
| Ladder: **B may rebut Y, not just the value** | Task 7 (`test_a_concedes_when_rebuttal_lands`, REBUT turn) |
| Ladder: bounded peer rounds **n=3** (generalizes `converge_fm_objections`) | Tasks 7–8 (`MAX_PEER_ROUNDS=3`; `test_escalates_to_arbiter_after_three_unresolved_rounds`) |
| Escalate to **arbiter (FM)** which CLASSIFIES evidence-resolvable vs genuine-decision | Task 8 (`test_arbiter_routes_genuine_decision_to_user`) |
| Arbiter routing: evidence-resolvable stays internal / rules | Task 8 (`test_escalates_to_arbiter_after_three_unresolved_rounds`) |
| Escalate to **user** ONLY for a certified real decision, as a single boxed choice | Task 8 (`user_question`, USER ASK turn) |
| **Typed terminal states recorded on `dialogue_turns`** (`A_conceded`/`B_conceded`/`arbiter_ruled`/`escalated_to_user`/`superseded`) | Task 6 (`TerminalState`), Task 9 (tables), Task 10 (persisted) |
| **Superseded objection cannot reopen** | Task 11 (`test_superseded_request_cannot_reopen`) |
| Data model: `change_requests` + `dialogue_turns` columns | Task 9 (models + migration 0071) |
| Layer-5 replayable thread (`load_thread`) for the Replay view | Task 10, Task 13 (`test_recipe_change_full_path...`) |
| **Wire `promote_gate` as the PUBLISH gate; no open hard/coherence flag may publish** | Task 12 (`test_open_hard_flag_blocks_even_when_authorities_clear`) |
| End-to-end adjudicate → ladder → persist | Task 13 |

**Reuse (DRY) honored:**
- The ladder **generalizes** `fm_objection_dialogue.converge_fm_objections` / `_terminal_state` (typed terminal states, ≤3 rounds, FM-as-arbiter) rather than duplicating it; the LLM seam is the injected `LadderParticipants`, which production wires to the same `AnalystResponderAgent` / `FundManagerDialogueVerdictAgent` that flow already uses.
- Builds directly on the Phase-1a `DerivationGraph`/`Node`/`NodeKind` API (`get`, `dependents`, `is_valid`) — no re-implementation.
- The publish gate **wraps** `promote_gate.evaluate_promotion` + `REQUIRED_AUTHORITIES` + `PromoteDecision` (does not re-define the authority set).
- Persistence follows existing model conventions (Text-JSON columns, `_utcnow`, `Mapped[...]`) and the migration follows `0070`'s shape; head advances `0070 → 0071`.
- Tests never make a live `claude.exe` call (the MEMORY gotcha that hangs synthesis-flow tests) — the ladder's LLM step is behind the `LadderParticipants` protocol and exercised with deterministic fakes.

**Placeholder scan:** none — every step has complete, runnable code + an exact run command. The only `raise NotImplementedError` is intentional and removed within the same plan (Task 7 introduces it as the not-yet-built arbiter rung; Task 8 replaces it and its test asserts the replacement).

**Type consistency:** `ChangeRequest(target_node_key, author, kind, payload, rationale)`, `Author(kind, role)`, `AuthorKind.{USER,AGENT}`, `ChangeKind.{SET_INPUT,SET_RECIPE,SET_DERIVED,OBJECTION}`, `Disposition.{ACCEPTED,REJECTED,NEEDS_LADDER,NEEDS_AUDIT}`, `LadderResult(terminal_state, turns, arbiter_class, user_question)`, `LadderTurn(round, speaker, stance, text, cited_nodes)`, `TerminalState` values, and the store fns `open_change_request/record_ladder_result/load_thread/supersede_change_request/assert_reopenable` are used identically across every task and test.
