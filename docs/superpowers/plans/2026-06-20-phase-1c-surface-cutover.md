# Phase 1c — Contradiction-prone surface cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the three remaining contradiction-prone surfaces — the **total (incl. residence) net-worth basis**, the **FI-crossing year**, and the **two RSU retention rates** — to render from their canonical registry figures, so every surface that states these numbers reads ONE owned value (no private copy can drift).

**Architecture:** Extend the existing canonical-surface machinery in `argosy/quality/live_surfaces.py` — the same pattern that already makes the FI verdict, retirement age, liquid/investable net worth, and US-situs estate render from one shared `DERIVED` node. Each new subject gets ONE canonical node (seeded from the authoritative resolver manifest in `incremental_plan.build_base_graph`) plus `SURFACE` nodes whose only inbound edge is that node, so two surfaces of the same subject are byte-identical by construction. Separately, de-conflate the one live prose contradiction (the assumption-ledger "47%" retention row) by hydrating it from the two canonical retention figures.

**Tech Stack:** Python 3.12, pytest. Pure derivation-graph nodes (no DB/LLM in the surface layer); resolver figures already exist and are owned in `figure_registry.OWNER_MAP`.

**Codex plan-review incorporated (2026-06-20):** the 0.0 fail-closed seed must never render as a real rate/year (retention recipes + A7 validate `0 < rate <= 1`); FI-crossing reconciliation is enforced by a pure guard in the base graph (the surface has no access to the margin/current year); `canonical_surface_concepts` must honor `SUBJECT_NODE_MAP` so coherence checks the actually-seeded node; coverage test strengthened to `set(builders) == set(CANONICAL_SUBJECT_NODE)`; A7 label narrowed to the capital-gain slice.

**Scope boundary (honest):** Phase 1c makes these surfaces render from the registry **in the derivation-graph / registry-rendered artifact** (the bytes the whole-artifact reader reviews once Phase 2 cuts it over) AND removes the one hardcoded retention number in the live synth assumption ledger. Rewiring the live React `/api/portfolio/wealth-dashboard` route to read graph surfaces is **deferred to Phase 2** (full render-from-registry cutover) — net worth is already structurally consistent dashboard↔resolver because both compute via `argosy/services/net_worth_bases.py` (Phase 1b DRY extraction), so no live dashboard contradiction remains for the total basis; this plan adds the canonical surface so the *plan body* renders it too.

---

### Task 1: Total (incl. residence) net-worth canonical surface

The resolver already publishes `portfolio.total_net_worth_incl_residence_nis` (Phase 1b) and the registry owns it (Balance-Sheet, `basis="total"`). Liquid + investable already have canonical surfaces; total does not. Add the third basis as a canonical subject so all three bases render distinctly labeled from one node each.

**Files:**
- Modify: `argosy/quality/live_surfaces.py`
- Modify: `argosy/orchestrator/flows/incremental_plan.py`
- Test: `tests/test_live_surfaces.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_live_surfaces.py
from argosy.quality.live_surfaces import NET_WORTH_TOTAL_NODE  # add to the existing import block


def test_net_worth_total_basis_renders_distinct_label() -> None:
    """The total (incl. residence) net-worth basis renders from its OWN node,
    distinctly labelled 'total' so it can never be confused with the liquid or
    investable basis (the ₪14.05M-vs-₪11.87M contradiction)."""
    g = _build_graph_with_canonical_inputs()
    g.set_input(NET_WORTH_TOTAL_NODE, 14_050_000.0)
    g.recompute()
    tile = g.get("surface:dashboard.net_worth_total_tile").value
    appendix = g.get("surface:appendix.net_worth_total").value
    assert "14,050,000" in tile
    assert "total" in tile.lower()
    assert "14,050,000" in appendix
    # distinct from the other two bases' node keys
    from argosy.quality.live_surfaces import (
        NET_WORTH_LIQUID_NODE, NET_WORTH_INVESTABLE_NODE,
    )
    assert NET_WORTH_TOTAL_NODE not in (NET_WORTH_LIQUID_NODE, NET_WORTH_INVESTABLE_NODE)
```

Also STRENGTHEN the existing `test_each_canonicalized_subject_maps_to_exactly_one_node` (codex nit #6) — add, after its `by_subject` loop:

```python
    # _SUBJECT_BUILDERS and CANONICAL_SUBJECT_NODE must stay in lock-step: every
    # subject with a builder has a node mapping and vice-versa (else a new subject
    # silently lacks surfaces or a node).
    assert set(by_subject) == set(CANONICAL_SUBJECT_NODE)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_live_surfaces.py::test_net_worth_total_basis_renders_distinct_label -v`
Expected: FAIL — `ImportError: cannot import name 'NET_WORTH_TOTAL_NODE'`.

- [ ] **Step 3: Implement the canonical node + surfaces**

In `argosy/quality/live_surfaces.py`, beside the other net-worth node constants (after line 40):

```python
NET_WORTH_TOTAL_NODE = "net_worth.total_incl_residence_nis"  # total basis, incl. residence
```

Add to `CANONICAL_SUBJECT_NODE`:

```python
    "net_worth_total": NET_WORTH_TOTAL_NODE,
```

Add the surfaces builder (beside `_net_worth_investable_surfaces`):

```python
def _net_worth_total_surfaces(node_key: str) -> list[Node]:
    """Net-worth (TOTAL basis, incl. residence) surfaces — distinctly labelled
    'total (incl. residence)' so it is never confused with the liquid or
    investable basis (resolves the ₪14.05M-vs-₪11.87M dashboard contradiction)."""
    return [
        make_surface_node(
            key="surface:dashboard.net_worth_total_tile",
            inputs=(node_key,),
            recipe=lambda i: f"Net worth (total basis, incl. residence): ₪{i[node_key]:,.0f}",
            compute_version="nw-total-tile-v1",
        ),
        make_surface_node(
            key="surface:appendix.net_worth_total",
            inputs=(node_key,),
            recipe=lambda i: f"| Net worth — total (incl. residence) | ₪{i[node_key]:,.0f} |",
            compute_version="nw-total-appendix-v1",
        ),
    ]
```

Register it in `_SUBJECT_BUILDERS`:

```python
    "net_worth_total": _net_worth_total_surfaces,
```

Add `"NET_WORTH_TOTAL_NODE"` to `__all__`.

**Also fix `canonical_surface_concepts` to honor the node-key override (codex should-fix #3)** — today it always binds concepts to the `CANONICAL_SUBJECT_NODE` default, so for any overridden subject (liquid/investable/us-situs, and now total) the coherence recheck reads a node key that is not in the live graph and silently skips it. Change the signature + body:

```python
def canonical_surface_concepts(
    subject_node_map: dict[str, str] | None = None,
) -> dict[str, list[SurfaceConcept]]:
    """The surface->concepts map ... Each surface binds to the SAME node key the
    surfaces actually render from (``subject_node_map`` override first, else the
    ``CANONICAL_SUBJECT_NODE`` default), so the coherence view reads the live
    seeded node — not an absent default key."""
    resolved = dict(CANONICAL_SUBJECT_NODE)
    if subject_node_map:
        resolved.update(subject_node_map)
    out: dict[str, list[SurfaceConcept]] = {}
    for subject_type, builder in _SUBJECT_BUILDERS.items():
        node_key = resolved[subject_type]
        for s in builder(node_key):
            out[s.key] = [SurfaceConcept(concept=subject_type, value_input_key=node_key)]
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_live_surfaces.py::test_net_worth_total_basis_renders_distinct_label -v`
Expected: PASS

- [ ] **Step 5: Seed the node from the resolver in the base graph**

In `argosy/orchestrator/flows/incremental_plan.py`, beside `LIQUID_NW_KEY` / `INVESTABLE_NW_KEY` (after line 87):

```python
TOTAL_NW_KEY = "portfolio.total_net_worth_incl_residence_nis"
```

Add to `SUBJECT_NODE_MAP`:

```python
    "net_worth_total": TOTAL_NW_KEY,
```

Add to `_RESOLVER_SCALAR_KEYS`:

```python
_RESOLVER_SCALAR_KEYS = (
    FI_MARGIN_NODE, EARLIEST_SAFE_AGE_NODE, LIQUID_NW_KEY, INVESTABLE_NW_KEY,
    TOTAL_NW_KEY,
)
```

(`build_base_graph` already seeds every `_RESOLVER_SCALAR_KEYS` entry as an INPUT node carrying the resolver value, then `register_canonical_surfaces(graph, subject_node_map=SUBJECT_NODE_MAP)` points the `net_worth_total` subject at `TOTAL_NW_KEY`.)

Also update the `canonical_surface_concepts()` call in `run_incremental_cycle` (around line 395) to pass the same override, so the coherence recheck reads the seeded keys (codex should-fix #3):

```python
    register_surface_concepts(canonical_surface_concepts(SUBJECT_NODE_MAP))
```

- [ ] **Step 6: Run the full surface + incremental-plan tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_live_surfaces.py tests/test_incremental_plan.py -v`
Expected: PASS. The direct `test_net_worth_total_basis_renders_distinct_label` covers the new surfaces; the strengthened `test_each_canonicalized_subject_maps_to_exactly_one_node` now enforces `_SUBJECT_BUILDERS`↔`CANONICAL_SUBJECT_NODE` lock-step (it covers `net_worth_total` only because Step 3 updated BOTH).

- [ ] **Step 7: Commit**

```bash
git add argosy/quality/live_surfaces.py argosy/orchestrator/flows/incremental_plan.py tests/test_live_surfaces.py
git commit -m "feat(surface): canonical total-incl-residence net-worth surface (Phase 1c)"
```

---

### Task 2: FI-crossing-year canonical surface

The resolver publishes `retirement.fi_crossing_year` (Phase 1b), already reconciled with the FI margin by construction (margin ≥ 0 → current year; margin < 0 → strictly future, else pending). Add a canonical surface so the FI-crossing statement renders from that one figure and can never claim a past/present crossing while the FI verdict says "not reached".

**Files:**
- Modify: `argosy/quality/live_surfaces.py`
- Modify: `argosy/orchestrator/flows/incremental_plan.py`
- Test: `tests/test_live_surfaces.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_live_surfaces.py
from argosy.quality.live_surfaces import FI_CROSSING_YEAR_NODE  # add to the import block


def test_fi_crossing_surface_renders_future_year_and_handles_pending() -> None:
    """The FI-crossing statement + tile render from the ONE reconciled
    crossing-year node. A resolved future year renders that year; the fail-closed
    0.0 seed (a pending crossing — FI not reached within the horizon) renders an
    explicit 'not reached' / 'beyond horizon' string, never 'year 0'."""
    g = _build_graph_with_canonical_inputs()
    g.set_input(FI_CROSSING_YEAR_NODE, 2027.0)
    g.recompute()
    stmt = g.get("surface:fi_crossing_statement").value
    tile = g.get("surface:dashboard.fi_crossing_tile").value
    assert "2027" in stmt and "crossing" in stmt.lower()
    assert "2027" in tile

    # Fail-closed seed (pending) -> explicit not-reached text on BOTH; no 'year 0'.
    g.set_input(FI_CROSSING_YEAR_NODE, 0.0)
    g.recompute()
    pending = g.get("surface:fi_crossing_statement").value
    pending_tile = g.get("surface:dashboard.fi_crossing_tile").value
    assert "year 0" not in pending.lower() and ": 0" not in pending
    assert "not reached" in pending.lower() and "horizon" in pending.lower()
    assert "beyond horizon" in pending_tile.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_live_surfaces.py::test_fi_crossing_surface_renders_future_year_and_handles_pending -v`
Expected: FAIL — `ImportError: cannot import name 'FI_CROSSING_YEAR_NODE'`.

- [ ] **Step 3: Implement the canonical node + surface**

In `argosy/quality/live_surfaces.py`, beside the other retirement node constants:

```python
FI_CROSSING_YEAR_NODE = "retirement.fi_crossing_year"     # reconciled trajectory crossing
```

Add to `CANONICAL_SUBJECT_NODE`:

```python
    "fi_crossing": FI_CROSSING_YEAR_NODE,
```

Add the surfaces builder:

```python
def _fi_crossing_surfaces(node_key: str) -> list[Node]:
    """FI-crossing-year surface — the projected calendar year the current liquid
    net worth plus a real-savings annuity reaches the FI total-capital target.
    The value is reconciled with the FI margin at the resolver (margin < 0 ->
    strictly future or pending), so this surface can never show a past/present
    crossing while the FI verdict says 'not reached'. A non-positive / pre-2000
    value is the fail-closed seed for a pending crossing and renders explicitly."""
    def _render(i: dict) -> str:
        yr = i[node_key]
        if yr and yr >= 2000:
            return (
                "Projected FI-capital crossing year (current liquid net worth + "
                f"real-savings trajectory): {int(yr)}."
            )
        return (
            "FI-capital crossing year: not reached within the projection horizon."
        )
    return [
        make_surface_node(
            key="surface:fi_crossing_statement",
            inputs=(node_key,),
            recipe=_render,
            compute_version="fi-crossing-v1",
        ),
        make_surface_node(
            key="surface:dashboard.fi_crossing_tile",
            inputs=(node_key,),
            recipe=lambda i: (
                f"FI crossing: {int(i[node_key])}"
                if i[node_key] and i[node_key] >= 2000
                else "FI crossing: beyond horizon"
            ),
            compute_version="fi-crossing-tile-v1",
        ),
    ]
```

Register it in `_SUBJECT_BUILDERS`:

```python
    "fi_crossing": _fi_crossing_surfaces,
```

Add `"FI_CROSSING_YEAR_NODE"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_live_surfaces.py::test_fi_crossing_surface_renders_future_year_and_handles_pending -v`
Expected: PASS

- [ ] **Step 5: Seed the node from the resolver**

In `argosy/orchestrator/flows/incremental_plan.py`, add `FI_CROSSING_YEAR_NODE` to the `live_surfaces` import line, then add it to `_RESOLVER_SCALAR_KEYS`:

```python
_RESOLVER_SCALAR_KEYS = (
    FI_MARGIN_NODE, EARLIEST_SAFE_AGE_NODE, LIQUID_NW_KEY, INVESTABLE_NW_KEY,
    TOTAL_NW_KEY, FI_CROSSING_YEAR_NODE,
)
```

The canonical default node key (`retirement.fi_crossing_year`) equals the resolver key, so no `SUBJECT_NODE_MAP` override is needed. When the resolver leaves it pending, `_resolver_scalars` omits it and `build_base_graph` seeds 0.0 (the fail-closed seed the surface renders as "not reached").

- [ ] **Step 6: Add the FI-crossing reconciliation guard in the base graph (codex blocker #2)**

The surface renders the year with NO access to the margin or current year, so a stale manifest or a bad `resolver_values` injection could render a past/present crossing while `surface:fi_verdict` says NOT reached. Enforce the resolver's invariant on the SEEDED scalars with a pure, unit-testable helper.

Add to `argosy/orchestrator/flows/incremental_plan.py` (a `from datetime import date` import is already present at module top; add it if not):

```python
def _reconcile_fi_crossing(scalars: dict[str, float], *, current_year: int) -> dict[str, float]:
    """Enforce the FI-crossing/margin invariant on the seeded canonical scalars
    (the surface can't see the margin): margin < 0 must never pair with a
    past/present crossing -> drop the crossing so the pending (0.0) seed renders
    'not reached'; margin >= 0 -> normalize the crossing to the current year
    (FI already reached). Pure; no graph, no DB."""
    out = dict(scalars)
    margin = out.get(FI_MARGIN_NODE)
    crossing = out.get(FI_CROSSING_YEAR_NODE)
    if margin is not None and crossing is not None:
        if margin < 0 and crossing <= current_year:
            out.pop(FI_CROSSING_YEAR_NODE, None)
        elif margin >= 0 and crossing != current_year:
            out[FI_CROSSING_YEAR_NODE] = float(current_year)
    return out
```

In `build_base_graph`, apply it right after `scalars` is resolved (after line 244, before the seeding loop):

```python
    scalars = _reconcile_fi_crossing(scalars, current_year=date.today().year)
```

- [ ] **Step 7: Write the reconciliation guard test (fail-first)**

```python
# append to tests/test_incremental_plan.py
import datetime
from argosy.orchestrator.flows.incremental_plan import (
    _reconcile_fi_crossing, FI_CROSSING_YEAR_NODE,
)
from argosy.quality.live_surfaces import FI_MARGIN_NODE


def test_fi_crossing_reconciled_against_negative_margin():
    cur = datetime.date.today().year
    # Contradiction: FI short (margin<0) but a current-year crossing -> dropped.
    out = _reconcile_fi_crossing(
        {FI_MARGIN_NODE: -500_000.0, FI_CROSSING_YEAR_NODE: float(cur)},
        current_year=cur)
    assert FI_CROSSING_YEAR_NODE not in out  # -> 0.0 seed -> 'not reached'
    # A genuine future crossing with a negative margin is preserved.
    out2 = _reconcile_fi_crossing(
        {FI_MARGIN_NODE: -500_000.0, FI_CROSSING_YEAR_NODE: float(cur + 1)},
        current_year=cur)
    assert out2[FI_CROSSING_YEAR_NODE] == float(cur + 1)
    # Margin reached -> crossing normalized to the current year.
    out3 = _reconcile_fi_crossing(
        {FI_MARGIN_NODE: 200_000.0, FI_CROSSING_YEAR_NODE: float(cur + 3)},
        current_year=cur)
    assert out3[FI_CROSSING_YEAR_NODE] == float(cur)
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_incremental_plan.py::test_fi_crossing_reconciled_against_negative_margin -v`
Expected: FAIL first (`_reconcile_fi_crossing` undefined), then PASS after Step 6.

- [ ] **Step 8: Run the surface + incremental-plan tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_live_surfaces.py tests/test_incremental_plan.py -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add argosy/quality/live_surfaces.py argosy/orchestrator/flows/incremental_plan.py tests/test_live_surfaces.py tests/test_incremental_plan.py
git commit -m "feat(surface): canonical FI-crossing-year surface + base-graph reconciliation guard (Phase 1c)"
```

---

### Task 3: RSU retention canonical surfaces (two distinct subjects)

The resolver publishes two DISTINCT statutory retention rates — `tax.retention_at_vest_pct` (0.50, at-vest ordinary income) and `tax.retention_capital_track_pct` (0.70, Section-102 capital track) — owned by Tax in the registry. They have NO surfaces today and the live prose conflates them into one "47%" (fixed in Task 5). Add each as its OWN canonical subject so the two rates can never be conflated.

**Files:**
- Modify: `argosy/quality/live_surfaces.py`
- Modify: `argosy/orchestrator/flows/incremental_plan.py`
- Test: `tests/test_live_surfaces.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_live_surfaces.py
from argosy.quality.live_surfaces import (  # add to the import block
    RETENTION_AT_VEST_NODE, RETENTION_CAPITAL_TRACK_NODE,
)


def test_retention_rates_render_as_two_distinct_labelled_surfaces() -> None:
    """The at-vest (ordinary) and capital-track (Section-102) retention rates
    render from two SEPARATE nodes, each distinctly labelled, so prose can never
    conflate 50% and 70% into one ambiguous 'retention'."""
    g = _build_graph_with_canonical_inputs()
    g.set_input(RETENTION_AT_VEST_NODE, 0.50)
    g.set_input(RETENTION_CAPITAL_TRACK_NODE, 0.70)
    g.recompute()
    at_vest = g.get("surface:retention_at_vest_statement").value
    cap = g.get("surface:retention_capital_track_statement").value
    assert "50%" in at_vest and "at-vest" in at_vest.lower()
    assert "70%" in cap and "capital" in cap.lower()
    # The two are distinct nodes -> a change to one never moves the other.
    assert RETENTION_AT_VEST_NODE != RETENTION_CAPITAL_TRACK_NODE
    g.set_input(RETENTION_AT_VEST_NODE, 0.40)
    g.recompute()
    assert "70%" in g.get("surface:retention_capital_track_statement").value


def test_retention_pending_seed_does_not_render_a_false_zero_rate() -> None:
    """The fail-closed 0.0 seed (pending / omitted resolver value) must NOT render
    as a live '0%' statutory rate — it renders an explicit pending string (codex
    blocker #1). Anything outside (0, 1] is treated as pending."""
    g = _build_graph_with_canonical_inputs()
    g.set_input(RETENTION_AT_VEST_NODE, 0.0)
    g.set_input(RETENTION_CAPITAL_TRACK_NODE, 1.5)  # >1 is also invalid
    g.recompute()
    at_vest = g.get("surface:retention_at_vest_statement").value
    cap = g.get("surface:retention_capital_track_statement").value
    assert "0%" not in at_vest and "pending" in at_vest.lower()
    assert "pending" in cap.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_live_surfaces.py::test_retention_rates_render_as_two_distinct_labelled_surfaces -v`
Expected: FAIL — `ImportError: cannot import name 'RETENTION_AT_VEST_NODE'`.

- [ ] **Step 3: Implement the canonical nodes + surfaces**

In `argosy/quality/live_surfaces.py`, add the node constants:

```python
RETENTION_AT_VEST_NODE = "tax.retention_at_vest_pct"            # at-vest ordinary income
RETENTION_CAPITAL_TRACK_NODE = "tax.retention_capital_track_pct"  # Section-102 capital track
```

Add to `CANONICAL_SUBJECT_NODE`:

```python
    "retention_at_vest": RETENTION_AT_VEST_NODE,
    "retention_capital_track": RETENTION_CAPITAL_TRACK_NODE,
```

Add the surfaces builders:

```python
def _retention_pct_or_pending(value: float, label: str) -> str:
    """Render a retention rate ONLY when it is a valid fraction in (0, 1]; the
    fail-closed 0.0 seed (a pending/omitted resolver value) and any out-of-range
    value render an explicit pending string, never a false '0%' rate (codex
    blocker #1). A true 0% retention is not a legitimate statutory value here, so
    0.0 is unambiguously the pending sentinel."""
    if isinstance(value, (int, float)) and 0.0 < value <= 1.0:
        return f"{label}: {value*100:.0f}%"
    return f"{label}: [derivation pending]"


def _retention_at_vest_surfaces(node_key: str) -> list[Node]:
    """At-vest RSU income retention (ordinary income — top marginal + surtax).
    Distinctly labelled 'at-vest (ordinary)' so it is never conflated with the
    capital-track rate."""
    return [
        make_surface_node(
            key="surface:retention_at_vest_statement",
            inputs=(node_key,),
            recipe=lambda i: _retention_pct_or_pending(
                i[node_key],
                "RSU net retention — at-vest (ordinary income, top marginal + surtax)"),
            compute_version="retention-at-vest-v2",
        ),
    ]


def _retention_capital_track_surfaces(node_key: str) -> list[Node]:
    """Capital-track RSU retention (Section-102 capital-gain slice — CGT + surtax).
    Distinctly labelled so it is never conflated with the at-vest ordinary rate."""
    return [
        make_surface_node(
            key="surface:retention_capital_track_statement",
            inputs=(node_key,),
            recipe=lambda i: _retention_pct_or_pending(
                i[node_key],
                "RSU net retention — capital-track (Section 102 capital-gain slice, CGT + surtax)"),
            compute_version="retention-capital-track-v2",
        ),
    ]
```

Register both in `_SUBJECT_BUILDERS`:

```python
    "retention_at_vest": _retention_at_vest_surfaces,
    "retention_capital_track": _retention_capital_track_surfaces,
```

Add `"RETENTION_AT_VEST_NODE"` and `"RETENTION_CAPITAL_TRACK_NODE"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_live_surfaces.py::test_retention_rates_render_as_two_distinct_labelled_surfaces -v`
Expected: PASS

- [ ] **Step 5: Seed the nodes from the resolver**

In `argosy/orchestrator/flows/incremental_plan.py`, add `RETENTION_AT_VEST_NODE, RETENTION_CAPITAL_TRACK_NODE` to the `live_surfaces` import line and to `_RESOLVER_SCALAR_KEYS`:

```python
_RESOLVER_SCALAR_KEYS = (
    FI_MARGIN_NODE, EARLIEST_SAFE_AGE_NODE, LIQUID_NW_KEY, INVESTABLE_NW_KEY,
    TOTAL_NW_KEY, FI_CROSSING_YEAR_NODE,
    RETENTION_AT_VEST_NODE, RETENTION_CAPITAL_TRACK_NODE,
)
```

The canonical default node keys equal the resolver keys — no `SUBJECT_NODE_MAP` override needed.

- [ ] **Step 6: Run the surface + incremental-plan tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_live_surfaces.py tests/test_incremental_plan.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add argosy/quality/live_surfaces.py argosy/orchestrator/flows/incremental_plan.py tests/test_live_surfaces.py
git commit -m "feat(surface): canonical RSU retention surfaces — two distinct rates (Phase 1c)"
```

---

### Task 4: De-conflate the live retention prose (assumption ledger A7)

The synth assumption ledger renders a single hardcoded "47%" for "RSU net retention" (`render.py::_ASSUMPTION_LEDGER_V1`, row A7), which is the live prose conflation Task 3's figures replace. Hydrate A7 from the two canonical retention figures, distinctly labelled, and drop the hardcoded value to `[derivation pending]` (matching the A5/A6 FX cold-cache pattern) so a cold cache never shows a wrong conflated number.

**Files:**
- Modify: `argosy/orchestrator/flows/plan_synthesis/render.py`
- Test: `tests/test_assumption_ledger.py` (create if absent; otherwise the existing render test module)

- [ ] **Step 1: Find any test asserting the old "47%"**

Run: `.venv/Scripts/python.exe -m pytest -k assumption -q` and `grep -rn "47%" tests/` (PowerShell: `Select-String -Path tests\*.py -Pattern "47%"`).
If a test asserts the literal "47%" A7 value, update it in Step 4 to assert the new split labels instead.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_assumption_ledger.py  (create if it does not exist)
from argosy.orchestrator.flows.plan_synthesis.render import _ledger_rows_with_manifest


class _RV:
    def __init__(self, value, status="resolved"):
        self.value = value
        self.status = status


class _Resolved:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, key):
        return self._m.get(key)


def test_a7_retention_row_split_from_canonical_rates():
    """A7 renders the two DISTINCT retention rates (at-vest 50% / capital-track
    70%), not a single conflated number, sourced from the resolver manifest."""
    resolved = _Resolved({
        "tax.retention_at_vest_pct": _RV(0.50),
        "tax.retention_capital_track_pct": _RV(0.70),
    })
    rows = _ledger_rows_with_manifest(resolved)
    a7 = {r["id"]: r for r in rows}["A7"]
    assert "50%" in a7["value"]
    assert "70%" in a7["value"]
    assert "at-vest" in a7["value"].lower()
    assert "capital" in a7["value"].lower()


def test_a7_cold_cache_is_pending_not_conflated():
    """With no resolver manifest, A7 shows [derivation pending], never a
    hardcoded conflated '47%'."""
    rows = _ledger_rows_with_manifest(None)
    a7 = {r["id"]: r for r in rows}["A7"]
    assert "47%" not in a7["value"]
    assert "pending" in a7["value"].lower()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_assumption_ledger.py -v`
Expected: FAIL — `test_a7_cold_cache_is_pending_not_conflated` fails (value is still "47%"); `test_a7_retention_row_split_from_canonical_rates` fails (A7 not hydrated).

- [ ] **Step 4: Implement**

In `argosy/orchestrator/flows/plan_synthesis/render.py`, change the A7 static default value (around line 469-472) from `"value": "47%"` to:

```python
        "value": "[derivation pending]",
```

In `_ledger_rows_with_manifest`, after the existing `_rv(...)` reads (after line 583), add:

```python
    ret_vest = _rv("tax.retention_at_vest_pct")
    ret_cap = _rv("tax.retention_capital_track_pct")
```

Then, before `return rows` (after line 633), add (validating BOTH rates are valid fractions in (0, 1] before hydrating, so a pending/garbage resolver value leaves A7 at `[derivation pending]` rather than showing a false rate — codex blocker #1):

```python
    def _valid_pct(x) -> bool:
        return isinstance(x, (int, float)) and 0.0 < x <= 1.0

    if _valid_pct(ret_vest) and _valid_pct(ret_cap) and "A7" in by_id:
        by_id["A7"]["value"] = (
            f"{ret_vest*100:.0f}% at-vest ordinary-income retention / "
            f"{ret_cap*100:.0f}% Section 102 capital-gain-slice retention"
        )
        by_id["A7"]["source"] = (
            "tax_analyst (resolver: tax.retention_at_vest_pct / "
            "tax.retention_capital_track_pct — statutory tax parameters; the "
            "active-grant net stream is the resolved A8 figure, not a recompute "
            "from either single rate)"
        )
```

If Step 1 found a test asserting the old "47%", update it to assert the split labels.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_assumption_ledger.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add argosy/orchestrator/flows/plan_synthesis/render.py tests/test_assumption_ledger.py
git commit -m "feat(render): split conflated RSU retention ledger row into two canonical rates (Phase 1c)"
```

---

### Task 5: Live verification — the cut-over surfaces are present, consistent, and the cycle still closes

Extend the live cutover script's surface checklist with the new surfaces and confirm the incremental cycle still closes on the real run-117 data, with no new cross-surface coherence violation.

**Files:**
- Modify: `tmp_review/live_cutover_cycle.py` (gitignored scratch — verification only, not committed)

- [ ] **Step 1: Add the new surfaces to the checklist**

In `tmp_review/live_cutover_cycle.py`, add to the `for skey in (...)` tuple (around line 80-84):

```python
                 "surface:dashboard.net_worth_total_tile",
                 "surface:appendix.net_worth_total",
                 "surface:fi_crossing_statement",
                 "surface:dashboard.fi_crossing_tile",
                 "surface:retention_at_vest_statement",
                 "surface:retention_capital_track_statement",
```

- [ ] **Step 2: Add a reconciliation assertion**

After the existing cross-surface identity checks block (after line 94), add:

```python
    w("\n--- Phase 1c cut-over consistency ---")
    import datetime
    crossing = g.get("surface:fi_crossing_statement").value
    verdict = g.get("surface:fi_verdict").value
    w(f"  fi_crossing : {crossing}")
    w(f"  fi_verdict  : {verdict}")
    # If the verdict says NOT reached, the crossing must NOT be a past/present year.
    if "NOT reached" in verdict:
        ok = ("not reached" in crossing.lower()) or any(
            str(y) in crossing for y in range(datetime.date.today().year + 1, 2100))
        w(f"  crossing-not-past-when-short = {ok}")
        assert ok, (
            f"FI verdict says NOT reached but crossing surface is not future/pending: {crossing!r}")
```

- [ ] **Step 3: Run the live cutover cycle**

Run: `.venv/Scripts/python.exe tmp_review/live_cutover_cycle.py`
Expected: the report (`tmp_review/live_cutover_cycle_report.txt`) shows all six new surfaces with real values (total net worth ~₪14.05M, FI crossing 2027, retention 50% / 70%), `closed = True`, and `crossing-not-past-when-short = True`.

- [ ] **Step 4: Run the registry live smoke + the touched-file test set**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py tests/test_live_surfaces.py tests/test_incremental_plan.py tests/test_assumption_ledger.py tests/test_cross_surface_consistency.py -v`
Expected: PASS (0 uncategorized, 0 blocked in the registry live smoke; all new surfaces covered).

---

## Self-Review

**1. Spec coverage** (spec Phase-1 item 4 — cut the contradiction-prone surfaces to registry rendering):
- Net-worth three labeled bases → Task 1 adds the missing TOTAL basis (liquid + investable already canonical); all three now render distinctly labeled from one node each. ✓
- FI-crossing table → Task 2 renders from `retirement.fi_crossing_year`, reconciled at the resolver so it can't claim a past/present crossing while FI is short. ✓
- Retention statements → Task 3 (two canonical surfaces, distinctly labeled) + Task 4 (the live prose A7 de-conflation). ✓
- Tranche pool/slice (spec item 4 also names it) → **NOT in this plan**; it is not flagged as a live blocker in the handover and `concentration.nvda_eligible_pool_sh` / `first_slice_sh` are not yet published resolver figures. Deferred (noted here so it is not silently dropped). The handover's Phase 1c scope is the three surfaces above.
- Live cutover closes with new figures present + consistent → Task 5. ✓

**2. Placeholder scan:** every code step shows the full code; commands have expected output. The one `[derivation pending]` string is an intentional product value (cold-cache render), not a plan placeholder.

**3. Type consistency:** node-constant names (`NET_WORTH_TOTAL_NODE`, `FI_CROSSING_YEAR_NODE`, `RETENTION_AT_VEST_NODE`, `RETENTION_CAPITAL_TRACK_NODE`) are identical across `live_surfaces.py`, `incremental_plan.py`, and the tests. Surface keys (`surface:dashboard.net_worth_total_tile`, `surface:fi_crossing_statement`, `surface:retention_at_vest_statement`, …) match between builders, tests, and the live script. `_RESOLVER_SCALAR_KEYS` grows monotonically across Tasks 1-3 to the final 8-key tuple. `_ledger_rows_with_manifest` / `_ASSUMPTION_LEDGER_V1` names match `render.py`.

**4. Risk / methodology:** the money-math (FI-crossing reconciliation, the two statutory retention rates) was codex-reviewed and shipped in Phase 1b; Phase 1c adds only rendering + labeling over those owned figures, so the methodology surface is low-risk. The one judgment call — A7 default → `[derivation pending]` and split labels — mirrors the established A5/A6 FX cold-cache pattern.
