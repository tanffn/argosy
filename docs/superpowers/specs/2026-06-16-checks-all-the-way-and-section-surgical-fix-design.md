# Checks all the way + section-level surgical fix — design

**Date:** 2026-06-16
**Status:** approved (design); implementation plan to follow
**Owner area:** `argosy/orchestrator/flows/plan_synthesis/`, `argosy/quality/`

## Problem

Plan synthesis runs a long sequential pipeline (9 analysts → debates →
synthesizer → risk → codex → fund-manager → whole-artifact reader → gate,
~60–90 min) with **all the substantive checking bolted on at the very end**.
Two consequences the user named directly:

1. **Surprises at the end.** A defect introduced by an analyst in minute 5 (a
   stale date, an inverted FX, a fabricated number) is not caught until the
   codex re-derivation / whole-artifact reader read the *finished* document
   ~80 minutes later.
2. **Big-bang correction.** When a reviewer blocks, the current reconcile loop
   (both the codex zigzag and the reader loop shipped 2026-06-16) re-runs the
   **entire synthesizer** — the most expensive Opus call — regenerating the
   whole plan and re-reviewing the whole document. Fixing one mislabeled
   paragraph re-does the whole project, and because the synthesizer is
   stochastic the regen can fix one thing and break another (whack-a-mole).

A real advisory team does neither: each contributor's work is checked as it
lands, and a reviewer who finds a contradiction routes it to the **owner of
that specific segment**, who patches **only that segment**, which is then
re-checked **in isolation**.

## Goal

Make Argosy's synthesis behave like that team:

- **Checks all the way through** — cheap validation at each stage, so the
  end-stage review finds little. No surprises at the end.
- **Fix only the affected segment** — a finding is corrected at the owning
  plan **section**, deterministically where mechanical and with a cheap
  single-section LLM editor where the issue is genuine prose; only that section
  is re-verified. Full re-synthesis is reserved for *structural* problems.

## Organizing principle: every finding has an owner

Any check — deterministic gate or LLM reviewer — must produce a finding that
resolves to a **`(section_id, horizon)`**: the segment that owns it.

- Deterministic gates already emit a structured `locator` (concept name, date
  string, `nvda_cap`, offset). The `locator → section_id` map is the new piece.
- The whole-artifact reader cites `surfaces_cited` (verbatim excerpts). Those
  excerpts are located back to the section whose rendered text contains them.

Attribution is **step zero** — without it you cannot "fix only the affected
segment." A finding that cannot be attributed to a section falls back to the
structural-escalation path (full re-synthesis), so attribution failure is
fail-safe, never silently dropped.

## Phase 1 — Shift-left gating (ships first)

Run the **cheap deterministic** checks where the content is produced, in
addition to the existing end-stage and /accept passes.

- **After the phase-1 analysts** — validate each analyst's key structured
  outputs against the deterministic checks that apply to them: FX value in the
  NIS-per-USD band and unit, no past-due dates rendered as pending, numbers
  internally consistent (e.g. `marketCap/shares ≈ price`). A failing analyst's
  issue is surfaced immediately (and in Phase 2, routed to a fix) rather than
  flowing 80 minutes downstream.
- **After the phase-3 synthesizer** — run the full deterministic doc-gate suite
  (`stale-date`, `fx-unit`, `cap-derivation`, `numeric-source`,
  `cross-surface-coherence`, `ips-allocation-sum`) on the freshly assembled
  draft, **before** the expensive codex / reader / fund-manager stages.
- **End-stage codex + reader stay** as the holistic safety net. By the time the
  document reaches them the mechanical defect classes are already gone, so the
  end review is *quiet*. This is the measurable "no surprises" contract: **if an
  end-stage reviewer keeps finding a class an in-stage gate should have caught,
  that gate is missing** — that gap is itself a tracked defect.

Phase 1 alone removes most late surprises, which is why it ships first. It does
not require the new section-editor or addressable-section machinery — it reuses
the deterministic checks that already exist, invoked earlier.

## Phase 2 — Section-level surgical correction (ships second)

For each finding attributed to a `(section_id, horizon)`:

1. **Deterministic patch** when the fix is mechanical, keyed by `GateCheck`:
   stale date → "overdue N days"; FX unit/inversion → corrected rendering;
   fabricated/uncited number → resolver value or `[derivation pending]`; changed
   cap → require the derivation citation. No LLM, instant, exact.
2. **Section-editor agent** for genuine prose contradictions / fragile claims:
   handed **only** the offending section's text + the finding + the canonical
   value/label it must agree with, returns a **minimal corrected section**.
   Cheap, bounded, single-section in/out.
3. **Re-verify only that section** — run the relevant checks scoped to the
   patched section, not the whole document.
4. **Escalate to full re-synthesis only for *structural* findings** — when the
   allocation/strategy itself is wrong, not when a label/date/unit/number is
   off. This is where the existing full-resynth reconcile loop survives.

This **demotes the full-resynth reconcile loop shipped 2026-06-16** to the
structural-escalation fallback: the expensive path becomes the exception, not
the default response to every BLOCK.

## New components

| Component | Responsibility | Depends on |
| --- | --- | --- |
| Finding attribution | `finding (locator / surfaces_cited) → (section_id, horizon)`; fail-safe to structural path | `GateViolation.locator`, `CoherenceFinding.surfaces_cited`, the rendered section map |
| Addressable sections | render and splice the body per `section_id`+`horizon` so a fix targets one section | existing `Section` / `HorizonSection`; new per-section render/splice |
| Deterministic section patchers | mechanical fixes keyed by `GateCheck` | the existing gate locators + the resolver manifest |
| Section-editor agent | cheap single-section prose correction under a canonical constraint | the section text + the finding |
| Scoped re-verification | run checks against just the patched section | the deterministic checks + reader (section-scoped) |
| In-stage gate hooks | invoke the deterministic suite after analysts + after the synthesizer | the orchestrator phase boundaries |

## Testing

- **Attribution** — unit tests mapping representative deterministic locators and
  reader `surfaces_cited` excerpts to the correct `(section_id, horizon)`,
  including the fail-safe (unattributable → structural path).
- **Deterministic patchers** — per `GateCheck`, a flagged section in → a clean
  section out → the same check passes on the output (red→green per patcher).
- **Section-editor** — stubbed-LLM wire test: a contradicting section + finding →
  corrected section spliced back → scoped re-verify passes; the rest of the
  document is byte-unchanged (proves "only the affected segment").
- **Shift-left hooks** — a synthesized draft carrying a planted stale date / bad
  FX is caught at the post-synthesizer hook, before codex/reader run.
- **No-surprise contract** — a regression test asserting that a class caught by
  an in-stage gate does not also surface fresh at the end-stage reviewer.
- **Isolation** — the section-editor / patchers run under pytest without real
  agent calls (mirror the reconcile-loop test's isolation of
  `run_alternatives_phase`).

## Out of scope (this design)

- Re-running the **owning analyst** (root-cause re-run) as the fix mechanism —
  considered and deferred; section-level correction (symptom + canonical
  constraint) is cheaper and sufficient for the observed finding classes. Revisit
  if a finding class proves un-fixable at the section level.
- The genuine ~1% FI-margin fragility under a −10% FX shock is **not** a wording
  defect; the FI-shock gate force-qualifies the "reached" claim. Surgical
  correction qualifies the claim honestly — it does not, and must not, hide the
  fragility.
- Changing the analyst fleet, the allocation methodology, or the speculation
  routing.

## Validation input

The live synthesis run started 2026-06-16 (`overnight_synth_run5.py`, with the
current reconcile loop) produces the concrete, current list of *which* segments
the reviewers block on. That list is the empirical target set for the
attribution + patcher work — harvest it before building Phase 2.
