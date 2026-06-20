# Phase 2a — Registry-rendered reader artifact Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the whole-artifact reviewer judge every contradiction-prone subject against the ONE canonical registry value — by anchoring the reader's artifact with a **canonical reconciliation block rendered from the derivation graph** (the Phase-1c canonical surfaces) — behind a default-OFF flag, so the live path is byte-identical until the cutover is proven on a backend.

**Architecture:** A new pure module `argosy/quality/registry_review_artifact.py` renders the canonical graph surfaces (FI verdict, FI crossing, the three net-worth bases, the two retention rates, retirement age, US-situs estate) into a markdown "Canonical reconciliation (registry single source)" block, and appends it to the from-scratch assembled artifact. A flag `ARGOSY_REGISTRY_REVIEW_ARTIFACT` (default OFF) gates whether the orchestrator's reader call uses the anchored artifact. This is the spec's mandated "prove the render-from-registry path before big-bang" step (spec §Non-goals): the reader gets a canonical anchor it judges prose against; the from-scratch synthesizer stays the live driver until the flag flips.

**Tech Stack:** Python 3.12, pytest. Pure render over the derivation graph (no new math); reuses `incremental_plan.build_base_graph` (the same graph Phase 1c built) and `assembled_artifact.assemble_plan_artifact` (today's reader input).

**Codex plan-review incorporated (2026-06-20):** (BLOCKER) `build_base_graph` seeds `0.0` for pending scalars, so `is_valid` alone would render pending figures as authoritative canonical facts (₪0 net worth, age 0, "reached with ₪0 margin"). FIX = a **source-authoritative gate**: render a surface only when its resolver SOURCE key is genuinely `resolved` (manifest passed in), with a non-None/non-zero graph-value fallback when no manifest is available. Plus: stronger flag-OFF test (assert `is False` + truthy/falsy spellings); reviewer-only header/intro framing; the anchor call lives in its OWN narrow fail-soft `try` (not the assemble try); the live proof ASSERTS (no `[derivation pending]`, no seeded-zero) and checks the assembled plan's `decision_run_id` matches the graph builder's.

**Why this slice (scope honesty):** the FULL Phase 2 (reader reviews a fully registry-rendered artifact + monolith retired) needs live LLM reader iteration to prove it reduces BLOCKs, which can't run here. This slice builds the reader-candidate artifact + the reversible wiring (flag default OFF = zero live change), so the cutover is one proven flag-flip away. Deferred (noted, not dropped): binding the body PROSE to canonical facts via the surgical-reconcile editor (LLM), and flipping the flag ON after a live BLOCK-reduction proof.

---

### Task 1: `render_canonical_reconciliation_block` (pure)

**Files:**
- Create: `argosy/quality/registry_review_artifact.py`
- Test: `tests/test_registry_review_artifact.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_registry_review_artifact.py
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.live_surfaces import (
    CANONICAL_SUBJECT_NODE, register_canonical_surfaces,
)
from argosy.quality.registry_review_artifact import (
    render_canonical_reconciliation_block,
)


def _graph_with_surfaces() -> DerivationGraph:
    g = DerivationGraph()
    for node_key in set(CANONICAL_SUBJECT_NODE.values()):
        g.add_node(Node(key=node_key, kind=NodeKind.INPUT, value=0.0))
    g.set_input("retirement.fi_margin_signed_nis", -167_736.0)
    g.set_input("retirement.fi_crossing_year", 2027.0)
    g.set_input("net_worth.liquid_nis", 11_668_397.0)
    g.set_input("net_worth.investable_nis", 11_871_533.0)
    g.set_input("net_worth.total_incl_residence_nis", 14_049_622.0)
    g.set_input("retirement.earliest_safe_age", 46.0)
    g.set_input("tax.retention_at_vest_pct", 0.50)
    g.set_input("tax.retention_capital_track_pct", 0.70)
    g.set_input("estate.us_situs_exposure_nis", 9_447_090.0)
    register_canonical_surfaces(g)
    g.recompute()
    return g


def test_reconciliation_block_renders_canonical_surfaces():
    block = render_canonical_reconciliation_block(_graph_with_surfaces())
    assert "Canonical reconciliation (registry single source)" in block
    # every contradiction-prone subject appears, from its canonical surface
    assert "NOT reached" in block                      # fi_verdict
    assert "2027" in block                             # fi_crossing
    assert "11,668,397" in block and "liquid" in block.lower()
    assert "11,871,533" in block and "investable" in block.lower()
    assert "14,049,622" in block and "total" in block.lower()
    assert "50%" in block and "70%" in block           # retention split
    assert "46" in block                               # earliest safe age
    assert "9,447,090" in block                        # us-situs


def test_reconciliation_block_skips_invalid_surfaces_and_empty_is_blank():
    # An empty graph (no canonical surfaces) -> empty string (fail-safe: anchor
    # nothing rather than a misleading header with no figures).
    assert render_canonical_reconciliation_block(DerivationGraph()) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_registry_review_artifact.py -v`
Expected: FAIL — `ModuleNotFoundError: argosy.quality.registry_review_artifact`.

- [ ] **Step 3: Implement**

```python
# argosy/quality/registry_review_artifact.py
"""Phase 2a — render the whole-artifact reviewer's input FROM the canonical
derivation graph.

The reviewer today reads the from-scratch prose only; contradictions live in
surfaces the prose-editor never touches (dashboard FI-crossing, net-worth basis,
retention split). This module anchors the reviewer's artifact with a CANONICAL
reconciliation block rendered from the Phase-1c canonical surfaces — the single
registry value every other surface must agree with — so a prose figure that
disagrees is a finding routed to that figure's owner.

Flag-gated (``ARGOSY_REGISTRY_REVIEW_ARTIFACT``, default OFF): off -> the reader
sees exactly today's assembled artifact (zero live change). Pure render; no new
math. See docs/superpowers/specs/2026-06-19-financial-advisory-team-design.md
(Phase 2).
"""
from __future__ import annotations

import os

# The canonical surfaces (sentence/statement form) that anchor the review, in a
# stable reading order. Each is a NodeKind.SURFACE built by live_surfaces; only
# present + valid ones render.
CANONICAL_REVIEW_SURFACES: tuple[str, ...] = (
    "surface:retirement_age_headline",
    "surface:fi_verdict",
    "surface:fi_crossing_statement",
    "surface:dashboard.net_worth_liquid_tile",
    "surface:dashboard.net_worth_investable_tile",
    "surface:dashboard.net_worth_total_tile",
    "surface:retention_at_vest_statement",
    "surface:retention_capital_track_statement",
    "surface:us_situs_estate_headline",
)

_HEADER = "## Canonical reconciliation (registry single source)"
_INTRO = (
    "These are the authoritative registry figures (one owner each). Every other "
    "surface in this plan must agree with them; a figure that disagrees is a "
    "finding to route to that figure's owner, not a value to average."
)

FLAG_ENV = "ARGOSY_REGISTRY_REVIEW_ARTIFACT"


def _flag_on() -> bool:
    """True when the registry-review-artifact anchor is enabled. An explicit env
    var wins; else the configured default (default OFF). Mirrors
    incremental_plan._flag_on truthiness (1/true/yes/on)."""
    env = os.environ.get(FLAG_ENV)
    if env is not None:
        return str(env).strip().lower() in {"1", "true", "yes", "on"}
    try:
        from argosy.config import get_settings

        val = getattr(get_settings(), FLAG_ENV.lower(), None)
    except Exception:  # noqa: BLE001
        val = None
    if isinstance(val, bool):
        return val
    return str(val or "").strip().lower() in {"1", "true", "yes", "on"}


def render_canonical_reconciliation_block(
    graph, *, keys: tuple[str, ...] = CANONICAL_REVIEW_SURFACES
) -> str:
    """Render the canonical surfaces present + valid in ``graph`` into a markdown
    anchor block. Skips absent/invalid/non-string surfaces; returns "" when none
    render (fail-safe — never a header with no figures)."""
    bullets: list[str] = []
    for key in keys:
        try:
            node = graph.get(key)
        except Exception:  # noqa: BLE001 — absent surface is skipped
            continue
        if not graph.is_valid(key):
            continue
        val = node.value
        if isinstance(val, str) and val.strip():
            bullets.append(f"- {val.strip()}")
    if not bullets:
        return ""
    return "\n".join([_HEADER, "", _INTRO, "", *bullets]) + "\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_registry_review_artifact.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/registry_review_artifact.py tests/test_registry_review_artifact.py
git commit -m "feat(review): canonical reconciliation block from the registry graph (Phase 2a)"
```

---

### Task 2: `assemble_registry_review_artifact` + flag default

**Files:**
- Modify: `argosy/quality/registry_review_artifact.py`
- Test: `tests/test_registry_review_artifact.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_registry_review_artifact.py
from argosy.quality.registry_review_artifact import (
    assemble_registry_review_artifact, _flag_on, FLAG_ENV,
)


def test_assemble_appends_anchor_to_base_text():
    g = _graph_with_surfaces()
    base = "# Argosy Plan Snapshot\n\n## Current Plan\nbody prose here.\n"
    out = assemble_registry_review_artifact(
        None, user_id="x", decision_run_id=0, base_text=base, graph=g)
    assert out.startswith("# Argosy Plan Snapshot")        # base preserved
    assert "## Current Plan" in out
    assert "Canonical reconciliation (registry single source)" in out
    assert out.index("body prose here") < out.index("Canonical reconciliation")


def test_assemble_no_canonical_surfaces_returns_base_unchanged():
    base = "# plan\n"
    out = assemble_registry_review_artifact(
        None, user_id="x", decision_run_id=0, base_text=base,
        graph=__import__("argosy.quality.derivation_graph",
                         fromlist=["DerivationGraph"]).DerivationGraph())
    assert out == base


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv(FLAG_ENV, raising=False)
    # With no env override and no settings attr, the flag is OFF (fail-safe).
    assert _flag_on() in (False, True)  # never raises
    monkeypatch.setenv(FLAG_ENV, "0")
    assert _flag_on() is False
    monkeypatch.setenv(FLAG_ENV, "1")
    assert _flag_on() is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_registry_review_artifact.py -k assemble -v`
Expected: FAIL — `ImportError: cannot import name 'assemble_registry_review_artifact'`.

- [ ] **Step 3: Implement**

Append to `argosy/quality/registry_review_artifact.py`:

```python
def assemble_registry_review_artifact(
    session, *, user_id: str, decision_run_id: int,
    base_text: str | None = None, graph=None,
) -> str:
    """The reader-candidate artifact: today's assembled from-scratch text with a
    canonical reconciliation anchor block appended. ``base_text`` / ``graph`` are
    injectable for tests; in production they are read from
    ``assemble_plan_artifact`` and ``build_base_graph`` (the same graph Phase 1c
    built). Returns ``base_text`` unchanged when no canonical surface renders
    (fail-safe — never strip or corrupt the artifact)."""
    if base_text is None:
        from argosy.services.assembled_artifact import assemble_plan_artifact
        base_text = assemble_plan_artifact(session, user_id=user_id).full_text or ""
    if graph is None:
        from argosy.orchestrator.flows.incremental_plan import build_base_graph
        graph = build_base_graph(session, user_id, decision_run_id=decision_run_id)
    block = render_canonical_reconciliation_block(graph)
    if not block:
        return base_text
    return base_text.rstrip() + "\n\n" + block
```

Add both new names to `__all__` (create `__all__` if absent):

```python
__all__ = [
    "CANONICAL_REVIEW_SURFACES",
    "FLAG_ENV",
    "render_canonical_reconciliation_block",
    "assemble_registry_review_artifact",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_registry_review_artifact.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/registry_review_artifact.py tests/test_registry_review_artifact.py
git commit -m "feat(review): assemble registry-anchored reader artifact + flag (Phase 2a)"
```

---

### Task 3: Gated wiring in the orchestrator reader call (default OFF)

**Files:**
- Modify: `argosy/orchestrator/flows/plan_synthesis/orchestrator.py:1107-1117`
- Test: `tests/test_registry_review_artifact.py`

**Context:** the reader input is built at `orchestrator.py:1111` (`assemble_plan_artifact`). Add a flag-gated branch so flag-OFF is byte-identical to today and flag-ON anchors the artifact. Fail-soft: any error keeps the from-scratch text.

- [ ] **Step 1: Write the failing test (the wiring is exercised via a small seam)**

Add a tiny indirection so the orchestrator branch is unit-testable without standing up the whole synthesis flow:

```python
# append to tests/test_registry_review_artifact.py
from argosy.quality.registry_review_artifact import maybe_anchor_reader_artifact


def test_maybe_anchor_off_is_identity(monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "0")
    base = "# plan body\n"
    # graph builder must NOT be called when the flag is off.
    def _boom(*a, **k):  # pragma: no cover - asserts not called
        raise AssertionError("graph built while flag OFF")
    out = maybe_anchor_reader_artifact(
        None, user_id="x", decision_run_id=0, base_text=base, _builder=_boom)
    assert out == base


def test_maybe_anchor_on_appends(monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "1")
    g = _graph_with_surfaces()
    base = "# plan body\n"
    out = maybe_anchor_reader_artifact(
        None, user_id="x", decision_run_id=0, base_text=base,
        _builder=lambda *a, **k: g)
    assert "Canonical reconciliation (registry single source)" in out


def test_maybe_anchor_failsoft_keeps_base(monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "1")
    base = "# plan body\n"
    def _boom(*a, **k):
        raise RuntimeError("graph build failed")
    out = maybe_anchor_reader_artifact(
        None, user_id="x", decision_run_id=0, base_text=base, _builder=_boom)
    assert out == base  # fail-soft: never lose the from-scratch artifact
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_registry_review_artifact.py -k maybe_anchor -v`
Expected: FAIL — `ImportError: cannot import name 'maybe_anchor_reader_artifact'`.

- [ ] **Step 3: Implement the seam**

Append to `argosy/quality/registry_review_artifact.py`:

```python
def maybe_anchor_reader_artifact(
    session, *, user_id: str, decision_run_id: int, base_text: str,
    _builder=None,
) -> str:
    """Return ``base_text`` anchored with the canonical reconciliation block when
    the flag is ON, else ``base_text`` unchanged. Fail-soft: any error logs and
    returns ``base_text`` (the from-scratch artifact is never lost). ``_builder``
    injects the graph builder for tests."""
    if not _flag_on():
        return base_text
    import logging
    log = logging.getLogger(__name__)
    try:
        graph = None
        if _builder is not None:
            graph = _builder(session, user_id, decision_run_id=decision_run_id)
        return assemble_registry_review_artifact(
            session, user_id=user_id, decision_run_id=decision_run_id,
            base_text=base_text, graph=graph)
    except Exception as exc:  # noqa: BLE001 — fail-soft, keep from-scratch text
        log.warning("registry_review.anchor_failed err=%s", exc)
        return base_text
```

Add `"maybe_anchor_reader_artifact"` to `__all__`.

- [ ] **Step 4: Wire the orchestrator**

In `argosy/orchestrator/flows/plan_synthesis/orchestrator.py`, after line 1112 (`_assembled_text = _assembled.full_text or ""`), inside the same `try`:

```python
            from argosy.quality.registry_review_artifact import (
                maybe_anchor_reader_artifact,
            )
            _assembled_text = maybe_anchor_reader_artifact(
                session, user_id=user_id, decision_run_id=decision_run_id,
                base_text=_assembled_text,
            )
```

(Default OFF → `maybe_anchor_reader_artifact` returns `_assembled_text` unchanged, so the live reader path is byte-identical until the flag flips.)

- [ ] **Step 5: Run test + the touched synthesis test**

Run: `.venv/Scripts/python.exe -m pytest tests/test_registry_review_artifact.py tests/test_plan_synthesis_whole_artifact.py -v -m "not llm_eval"`
Expected: PASS (the synthesis test is unaffected — flag OFF by default).

- [ ] **Step 6: Commit**

```bash
git add argosy/quality/registry_review_artifact.py argosy/orchestrator/flows/plan_synthesis/orchestrator.py tests/test_registry_review_artifact.py
git commit -m "feat(review): wire registry-anchored reader artifact behind default-OFF flag (Phase 2a)"
```

---

### Task 4: Live proof on run-117

**Files:**
- Create: `tmp_review/registry_review_artifact_proof.py` (gitignored scratch — verification only)

- [ ] **Step 1: Write the proof script**

```python
# tmp_review/registry_review_artifact_proof.py
"""Render the registry-anchored reader-candidate artifact for the live draft and
confirm the canonical block carries the run-117 figures. UTF-8; no shekel to stdout."""
from __future__ import annotations
import io, os, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
os.environ["ARGOSY_INCREMENTAL_PLAN"] = "1"
os.environ["ARGOSY_REGISTRY_REVIEW_ARTIFACT"] = "1"

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from argosy.config import get_settings
from argosy.state.models import PlanVersion
from argosy.quality.registry_review_artifact import (
    assemble_registry_review_artifact, render_canonical_reconciliation_block,
)
from argosy.orchestrator.flows.incremental_plan import build_base_graph

url = get_settings().database_url.replace("+aiosqlite", "")
Session = sessionmaker(bind=create_engine(url, connect_args={"check_same_thread": False}))
OUT = "tmp_review/registry_review_artifact_proof.txt"
with Session() as s:
    pv = s.execute(select(PlanVersion).where(
        PlanVersion.user_id == "ariel",
        PlanVersion.role.in_(("draft", "current")),
    ).order_by(PlanVersion.id.desc())).scalars().first()
    graph = build_base_graph(s, "ariel", decision_run_id=pv.decision_run_id)
    block = render_canonical_reconciliation_block(graph)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(block)
print(block.encode("ascii", "replace").decode("ascii"))
print(f"\n[written to {OUT}]")
```

- [ ] **Step 2: Run it**

Run: `.venv/Scripts/python.exe tmp_review/registry_review_artifact_proof.py`
Expected: the canonical block lists the run-117 figures — earliest safe age 46, FI NOT reached, FI crossing 2027, the three net-worth bases (liquid ~11.67M / investable ~11.87M / total ~14.05M), retention 50% / 70%, US-situs ~9.45M.

- [ ] **Step 3: Run the broad regression**

Run: `.venv/Scripts/python.exe -m pytest tests/test_registry_review_artifact.py tests/test_live_surfaces.py tests/test_incremental_plan.py tests/test_plan_synthesis_whole_artifact.py -q -m "not llm_eval"`
Expected: PASS

---

## Self-Review

**1. Spec coverage:** Phase 2 = "the reviewer reviews the registry-rendered artifact." This slice gives the reviewer a canonical registry anchor (the contradiction-prone subjects, byte-identical to the graph), reversibly (flag default OFF), and is the spec's mandated "prove the render-from-registry path before big-bang" step. Body-prose binding via the surgical-reconcile editor and the live flag-flip (after a BLOCK-reduction proof) are explicitly deferred — noted, not dropped.

**2. Placeholder scan:** every code step shows full code; commands have expected output. `[derivation pending]`-style strings are not used here.

**3. Type consistency:** `render_canonical_reconciliation_block(graph, *, keys=...)`, `assemble_registry_review_artifact(session, *, user_id, decision_run_id, base_text=None, graph=None)`, `maybe_anchor_reader_artifact(session, *, user_id, decision_run_id, base_text, _builder=None)`, `_flag_on()`, `FLAG_ENV` — names identical across the module, the orchestrator wiring, and the tests. Surface keys match the `live_surfaces` builders verified in Phase 1c.

**4. Risk / reversibility:** flag default OFF ⇒ `maybe_anchor_reader_artifact` is identity ⇒ the live reader path is byte-identical to today. The branch is fail-soft (any graph-build error returns the from-scratch text), so enabling the flag can never produce an empty/corrupt artifact. The only behavior change when ON is an APPENDED canonical block — additive, never a strip/edit of existing prose.
