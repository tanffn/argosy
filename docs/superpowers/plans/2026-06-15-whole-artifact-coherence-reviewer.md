# Whole-Artifact Coherence — Closing the Consistency / Coherence / Currency Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the review stage Argosy has never had — a check on the *complete, assembled* plan artifact (the exact bytes the user reads), against itself and against the world — so a single fresh LLM prompt can no longer out-find the whole agent fabric.

**Architecture:** Two layers. (1) A **deterministic cross-surface coherence gate** that pulls the same concept (net worth, NVDA weight, FI margin sign, estate tail) from every surface and fails if they disagree or share a label — plus a deterministic FI-sufficiency-under-NVDA-shock derivation and an input-freshness check. (2) A **whole-artifact adversarial reader**: a final-stage agent fed the assembled rendered document + a fresh-external-context packet + the prior plan to diff, blind to the synthesis logic, that reads it like a hostile human and fails closed. Deterministic catches the known classes; the reader catches the unanticipated tail.

**Tech Stack:** Python 3.12, SQLAlchemy, Pydantic, pytest. Reuses `argosy/quality/` gate framework, `argosy/services/plan_numeric_resolver.py`, `argosy/services/wealth_dashboard.py`, and the `tools/codex-tandem` kit (`engine_codex.run_codex`) already used by `codex_second_opinion.py`.

---

## WHY THIS EXISTS (the diagnosis — read once, do not re-litigate)

A single LLM prompt repeatedly finds holes in promoted plans that the whole adversarial fabric (analysts → debate → synth → 3 risk officers → codex → FM → gate) misses, **and the holes are real.** Root cause: **Argosy is built to produce a plan, and has no stage that reads the finished plan as a whole.** Every review operates on inputs or drafts-in-pieces; nothing holds the assembled artifact the user reads and asks "does this cohere with itself and with the world today?"

The missed defects are **emergent properties of the whole**, not wrong numbers:
- **Cross-surface contradiction** (FI "reached" on one line, "not reached" on another; net worth ₪11.95M in the body, ₪14.44M on the dashboard; NVDA 62.5% body vs 56.9% dashboard) — lives in the *seams between components* that no specialist owns; the body and dashboard are produced by different subsystems that never see each other.
- **Compositional fragility** ("FI reached" is true only at full NVDA mark; a −30% NVDA move breaks even the perpetuity base) — only visible when you *combine* the synthesizer's claim with the risk officer's tail; no agent's job.
- **Currency** (macro reads a stale regime; snapshot is the pre-sale book) — the system trusts its own stored state as ground truth; nothing checks it against now.

Four distinct properties: **consistency** (one number everywhere) and **correctness** (re-derive from raw) were addressed in the 2026-06-15 session. **Coherence** (the whole agrees with itself) and **currency** (matches reality now) have **no stage** — this plan adds them. Per-number gates and even a blind per-number re-derivation cannot catch coherence: coherence is a property of the whole.

Anti-pattern to avoid: "add more specialist reviewers." More specialists = more seams. The fix is the *unit and stance* of review (holistic, whole-artifact, outside-informed), not the count.

---

## FILE STRUCTURE

- `argosy/quality/coherence_gate.py` — **new.** Deterministic cross-surface coherence checks (`check_cross_surface_coherence`) + the FI-shock sufficiency check (`check_fi_sufficiency_under_shock`). Pure functions over resolver values + the assembled-surface values. One responsibility: "do the surfaces agree, and does the headline claim survive the plan's own risk."
- `argosy/quality/freshness_gate.py` — **new.** `check_input_freshness` — snapshot date, cached analyst-report age, macro timestamp vs `today`.
- `argosy/services/retirement/fi_shock.py` — **new.** `fi_sufficiency_under_shock(...)` deterministic derivation (recompute FI-eligible capital at NVDA −30%/−50%/p5).
- `argosy/services/assembled_artifact.py` — **new.** `assemble_plan_artifact(session, user_id, plan_version) -> AssembledArtifact` — concatenates the exact surfaces a user reads (plan body markdown + the rendered wealth-dashboard block + appendices) into one string + a typed map of each surface's headline values. The single source the whole-artifact reader and the cross-surface gate both consume.
- `argosy/orchestrator/flows/plan_synthesis/whole_artifact_reader.py` — **new.** The final-stage adversarial LLM reader (mirrors `codex_second_opinion.py`: blind, structured output, fail-closed). Fed the assembled artifact + an external-context packet + the prior plan.
- `argosy/quality/gate_types.py` — **modify.** Add `GateCheck.CROSS_SURFACE_COHERENCE`, `GateCheck.FI_SHOCK_SUFFICIENCY`, `GateCheck.INPUT_FRESHNESS`.
- `argosy/services/plan_numeric_resolver.py` — **modify.** Add `retirement.fi_margin_signed_nis` (one signed value: net_worth − fi_total) so every surface cites one number for "reached/not-reached" (kills the L72/L188 sign-flip class).
- `docs/design/SDD.md` — **modify.** Document the coherence/currency stage as current-state design once shipped.

---

## PHASE 1 — Deterministic cross-surface coherence (the fast, testable wins)

### Task 1: One signed FI margin in the resolver (kills the reached/not-reached sign-flip)

**Files:**
- Modify: `argosy/services/plan_numeric_resolver.py` (add a key in `resolve_plan_numbers`, after `_apply_fi_methodology` populates `retirement.fi_total_capital_nis` and `portfolio.net_worth_nis`)
- Test: `tests/test_plan_numeric_resolver.py`

- [ ] **Step 1: Write the failing test**

```python
def test_fi_margin_signed_is_single_sourced(session):
    """The FI sufficiency margin must be ONE signed value (net_worth − FI-total)
    so every surface cites the same number with the same sign. The L72/L188
    'reached vs −118,020 not-reached' contradiction was two surfaces computing
    the margin independently with opposite sign conventions."""
    _seed_snapshot_and_reports(session)  # reuse the file's existing seeding helper
    res = resolve_plan_numbers(session, user_id="ariel", decision_run_id=_RUN, include_canonical_ages=False)
    nw = res.get("portfolio.net_worth_nis")
    tot = res.get("retirement.fi_total_capital_nis")
    margin = res.get("retirement.fi_margin_signed_nis")
    assert margin is not None and margin.status == "resolved"
    # margin = net_worth − fi_total; positive => total target reached.
    assert abs(float(margin.value) - (float(nw.value) - float(tot.value))) < 1.0
    assert "net_worth" in margin.formula and "fi_total" in margin.formula
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_plan_numeric_resolver.py::test_fi_margin_signed_is_single_sourced -q -p no:cacheprovider`
Expected: FAIL — `retirement.fi_margin_signed_nis` is `None`.

- [ ] **Step 3: Implement — add `_apply_fi_margin` and call it after the FI/networth applies**

In `resolve_plan_numbers`, after `_apply_fi_methodology(...)` and `_apply_fx_boi(...)` (which populate net worth + FI total), add `_apply_fi_margin(session, values)`. Implement:

```python
def _apply_fi_margin(values: dict[str, ResolvedValue]) -> None:
    """Single signed FI sufficiency margin = net_worth − FI-total-capital.
    Positive => the total capital target is (marginally) reached. Every surface
    that states 'FI reached / not reached' MUST cite this one value, so the
    reached/not-reached sign can never diverge across surfaces."""
    key = "retirement.fi_margin_signed_nis"
    nw = values.get("portfolio.net_worth_nis")
    tot = values.get("retirement.fi_total_capital_nis")
    if (nw is None or tot is None or nw.status != "resolved" or tot.status != "resolved"
            or nw.value is None or tot.value is None):
        values[key] = ResolvedValue.pending(key, "nis", "net_worth − fi_total_capital")
        return
    values[key] = ResolvedValue(
        key=key, value=float(nw.value) - float(tot.value), unit="nis",
        status="resolved",
        source_locator="portfolio.net_worth_nis − retirement.fi_total_capital_nis",
        agent_report_id=None, confidence="HIGH",
        formula="net_worth_nis − fi_total_capital_nis (signed; >0 => total target reached)",
    )
```

Also register `"retirement.fi_margin_signed_nis": "nis"` in the units map (near line 132 where `concentration.nvda_current_pct` is registered) and add a label row in `render_numbers_for_synth`/the headline render list ("FI sufficiency margin (net worth − total target)").

- [ ] **Step 4: Run test to verify it passes**

Run: same as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add argosy/services/plan_numeric_resolver.py tests/test_plan_numeric_resolver.py
git commit -m "feat(resolver): single signed FI margin (one reached/not-reached value across surfaces)"
```

---

### Task 2: The assembled-artifact builder (the thing the reader + gate consume)

**Files:**
- Create: `argosy/services/assembled_artifact.py`
- Test: `tests/test_assembled_artifact.py`

**Discovery first (do this, then write the test):** the user-facing plan markdown is rendered in `argosy/orchestrator/flows/plan_synthesis/render.py` (grep `def render_` — `render_plan_appendices`, the horizon renderers) and the dashboard block by `argosy/services/wealth_dashboard.py::compute_wealth_dashboard`. Confirm which function produces the exported body the user reads (grep for where `argosy-plan-*.md` / a plan export is built; check `argosy/services/plan_export.py`). The assembler must reproduce **exactly what the user sees**: body (all 3 horizons) + the dashboard block + appendices, concatenated.

- [ ] **Step 1: Write the failing test**

```python
def test_assemble_includes_every_user_facing_surface(session):
    """The assembled artifact must contain EVERY surface the user reads — body,
    dashboard, appendices — in one string, plus a typed map of each surface's
    headline values. This is the artifact no existing review stage ever holds."""
    _seed_full_plan(session)  # current plan + snapshot + reports
    art = assemble_plan_artifact(session, user_id="ariel")
    # All surfaces present in the concatenated text:
    assert "## Wealth Dashboard" in art.full_text
    assert "Long horizon" in art.full_text or "# Long" in art.full_text
    assert "Appendix" in art.full_text
    # Typed headline map carries the same concept from each surface that states it:
    assert "net_worth_nis" in art.surface_values  # dict[concept] -> list[(surface, value)]
    assert len(art.surface_values["net_worth_nis"]) >= 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_assembled_artifact.py -q -p no:cacheprovider`
Expected: FAIL — module/function missing.

- [ ] **Step 3: Implement `assemble_plan_artifact`**

```python
from dataclasses import dataclass, field

@dataclass
class AssembledArtifact:
    full_text: str  # exact concatenation of every user-facing surface
    surface_values: dict[str, list[tuple[str, float]]]  # concept -> [(surface_name, value)]

def assemble_plan_artifact(session, *, user_id: str) -> AssembledArtifact:
    """Concatenate every surface the user reads into one artifact + extract the
    headline value each surface states for each shared concept, so a coherence
    check (or a reader) can compare them. Surfaces: plan body (render path),
    wealth dashboard (compute_wealth_dashboard), appendices."""
    # 1. get_current_plan -> render the body markdown via the render path.
    # 2. compute_wealth_dashboard(session, user_id) -> render its block (reuse
    #    the dashboard->markdown helper used by the export).
    # 3. Concatenate body + dashboard + appendices into full_text.
    # 4. surface_values: from each surface, capture the value it states for
    #    each concept it exposes. Body+resolver concepts come from the resolver
    #    keys; the dashboard concepts come from the WealthDashboard dataclass
    #    fields (retirement.net_worth_nis, concentration.current_pct, estate_*).
    ...
```

Wire the concept extraction to **named concepts**, not regex over prose: pull body values from `resolve_plan_numbers` and dashboard values from the `WealthDashboard` dataclass fields. Map both into `surface_values` keyed by a shared concept name (`net_worth_nis`, `nvda_weight_pct`, `us_situs_estate_nis`, `fi_margin_signed_nis`).

- [ ] **Step 4: Run test to verify it passes**

Run: same as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add argosy/services/assembled_artifact.py tests/test_assembled_artifact.py
git commit -m "feat(plan): assemble the full user-facing artifact + per-surface headline map"
```

---

### Task 3: Deterministic cross-surface coherence gate

**Files:**
- Modify: `argosy/quality/gate_types.py` (add `CROSS_SURFACE_COHERENCE`)
- Create: `argosy/quality/coherence_gate.py`
- Test: `tests/test_coherence_gate.py`

- [ ] **Step 1: Add the GateCheck enum value**

In `gate_types.py`, in the `GateCheck` enum:

```python
    # S22 — the same concept (net worth, NVDA weight, FI margin, estate) must
    # carry the SAME value across every surface the user reads (body, dashboard,
    # appendices), or carry explicitly distinct labels. Catches the cross-surface
    # contradiction class (FI reached-vs-not; body 62.5% vs dashboard 56.9%) that
    # no per-surface agent owns. Deterministic — coherence is a property of the
    # whole, not eyeballed by an LLM reviewer.
    CROSS_SURFACE_COHERENCE = "cross_surface_coherence"
```

- [ ] **Step 2: Write the failing test**

```python
from types import SimpleNamespace
from argosy.quality.coherence_gate import check_cross_surface_coherence
from argosy.quality.gate_types import GateCheck

def _art(surface_values):
    return SimpleNamespace(full_text="", surface_values=surface_values)

def test_coherence_flags_divergent_nvda_weight_across_surfaces():
    """Body 62.5% vs dashboard 56.9% for the same concept must fail."""
    art = _art({"nvda_weight_pct": [("body", 62.5), ("dashboard", 56.9)]})
    viol = check_cross_surface_coherence(art)
    assert len(viol) == 1
    assert viol[0].check is GateCheck.CROSS_SURFACE_COHERENCE
    assert "nvda_weight_pct" in viol[0].detail

def test_coherence_passes_when_surfaces_agree():
    art = _art({"nvda_weight_pct": [("body", 62.52), ("dashboard", 62.5)]})  # within tol
    assert check_cross_surface_coherence(art) == []

def test_coherence_flags_sign_flip_on_fi_margin():
    """The L72/L188 class: +118,020 on one surface, −118,020 on another."""
    art = _art({"fi_margin_signed_nis": [("capital_sufficiency", 118020.0), ("body", -118020.0)]})
    viol = check_cross_surface_coherence(art)
    assert len(viol) == 1 and "fi_margin_signed_nis" in viol[0].detail
```

- [ ] **Step 3: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_coherence_gate.py -q -p no:cacheprovider`
Expected: FAIL — module missing.

- [ ] **Step 4: Implement `check_cross_surface_coherence`**

```python
from argosy.quality.gate_types import GateCheck, GateViolation

# Relative tolerance for "same concept, same value across surfaces".
_REL_TOL = 0.01  # 1%

def check_cross_surface_coherence(artifact) -> list[GateViolation]:
    """Every concept stated on >1 surface must agree within tolerance (and not
    flip sign). A concept that two surfaces report differently is a coherence
    defect — the surfaces must bind to one source or carry distinct labels."""
    violations: list[GateViolation] = []
    for concept, pairs in (artifact.surface_values or {}).items():
        vals = [(s, v) for s, v in pairs if isinstance(v, (int, float))]
        if len(vals) < 2:
            continue
        lo = min(v for _, v in vals)
        hi = max(v for _, v in vals)
        base = max(abs(lo), abs(hi), 1.0)
        sign_flip = (lo < 0 < hi)
        if sign_flip or (hi - lo) / base > _REL_TOL:
            listing = "; ".join(f"{s}={v}" for s, v in vals)
            violations.append(GateViolation(
                check=GateCheck.CROSS_SURFACE_COHERENCE,
                detail=(f"concept `{concept}` disagrees across surfaces "
                        f"({'SIGN FLIP — ' if sign_flip else ''}{listing}). "
                        "Bind all surfaces to one source or give them distinct labels."),
                locator=concept,
            ))
    return violations
```

- [ ] **Step 5: Run to verify it passes**

Run: same as Step 3. Expected: PASS (3 tests).

- [ ] **Step 6: Export + commit**

Add `check_cross_surface_coherence` to `argosy/quality/__init__.py` imports + `__all__`.

```bash
git add argosy/quality/gate_types.py argosy/quality/coherence_gate.py argosy/quality/__init__.py tests/test_coherence_gate.py
git commit -m "feat(gate): deterministic cross-surface coherence check (same concept, same value everywhere)"
```

---

### Task 4: FI-sufficiency-under-NVDA-shock (the compositional check)

**Files:**
- Create: `argosy/services/retirement/fi_shock.py`
- Test: `tests/test_fi_shock.py`

- [ ] **Step 1: Write the failing test**

```python
from argosy.services.retirement.fi_shock import fi_sufficiency_under_shock

def test_fi_reached_only_at_full_nvda_mark():
    """The 2026-06-15 reality: NW ₪11.95M, perpetuity ₪10.39M, NVDA ₪6.81M in
    the book. A −30% NVDA move drops NW below the perpetuity base — so 'FI
    reached' is true ONLY at the full NVDA mark. This composes the synthesizer's
    sufficiency claim with the risk officer's concentration; no single agent did."""
    out = fi_sufficiency_under_shock(
        net_worth_nis=11_954_153, nvda_value_nis=6_807_040,
        perpetuity_base_nis=10_386_133, fi_total_nis=11_836_133,
        shocks=(0.30, 0.50),
    )
    assert out["base"]["total_reached"] is True
    assert out["shock_0.30"]["perpetuity_reached"] is False  # ₪9.91M < ₪10.39M
    assert out["shock_0.50"]["perpetuity_reached"] is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_fi_shock.py -q -p no:cacheprovider`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
def fi_sufficiency_under_shock(*, net_worth_nis, nvda_value_nis, perpetuity_base_nis,
                               fi_total_nis, shocks=(0.30, 0.50)):
    """Recompute FI sufficiency after marking NVDA down by each shock. Returns a
    dict with the base case + one row per shock: shocked net worth and whether
    the perpetuity base and total target still clear."""
    def row(nw):
        return {"net_worth_nis": round(nw, 2),
                "perpetuity_reached": nw >= perpetuity_base_nis,
                "total_reached": nw >= fi_total_nis}
    out = {"base": row(net_worth_nis)}
    for s in shocks:
        out[f"shock_{s}"] = row(net_worth_nis - s * nvda_value_nis)
    return out
```

- [ ] **Step 4: Run to verify it passes** — same as Step 2. Expected: PASS.

- [ ] **Step 5: Wire into the gate + plan render**

Add `GateCheck.FI_SHOCK_SUFFICIENCY` to `gate_types.py`. Add `check_fi_sufficiency_under_shock(resolved)` to `coherence_gate.py` that calls `fi_sufficiency_under_shock` from resolver values and emits a violation if the plan text asserts "reached / capital sufficiency reached" **without** a shock qualifier when `shock_0.30.perpetuity_reached` is False. Surface the shock table in the plan's capital_sufficiency section (render path). Add tests mirroring Task 3's style.

- [ ] **Step 6: Commit**

```bash
git add argosy/services/retirement/fi_shock.py argosy/quality/coherence_gate.py argosy/quality/gate_types.py tests/test_fi_shock.py tests/test_coherence_gate.py
git commit -m "feat(retirement): FI sufficiency under NVDA shock + gate the 'reached' claim against it"
```

---

### Task 5: Input-freshness gate (currency)

**Files:**
- Modify: `argosy/quality/gate_types.py` (add `INPUT_FRESHNESS`)
- Create: `argosy/quality/freshness_gate.py`
- Test: `tests/test_freshness_gate.py`

- [ ] **Step 1: Write the failing test**

```python
from datetime import date
from argosy.quality.freshness_gate import check_input_freshness
from argosy.quality.gate_types import GateCheck

def test_stale_snapshot_is_flagged():
    """Snapshot older than the freshness window vs `today` is a currency defect —
    the system must distrust its own stored state (the pre-sale-book class)."""
    viol = check_input_freshness(
        today=date(2026, 6, 15),
        snapshot_date=date(2026, 6, 12),
        analyst_report_dates={"macro": date(2026, 6, 14)},
        max_snapshot_age_days=2,  # 06-12 -> 3 days old > 2
        max_report_age_days=2,
    )
    assert any(v.check is GateCheck.INPUT_FRESHNESS and "snapshot" in v.detail for v in viol)

def test_fresh_inputs_pass():
    viol = check_input_freshness(
        today=date(2026, 6, 15), snapshot_date=date(2026, 6, 15),
        analyst_report_dates={"macro": date(2026, 6, 15)},
        max_snapshot_age_days=2, max_report_age_days=2,
    )
    assert viol == []
```

- [ ] **Step 2-4: fail → implement → pass.** Implement `check_input_freshness(*, today, snapshot_date, analyst_report_dates, max_snapshot_age_days=2, max_report_age_days=3)` returning a `GateViolation(check=GateCheck.INPUT_FRESHNESS, ...)` per stale input. Run `.venv\Scripts\python.exe -m pytest tests/test_freshness_gate.py -q -p no:cacheprovider`.

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/gate_types.py argosy/quality/freshness_gate.py argosy/quality/__init__.py tests/test_freshness_gate.py
git commit -m "feat(gate): input-freshness check (distrust stale snapshot / cached analyst outputs vs today)"
```

---

## PHASE 2 — The whole-artifact adversarial reader (the holistic stage)

### Task 6: The reader agent (blind, fed the assembled artifact, fail-closed)

**Files:**
- Create: `argosy/orchestrator/flows/plan_synthesis/whole_artifact_reader.py`
- Test: `tests/test_whole_artifact_reader.py`

**Pattern to mirror:** `argosy/orchestrator/flows/plan_synthesis/codex_second_opinion.py` (this session's blind-reviewer work) — structured Pydantic output, `engine_codex.run_codex(..., sandbox="danger-full-access")`, `_parse_*` with strict→lenient→fail-closed, and the env/pytest kill switches. Reuse that structure verbatim where possible.

- [ ] **Step 1: Define the output schema + write the failing parse test**

```python
# schema
class CoherenceFinding(BaseModel):
    kind: Literal["contradiction", "cross_surface", "fragile_claim", "stale", "other"]
    severity: Literal["BLOCKER", "AMBER", "YELLOW"]
    detail: str
    surfaces_cited: list[str] = Field(default_factory=list)  # verbatim excerpts that conflict

class WholeArtifactVerdict(BaseModel):
    overall_assessment: Literal["APPROVE", "APPROVE_WITH_CONDITIONS", "BLOCK"]
    findings: list[CoherenceFinding] = Field(default_factory=list)

# test
def test_reader_parse_fails_closed_on_empty():
    from argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader import _parse_verdict
    v = _parse_verdict("")
    assert v.overall_assessment == "BLOCK"  # timeout/unparseable => fail closed (S21 lesson)
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_whole_artifact_reader.py::test_reader_parse_fails_closed_on_empty -q -p no:cacheprovider`. Expected FAIL.

- [ ] **Step 3: Implement the prompt + dispatch + parse**

Prompt contract (the centerpiece — write it as a module constant):
- Input is the FULL assembled artifact (`AssembledArtifact.full_text`) — the exact bytes the user reads — plus a fresh-external-context packet (today's date + any market/event context the caller passes) plus the prior plan's text to diff.
- Instruction: "You are a hostile reader. Read this complete document the way a skeptical client would. Find: (1) any place it CONTRADICTS itself (same concept, different value/conclusion in two places — quote both); (2) any headline CLAIM that the document's own other sections undercut (e.g. a sufficiency claim that a stated concentration/tail makes fragile); (3) anything STALE relative to today's date / the external context; (4) anything that REGRESSED vs the prior plan. Quote the conflicting passages verbatim. Do not re-derive numbers from scratch — that is a separate gate; your job is the COHERENCE OF THE WHOLE."
- Dispatch via `run_codex` (Layout A, `sandbox="danger-full-access"`, prompt-to-file — see `reference_codex_tandem` memory for the Windows gotchas).
- `_parse_verdict`: strict→lenient→**fail-closed to BLOCK** on empty/unparseable (reuse the S21 `_enforce`-style logic).

- [ ] **Step 4: Run to verify it passes** — same as Step 2. Expected PASS.

- [ ] **Step 5: Commit**

```bash
git add argosy/orchestrator/flows/plan_synthesis/whole_artifact_reader.py tests/test_whole_artifact_reader.py
git commit -m "feat(review): whole-artifact adversarial reader (holistic, blind, fail-closed)"
```

---

### Task 7: Wire the reader into the synthesis flow as the FINAL stage

**Files:**
- Modify: `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` (after the FM / codex stage, before promotion eligibility)
- Test: `tests/test_plan_synthesis_whole_artifact.py`

- [ ] **Step 1: Write the failing test** — assert that after synthesis, a `whole_artifact_reader` `AgentReport` row exists for the run and that a draft whose assembled artifact contains an injected contradiction yields a BLOCK verdict (monkeypatch `run_codex` to return a BLOCK with a `contradiction` finding, as the codex tests do).

- [ ] **Step 2-4: fail → implement → pass.** In `orchestrator.py`, after the codex/FM stage: build the assembled artifact (`assemble_plan_artifact`), build the external-context packet (today + any caller-supplied market context), fetch the prior plan text, dispatch `run_whole_artifact_review(...)`, persist the verdict as an `AgentReport(agent_role="whole_artifact_reader")`, and feed its BLOCKERs into the same promotion-eligibility path the FM uses (a BLOCK marks the draft not-auto-promotable). Gate it behind the same env/pytest kill switches as codex.

- [ ] **Step 5: Commit**

```bash
git add argosy/orchestrator/flows/plan_synthesis/orchestrator.py tests/test_plan_synthesis_whole_artifact.py
git commit -m "feat(synthesis): run the whole-artifact reader as the final pre-promotion stage"
```

---

### Task 8: Wire the deterministic coherence gates into `gate_plan_output`

**Files:**
- Modify: `argosy/quality/plan_output_gate.py` (`gate_plan_output`)
- Test: `tests/test_plan_output_gate.py`

- [ ] **Step 1-4:** Add `check_cross_surface_coherence(artifact)`, `check_fi_sufficiency_under_shock(resolved)`, and `check_input_freshness(...)` to the aggregator (they need the assembled artifact + resolver + today — extend `gate_plan_output`'s signature with an optional `artifact` and `today` param, skipping the checks when absent, matching the existing `resolved is not None` skip pattern). Add an aggregator test asserting a divergent-surface artifact produces a `CROSS_SURFACE_COHERENCE` violation through `gate_plan_output`. Run the full `tests/test_plan_output_gate.py`.

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/plan_output_gate.py tests/test_plan_output_gate.py
git commit -m "feat(gate): aggregate coherence + shock + freshness into the plan output gate"
```

---

## PHASE 3 — Docs

### Task 9: SDD §6.11 — document the coherence/currency stage (current-state)

**Files:**
- Modify: `docs/design/SDD.md` (§6.11, after the blind-reviewer paragraph added 2026-06-15)

- [ ] **Step 1:** Add a current-state paragraph: the synthesis flow's final stage reads the **assembled artifact the user sees**, both deterministically (cross-surface coherence: same concept → same value across body/dashboard/appendices; FI-sufficiency-under-shock; input freshness) and holistically (the whole-artifact adversarial reader, blind, fail-closed, fed the assembled artifact + external context + the prior plan to diff). State the four properties — consistency, correctness, **coherence, currency** — and that coherence/currency are properties of the whole, enforced here rather than left to per-number gates or an LLM eyeball. NO history/changelog narration (per `feedback_docs_current_state_only`).

- [ ] **Step 2: Commit**

```bash
git add docs/design/SDD.md
git commit -m "docs(sdd): §6.11 — whole-artifact coherence + currency final stage"
```

---

## Self-review checklist (run before handing off for execution)

- [ ] Spec coverage: cross-surface contradiction (Tasks 1,2,3,8) · compositional fragility (Task 4) · currency (Task 5) · holistic catch-all (Tasks 6,7) · docs (Task 9). The four review properties all have a task.
- [ ] No placeholder gates promoted: every deterministic check has a failing test first.
- [ ] Fail-closed preserved: the reader BLOCKs on empty/unparseable (Task 6 Step 1), matching the S21 lesson.
- [ ] One canonical source: Task 1 makes the FI margin one signed value; Task 8 enforces all surfaces agree.
- [ ] Type consistency: `AssembledArtifact.surface_values: dict[str, list[tuple[str, float]]]` is the shape consumed by `check_cross_surface_coherence` (Task 3) and produced by `assemble_plan_artifact` (Task 2) — keep the concept-name keys identical (`net_worth_nis`, `nvda_weight_pct`, `us_situs_estate_nis`, `fi_margin_signed_nis`).

## Known discovery points (not placeholders — bounded lookups the executor must do)
- Task 2: confirm the exact body-render + dashboard→markdown functions that produce the *exported* artifact (grep `render_plan_appendices`, `plan_export.py`, the dashboard markdown helper). The assembler must reproduce the export, not re-invent it.
- Task 7: confirm the promotion-eligibility hook the FM uses (`argosy/api/routes/plan.py::post_draft_accept` + the `fund_manager_decision` field) so the reader's BLOCK plugs into the same path.
