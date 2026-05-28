# Retirement Companion Overhaul — Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan wave-by-wave. Steps use checkbox (`- [ ]`) syntax for tracking. Codex-tandem reviewer runs on every diff before commit — see "Codex review checkpoint" pattern in §0. **Risky money math + tax rules + Israeli pension mechanics**: do NOT skip the codex pass. The plan is split into 7 waves; full TDD detail lives in Wave 0 (foundation, load-bearing for everything else). Waves 1-7 get spec-level detail in this master document; each wave's full TDD sub-plan is expanded just-in-time before that wave starts, as a daughter plan in `docs/superpowers/plans/2026-05-28-retirement-companion-overhaul-wave-N.md`.

**Goal:** Transform Argosy from a "shows you data and trends" tool into a **policy-grade retirement companion** that (a) tells the user concrete, prioritized, time-bounded next actions, (b) gates "retire-ready" on probability-of-ruin + safety constraints rather than a single-month income-vs-expenses crossing, (c) closes 30 specific design gaps identified in the 2026-05-28 SDD review (Codex + Explore agent + main-agent synthesis), (d) makes every value and assumption traceable to a source via UI tooltips + Sources panel, and (e) follows a uniform "hero card + chart + drill-down" visualization pattern across all retirement-relevant pages.

**Architecture:** 7 waves on `main` with checkpoints after each wave for user review. Wave 0 ships the cross-cutting foundation: a `ValueWithRationale` dataclass + TS interface, a JSON-backed sources registry, three UI primitives (`<HeroCard>`, `<ValueWithTooltip>`, `<DrilldownSection>`), and a hybrid-defaults loader (`argosy/data/israel_retirement_reference.yaml` + per-user override in `identity_yaml` + freshness warning). Waves 1-7 build on that foundation: Wave 1 = Israeli structural facts (mekadem, BL stipend); Wave 2 = safety gates (NRA estate, emergency liquidity, conflict scenarios); Wave 3 = projection trust layer (P-of-ruin gate, sigma auto-calibration, regime-switch MC, stochastic FX, withdrawal policy); Wave 4 = decision policy (glide path, rebalancing, lifecycle income, phase expenses, IDF, healthcare); Wave 5 = account-aware tax engine + decumulation + lump-vs-annuity; Wave 6 = balance sheet completeness (real estate, mortgage, partner, severance split); Wave 7 = companion UX (insurance gaps, action-items policy engine, replan triggers, multi-goal, behavioral, route dedup).

**Tech Stack:** Python 3.12, FastAPI, pydantic v2, SQLAlchemy 2 (sync session); TypeScript 5, Next.js 16 (Turbopack), recharts 3.8; pytest backend; codex-tandem (Layout A, Codex reviewer) for every committed diff per user's "tandem for risky work" binding preference.

---

## §0. Foundational decisions locked at plan-start

These shape every wave. Re-read before designing any new feature.

### §0.1 Visualization standard — "Hero + chart + drill-down"

Every retirement-relevant page or panel follows this top-down structure:

```
┌────────────────────────────────────────────┐
│  HERO CARD                                 │
│  - 1-line verdict ("ON TRACK" / "WARN")    │
│  - 1-3 key numbers, color-coded            │
│  - Delta vs prior projection if relevant   │
└────────────────────────────────────────────┘

[ Chart (existing or new) ]

[ Assumptions strip — sliders that affect the chart ]

▼ Methodology  (collapsible)
   How we compute this; the formula; references inline

▼ Sensitivity  (collapsible)
   "Top 3 levers that move the verdict" — auto-generated

▼ Sources  (collapsible)
   Numbered list with URLs + as-of dates
```

Three UI primitives encode this:

- **`<HeroCard>`** — verdict + key numbers + status badge.
- **`<DrilldownSection title="..." defaultOpen={false}>`** — wraps the bottom collapsibles. Consistent chevron + animation.
- **`<ValueWithTooltip rationale={...} sourceId={...}>{value}</ValueWithTooltip>`** — hover-to-explain on every number. Shows: rationale (1-3 sentences) + "Source: [#N]" link that scrolls to the Sources panel.

### §0.2 Citation metadata schema

Every value or assumption that appears on a retirement page MUST flow through this shape from backend to UI:

```python
# argosy/services/retirement/citations.py
@dataclass(frozen=True)
class ValueWithRationale:
    value: float | int | str | None
    unit: str  # "NIS/mo", "USD", "%", "years", "shares", "boolean"
    source_id: str | None  # FK into sources registry; None = derived/computed
    rationale: str  # 1-3 sentences explaining why this value
    alternatives_considered: list[str] = field(default_factory=list)
    as_of_date: str | None = None  # YYYY-MM
    freshness_warning: str | None = None  # nudge user to verify
    confidence: Literal["high", "medium", "low"] = "medium"
```

```typescript
// ui/src/lib/retirement-types.ts
export interface ValueWithRationale {
  value: number | string | null;
  unit: string;
  source_id: string | null;
  rationale: string;
  alternatives_considered: string[];
  as_of_date: string | null;
  freshness_warning: string | null;
  confidence: "high" | "medium" | "low";
}
```

**Rule:** any retirement-related route returning a number that affects a decision MUST wrap that number in `ValueWithRationale` (or its equivalent dict in a response payload). The chart components extract `.value` for plotting and `.rationale` / `.source_id` for tooltips.

Convention: when a value is derived from other values, set `source_id=None` and put the derivation in `rationale` ("computed as `portfolio_value * 0.04 / 12`; see Bengen 1994 [src:bengen_1994]").

### §0.3 Sources registry

Centralized JSON-backed registry:

```
argosy/data/sources.yaml      ← canonical seed (committed)
argosy/data/sources_user.yaml ← per-user additions/overrides (gitignored, optional)
```

Schema:

```yaml
sources:
  bituach_leumi_old_age_2026:
    title: "Bituach Leumi — Old-age pension rates"
    url: "https://www.btl.gov.il/benefits/Old_age/Pages/MisparMeguarot.aspx"
    as_of: "2026-05"
    kind: "official"  # official | research | derived | best_effort
    notes: "Single-person base rate; spouse adds ~50%"
  bengen_1994:
    title: "Bengen — Determining Withdrawal Rates Using Historical Data"
    url: "https://www.retailinvestor.org/pdf/Bengen1.pdf"
    as_of: "1994"
    kind: "research"
  guyton_klinger_2006:
    title: "Guyton-Klinger decision rules withdrawal policy"
    url: "https://www.fpanet.org/journal/articles/2006_Issues/jfp0306-art6.cfm"
    as_of: "2006"
    kind: "research"
  damodaran_implied_erp_2026:
    title: "Damodaran — Implied ERP / US market mu estimate"
    url: "https://pages.stern.nyu.edu/~adamodar/"
    as_of: "2026-01"
    kind: "research"
  argosy_derived:
    title: "Argosy derived computation"
    url: ""
    as_of: ""
    kind: "derived"
```

Loader: `argosy/services/retirement/sources.py::load_sources()` returns `dict[str, Source]`. Cached per-process.

### §0.4 Hybrid defaults architecture

For every Israeli-specific value that has both a "shipped default" and a "per-user override":

1. **Shipped default** in `argosy/data/israel_retirement_reference.yaml`. Each value has its own `ValueWithRationale` block (`value` + `source_id` + `as_of_date` + `rationale` + `confidence`).
2. **Per-user override** in `identity_yaml` (existing intake field). When present, overrides the shipped default; UI shows "From your intake" provenance.
3. **Freshness warning** generated when:
   - Shipped-default `as_of_date` is > 12 months old
   - Per-user override `as_of_date` is > 18 months old
   - User has explicitly flagged the value as "stale" in intake

```yaml
# argosy/data/israel_retirement_reference.yaml
mekadem_by_fund:
  clal_pensia:
    value: 200
    unit: "ratio"
    source_id: "clal_published_table_2026"
    as_of_date: "2026-03"
    rationale: "Clal published mekadem for kupat_pensia at standard retirement age 67"
    confidence: "high"
  migdal_pensia:
    value: 198
    ...
bituach_leumi_old_age_single_2026:
  value: 2100
  unit: "NIS/mo"
  source_id: "bituach_leumi_old_age_2026"
  as_of_date: "2026-05"
  rationale: "Single-person base rate at age 67 with full contribution history"
  confidence: "medium"
  freshness_warning: "Indexed annually each January; verify if reading after Jan 1"
...
```

Resolver function: `argosy/services/retirement/reference.py::resolve(key, user_id, session) -> ValueWithRationale` checks user override first, then shipped default. Stamps `freshness_warning` if applicable.

### §0.5 Codex review checkpoint pattern (every diff)

Per the user's "codex tandem reviewer on every diff" decision, every committed diff in this plan passes through codex review BEFORE commit. The pattern:

```python
# After writing a diff but before committing:
import sys; sys.path.insert(0, "D:/Projects/financial-advisor/tools/codex-tandem/scripts")
from engine_codex import run_codex
from pathlib import Path

REVIEW_PROMPT = f"""\
Review the unstaged diff in this repo. Working dir is the project root.

Run `git diff` to see what's about to be committed. Then assess:
1. Correctness — does the code do what the commit message says?
2. Type/contract integrity — pydantic schemas, TS interfaces, function signatures
3. Test coverage — are the new tests actually testing the new behavior?
4. Israeli pension / tax / FX correctness if applicable
5. Visualization — does the new UI honor the §0.1 hero+drill-down standard?
6. Citations — does every new value flow through ValueWithRationale?

Wave context: <FILL IN per task>
Task name: <FILL IN per task>

Output one of:
  COMMIT AS-IS — diff is good, ship it
  BLOCKERS:
    - <issue 1>
    - <issue 2>
    ...
  NITS (non-blocking):
    - <nit 1>

Be specific. Cite file:line. Don't approve out of politeness.
"""

r = run_codex(
    node_dir=Path("D:/Projects/financial-advisor"),
    prompt=REVIEW_PROMPT,
    agent_name=f"wave{N}_task{M}_review",
    sandbox="read-only",
    timeout_s=600,
    role="reviewer",
)

# If COMMIT AS-IS → commit and move on
# If BLOCKERS → fix them and re-review (up to 3 iterations); if still failing, surface to user
# If only NITS → commit; address nits at end-of-wave if time permits
```

Convention: write the codex prompt with wave + task identifiers so the cost-tracking jsonl in `argosy/.stats/calls.jsonl` carries useful metadata.

### §0.6 Wave checkpoint cadence

After EACH wave:
1. Run full test suite for affected files (per CLAUDE.md mapping)
2. Run `cd ui && npm run typecheck && npm run lint`
3. Update SDD handover note with what shipped + open items
4. Print summary to user: "Wave N complete — X commits, Y tests passing, Z visualizations added. Proceed to Wave N+1?"
5. Wait for explicit "continue" or "fix X first" from user.

---

## §1. Filesystem layout (master)

### New files (created in this plan)

**Backend — retirement engine:**
- `argosy/services/retirement/__init__.py` — package marker + re-exports
- `argosy/services/retirement/citations.py` — `ValueWithRationale` dataclass + helpers
- `argosy/services/retirement/sources.py` — sources registry loader
- `argosy/services/retirement/reference.py` — hybrid-defaults resolver
- `argosy/services/retirement/mekadem.py` — mekadem variance by fund/age (Wave 1)
- `argosy/services/retirement/bituach_leumi.py` — BL old-age stipend module (Wave 1)
- `argosy/services/retirement/safety_gates.py` — NRA estate + liquidity + conflict (Wave 2)
- `argosy/services/retirement/ruin_probability.py` — P(ruin) gate + verdict computation (Wave 3)
- `argosy/services/retirement/sigma_calibration.py` — auto-calibrate sigma from holdings (Wave 3)
- `argosy/services/retirement/regime_switch_mc.py` — regime-switch / fat-tail Monte Carlo (Wave 3)
- `argosy/services/retirement/stochastic_fx.py` — joint USD/NIS process (Wave 3)
- `argosy/services/retirement/withdrawal_policy.py` — 4%/VPW/Guyton-Klinger policies (Wave 3)
- `argosy/services/retirement/glide_path.py` — equity allocation by age (Wave 4)
- `argosy/services/retirement/rebalancing.py` — 5/25 + quarterly rules (Wave 4)
- `argosy/services/retirement/lifecycle_income.py` — RSU/partner/side-income timeline (Wave 4)
- `argosy/services/retirement/phase_expenses.py` — kids+empty-nest+healthcare ramp (Wave 4)
- `argosy/services/retirement/idf_service.py` — kids' IDF budget phase (Wave 4)
- `argosy/services/retirement/healthcare.py` — Israeli health basket + Mashlim + private (Wave 4)
- `argosy/services/retirement/tax_engine.py` — account-aware tax engine (Wave 5)
- `argosy/services/retirement/hishtalmut.py` — 6-year tax-free + lump-vs-continue (Wave 5)
- `argosy/services/retirement/decumulation.py` — withdrawal order optimizer (Wave 5)
- `argosy/services/retirement/lump_vs_annuity.py` — age-60 lump vs age-67 annuity tool (Wave 5)
- `argosy/services/retirement/real_estate.py` — primary-residence equity + appreciation (Wave 6)
- `argosy/services/retirement/mortgage.py` — amortization schedule (Wave 6)
- `argosy/services/retirement/partner_state.py` — spouse pension + income merge (Wave 6)
- `argosy/services/retirement/severance.py` — pizurim split from kupat_pensia (Wave 6)
- `argosy/services/retirement/insurance_gaps.py` — life + disability + LTC coverage gap (Wave 7)
- `argosy/services/retirement/action_engine.py` — risk-prioritized action items (Wave 7)
- `argosy/services/retirement/replan_triggers.py` — registered triggers for recompute (Wave 7)
- `argosy/services/retirement/multi_goal.py` — retirement+education+house balancer (Wave 7)
- `argosy/services/retirement/behavioral.py` — panic-sell cooldown + recency check (Wave 7)

**Backend — data / config:**
- `argosy/data/sources.yaml` — canonical sources registry (Wave 0)
- `argosy/data/israel_retirement_reference.yaml` — shipped defaults (Wave 0; populated in Waves 1+)

**Backend — routes:**
- `argosy/api/routes/retirement.py` — new umbrella route file for retirement-engine endpoints (deepens existing `plan.py` rather than replacing). All new endpoints registered under `/api/retirement/*`.

**Backend — tests:**
- `tests/test_retirement_citations.py`
- `tests/test_retirement_sources.py`
- `tests/test_retirement_reference.py`
- `tests/test_retirement_mekadem.py`
- `tests/test_retirement_bituach_leumi.py`
- `tests/test_retirement_safety_gates.py`
- `tests/test_retirement_ruin_probability.py`
- `tests/test_retirement_sigma_calibration.py`
- `tests/test_retirement_regime_switch_mc.py`
- `tests/test_retirement_stochastic_fx.py`
- `tests/test_retirement_withdrawal_policy.py`
- `tests/test_retirement_glide_path.py`
- `tests/test_retirement_rebalancing.py`
- `tests/test_retirement_lifecycle_income.py`
- `tests/test_retirement_phase_expenses.py`
- `tests/test_retirement_idf_service.py`
- `tests/test_retirement_healthcare.py`
- `tests/test_retirement_tax_engine.py`
- `tests/test_retirement_hishtalmut.py`
- `tests/test_retirement_decumulation.py`
- `tests/test_retirement_lump_vs_annuity.py`
- `tests/test_retirement_real_estate.py`
- `tests/test_retirement_mortgage.py`
- `tests/test_retirement_partner_state.py`
- `tests/test_retirement_severance.py`
- `tests/test_retirement_insurance_gaps.py`
- `tests/test_retirement_action_engine.py`
- `tests/test_retirement_replan_triggers.py`
- `tests/test_retirement_multi_goal.py`
- `tests/test_retirement_behavioral.py`
- `tests/test_retirement_route.py` (integration tests for the umbrella route)

**Frontend — UI primitives:**
- `ui/src/components/retirement/HeroCard.tsx` — verdict card primitive (Wave 0)
- `ui/src/components/retirement/ValueWithTooltip.tsx` — hover-explain primitive (Wave 0)
- `ui/src/components/retirement/DrilldownSection.tsx` — collapsible primitive (Wave 0)
- `ui/src/components/retirement/SourcesPanel.tsx` — sources registry display (Wave 0)
- `ui/src/components/retirement/MethodologyPanel.tsx` — how-we-compute panel (Wave 0)
- `ui/src/components/retirement/SensitivityPanel.tsx` — top-3-levers auto-generated (Wave 0)
- `ui/src/components/retirement/AssumptionsStrip.tsx` — slider strip primitive (Wave 0)

**Frontend — per-feature components (Waves 1-7):**
- `ui/src/components/retirement/MekademBand.tsx` (Wave 1)
- `ui/src/components/retirement/BituachLeumiCard.tsx` (Wave 1)
- `ui/src/components/retirement/SafetyGatesPanel.tsx` (Wave 2)
- `ui/src/components/retirement/NraEstateAlert.tsx` (Wave 2)
- `ui/src/components/retirement/EmergencyLiquidityCard.tsx` (Wave 2)
- `ui/src/components/retirement/ConflictScenarioToggle.tsx` (Wave 2)
- `ui/src/components/retirement/RuinProbabilityHero.tsx` (Wave 3 — replaces the existing single-month crossing display)
- `ui/src/components/retirement/SigmaCalibrationNote.tsx` (Wave 3)
- `ui/src/components/retirement/RegimeSwitchSelector.tsx` (Wave 3)
- `ui/src/components/retirement/StochasticFxToggle.tsx` (Wave 3)
- `ui/src/components/retirement/WithdrawalPolicySelector.tsx` (Wave 3)
- `ui/src/components/retirement/GlidePathChart.tsx` (Wave 4)
- `ui/src/components/retirement/RebalancingAlerts.tsx` (Wave 4)
- `ui/src/components/retirement/LifecycleIncomeTimeline.tsx` (Wave 4)
- `ui/src/components/retirement/PhaseExpenseChart.tsx` (Wave 4)
- `ui/src/components/retirement/IdfServicePhase.tsx` (Wave 4)
- `ui/src/components/retirement/HealthcareCurve.tsx` (Wave 4)
- `ui/src/components/retirement/TaxBreakdownTable.tsx` (Wave 5)
- `ui/src/components/retirement/HishtalmutTimer.tsx` (Wave 5)
- `ui/src/components/retirement/DecumulationOrderCard.tsx` (Wave 5)
- `ui/src/components/retirement/LumpVsAnnuityWizard.tsx` (Wave 5)
- `ui/src/components/retirement/RealEstateCard.tsx` (Wave 6)
- `ui/src/components/retirement/MortgageSchedule.tsx` (Wave 6)
- `ui/src/components/retirement/PartnerStatePanel.tsx` (Wave 6)
- `ui/src/components/retirement/SeveranceSplitCard.tsx` (Wave 6)
- `ui/src/components/retirement/InsuranceGapsCard.tsx` (Wave 7)
- `ui/src/components/retirement/ActionEngineList.tsx` (Wave 7 — replaces existing action-items widget)
- `ui/src/components/retirement/ReplanTriggerLog.tsx` (Wave 7)
- `ui/src/components/retirement/MultiGoalBalancer.tsx` (Wave 7)
- `ui/src/components/retirement/BehavioralCheckpoint.tsx` (Wave 7)

**Frontend — pages:**
- `ui/src/app/retirement/page.tsx` — new dedicated retirement-companion page (Wave 0 scaffolds; later waves fill in)
- `ui/src/app/plan/page.tsx` — modified to embed the hero card + safety gates panel (Wave 2 onwards)

**Frontend — lib:**
- `ui/src/lib/retirement-types.ts` — TS counterparts to backend dataclasses
- `ui/src/lib/api.ts` — extended with `api.retirement.*` namespace

### Modified files

- `argosy/services/cashflow_projection.py` — major edits in Waves 1, 3, 5 to consume the new engine modules
- `argosy/api/routes/plan.py` — extended (or split) to include new retirement endpoints (Wave 0 scaffolds; later waves fill in). Wave 7 dedup'd `/action-items` route.
- `ui/src/components/plan/cashflow-projection-chart.tsx` — heavy refactor in Wave 3 to consume the new ruin-probability output + display the hero card
- `docs/design/SDD.md` — refreshed after each wave with "what landed" subsection

### Deleted files

- None initially. Wave 7 may delete the legacy action-items collector after the new engine ships.

---

## §2. Test discipline + commit cadence

Per CLAUDE.md mapping:

- Per task: run ONLY affected files. `python -m pytest tests/test_retirement_<module>.py -xvs`
- Per wave: full retirement test suite + integration. `python -m pytest tests/test_retirement_*.py -q`
- Per wave: UI typecheck. `cd ui && npm run typecheck && npm run lint`
- Per wave: integration smoke via running backend + checking `/api/retirement/<endpoint>` returns valid JSON

Commit cadence: one commit per `step`-level task; rebase / squash NOT used. Per CLAUDE.md "Prefer to create a new commit rather than amending."

Commit message style: `feat(retirement): <action>` / `fix(retirement): <action>` / `test(retirement): <action>`. Wave + gap number in body: `Wave 1 · Gap #3 (mekadem variance) · ...`.

---

## Wave 0: Foundation infrastructure

**Goal:** Ship the cross-cutting primitives — citations metadata, sources registry, hybrid-defaults loader, three UI primitives, the retirement page scaffold, and the `api.retirement` TS client. After Wave 0, every subsequent wave just plugs into this scaffolding.

**Files (created in this wave):**
- `argosy/services/retirement/__init__.py`
- `argosy/services/retirement/citations.py`
- `argosy/services/retirement/sources.py`
- `argosy/services/retirement/reference.py`
- `argosy/data/sources.yaml`
- `argosy/data/israel_retirement_reference.yaml` (seeded, but values are best-effort placeholders refined in later waves)
- `argosy/api/routes/retirement.py`
- `tests/test_retirement_citations.py`
- `tests/test_retirement_sources.py`
- `tests/test_retirement_reference.py`
- `tests/test_retirement_route.py`
- `ui/src/lib/retirement-types.ts`
- `ui/src/components/retirement/HeroCard.tsx`
- `ui/src/components/retirement/ValueWithTooltip.tsx`
- `ui/src/components/retirement/DrilldownSection.tsx`
- `ui/src/components/retirement/SourcesPanel.tsx`
- `ui/src/components/retirement/MethodologyPanel.tsx`
- `ui/src/components/retirement/SensitivityPanel.tsx`
- `ui/src/components/retirement/AssumptionsStrip.tsx`
- `ui/src/app/retirement/page.tsx`

**Modified:**
- `argosy/api/main.py` — register new `retirement` router
- `ui/src/lib/api.ts` — add `api.retirement` namespace
- `docs/design/SDD.md` — Wave 0 subsection in handover

### Task 0.1: `ValueWithRationale` dataclass + serializer

**Files:**
- Create: `argosy/services/retirement/citations.py`
- Create: `tests/test_retirement_citations.py`

- [ ] **Step 0.1.1: Write the failing tests**

```python
# tests/test_retirement_citations.py
"""Tests for the ValueWithRationale citation primitive."""
import pytest
from argosy.services.retirement.citations import (
    ValueWithRationale,
    as_dict,
    DERIVED,
)


class TestValueWithRationale:
    def test_minimal_construction(self):
        v = ValueWithRationale(
            value=42.0,
            unit="NIS/mo",
            source_id="bituach_leumi_old_age_2026",
            rationale="Single-person base rate at age 67.",
        )
        assert v.value == 42.0
        assert v.unit == "NIS/mo"
        assert v.source_id == "bituach_leumi_old_age_2026"
        assert v.alternatives_considered == []
        assert v.confidence == "medium"

    def test_derived_marker(self):
        v = ValueWithRationale(
            value=0.087,
            unit="probability",
            source_id=DERIVED,
            rationale="Computed from MC: 87 failed paths / 1000.",
        )
        assert v.source_id is None

    def test_as_dict_strips_none_freshness(self):
        v = ValueWithRationale(value=200, unit="ratio", source_id="x", rationale="y")
        d = as_dict(v)
        assert "freshness_warning" not in d  # None values stripped for compact JSON
        assert d["value"] == 200

    def test_as_dict_preserves_freshness_when_set(self):
        v = ValueWithRationale(
            value=200, unit="ratio", source_id="x", rationale="y",
            freshness_warning="Verify with your fund.",
        )
        d = as_dict(v)
        assert d["freshness_warning"] == "Verify with your fund."

    def test_confidence_must_be_one_of_three(self):
        with pytest.raises(ValueError):
            ValueWithRationale(
                value=1, unit="x", source_id=None, rationale="y",
                confidence="invalid",  # type: ignore
            )

    def test_alternatives_considered_default_empty_list(self):
        v = ValueWithRationale(value=1, unit="x", source_id=None, rationale="y")
        v2 = ValueWithRationale(value=2, unit="x", source_id=None, rationale="y")
        v.alternatives_considered.append("foo")  # would mutate shared default if buggy
        assert v2.alternatives_considered == []  # confirm not shared
```

- [ ] **Step 0.1.2: Run test to verify it fails (red)**

Run: `.venv/Scripts/python.exe -m pytest tests/test_retirement_citations.py -xvs`
Expected: FAIL with `ModuleNotFoundError: No module named 'argosy.services.retirement.citations'`

- [ ] **Step 0.1.3: Create the package marker**

```python
# argosy/services/retirement/__init__.py
"""Argosy retirement-companion engine.

Modular: each gap from the 2026-05-28 SDD review lives in its own submodule
under this package. The umbrella ``argosy/api/routes/retirement.py`` exposes
HTTP endpoints; per-feature UI lives under ``ui/src/components/retirement/``.

Cross-cutting primitives — citations / sources / reference — live at the
package root and are imported by all feature modules.
"""

from argosy.services.retirement.citations import (
    ValueWithRationale,
    as_dict,
    DERIVED,
)
from argosy.services.retirement.sources import Source, load_sources
from argosy.services.retirement.reference import resolve

__all__ = [
    "ValueWithRationale",
    "as_dict",
    "DERIVED",
    "Source",
    "load_sources",
    "resolve",
]
```

- [ ] **Step 0.1.4: Implement `citations.py`**

```python
# argosy/services/retirement/citations.py
"""ValueWithRationale — the single shape every retirement-related value passes
through on its way from the backend to the UI.

Why a wrapper instead of a bare number: every chart, table, and tooltip in
the retirement companion needs to surface (a) the value itself, (b) WHY this
value, (c) the source. Without a uniform wrapper, half the UI ends up with
hover tooltips and half doesn't.
"""
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

DERIVED = None  # explicit marker for "this value is computed, not sourced"


@dataclass
class ValueWithRationale:
    """Wraps one user-facing value with provenance + rationale.

    Always serialize via ``as_dict()`` so None-valued ``freshness_warning``
    and empty ``alternatives_considered`` are stripped — JSON payloads stay
    compact and the UI's "show warning if present" logic is uniform.
    """
    value: float | int | str | None
    unit: str
    source_id: str | None
    rationale: str
    alternatives_considered: list[str] = field(default_factory=list)
    as_of_date: str | None = None
    freshness_warning: str | None = None
    confidence: Literal["high", "medium", "low"] = "medium"

    def __post_init__(self) -> None:
        if self.confidence not in ("high", "medium", "low"):
            raise ValueError(
                f"confidence must be one of high/medium/low; got {self.confidence!r}"
            )


def as_dict(v: ValueWithRationale) -> dict[str, Any]:
    """Compact JSON-friendly serialization.

    Drops keys whose values are ``None`` or empty list, except ``value`` and
    ``source_id`` which are kept even when None (those Nones are semantic:
    ``value=None`` means "not enough data"; ``source_id=None`` means derived).
    """
    d = asdict(v)
    out: dict[str, Any] = {}
    for k, val in d.items():
        if k in ("value", "source_id"):
            out[k] = val
            continue
        if val is None or val == []:
            continue
        out[k] = val
    return out
```

- [ ] **Step 0.1.5: Run tests to verify green**

Run: `.venv/Scripts/python.exe -m pytest tests/test_retirement_citations.py -xvs`
Expected: 6/6 PASS.

- [ ] **Step 0.1.6: Codex review checkpoint**

Dispatch codex with the §0.5 prompt template, wave=0, task=0.1. Wait for COMMIT-AS-IS / BLOCKERS. Fix blockers (max 3 iterations) before commit.

- [ ] **Step 0.1.7: Commit**

```bash
git add argosy/services/retirement/__init__.py argosy/services/retirement/citations.py tests/test_retirement_citations.py
git commit -m "feat(retirement): ValueWithRationale citation primitive (Wave 0 · gap-foundation)"
```

### Task 0.2: Sources registry — YAML + loader

**Files:**
- Create: `argosy/data/sources.yaml` (initial seed: ~10 sources)
- Create: `argosy/services/retirement/sources.py`
- Create: `tests/test_retirement_sources.py`

- [ ] **Step 0.2.1: Write the failing tests**

```python
# tests/test_retirement_sources.py
"""Tests for the sources registry loader."""
import pytest
from pathlib import Path
from argosy.services.retirement.sources import (
    Source,
    load_sources,
    SourcesRegistry,
)


class TestLoadSources:
    def test_loads_canonical_yaml(self):
        reg = load_sources()
        assert isinstance(reg, SourcesRegistry)
        # The canonical YAML is hand-seeded with at least these:
        assert "bituach_leumi_old_age_2026" in reg.sources
        assert "bengen_1994" in reg.sources

    def test_source_shape(self):
        reg = load_sources()
        bl = reg.sources["bituach_leumi_old_age_2026"]
        assert isinstance(bl, Source)
        assert bl.title.startswith("Bituach Leumi")
        assert bl.kind in ("official", "research", "derived", "best_effort")
        assert bl.as_of  # non-empty

    def test_get_returns_source(self):
        reg = load_sources()
        s = reg.get("bengen_1994")
        assert s is not None
        assert s.kind == "research"

    def test_get_returns_none_for_missing(self):
        reg = load_sources()
        assert reg.get("nonexistent_source_xyz") is None

    def test_load_sources_is_cached(self):
        reg1 = load_sources()
        reg2 = load_sources()
        # Same object identity → cached
        assert reg1 is reg2

    def test_user_override_yaml_merged_when_present(self, tmp_path, monkeypatch):
        # Drop a user-override YAML next to the canonical and confirm
        # load_sources(canonical_path=..., user_path=...) merges them.
        canonical = tmp_path / "sources.yaml"
        canonical.write_text("""
sources:
  test_canonical:
    title: "Test canonical source"
    url: "https://example.com/canonical"
    as_of: "2026-01"
    kind: "research"
""", encoding="utf-8")
        user = tmp_path / "sources_user.yaml"
        user.write_text("""
sources:
  test_user_only:
    title: "User-supplied source"
    url: "https://example.com/user"
    as_of: "2026-05"
    kind: "official"
  test_canonical:
    title: "User OVERRIDES canonical title"
    url: "https://example.com/canonical"
    as_of: "2026-05"
    kind: "research"
""", encoding="utf-8")
        reg = load_sources(canonical_path=canonical, user_path=user, _bypass_cache=True)
        assert "test_canonical" in reg.sources
        assert reg.sources["test_canonical"].title.startswith("User OVERRIDES")
        assert "test_user_only" in reg.sources
```

- [ ] **Step 0.2.2: Run test to verify it fails (red)**

Run: `.venv/Scripts/python.exe -m pytest tests/test_retirement_sources.py -xvs`
Expected: FAIL with import error.

- [ ] **Step 0.2.3: Seed `argosy/data/sources.yaml`**

```yaml
# argosy/data/sources.yaml
# Canonical sources registry for the retirement companion.
# Each entry is a citable source: official tables, research papers, or
# explicit "derived" markers.
#
# To add a per-user source, drop a sibling file ``sources_user.yaml`` at the
# same path; entries there override / extend this file (see
# ``argosy/services/retirement/sources.py::load_sources``).

sources:
  # ─── Israeli statutory + official ─────────────────────────────────────
  bituach_leumi_old_age_2026:
    title: "Bituach Leumi — Old-age pension rates (קצבת זקנה)"
    url: "https://www.btl.gov.il/benefits/Old_age/Pages/MisparMeguarot.aspx"
    as_of: "2026-05"
    kind: "official"
    notes: "Single-person base rate; spouse adds ~50%; indexed annually each January"

  israeli_tax_authority_cgt_2026:
    title: "Israel Tax Authority — Capital Gains 25% rate on equity"
    url: "https://www.gov.il/he/departments/topics/income-tax"
    as_of: "2026-01"
    kind: "official"

  hishtalmut_6yr_rule:
    title: "Israeli Income Tax Ordinance §3(e) — Hishtalmut tax-free window"
    url: "https://www.nevo.co.il/law_html/law01/p182_001.htm"
    as_of: "2024-01"
    kind: "official"
    notes: "Withdrawal tax-free after 6 years from first deposit (or age 67 lump)"

  # ─── Pension fund tables (best-effort; verify per fund + per user) ────
  clal_published_table_2026:
    title: "Clal Pension — Published mekadem table"
    url: "https://www.clal.co.il/pension"
    as_of: "2026-03"
    kind: "best_effort"
    notes: "Verify with fund directly; mekadem varies by age + plan + spouse benefit"

  migdal_published_table_2026:
    title: "Migdal Pension — Published mekadem table"
    url: "https://www.migdal.co.il/pension"
    as_of: "2026-03"
    kind: "best_effort"

  menorah_published_table_2026:
    title: "Menorah Pension — Published mekadem table"
    url: "https://www.menoramivt.co.il/pension"
    as_of: "2026-03"
    kind: "best_effort"

  # ─── Research / academia ──────────────────────────────────────────────
  bengen_1994:
    title: "Bengen — Determining Withdrawal Rates Using Historical Data (1994)"
    url: "https://www.retailinvestor.org/pdf/Bengen1.pdf"
    as_of: "1994"
    kind: "research"
    notes: "Original '4% rule'. Updated in Bengen 2020 to ~4.7% with US data through 2019."

  bengen_2020:
    title: "Bengen 2020 — Updating the 4% rule"
    url: "https://www.financialplanningassociation.org/article/journal/AUG20-choose-your-floor"
    as_of: "2020"
    kind: "research"

  guyton_klinger_2006:
    title: "Guyton-Klinger decision rules withdrawal policy (2006)"
    url: "https://www.fpanet.org/journal/articles/2006_Issues/jfp0306-art6.cfm"
    as_of: "2006"
    kind: "research"
    notes: "Guardrails: ratchet up 10% in good years; cut 10% when WR > 120% of initial"

  trinity_study_1998:
    title: "Cooley/Hubbard/Walz — Retirement Spending: Choosing a Sustainable Withdrawal Rate"
    url: "https://www.aaii.com/journal/article/retirement-savings-choosing-a-withdrawal-rate-that-is-sustainable.touch"
    as_of: "1998"
    kind: "research"

  damodaran_implied_erp_2026:
    title: "Damodaran — Implied Equity Risk Premium (current)"
    url: "https://pages.stern.nyu.edu/~adamodar/"
    as_of: "2026-01"
    kind: "research"
    notes: "Damodaran's monthly-updated ERP page; the mu_nominal default of 8% trails Damodaran's implied ERP + 10y treasury"

  vanguard_capital_markets_model:
    title: "Vanguard Capital Markets Model — 10y nominal equity return forecast"
    url: "https://corporate.vanguard.com/content/corporatesite/us/en/corp/articles/capital-markets-model.html"
    as_of: "2026-01"
    kind: "research"

  bogleheads_three_fund:
    title: "Bogleheads — Three-fund portfolio + lazy-portfolio glide paths"
    url: "https://www.bogleheads.org/wiki/Three-fund_portfolio"
    as_of: "2025"
    kind: "research"

  # ─── US tax / estate ──────────────────────────────────────────────────
  us_nra_estate_tax:
    title: "IRS — Non-Resident-Alien estate tax on US-situs assets"
    url: "https://www.irs.gov/individuals/international-taxpayers/some-nonresidents-with-us-assets-must-file-estate-tax-returns"
    as_of: "2024"
    kind: "official"
    notes: "$60K exemption for non-US-persons. Above that, 18-40% federal estate tax on US-situs assets including US-domiciled stocks/ETFs."

  # ─── Derived marker (computed values) ─────────────────────────────────
  argosy_derived:
    title: "Argosy derived computation"
    url: ""
    as_of: ""
    kind: "derived"
    notes: "Used when source_id=None marker is rendered explicitly in UI; usually citations use source_id=None directly"
```

- [ ] **Step 0.2.4: Implement `sources.py`**

```python
# argosy/services/retirement/sources.py
"""Loader for the canonical sources registry.

Cached per-process via ``functools.lru_cache``; pass ``_bypass_cache=True``
in tests to force re-read. A per-user override YAML (``sources_user.yaml``,
sibling to the canonical) is merged if present; user-side keys take
precedence on collision.
"""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


_DEFAULT_CANONICAL = (
    Path(__file__).resolve().parents[2] / "data" / "sources.yaml"
)
_DEFAULT_USER = (
    Path(__file__).resolve().parents[2] / "data" / "sources_user.yaml"
)


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    url: str
    as_of: str
    kind: str  # "official" | "research" | "derived" | "best_effort"
    notes: str = ""


@dataclass(frozen=True)
class SourcesRegistry:
    sources: dict[str, Source]

    def get(self, source_id: str) -> Source | None:
        return self.sources.get(source_id)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@lru_cache(maxsize=1)
def _cached_load(canonical_path: str, user_path: str) -> SourcesRegistry:
    canonical = _load_yaml(Path(canonical_path))
    user = _load_yaml(Path(user_path))
    canonical_sources = canonical.get("sources", {})
    user_sources = user.get("sources", {})
    merged: dict[str, Any] = {**canonical_sources, **user_sources}
    return SourcesRegistry(
        sources={
            sid: Source(
                id=sid,
                title=entry.get("title", ""),
                url=entry.get("url", ""),
                as_of=entry.get("as_of", ""),
                kind=entry.get("kind", "research"),
                notes=entry.get("notes", ""),
            )
            for sid, entry in merged.items()
        }
    )


def load_sources(
    *,
    canonical_path: Path | None = None,
    user_path: Path | None = None,
    _bypass_cache: bool = False,
) -> SourcesRegistry:
    cp = canonical_path or _DEFAULT_CANONICAL
    up = user_path or _DEFAULT_USER
    if _bypass_cache:
        _cached_load.cache_clear()
    return _cached_load(str(cp), str(up))
```

- [ ] **Step 0.2.5: Run tests to verify green**

Run: `.venv/Scripts/python.exe -m pytest tests/test_retirement_sources.py -xvs`
Expected: 6/6 PASS.

- [ ] **Step 0.2.6: Codex review checkpoint** (per §0.5 pattern)

- [ ] **Step 0.2.7: Commit**

```bash
git add argosy/data/sources.yaml argosy/services/retirement/sources.py tests/test_retirement_sources.py
git commit -m "feat(retirement): sources registry — canonical YAML + cached loader (Wave 0 · gap-foundation)"
```

### Task 0.3: Hybrid-defaults resolver

**Files:**
- Create: `argosy/data/israel_retirement_reference.yaml` (seeded with placeholders; values fleshed out in Waves 1+)
- Create: `argosy/services/retirement/reference.py`
- Create: `tests/test_retirement_reference.py`

- [ ] **Step 0.3.1: Seed `israel_retirement_reference.yaml`**

Seed with one example entry per category — full population happens in Waves 1+:

```yaml
# argosy/data/israel_retirement_reference.yaml
# Shipped defaults for Israel-specific retirement values.
# Per-user overrides live in ``identity_yaml`` and take precedence.
#
# Each entry follows the ValueWithRationale shape (argosy/services/retirement/
# citations.py). Resolver: argosy/services/retirement/reference.py::resolve.
#
# Freshness policy: shipped defaults > 12 months old auto-stamp a
# ``freshness_warning``. Override the as_of_date when refreshing.

values:
  # ─── Mekadem (annuity coefficient) ─────────────────────────────────────
  mekadem.clal_pensia:
    value: 200
    unit: "ratio"
    source_id: "clal_published_table_2026"
    as_of_date: "2026-03"
    rationale: "Clal kupat_pensia published mekadem for standard retirement age 67."
    confidence: "high"
    alternatives_considered:
      - "Migdal: 198 (slightly lower; different mortality table)"
      - "Menorah: 202 (slightly higher; different actuarial assumptions)"

  # ─── Bituach Leumi (old-age stipend) ──────────────────────────────────
  bituach_leumi.single_age_67_base_2026:
    value: 2100
    unit: "NIS/mo"
    source_id: "bituach_leumi_old_age_2026"
    as_of_date: "2026-05"
    rationale: |
      Single-person base rate at age 67 with full contribution history (35+
      years of insured periods). Spouse stipend adds ~50% if eligible.
    confidence: "medium"
    freshness_warning: "Indexed annually each January; verify before relying on this in Q1."

  # ─── Tax brackets / rates ─────────────────────────────────────────────
  tax.israeli_cgt_equity:
    value: 0.25
    unit: "fraction"
    source_id: "israeli_tax_authority_cgt_2026"
    as_of_date: "2026-01"
    rationale: "Israeli flat capital-gains rate on equity sales for resident individuals."
    confidence: "high"

  # ─── US NRA estate tax ────────────────────────────────────────────────
  us_estate.nra_exemption_usd:
    value: 60000
    unit: "USD"
    source_id: "us_nra_estate_tax"
    as_of_date: "2024-01"
    rationale: |
      $60K exemption for non-US-persons holding US-situs assets at death.
      Federal estate tax rate above the exemption is graduated 18-40%.
    confidence: "high"
```

- [ ] **Step 0.3.2: Write failing tests for the resolver**

```python
# tests/test_retirement_reference.py
"""Tests for the hybrid-defaults resolver (shipped + user override + freshness)."""
import pytest
from argosy.services.retirement.citations import ValueWithRationale
from argosy.services.retirement.reference import resolve, ResolveError


class TestResolveShippedDefault:
    def test_returns_shipped_value(self, client_with_db):
        # No user override; should return the shipped default.
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            v = resolve("mekadem.clal_pensia", user_id="ariel", session=s)
        assert isinstance(v, ValueWithRationale)
        assert v.value == 200
        assert v.unit == "ratio"
        assert v.source_id == "clal_published_table_2026"

    def test_returns_none_when_key_missing(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            with pytest.raises(ResolveError, match="unknown reference key"):
                resolve("nonexistent.key", user_id="ariel", session=s)


class TestResolveUserOverride:
    def test_user_override_takes_precedence(self, client_with_db, _seed_user, _seed_user_context):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s, identity_yaml_extra={
                "retirement_reference_overrides": {
                    "mekadem.clal_pensia": {
                        "value": 195,
                        "source": "user_intake",
                        "as_of_date": "2026-04",
                    },
                },
            })
            v = resolve("mekadem.clal_pensia", user_id="ariel", session=s)
        assert v.value == 195
        assert v.source_id == "user_intake"
        assert v.rationale.startswith("Provided by user via intake")


class TestFreshnessWarning:
    def test_stale_shipped_default_warns(self, client_with_db, monkeypatch):
        # Mock "today" to be > 12 months after the shipped as_of_date.
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            v = resolve(
                "bituach_leumi.single_age_67_base_2026",
                user_id="ariel",
                session=s,
                today="2027-08-01",  # >12 months after 2026-05
            )
        assert v.freshness_warning is not None
        assert "verify" in v.freshness_warning.lower() or "12 months" in v.freshness_warning.lower()

    def test_fresh_shipped_default_no_warning_unless_intrinsic(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            v = resolve(
                "mekadem.clal_pensia",
                user_id="ariel",
                session=s,
                today="2026-06-01",
            )
        # mekadem.clal_pensia is as_of 2026-03; fresh enough — but it has no
        # intrinsic warning in the YAML, so the resolver shouldn't add one
        assert v.freshness_warning is None


# Fixture seeds for the override test — wire into tests/conftest.py if not
# already there:
@pytest.fixture
def _seed_user():
    from tests.conftest import _seed_user as f
    return f


@pytest.fixture
def _seed_user_context():
    from tests.conftest import _seed_user_context as f
    return f
```

- [ ] **Step 0.3.3: Implement `reference.py`**

```python
# argosy/services/retirement/reference.py
"""Hybrid-defaults resolver — shipped YAML + per-user identity_yaml override.

Priority order:
  1. ``identity_yaml.retirement_reference_overrides.<key>``  (per-user, intake)
  2. Shipped default in ``argosy/data/israel_retirement_reference.yaml``
  3. ``ResolveError`` if neither.

Freshness:
  - If shipped default's ``as_of_date`` is > 12 months before today, stamp a
    generic "verify with your fund" warning on the returned object (unless
    the YAML already provides one; intrinsic warnings win).
  - User overrides get a similar check at 18 months.
"""
from dataclasses import replace
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from argosy.services.retirement.citations import ValueWithRationale


_REFERENCE_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "israel_retirement_reference.yaml"
)


class ResolveError(KeyError):
    """Raised when a reference key is not in shipped YAML or user override."""


@lru_cache(maxsize=1)
def _load_shipped() -> dict[str, Any]:
    raw = yaml.safe_load(_REFERENCE_PATH.read_text(encoding="utf-8")) or {}
    return raw.get("values", {})


def _build_value(key: str, entry: dict[str, Any]) -> ValueWithRationale:
    return ValueWithRationale(
        value=entry.get("value"),
        unit=entry.get("unit", ""),
        source_id=entry.get("source_id"),
        rationale=entry.get("rationale", ""),
        alternatives_considered=list(entry.get("alternatives_considered", [])),
        as_of_date=entry.get("as_of_date"),
        freshness_warning=entry.get("freshness_warning"),
        confidence=entry.get("confidence", "medium"),
    )


def _stamp_freshness(v: ValueWithRationale, today_iso: str, threshold_months: int) -> ValueWithRationale:
    if v.freshness_warning:
        return v  # intrinsic warning wins
    if not v.as_of_date:
        return v
    today_d = date.fromisoformat(today_iso)
    asof_d = date.fromisoformat(v.as_of_date + "-01" if len(v.as_of_date) == 7 else v.as_of_date)
    months = (today_d.year - asof_d.year) * 12 + (today_d.month - asof_d.month)
    if months > threshold_months:
        return replace(
            v,
            freshness_warning=(
                f"As-of date {v.as_of_date} is > {threshold_months} months old; "
                "verify with your fund / official source."
            ),
        )
    return v


def _load_user_override(session: Session, user_id: str, key: str) -> dict[str, Any] | None:
    """Pull the per-user override block from identity_yaml.

    Schema (in identity_yaml):
      retirement_reference_overrides:
        <key>:
          value: ...
          source: <free-form string for now>
          as_of_date: "YYYY-MM"
          rationale: optional
    """
    from argosy.services.wealth_dashboard import _load_user_context_yaml
    ctx = _load_user_context_yaml(session, user_id) or {}
    overrides = ctx.get("retirement_reference_overrides", {}) or {}
    return overrides.get(key)


def resolve(
    key: str,
    *,
    user_id: str,
    session: Session,
    today: str | None = None,
) -> ValueWithRationale:
    """Resolve a reference value with hybrid defaults.

    Returns a ValueWithRationale stamped with freshness warning if applicable.
    Raises ResolveError if the key is unknown.
    """
    today_iso = today or date.today().isoformat()

    user_override = _load_user_override(session, user_id, key)
    if user_override is not None:
        v = ValueWithRationale(
            value=user_override.get("value"),
            unit=user_override.get("unit", ""),
            source_id=user_override.get("source", "user_intake"),
            rationale=user_override.get(
                "rationale",
                "Provided by user via intake — overrides the shipped Argosy default.",
            ),
            alternatives_considered=[],
            as_of_date=user_override.get("as_of_date"),
            freshness_warning=user_override.get("freshness_warning"),
            confidence=user_override.get("confidence", "high"),
        )
        return _stamp_freshness(v, today_iso, threshold_months=18)

    shipped = _load_shipped()
    if key not in shipped:
        raise ResolveError(f"unknown reference key: {key!r}")
    v = _build_value(key, shipped[key])
    return _stamp_freshness(v, today_iso, threshold_months=12)
```

- [ ] **Step 0.3.4: Run tests to verify green**

Run: `.venv/Scripts/python.exe -m pytest tests/test_retirement_reference.py -xvs`
Expected: 4/4 PASS (or 5/5 depending on parametrization).

- [ ] **Step 0.3.5: Codex review checkpoint**

- [ ] **Step 0.3.6: Commit**

```bash
git add argosy/data/israel_retirement_reference.yaml argosy/services/retirement/reference.py tests/test_retirement_reference.py
git commit -m "feat(retirement): hybrid-defaults resolver — shipped YAML + user override + freshness (Wave 0)"
```

### Task 0.4: Umbrella route + integration test

**Files:**
- Create: `argosy/api/routes/retirement.py`
- Create: `tests/test_retirement_route.py`
- Modify: `argosy/api/main.py` — register the router

- [ ] **Step 0.4.1: Write failing route test**

```python
# tests/test_retirement_route.py
"""Smoke tests for the umbrella /api/retirement/* router.

Wave 0 ships only the sources + reference endpoints; later waves add
projection / safety / ruin endpoints.
"""
import pytest


class TestSourcesEndpoint:
    def test_get_sources_returns_canonical_registry(self, client_with_db):
        r = client_with_db.get("/api/retirement/sources")
        assert r.status_code == 200
        body = r.json()
        assert "sources" in body
        assert "bituach_leumi_old_age_2026" in body["sources"]
        bl = body["sources"]["bituach_leumi_old_age_2026"]
        assert bl["kind"] == "official"
        assert bl["url"].startswith("https://www.btl.gov.il")

    def test_get_source_by_id(self, client_with_db):
        r = client_with_db.get("/api/retirement/sources/bengen_1994")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == "bengen_1994"
        assert body["kind"] == "research"

    def test_unknown_source_returns_404(self, client_with_db):
        r = client_with_db.get("/api/retirement/sources/nonexistent_xyz")
        assert r.status_code == 404


class TestReferenceEndpoint:
    def test_get_reference_returns_value_with_rationale(self, client_with_db):
        r = client_with_db.get(
            "/api/retirement/reference/mekadem.clal_pensia?user_id=ariel",
        )
        assert r.status_code == 200
        body = r.json()
        assert body["value"] == 200
        assert body["unit"] == "ratio"
        assert body["source_id"] == "clal_published_table_2026"

    def test_unknown_reference_returns_404(self, client_with_db):
        r = client_with_db.get(
            "/api/retirement/reference/nonexistent.key?user_id=ariel",
        )
        assert r.status_code == 404
```

- [ ] **Step 0.4.2: Implement `argosy/api/routes/retirement.py`**

```python
# argosy/api/routes/retirement.py
"""Umbrella router for retirement-engine endpoints.

Wave 0 surfaces only the sources + reference primitives. Later waves
register additional endpoints on this same prefix (/api/retirement/*).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from argosy.api.deps import get_db
from argosy.services.retirement.citations import as_dict
from argosy.services.retirement.reference import resolve, ResolveError
from argosy.services.retirement.sources import load_sources

router = APIRouter(prefix="/api/retirement", tags=["retirement"])


class SourceDTO(BaseModel):
    id: str
    title: str
    url: str
    as_of: str
    kind: str
    notes: str = ""


class SourcesResponse(BaseModel):
    sources: dict[str, SourceDTO]


@router.get("/sources", response_model=SourcesResponse)
def get_sources() -> SourcesResponse:
    reg = load_sources()
    return SourcesResponse(
        sources={
            sid: SourceDTO(
                id=s.id, title=s.title, url=s.url,
                as_of=s.as_of, kind=s.kind, notes=s.notes,
            )
            for sid, s in reg.sources.items()
        },
    )


@router.get("/sources/{source_id}", response_model=SourceDTO)
def get_source(source_id: str) -> SourceDTO:
    reg = load_sources()
    s = reg.get(source_id)
    if s is None:
        raise HTTPException(status_code=404, detail=f"unknown source: {source_id!r}")
    return SourceDTO(
        id=s.id, title=s.title, url=s.url,
        as_of=s.as_of, kind=s.kind, notes=s.notes,
    )


@router.get("/reference/{key}")
def get_reference(
    key: str,
    user_id: str,
    db: Session = Depends(get_db),
) -> dict:
    try:
        v = resolve(key, user_id=user_id, session=db)
    except ResolveError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return as_dict(v)
```

- [ ] **Step 0.4.3: Register the router in `argosy/api/main.py`**

Find the existing `app.include_router(...)` block; add:

```python
from argosy.api.routes import retirement as retirement_routes
app.include_router(retirement_routes.router)
```

- [ ] **Step 0.4.4: Run integration tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_retirement_route.py -xvs`
Expected: 5/5 PASS.

- [ ] **Step 0.4.5: Codex review checkpoint**

- [ ] **Step 0.4.6: Commit**

```bash
git add argosy/api/routes/retirement.py argosy/api/main.py tests/test_retirement_route.py
git commit -m "feat(retirement): /api/retirement/* umbrella route — sources + reference endpoints (Wave 0)"
```

### Task 0.5: TS types + api client

**Files:**
- Create: `ui/src/lib/retirement-types.ts`
- Modify: `ui/src/lib/api.ts` — add `api.retirement` namespace

- [ ] **Step 0.5.1: Write `retirement-types.ts`**

```typescript
// ui/src/lib/retirement-types.ts
// TS counterparts to ``argosy/services/retirement/citations.py`` and
// ``argosy/services/retirement/sources.py``. Keep these in lockstep with
// the backend dataclasses — pydantic v2 + ``as_dict()`` serializes to this
// shape directly.

export interface ValueWithRationale {
  value: number | string | null;
  unit: string;
  source_id: string | null;
  rationale: string;
  alternatives_considered?: string[];
  as_of_date?: string;
  freshness_warning?: string;
  confidence?: "high" | "medium" | "low";
}

export interface Source {
  id: string;
  title: string;
  url: string;
  as_of: string;
  kind: "official" | "research" | "derived" | "best_effort";
  notes?: string;
}

export interface SourcesResponse {
  sources: Record<string, Source>;
}
```

- [ ] **Step 0.5.2: Extend `api.ts` with `api.retirement` namespace**

In the `api = { ... }` block, ADD a new property `retirement` (don't replace existing properties):

```typescript
  retirement: {
    sources: () =>
      getJSON<SourcesResponse>("/api/retirement/sources"),
    source: (sourceId: string) =>
      getJSON<Source>(`/api/retirement/sources/${encodeURIComponent(sourceId)}`),
    reference: (key: string, userId: string) =>
      getJSON<ValueWithRationale>(
        `/api/retirement/reference/${encodeURIComponent(key)}?user_id=${encodeURIComponent(userId)}`,
      ),
  },
```

And at the top of `api.ts`, add the type imports:

```typescript
import type {
  Source,
  SourcesResponse,
  ValueWithRationale,
} from "@/lib/retirement-types";
```

- [ ] **Step 0.5.3: Typecheck**

Run: `cd ui && npm run typecheck`
Expected: clean.

- [ ] **Step 0.5.4: Codex review checkpoint**

- [ ] **Step 0.5.5: Commit**

```bash
git add ui/src/lib/retirement-types.ts ui/src/lib/api.ts
git commit -m "feat(retirement-ui): TS types + api.retirement client namespace (Wave 0)"
```

### Task 0.6: UI primitives — `<ValueWithTooltip>` + `<DrilldownSection>`

**Files:**
- Create: `ui/src/components/retirement/ValueWithTooltip.tsx`
- Create: `ui/src/components/retirement/DrilldownSection.tsx`

- [ ] **Step 0.6.1: Implement `<ValueWithTooltip>`**

```tsx
// ui/src/components/retirement/ValueWithTooltip.tsx
"use client";

import { useState } from "react";
import type { ValueWithRationale } from "@/lib/retirement-types";

interface Props {
  /** The value to display. Falls back to children if omitted. */
  display?: string;
  /** Full citation metadata. */
  data: ValueWithRationale;
  /** Optional className for the trigger span. */
  className?: string;
  children?: React.ReactNode;
}

/**
 * Hover-explainable value. Renders the value as a subtle dotted-underline span;
 * on hover, a popover surfaces the rationale + source link.
 *
 * Single visual primitive used by every retirement-relevant number.
 */
export function ValueWithTooltip({ display, data, className, children }: Props) {
  const [open, setOpen] = useState(false);
  const shown = display ?? children ?? formatDefault(data);

  return (
    <span
      className={`relative inline-block border-b border-dotted border-muted-foreground/50 cursor-help ${className ?? ""}`}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
      tabIndex={0}
      aria-describedby={open ? "vwt-popover" : undefined}
    >
      {shown}
      {open && (
        <span
          id="vwt-popover"
          role="tooltip"
          className="absolute left-1/2 -translate-x-1/2 top-full mt-1 z-50 w-72 rounded-md border border-border/60 bg-background/95 px-3 py-2 text-xs shadow-md"
        >
          <span className="block font-medium text-foreground">
            {data.unit ? `${shown} ${data.unit}` : shown}
          </span>
          <span className="mt-1 block text-muted-foreground">{data.rationale}</span>
          {data.alternatives_considered && data.alternatives_considered.length > 0 && (
            <span className="mt-1 block text-muted-foreground">
              <span className="font-medium">Alternatives:</span>{" "}
              {data.alternatives_considered.join(" · ")}
            </span>
          )}
          {data.freshness_warning && (
            <span className="mt-1 block text-warning">
              ⚠ {data.freshness_warning}
            </span>
          )}
          {data.source_id && (
            <span className="mt-1 block text-[10px] opacity-70 font-mono">
              src: {data.source_id}
              {data.as_of_date && ` · ${data.as_of_date}`}
            </span>
          )}
        </span>
      )}
    </span>
  );
}

function formatDefault(d: ValueWithRationale): string {
  if (d.value === null || d.value === undefined) return "—";
  if (typeof d.value === "number") {
    if (d.unit === "fraction") return `${(d.value * 100).toFixed(1)}%`;
    if (d.unit === "NIS/mo") return `₪${d.value.toLocaleString()}`;
    if (d.unit === "USD") return `$${d.value.toLocaleString()}`;
    return d.value.toLocaleString();
  }
  return String(d.value);
}
```

- [ ] **Step 0.6.2: Implement `<DrilldownSection>`**

```tsx
// ui/src/components/retirement/DrilldownSection.tsx
"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

interface Props {
  title: string;
  defaultOpen?: boolean;
  badge?: string;  // optional small count or status (e.g., "3 sources")
  children: React.ReactNode;
}

/**
 * Collapsible "drill-down" section. Used for Methodology / Sensitivity /
 * Sources panels — the bottom-half of every retirement-relevant page.
 *
 * Visual contract: chevron + title in a single clickable row; expanded
 * content inset with a thin left border to signal hierarchy.
 */
export function DrilldownSection({ title, defaultOpen = false, badge, children }: Props) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="mt-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors"
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="h-4 w-4" />
        ) : (
          <ChevronRight className="h-4 w-4" />
        )}
        <span>{title}</span>
        {badge && (
          <span className="ml-1 rounded bg-muted px-1.5 py-0.5 text-[10px] font-mono">
            {badge}
          </span>
        )}
      </button>
      {open && (
        <div className="mt-2 ml-2 border-l-2 border-border/40 pl-3 text-sm">
          {children}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 0.6.3: Typecheck**

Run: `cd ui && npm run typecheck`
Expected: clean.

- [ ] **Step 0.6.4: Codex review checkpoint**

- [ ] **Step 0.6.5: Commit**

```bash
git add ui/src/components/retirement/ValueWithTooltip.tsx ui/src/components/retirement/DrilldownSection.tsx
git commit -m "feat(retirement-ui): ValueWithTooltip + DrilldownSection primitives (Wave 0)"
```

### Task 0.7: UI primitives — `<HeroCard>` + `<SourcesPanel>`

**Files:**
- Create: `ui/src/components/retirement/HeroCard.tsx`
- Create: `ui/src/components/retirement/SourcesPanel.tsx`

(Per the same TDD-then-implement pattern as 0.6. Full code blocks shown at execution time. Visual contracts:

- `<HeroCard>`: title row + status badge (green/yellow/red) + 1-3 primary numbers + optional delta-vs-baseline annotation. Width: full container. Background: subtle gradient tied to status.
- `<SourcesPanel>`: receives a list of `source_id`s used on the page; fetches `api.retirement.sources()` once on mount; renders numbered list with title + URL + as_of + kind badge. Auto-scrolls to a specific source when `<ValueWithTooltip>` is hovered and emits a `data-source-id` attribute (cross-component scroll-into-view).)

- [ ] **Step 0.7.1: Implement `<HeroCard>` (TDD-skipped for pure presentational primitive; visual verified by running the page)**

- [ ] **Step 0.7.2: Implement `<SourcesPanel>` (fetches `api.retirement.sources()` and renders the registry filtered to ids referenced on the page)**

- [ ] **Step 0.7.3: Codex review checkpoint**

- [ ] **Step 0.7.4: Commit**

```bash
git add ui/src/components/retirement/HeroCard.tsx ui/src/components/retirement/SourcesPanel.tsx
git commit -m "feat(retirement-ui): HeroCard + SourcesPanel primitives (Wave 0)"
```

### Task 0.8: UI primitives — `<MethodologyPanel>` + `<SensitivityPanel>` + `<AssumptionsStrip>`

**Files:**
- Create: `ui/src/components/retirement/MethodologyPanel.tsx`
- Create: `ui/src/components/retirement/SensitivityPanel.tsx`
- Create: `ui/src/components/retirement/AssumptionsStrip.tsx`

- `<MethodologyPanel>`: receives prose (or a React node) + optional formula block (LaTeX rendered via `katex` — already in package.json per `ui/src/lib/api.ts:1903` references). Used inside `<DrilldownSection title="Methodology">`.
- `<SensitivityPanel>`: receives an array `levers: { name: string; delta_percentage_points: number; direction: "up" | "down"; sourceData: ValueWithRationale }[]`; auto-renders the top 3 sorted by absolute effect. Standardised "what moves the verdict most" view.
- `<AssumptionsStrip>`: horizontal flex of sliders + reset button. Each slider gets a `<ValueWithTooltip>` wrapped label.

(Same TDD-skipped-for-presentational pattern + codex review + commit.)

### Task 0.9: Retirement page scaffold + SDD update

**Files:**
- Create: `ui/src/app/retirement/page.tsx`
- Modify: `docs/design/SDD.md` — Wave 0 entry

- [ ] **Step 0.9.1: Scaffold the page**

```tsx
// ui/src/app/retirement/page.tsx
"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { SourcesPanel } from "@/components/retirement/SourcesPanel";

const USER_ID = "ariel";

export default function RetirementPage() {
  return (
    <div className="container mx-auto px-4 py-6 max-w-5xl">
      <h1 className="text-2xl font-semibold mb-4">Retirement companion</h1>
      <p className="text-sm text-muted-foreground mb-6">
        Wave 0 scaffold. Later waves populate the hero card + safety gates +
        cashflow + glide path + decumulation sections.
      </p>

      <Card className="mb-4">
        <CardHeader>
          <CardTitle className="text-base">Coming up</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          <ul className="list-disc pl-5 space-y-1">
            <li>Wave 1: Mekadem variance band + Bituach Leumi stipend module</li>
            <li>Wave 2: Safety gates (NRA estate · emergency liquidity · conflict scenarios)</li>
            <li>Wave 3: Probability-of-ruin gate + sigma auto-calibration + regime-switch MC + stochastic FX + withdrawal policy</li>
            <li>Wave 4: Glide path + rebalancing + lifecycle income + phase expenses + IDF service + healthcare</li>
            <li>Wave 5: Account-aware tax engine + decumulation + lump-vs-annuity + hishtalmut</li>
            <li>Wave 6: Real estate · mortgage · partner · severance split</li>
            <li>Wave 7: Insurance gaps + action engine + replan triggers + multi-goal + behavioral + route dedup</li>
          </ul>
        </CardContent>
      </Card>

      <DrilldownSection title="Sources" badge="all">
        <SourcesPanel filterIds={null} />
      </DrilldownSection>
    </div>
  );
}
```

- [ ] **Step 0.9.2: Add navigation link** — Modify `ui/src/components/AppShell.tsx` (or wherever the nav menu lives) to add a "Retirement" link pointing to `/retirement`.

- [ ] **Step 0.9.3: Smoke-test the page**

Browse to `http://localhost:1337/retirement`. Confirm the page renders + Sources panel populates with the registry. No console errors.

- [ ] **Step 0.9.4: Update SDD handover**

Add a new subsection in the SDD handover note + a Wave 0 entry in the cashflow projection section (or a new "Retirement Companion Overhaul" section sibling to it).

- [ ] **Step 0.9.5: Codex review checkpoint** (final Wave 0 review — codex audits the full Wave 0 diff via `git log --since="6 hours ago"` etc.)

- [ ] **Step 0.9.6: Commit**

```bash
git add ui/src/app/retirement/page.tsx ui/src/components/AppShell.tsx docs/design/SDD.md
git commit -m "feat(retirement-ui): retirement page scaffold + nav link + SDD Wave 0 entry"
```

### Wave 0 checkpoint

After Task 0.9, **pause** for user review:

```
Wave 0 complete:
- 10 commits, 6 backend + 4 frontend
- Tests passing: tests/test_retirement_*.py — N/N (will be 15-20 by Wave 0 end)
- TypeCheck: clean
- New /retirement page renders + Sources panel populated
Proceed to Wave 1 (Israeli reference values — mekadem + BL stipend)?
```

---

## Wave 1: Israeli reference values

**Goal:** Close gaps #3 (mekadem variance) + #6 (Bituach Leumi stipend). Populate `israel_retirement_reference.yaml` with the real values for the user's funds. Surface both in the existing cashflow chart and in a new dedicated card on the retirement page.

**Gaps closed:** BLOCKER #3 (mekadem variance), HIGH #6 (Bituach Leumi)

**Files:**
- Modify: `argosy/data/israel_retirement_reference.yaml` — add full mekadem table (low/typical/high per fund) + BL rate table by contribution-history-bands
- Create: `argosy/services/retirement/mekadem.py` — `MekademBand(low, typical, high)` per fund + age
- Create: `argosy/services/retirement/bituach_leumi.py` — `BLStipendEstimate(months, monthly_nis, source_band)` from age + contribution history
- Modify: `argosy/services/cashflow_projection.py` — consume `MekademBand` (replace hardcoded 200); add BL stipend to monthly income line
- Modify: `argosy/api/routes/retirement.py` — add `/mekadem/{fund_id}` + `/bituach-leumi` endpoints returning `ValueWithRationale`
- Create: `tests/test_retirement_mekadem.py` + `tests/test_retirement_bituach_leumi.py`
- Create: `ui/src/components/retirement/MekademBand.tsx` — visualizes the low/typical/high as a band on the cashflow chart's annuity line
- Create: `ui/src/components/retirement/BituachLeumiCard.tsx` — hero card showing estimated BL monthly + sensitivity to retirement age + contribution history
- Modify: `ui/src/components/plan/cashflow-projection-chart.tsx` — wire mekadem band into the annuity line display
- Modify: `ui/src/app/retirement/page.tsx` — embed the BL card

**Interfaces (key signatures):**

```python
# argosy/services/retirement/mekadem.py
@dataclass
class MekademBand:
    low: ValueWithRationale       # bear case (oldest mortality table)
    typical: ValueWithRationale   # central estimate
    high: ValueWithRationale      # bull case (newest mortality table; spouse benefit)

def get_mekadem_for_fund(
    fund_id: str,
    *,
    retirement_age: int = 67,
    spouse_benefit: bool = False,
    session: Session,
    user_id: str,
) -> MekademBand: ...

# argosy/services/retirement/bituach_leumi.py
@dataclass
class BLStipendEstimate:
    monthly_nis: ValueWithRationale  # central estimate
    monthly_nis_low: ValueWithRationale  # if eligibility weaker than user states
    monthly_nis_high: ValueWithRationale  # if spouse benefit
    eligibility_age: ValueWithRationale  # 67 or 70 depending
    sensitivity_levers: list[dict]  # for SensitivityPanel

def estimate_bl_stipend(
    *,
    current_age: int,
    contribution_history_years: int,
    spouse_eligible: bool,
    session: Session,
    user_id: str,
) -> BLStipendEstimate: ...
```

**Key tests:**
- `test_mekadem_returns_band_with_low_lt_typical_lt_high`
- `test_mekadem_user_override_takes_precedence`
- `test_mekadem_freshness_warning_after_12mo`
- `test_bl_stipend_scales_with_contribution_history`
- `test_bl_stipend_spouse_benefit_adds_50pct_when_eligible`
- `test_bl_stipend_value_with_rationale_includes_source`

**Visualization design (per §0.1 standard):**
- Hero on `/retirement`: `<BituachLeumiCard>` showing `Estimated BL stipend at 67: ₪2,100/mo ± ₪400` with a status badge (`SOLID` if contribution history > 35y) + delta-vs-baseline if user-override differs from shipped default
- Chart augmentation: the existing cashflow chart's annuity line splits into a band (mekadem low/typical/high) — visible when "show pension annuity" toggle is on
- Drilldown sections:
  - Methodology: how mekadem variance is computed, formula `monthly_annuity = balance / mekadem`, link to source
  - Sensitivity: "Top levers — mekadem swing ±20 → ±10% lifetime annuity"
  - Sources: clal_published_table_2026, migdal_published_table_2026, menorah_published_table_2026, bituach_leumi_old_age_2026

**Codex review:** every commit. Codex's special focus on this wave: Israeli pension math correctness — has the spouse-benefit handling matched real BL rules? Are the fund-specific mekadem values plausible vs. published tables?

**Success criteria:**
- Cashflow chart annuity line shows mekadem band (low/typical/high) instead of single 200-flat line
- Retirement page shows BL stipend hero card with hover-explain on every number
- All tests pass; codex signed off on each commit; SDD updated

### Wave 1 checkpoint
Pause for user review.

---

## Wave 2: Safety gates (NRA + Liquidity only)

**Goal:** Close gaps #4 (NRA estate tax gate) + #5 (emergency liquidity floor). Surface a `<SafetyGatesPanel>` on both `/retirement` and `/plan` that hard-blocks "approve retirement plan" when any gate is failing.

> **Sequencing note (resolved 2026-05-28 codex plan review):** Gap #15 (war/conflict scenarios) originally lived in Wave 2 but the conflict-scenario gate's P(ruin) computation depends on the ruin-probability infrastructure built in Wave 3. Moved to **Wave 3.6** (Conflict-scenario gate, after the P-of-ruin gate is built). Wave 2 now ships only the two gates that don't depend on Monte Carlo infrastructure.

**Gaps closed:** BLOCKER #4, BLOCKER #5

(HIGH #15 closed in Wave 3.6.)

**Files:**
- Create: `argosy/services/retirement/safety_gates.py` — two sub-classes: `NraEstateGate`, `LiquidityGate`. Each returns `GateVerdict(status, value_with_rationale, suggested_action)`. (The third — `ConflictScenarioGate` — moves to Wave 3.6.)
- Create: `tests/test_retirement_safety_gates.py`
- Modify: `argosy/api/routes/retirement.py` — add `/safety-gates?user_id=...` returning the two gates
- Create: `ui/src/components/retirement/SafetyGatesPanel.tsx` — render the gates side-by-side
- Create: `ui/src/components/retirement/NraEstateAlert.tsx` — dedicated alert when NRA gate fires (estate-tax exposure > $200K)
- Create: `ui/src/components/retirement/EmergencyLiquidityCard.tsx`
- Modify: `ui/src/app/retirement/page.tsx` — embed `<SafetyGatesPanel>` near top
- Modify: `ui/src/app/plan/page.tsx` — embed `<SafetyGatesPanel>` near top of the plan view

**Interfaces:**

```python
@dataclass
class GateVerdict:
    gate_id: Literal["nra_estate", "emergency_liquidity", "conflict_scenario"]
    status: Literal["PASS", "WARN", "FAIL"]
    value: ValueWithRationale  # the headline number
    threshold: ValueWithRationale  # what the gate checks against
    suggested_action: ValueWithRationale  # 1-sentence next step
    detail_url: str | None  # link to a methodology page or doc

def compute_safety_gates(*, user_id: str, session: Session) -> list[GateVerdict]: ...
```

**Safety-gate math:**
1. **NRA estate gate**: pulls US-situs assets total (NVDA, US-domiciled ETFs — NOT UCITS-domiciled which are non-US-situs by IRS rules) from `portfolio_snapshots`. If > $200K: FAIL with suggested action "Begin UCITS migration; current US-situs exposure is $X over the $60K NRA exemption". WARN at $60K-$200K. PASS below $60K.
2. **Emergency liquidity gate**: pulls cash + HYSA balance from snapshot; computes months-of-essential-expenses (essential = burn × 0.6 conservative). FAIL if < 6 months; WARN if 6-12; PASS if ≥ 12. Threshold parametrized by `identity_yaml.retirement_reference_overrides.emergency_liquidity_floor_months` (default 12).

(Conflict-scenario gate moved to Wave 3.6.)

**Visualization design:**
- Hero: 3 mini-cards in a row, each colored by status. Click any → expands to full detail.
- The retirement page's overall hero card gets a "Safety: 2/3 gates passing · 1 WARN" subline.
- NRA estate alert is rendered modally when the user opens the proposals queue and a relevant proposal touches US-situs assets.

**Codex review:** every commit. Special focus: US NRA estate tax law accuracy (is the $60K threshold correct? Is graduated rate 18-40% accurate? Does it apply to UCITS-domiciled ETFs?), Israeli emergency-fund convention, plausibility of conflict-scenario stress params.

**Success criteria:**
- 3 safety-gate verdicts computed + rendered + tooltipped
- Both `/retirement` and `/plan` show the panel
- All 3 gates have tests with red/yellow/green threshold examples

### Wave 2 checkpoint
Pause for user review.

---

## Wave 3: Projection trust layer

**Goal:** Close BLOCKER #1 + HIGHs #7, #8, #11, #12. The single biggest narrative-changing wave: replace the misleading single-month "retire-ready age" with a probability-of-ruin gate; auto-calibrate sigma from holdings concentration; ship regime-switch / fat-tail MC + stochastic FX + selectable withdrawal policy.

**Gaps closed:** BLOCKER #1, HIGH #7, HIGH #8, HIGH #11, HIGH #12

**Files:**
- Create: `argosy/services/retirement/ruin_probability.py`
- Create: `argosy/services/retirement/sigma_calibration.py`
- Create: `argosy/services/retirement/regime_switch_mc.py`
- Create: `argosy/services/retirement/stochastic_fx.py`
- Create: `argosy/services/retirement/withdrawal_policy.py`
- Modify: `argosy/services/cashflow_projection.py` — heavy refactor to consume regime-switch MC + stochastic FX + policy-aware withdrawals
- Modify: `argosy/api/routes/retirement.py` — `/projection/ruin-probability`, `/projection/sigma-calibrated`, `/projection/withdrawal-policies` endpoints
- Create: `tests/test_retirement_ruin_probability.py`, `tests/test_retirement_sigma_calibration.py`, `tests/test_retirement_regime_switch_mc.py`, `tests/test_retirement_stochastic_fx.py`, `tests/test_retirement_withdrawal_policy.py`
- Create: `ui/src/components/retirement/RuinProbabilityHero.tsx` — the new hero (replaces the existing single-month crossing display)
- Create: `ui/src/components/retirement/SigmaCalibrationNote.tsx`
- Create: `ui/src/components/retirement/RegimeSwitchSelector.tsx`
- Create: `ui/src/components/retirement/StochasticFxToggle.tsx`
- Create: `ui/src/components/retirement/WithdrawalPolicySelector.tsx`
- Modify: `ui/src/components/plan/cashflow-projection-chart.tsx` — major refactor; hero card embedded at top; policy selector drives the chart

**Interfaces:**

```python
# ruin_probability.py
@dataclass
class RuinProbabilityVerdict:
    p_solvent_at_75: ValueWithRationale  # P(solvent at age 75)
    p_solvent_at_85: ValueWithRationale
    p_solvent_at_95: ValueWithRationale
    p_solvent_at_95_ci_low: ValueWithRationale  # bootstrap 95% CI lower bound
    p_solvent_at_95_ci_high: ValueWithRationale  # upper bound
    target_p_solvent: ValueWithRationale  # e.g., 0.90
    verdict: Literal["ON_TRACK", "WARN", "OFF_TRACK", "UNCERTAIN"]
    retire_ready_age: ValueWithRationale | None  # earliest age where target is met
    suggested_action: ValueWithRationale

def compute_ruin_probability(
    *,
    user_id: str,
    session: Session,
    target_p_solvent: float = 0.90,
    withdrawal_policy: str = "guyton_klinger",
    n_paths: int = 2000,  # raised from 1000 per codex plan review — see CI note
    regime: Literal["calm", "turbulent", "regime_switch"] = "regime_switch",
    bootstrap_ci_samples: int = 200,
) -> RuinProbabilityVerdict: ...

# Uncertainty handling (codex plan review BLOCKER #6):
# - n_paths raised to 2000 so the empirical proportion's standard error at
#   ``p ≈ 0.10`` is ≈ 0.007 (sqrt(p*(1-p)/n)); that's tight enough to claim
#   a 1pp verdict band.
# - In addition, bootstrap_ci_samples=200 resampled subsets compute a 95%
#   bootstrap CI on each P(solvent at age) value.
# - Verdict logic uses the CI, not the point estimate:
#     CI lower bound >= target → ON_TRACK
#     CI upper bound < target  → OFF_TRACK
#     otherwise                → UNCERTAIN (with "more paths needed" hint)
# - UI displays the CI as a small ±X% next to the headline percentage.

# sigma_calibration.py
def calibrate_sigma_from_holdings(
    *,
    user_id: str,
    session: Session,
) -> ValueWithRationale: ...
# Returns concentration-weighted sigma. For Ariel's NVDA-heavy portfolio
# this returns ~0.32 (between NVDA single-stock 0.45 and S&P 0.18).

# regime_switch_mc.py
def simulate_regime_switch_mc(
    *,
    n_paths: int,
    horizon_months: int,
    initial_value: float,
    regimes: dict[str, dict],  # "calm": {mu: 0.10, sigma: 0.15}, ...
    transition_matrix: np.ndarray,  # P(regime change per month)
    seed: int | None = None,
) -> np.ndarray:  # shape (n_paths, horizon_months)
    ...

# stochastic_fx.py
def simulate_stochastic_fx(
    *,
    n_paths: int,
    horizon_months: int,
    initial_fx: float,
    mu_fx: float = 0.0,
    sigma_fx: float = 0.08,
    seed: int | None = None,
) -> np.ndarray:  # shape (n_paths, horizon_months)
    ...

# withdrawal_policy.py
@dataclass
class WithdrawalPolicy:
    id: Literal["fixed_4pct", "vpw", "guyton_klinger", "bucket"]
    label: str
    rationale: str
    monthly_withdrawal_fn: Callable[[float, int], float]  # (portfolio_value, month) -> monthly NIS

POLICIES: dict[str, WithdrawalPolicy] = {...}

def apply_withdrawal_policy(
    *,
    policy_id: str,
    portfolio_path: np.ndarray,  # (n_paths, n_months)
    expense_path: np.ndarray,    # (n_months,)
    ...
) -> np.ndarray: ...  # net withdrawal per (path, month)
```

**Visualization design:**
- New hero card on top of `/plan` and `/retirement` charts:
  ```
  ┌─────────────────────────────────────────────────┐
  │  P(solvent at 95):   87%   ●WARN                │
  │  Target:             90%                        │
  │  Retire-ready:       51 (was: 49)               │
  │  Top lever:          +5y in workforce → 93%     │
  └─────────────────────────────────────────────────┘
  ```
- The existing chart toggle (Deterministic | Monte Carlo) gets a third option: "Regime-switch MC" (slower; ~3s). Default switches to MC + regime-switch.
- Sigma calibration: a small inline note next to the sigma slider — "Auto-calibrated from your holdings: σ = 0.32 (NVDA-weighted)" with a "reset to 0.18 (diversified)" link.
- Withdrawal policy: dropdown selector. Tooltips on each policy explain Bengen/Guyton-Klinger/VPW.
- Stochastic FX: toggle. When on, the cashflow chart shows in NIS (base liability currency) with a confidence band. Methodology drill-down explains the joint USD/NIS process.

**Codex review focus:** Monte Carlo correctness (path-failure semantics — once failed, stay failed; no resurrection from lump-unlock), regime-switch matrix validity, withdrawal-policy formulas vs. published research, sigma calibration formula (covariance-weighted vs naive max).

**Success criteria:**
- Single-month "retire-ready" message is GONE — replaced everywhere by P(ruin) verdict
- Default chart shows regime-switch MC + auto-calibrated sigma + Guyton-Klinger withdrawal
- All 5 new modules + tests pass + codex green on each commit
- /plan and /retirement both show the new hero card

### Wave 3.6: Conflict-scenario gate (HIGH #15) — runs at end of Wave 3

**Why here, not Wave 2:** the conflict-scenario gate uses `compute_ruin_probability(...)` under stressed parameters. Wave 2 ships before that infrastructure exists; placing the gate here keeps the dependency clean (codex plan-review fix).

**Files:**
- Modify: `argosy/services/retirement/safety_gates.py` — add `ConflictScenarioGate` (consumes the Wave 3 ruin-probability function under stressed params)
- Modify: `tests/test_retirement_safety_gates.py` — add tests for conflict gate
- Modify: `argosy/api/routes/retirement.py` — `/safety-gates` now returns 3 gates
- Create: `ui/src/components/retirement/ConflictScenarioToggle.tsx` — toggle that re-runs the projection under conflict params on user demand
- Modify: `ui/src/components/retirement/SafetyGatesPanel.tsx` — third tile

**Stressed params for conflict scenario:**
- `sigma_annual=0.40` (~2008 turbulent regime)
- `inflation_annual=0.06` (post-conflict spike)
- `fx_nis_devalue_pct=0.30` (NIS devalues vs USD)
- `market_closure_months=6` (forced illiquid period during which withdrawals draw 100% from cash buffer)

**Threshold:** if `P(ruin at 85) > 30%` under conflict params: WARN. If > 50%: FAIL.

### Wave 3 checkpoint
Pause for user review.

---

## Wave 4: Decision policy

**Goal:** Close HIGHs #9, #10, #13, #14 + MEDs #21, #22. Layer the prescriptive policies on top of the projection: glide path (equity % by age), rebalancing rule, lifecycle income timeline, phase-based expenses, IDF service phase, healthcare cost curve.

**Gaps closed:** HIGH #9, HIGH #10, HIGH #13, HIGH #14, MED #21, MED #22

**Files:** see §1 master list. Modules: `glide_path.py`, `rebalancing.py`, `lifecycle_income.py`, `phase_expenses.py`, `idf_service.py`, `healthcare.py`. Tests for each. UI components: `GlidePathChart`, `RebalancingAlerts`, `LifecycleIncomeTimeline`, `PhaseExpenseChart`, `IdfServicePhase`, `HealthcareCurve`.

**Interfaces (key):**

```python
@dataclass
class GlidePathPoint:
    age: int
    target_equity_pct: ValueWithRationale
    target_bond_pct: ValueWithRationale
    target_cash_pct: ValueWithRationale

def compute_glide_path(
    *,
    user_id: str,
    session: Session,
    policy: Literal["vanguard_target_date", "age_minus_30_bonds", "custom"] = "vanguard_target_date",
) -> list[GlidePathPoint]: ...

@dataclass
class RebalancingAlert:
    asset_class: str
    current_pct: ValueWithRationale
    target_pct: ValueWithRationale
    drift_pct: ValueWithRationale
    rule_fired: Literal["5_25_threshold", "quarterly_check", "annual_review"]
    suggested_proposal: str  # "Sell $X NVDA, buy $Y BND" (links into existing T1/T2 proposal flow)

def detect_rebalancing_alerts(*, user_id: str, session: Session) -> list[RebalancingAlert]: ...

@dataclass
class LifecycleIncomeEvent:
    age: int
    event_type: Literal["rsu_vest", "rsu_cliff", "partner_job_change", "side_income", "unemployment_risk"]
    monthly_impact_nis: ValueWithRationale
    probability: ValueWithRationale  # 1.0 for known events; < 1 for risks
    rationale: str

def build_lifecycle_income_timeline(*, user_id: str, session: Session) -> list[LifecycleIncomeEvent]: ...

@dataclass
class ExpensePhase:
    start_age: int
    end_age: int
    label: str  # "kids_peak" / "empty_nest" / "healthcare_ramp"
    monthly_multiplier: ValueWithRationale  # relative to current burn
    inflation_premium: ValueWithRationale  # extra %/yr above CPI

def compute_phase_expenses(*, user_id: str, session: Session) -> list[ExpensePhase]: ...
```

**Visualization design:**
- New page section: "Decision policy". Shows the glide path as a stacked-area chart (equity/bond/cash %) by age, with a vertical line at "current age" showing where Ariel is today + a delta annotation if he's off-glide-path.
- Rebalancing alerts surface as `<Card>`s in the action-engine panel (Wave 7 finishes this; Wave 4 just emits the alerts).
- Lifecycle income timeline: horizontal timeline with event chips at each age. Hover for monthly_impact_nis details.
- Phase expenses: the cashflow chart's expense line is no longer flat (× inflation); it follows the multi-phase curve. Each phase is shaded on the chart with a label.
- IDF service: special phase event chip showing "Year 17-19: kid1 in IDF, household income +₪X / mo from existing income retained while kid is housed-and-fed". (Or expense -₪Y if kid not in IDF range.)
- Healthcare: separate small chart on `/retirement` showing healthcare-as-%-of-burn curve from current age to 95.

**Codex review focus:** Israeli IDF reality check, healthcare cost magnitudes plausible vs published OECD Israel data, glide-path policy correctness vs Vanguard target-date funds, rebalancing 5/25 rule implementation correctness.

**Success criteria:** all 6 modules + UI components shipped; cashflow projection consumes phase expenses + lifecycle income; rebalancing alerts visible on `/retirement`.

### Wave 4 checkpoint
Pause for user review.

---

## Wave 5: Account-aware tax engine + decumulation

> **Wave 5 split into three sub-waves (codex plan-review fix BLOCKER #7):** Original estimate "~500 LOC" was unrealistic for Israeli account-aware tax + treaty handling. Subdivided into:
> - **Wave 5a:** Core engine + pension annuity tax (post-67 partial exemption with rights-fixation mechanics)
> - **Wave 5b:** Hishtalmut + kupat_gemel + severance (vehicle-specific tax rules; each treated as a distinct case, NOT "similar to hishtalmut")
> - **Wave 5c:** Lump-vs-annuity decision wizard + decumulation order optimizer

**Goal:** Close BLOCKER #2 (tax engine) + MED #20 (hishtalmut) + LOW #29 (lump-vs-annuity) + LOW #30 (decumulation). The single biggest backend lift in this plan. Replaces the current flat `tax_rate` slider with a proper per-account, per-cashflow-type tax engine.

> **Israeli tax law accuracy (codex plan-review fixes #2-5):** Earlier draft asserted simplifications that are wrong for "policy-grade" use. Corrections:
> - **Kupat_pensia post-67 partial exemption is NOT a flat 35%.** Per Israel Tax Authority procedure (Jan 2025), the exemption regime moved to a rights-fixation model with rate phasing (~57% in 2025, stepping further by 2027-2030). Source: `israeli_tax_authority_pension_exemption_2025`. Wave 5a MUST consume the rights-fixation table, not a hardcoded 35%.
> - **Kupat_gemel is NOT "similar to hishtalmut."** They share a class but have different tax treatment per vehicle origin (pre-2008 vs post-2008 contributions), withdrawal rules, and tax-free thresholds. Wave 5b treats each as a distinct case with its own test file.
> - **Hishtalmut tax-free conditions are NOT universally "after 6yr OR after age 67."** Eligibility depends on deposit purpose, timing, and rule-set context (employee-deposited vs self-employed). Wave 5b handles these conditions explicitly per ITA guidance.
> - **US dividend withholding uses foreign-tax-credit interaction, NOT "reclaim."** Israeli residents claim a credit for US treaty withholding (15% for US-source dividends to Israeli residents under the US-Israel treaty) against their Israeli tax liability. Wave 5a tax engine implements as: `israeli_tax = max(0, 25% * gross - 15% * us_withholding)` per the credit mechanism.

**Gaps closed:** BLOCKER #2, MED #20, LOW #29, LOW #30

**Files:**
- Create: `argosy/services/retirement/tax_engine.py` (estimated 700-1000 LOC after corrections above; split into sub-modules if it grows past 800)
- Create: `argosy/services/retirement/hishtalmut.py`
- Create: `argosy/services/retirement/kupat_gemel.py` (NEW per codex correction — was implicitly merged with hishtalmut in the draft)
- Create: `argosy/services/retirement/decumulation.py`
- Create: `argosy/services/retirement/lump_vs_annuity.py`
- Tests: 5 new test files (one per module)
- UI: `TaxBreakdownTable`, `HishtalmutTimer`, `DecumulationOrderCard`, `LumpVsAnnuityWizard`

**Interfaces (key):**

```python
# tax_engine.py
@dataclass
class TaxableCashflow:
    source: Literal["pension_annuity", "lump_withdrawal", "rsu_vest", "capital_gain", "dividend", "salary", "interest", "rental"]
    gross_amount: float
    account: Literal["taxable", "kupat_pensia", "keren_hishtalmut", "kupat_gemel", "executive_insurance"]
    holding_years: int  # for hishtalmut 6yr rule
    user_age: int

def compute_tax(cashflow: TaxableCashflow, *, user_id: str, session: Session) -> ValueWithRationale:
    """Returns net amount after Israeli tax (with US treaty withholding where applicable)."""
    ...

# Standard cases (corrected per codex plan-review):
# - capital_gain on taxable Israeli-resident equity: × (1 - 0.25)
# - pension_annuity (kupat_pensia post-67): age-banded marginal rate against
#   a partial-exemption envelope. The exemption is a rights-fixation regime
#   per ITA Jan-2025 procedure: ~57% in 2025, phasing further by 2027+.
#   NOT a flat 35%. See ``argosy/data/israel_retirement_reference.yaml::
#   tax.pension_exemption_envelope_by_year`` for the year-by-year table.
# - hishtalmut tax-free eligibility — three distinct conditions; ALL must
#   be checked: (a) employee-deposited 6yr from first deposit, (b) self-
#   employed 6yr from first deposit with different aggregation rules, OR
#   (c) age 67 lump. Early withdrawal: marginal rate ~47% with bituach-
#   leumi adjustments. See ``hishtalmut.py`` for the case logic.
# - kupat_gemel: per-vehicle rules (pre-2008 vs post-2008 contributions
#   have different treatment). See ``kupat_gemel.py`` — NOT inferred from
#   hishtalmut logic.
# - executive_insurance: handled per legacy policy contract (each policy
#   has its own actuarial table; this is a "see policy document" case)
# - US dividends (Israeli resident): treaty withholding 15% at source.
#   Israeli liability is 25% on the gross; the 15% US withholding becomes
#   a foreign-tax-credit against the Israeli liability. Engine implements:
#     israeli_tax_due = max(0, 0.25 * gross - 0.15 * gross_us_withheld)
# - rsu_vest: marginal income tax + bituach-leumi cap-aware (BL caps at
#   the ceiling table value, currently ~₪50k/mo gross)

# hishtalmut.py
@dataclass
class HishtalmutEligibility:
    months_until_taxfree: ValueWithRationale  # 0 if already eligible
    first_deposit_date: ValueWithRationale
    six_yr_eligible: ValueWithRationale  # bool
    age_67_eligible: ValueWithRationale  # bool

def check_hishtalmut_eligibility(*, user_id: str, session: Session) -> HishtalmutEligibility: ...

# decumulation.py
@dataclass
class DecumulationStep:
    order: int
    account: str
    monthly_draw_nis: ValueWithRationale
    rationale: str  # "Drawing from taxable first — locked at age 60 capital-gains step-up"

def optimize_decumulation_order(
    *,
    user_id: str,
    session: Session,
    monthly_need_nis: float,
) -> list[DecumulationStep]: ...

# lump_vs_annuity.py
@dataclass
class LumpVsAnnuityVerdict:
    recommendation: Literal["take_annuity", "take_lump", "split"]
    annuity_path: dict  # P(ruin at 95) + lifetime NPV under annuity
    lump_path: dict      # P(ruin at 95) + lifetime NPV under lump
    split_path: dict     # 50/50 hybrid
    rationale: str

def compute_lump_vs_annuity(
    *,
    user_id: str,
    session: Session,
    decision_age: int = 60,  # for hishtalmut/gemel; 67 for kupat_pensia
) -> LumpVsAnnuityVerdict: ...
```

**Visualization design:**
- `<TaxBreakdownTable>`: line-item breakdown per cashflow source showing gross, tax, net, source-of-tax-rule citation
- `<HishtalmutTimer>`: countdown timer if not yet 6-year eligible; "EligibleNow" badge if past the threshold
- `<DecumulationOrderCard>`: ordered list with monthly draw amounts + rationale per step
- `<LumpVsAnnuityWizard>`: side-by-side comparison + interactive split slider; both paths' P(ruin at 95) shown as gauges

**Codex review focus:** Israeli tax law accuracy is the highest-risk part of the entire plan. Codex's job: catch tax-rule errors. Specifically check kupat_pensia partial exemption at age 67, US treaty withholding, hishtalmut 6yr rule edge cases, marginal vs flat for different cashflow types.

**Success criteria:** Flat `tax_rate` slider gone; replaced by per-account engine. Hishtalmut timer + decumulation order + lump-vs-annuity wizard all visible on `/retirement`.

### Wave 5 checkpoint
Pause for user review.

---

## Wave 6: Balance sheet completeness

**Goal:** Close MEDs #16, #17, #18, #19. Bring real-estate equity, mortgage schedule, partner income/assets, and severance split into the model.

**Gaps closed:** MED #16, MED #17, MED #18, MED #19

**Files:** `real_estate.py`, `mortgage.py`, `partner_state.py`, `severance.py`. Tests. UI: `RealEstateCard`, `MortgageSchedule`, `PartnerStatePanel`, `SeveranceSplitCard`.

**Interfaces (key):**

```python
@dataclass
class RealEstateState:
    primary_residence_value: ValueWithRationale
    mortgage_balance: ValueWithRationale
    equity: ValueWithRationale  # value - balance
    appreciation_annual: ValueWithRationale  # default Israeli historical ~3.5%
    illiquidity_haircut: ValueWithRationale  # default 10% for primary; 5% for rental
    monthly_property_tax: ValueWithRationale

def extract_real_estate_state(*, user_id: str, session: Session) -> RealEstateState: ...

@dataclass
class MortgageScheduleRow:
    month: int
    payment: ValueWithRationale  # principal + interest
    principal_paid: ValueWithRationale
    interest_paid: ValueWithRationale
    remaining_balance: ValueWithRationale

def build_mortgage_schedule(
    *,
    initial_balance: float,
    annual_rate: float,
    term_months: int,
    start_month: int = 0,
) -> list[MortgageScheduleRow]: ...

@dataclass
class PartnerState:
    age: ValueWithRationale
    monthly_income_nis: ValueWithRationale
    pension_balance_nis: ValueWithRationale
    retirement_age: ValueWithRationale

def extract_partner_state(*, user_id: str, session: Session) -> PartnerState | None: ...

@dataclass
class SeveranceState:
    accrued_pizurim_nis: ValueWithRationale  # the 8.33% portion split out
    withdrawn_history_nis: ValueWithRationale
    annuitization_probability: ValueWithRationale  # user's stated intent
    tax_treatment: ValueWithRationale

def extract_severance_state(*, user_id: str, session: Session) -> SeveranceState: ...
```

**Visualization design:**
- `<RealEstateCard>` on `/retirement`: value + equity + appreciation trajectory + illiquidity haircut clearly labeled
- `<MortgageSchedule>`: line chart of principal vs interest over time + payoff-date highlight
- `<PartnerStatePanel>`: mirror of Ariel's retirement view but for partner; "Household-level retire-ready age" combining both
- `<SeveranceSplitCard>`: shows the kupat_pensia balance with severance carved out as a separate stacked bar; methodology cites "documented optimistic bias" + explains how Argosy now models it correctly

**Codex review focus:** mortgage amortization formula correctness, Israeli pizurim taxation, partner-data merging logic (avoid double-counting joint assets).

**Success criteria:** real estate + mortgage + partner + severance all flow into the projection; net worth on `/portfolio` includes real estate equity; severance bias warning is GONE because it's now modeled properly.

### Wave 6 checkpoint
Pause for user review.

---

## Wave 7: Companion UX + policy engine + cleanup

**Goal:** Close MEDs #23, #24, #25, #26, #28 + LOW #27. The "make Argosy actually feel like a companion" wave: insurance gap calculators, real action-items policy engine, replan triggers, multi-goal balancing, behavioral guardrails, route dedup.

**Gaps closed:** MED #23, MED #24, MED #25, MED #26, MED #28, LOW #27

**Files:** `insurance_gaps.py`, `action_engine.py`, `replan_triggers.py`, `multi_goal.py`, `behavioral.py`. Tests. UI: `InsuranceGapsCard`, `ActionEngineList` (replaces existing widget), `ReplanTriggerLog`, `MultiGoalBalancer`, `BehavioralCheckpoint`. Plus the route dedup for `/plan/action-items`.

**Interfaces (key):**

```python
@dataclass
class InsuranceGap:
    insurance_type: Literal["life", "disability", "ltc", "health_supplementary"]
    recommended_coverage: ValueWithRationale
    actual_coverage: ValueWithRationale
    gap_nis: ValueWithRationale
    suggested_action: ValueWithRationale  # "Increase life insurance to ₪X" or "Already adequate"

def compute_insurance_gaps(*, user_id: str, session: Session) -> list[InsuranceGap]: ...

@dataclass
class PrioritizedAction:
    id: str  # stable hash
    title: str
    rationale: str
    severity: Literal["BLOCKER", "HIGH", "MEDIUM", "LOW"]
    due_date: str | None  # ISO YYYY-MM-DD
    owner: Literal["ariel", "noga", "joint", "advisor"]
    consequence_score: ValueWithRationale  # lifetime NPV impact of skipping
    dependencies: list[str]  # other action ids
    sources: list[str]  # source_ids used

def compute_prioritized_actions(*, user_id: str, session: Session) -> list[PrioritizedAction]: ...

@dataclass
class ReplanTrigger:
    trigger_id: str
    fired_at: datetime
    cause: Literal["market_drawdown", "job_change", "tax_law_change", "health_event", "fx_shock", "user_request"]
    recompute_status: Literal["pending", "running", "complete"]

def get_replan_trigger_log(*, user_id: str, session: Session) -> list[ReplanTrigger]: ...

@dataclass
class GoalConstraint:
    goal_id: str  # "retirement" | "kids_education" | "house_upgrade" | "charity"
    constraint_type: Literal["hard_floor", "soft_target", "no_later_than"]
    target_nis: ValueWithRationale
    deadline: str | None  # ISO YYYY-MM-DD for hard deadlines
    priority: int  # 1-10; used as lexicographic tiebreaker when constraints permit slack
    rationale: str

@dataclass
class GoalBalance:
    goal_id: str
    target_nis: ValueWithRationale
    funded_pct: ValueWithRationale
    binding_constraints: list[str]  # which other goals constrain this one
    tradeoffs: list[ValueWithRationale]  # "Retire 2y later → fully fund education"

# Per codex plan-review BLOCKER #8: NOT "Lagrangian vs priority order."
# Approach: constrained optimization with explicit hard/soft constraints
# + explainable policy outputs. Hard constraints (e.g. kids' tuition due
# in 18mo) MUST be met; soft constraints (e.g. retirement at 49) are
# optimized within remaining budget. Explainability requires that every
# "trade off X for Y" suggestion cite which constraint is binding.
def balance_multi_goals(
    *,
    user_id: str,
    session: Session,
    constraints: list[GoalConstraint] | None = None,
) -> list[GoalBalance]: ...
```

**Visualization design:**
- `<ActionEngineList>` replaces existing action-items widget: pri-sorted with severity badges, due dates, "skip-consequences" expand
- `<ReplanTriggerLog>` on home: timeline of "what happened recently that triggered a re-compute"
- `<InsuranceGapsCard>` on `/retirement`: per-type gap with status badge + recommended-vs-actual delta
- `<MultiGoalBalancer>` on `/retirement`: stacked progress bars per goal; sliders to trade off (e.g., retire age vs education funding)
- `<BehavioralCheckpoint>` modal: fires when user proposes a trade that matches a behavioral pattern (panic-sell after drawdown, FOMO after rally)

**Codex review focus:** behavioral checkpoint triggering rules (false-positive rate), multi-goal optimizer math (Lagrangian or simple priority order?), replan trigger registry completeness.

**Cleanup tasks in this wave:**
- Dedupe `_collect_action_items` + `@router.get("/action-items")` in `argosy/api/routes/plan.py` (verified bug at lines 2413/2477 and 2642/2706)
- Update SDD with the full "what shipped across all 7 waves" summary

**Success criteria:** action engine ships with at least 5 working prioritized actions for Ariel's current state; replan trigger log shows recent activity; insurance gaps card surfaces 4 gap analyses; multi-goal balancer shows retirement + education + house tradeoffs; route dedup committed.

### Final checkpoint
Pause for user review + write a comprehensive SDD update + close out.

---

## §3. Sequencing dependencies

Waves are NOT fully independent — keep this order. Key cross-wave dependencies:

- Wave 0 → ALL: every wave consumes `ValueWithRationale` + `resolve()` + UI primitives
- Wave 1 (mekadem + BL) → Wave 3 (P-of-ruin gate consumes both)
- Wave 3 (regime-switch MC) → Wave 5 (tax engine consumes MC output for tax-adjusted retire-ready)
- Wave 4 (glide path + lifecycle income + phase expenses) → Wave 7 (action engine prioritizes based on glide-path off-target distance)
- Wave 5 (tax engine + decumulation) → Wave 7 (action engine surfaces tax-optimization actions)

Within a wave, tasks can mostly be parallelized; the master plan orders them serially for clarity.

---

## §4. Codex tandem invocation pattern (canonical)

Every commit in this plan goes through codex review per the §0.5 template. Practical invocation:

```python
# argosy/services/.progress/codex_review_runner.py
"""Reusable codex-review dispatcher.

Usage from a daughter wave plan or a one-off review:
  python argosy/services/.progress/codex_review_runner.py \
    --wave 1 --task 1.2 \
    --diff "$(git diff --cached)" \
    --context "Wave 1 · gap-3 mekadem variance · adds MekademBand class + tests"
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, "D:/Projects/financial-advisor/tools/codex-tandem/scripts")
from engine_codex import run_codex

REVIEW_PROMPT_TEMPLATE = """\
Review the staged diff. Working dir is project root; run `git diff --cached`.

Wave: {wave}
Task: {task}
Context: {context}

Assess:
1. Correctness — does the code do what the task says?
2. Type/contract integrity — pydantic schemas, TS interfaces, signatures
3. Test coverage — are new tests testing new behavior, not just covering lines?
4. Israeli pension / tax / FX correctness if applicable
5. Visualization — hero+drill-down standard honored if UI touched
6. Citations — every new value flows through ValueWithRationale?

Output exactly one of:
  COMMIT AS-IS — diff is good
  BLOCKERS:
    - <file:line> <issue>
    ...
  NITS (non-blocking):
    - <file:line> <nit>
"""


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--wave", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--context", required=True)
    args = p.parse_args()

    prompt = REVIEW_PROMPT_TEMPLATE.format(
        wave=args.wave, task=args.task, context=args.context,
    )
    r = run_codex(
        node_dir=Path("D:/Projects/financial-advisor"),
        prompt=prompt,
        agent_name=f"wave{args.wave}_task{args.task}_review",
        sandbox="read-only",
        timeout_s=600,
        role="reviewer",
    )
    print(f"\n=== CODEX REVIEW — exit={r.exit_code} tokens={r.tokens} wall={r.wall_s:.1f}s ===\n")
    print(r.verdict_text)
    # Move result.md into the wave-progress folder
    src = Path("D:/Projects/financial-advisor/result.md")
    dst = Path(f"D:/Projects/financial-advisor/argosy/services/.progress/wave{args.wave}_task{args.task}_review.md")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        src.rename(dst)


if __name__ == "__main__":
    main()
```

Drop this file in Wave 0 (or Wave 1) and call it from each task's review step.

---

## §5. Self-review against the original 30-gap list

Mapping every gap to its closing task:

| # | Severity | Gap | Closed in |
|---|---|---|---|
| 1 | BLOCKER | P(ruin) gate | Wave 3 |
| 2 | BLOCKER | Account-aware tax engine | Wave 5 |
| 3 | BLOCKER | Mekadem variance | Wave 1 |
| 4 | BLOCKER | NRA estate-tax gate | Wave 2 |
| 5 | BLOCKER | Emergency liquidity floor | Wave 2 |
| 6 | HIGH | Bituach Leumi stipend | Wave 1 |
| 7 | HIGH | Sigma auto-calibration | Wave 3 |
| 8 | HIGH | Withdrawal-policy framework | Wave 3 |
| 9 | HIGH | Glide path | Wave 4 |
| 10 | HIGH | Rebalancing rule | Wave 4 |
| 11 | HIGH | Regime-switch / fat-tail MC | Wave 3 |
| 12 | HIGH | Stochastic FX | Wave 3 |
| 13 | HIGH | Lifecycle income | Wave 4 |
| 14 | HIGH | Phase expenses | Wave 4 |
| 15 | HIGH | War/conflict scenarios | Wave 2 |
| 16 | MED | Real-estate equity | Wave 6 |
| 17 | MED | Mortgage schedule | Wave 6 |
| 18 | MED | Partner income/assets | Wave 6 |
| 19 | MED | Severance split | Wave 6 |
| 20 | MED | Hishtalmut tax-aware | Wave 5 |
| 21 | MED | IDF service phase | Wave 4 |
| 22 | MED | Healthcare cost module | Wave 4 |
| 23 | MED | Insurance gap calculators | Wave 7 |
| 24 | MED | Action-items policy engine | Wave 7 |
| 25 | MED | Replan triggers | Wave 7 |
| 26 | MED | Multi-goal balancing | Wave 7 |
| 27 | LOW | Duplicate /action-items route | Wave 7 |
| 28 | LOW | Behavioral guardrails | Wave 7 |
| 29 | LOW | Lump-vs-annuity tool | Wave 5 |
| 30 | LOW | Decumulation order | Wave 5 |

All 30 closed. Cross-cutting visualization standard (hero + chart + drill-down) applied to every new UI surface. Cross-cutting citations standard (`ValueWithRationale` + Sources panel + tooltips) applied to every new value.

---

## §6. Open questions deferred to wave-start

Things I'll decide just-in-time when each wave starts (and ask the user if non-trivial):

- **Wave 1**: Which 3 Israeli pension funds get full mekadem coverage? (Clal/Migdal/Menorah seem right for Ariel — confirm at wave start.)
- **Wave 2**: Emergency-liquidity threshold months — 6/12/24? (User decides — default 12 with override slider.)
- **Wave 3**: Default withdrawal policy — Bengen 4%, Guyton-Klinger, or VPW? (Recommend Guyton-Klinger; user picks at wave start.)
- **Wave 3**: Default `target_p_solvent` threshold — 0.85, 0.90, 0.95? (Recommend 0.90; user picks.)
- **Wave 4**: Glide path policy — Vanguard target-date, age-minus-30 bonds, or custom slider? (Recommend Vanguard target-date as default with custom override; user confirms.)
- **Wave 5**: How granular should the tax engine get? Per-cashflow-source is enough for retirement; per-lot is needed for active trading. (Recommend per-source for now; per-lot deferred.)
- **Wave 6**: Real-estate appreciation assumption — Israeli historical 3.5%/yr or current TLV market data? (Use Israeli historical; surface user override.)
- **Wave 7**: Action engine — daily / weekly / on-demand re-compute? (Recommend weekly + on-trigger; user confirms.)

Each gets surfaced as an `AskUserQuestion` at wave start. None block the plan as a whole.
