# Alternatives Sleeve — Fleet-Sourced — HANDOVER

**Branch:** `feat/alternatives-asset-class` (NOT merged; off `main` @ `9941834`)
**Plan:** `docs/superpowers/plans/2026-06-13-alternatives-fleet-sourcing.md`
**Spec source:** `tmp_review/codex_alternatives_e2e_verdict.txt` (8 findings + 10-step build).

---

## THE BINDING PRINCIPLE (do not violate)
The agent team SOURCES + VERIFIES + SIZES the Alternatives sleeve. The user supplies no tickers, no size, no tilts. **Nothing hardcoded** — instruments AND sleeve % are team-derived; **0% is a valid team answer**. Every instrument is deterministically verified (real ISIN, real domicile, tradeable) AND estate-gated (non-US). Frozen registry entries are *seeds for verification*, never authority over what the team may pick.

---

## CURRENT STATE — what is built (all on the branch, all tested)

The full pipeline **A→F is built and unit-tested** (106 alternatives-related tests green). End-to-end shape:

**source → deterministic verify → estate gate → ETP-aware fleet debate → fund-manager decision → threaded into the canonical plan as a draft.**

### Phase C — deterministic verification (the core safety gate)
- `argosy/services/alternatives_types.py` — `VerificationEvidence`, `VerificationResult` (+`resolved_isin`/`resolved_domicile`), `VerifiedAlternativesCandidate`, `AlternativesSleeveDecision` (0%-coherence validated).
- `argosy/services/instrument_verification.py`:
  - `isin_is_valid` — ISO-6166 checksum, **ASCII-strict** (rejects Unicode lookalikes), prefix gated to **real ISO-3166 alpha-2 + ISIN specials** (rejects reserved ZZ). Pure structure, no estate policy.
  - `normalize_domicile` — canonical non-US set `{IE,LU,UK,DE,CH,JE,IL}` (= `AllocationInstrument.domicile` schema) + country-name synonyms + GB→UK; US synonyms can't bypass; XS is NOT a domicile.
  - `verify_instrument` — **registry hit = authoritative** (agent claims ignored), requires a complete row (isin+domicile+http source+verified_on+exchange), checksum+coherence+non-US ⇒ GREEN; US/bad-checksum ⇒ RED; unknown ⇒ YELLOW. Nothing unverified is ever `verified=True`.
- `argosy/data/verified_instruments.yaml` — verified-facts registry (cache, NOT an allow-list). Seeded with **SGLD** (`IE00B579F325`) + **IGLN** (`IE00B4ND3602`), both confirmed against issuer factsheets/justETF/Euronext. BTC/commodity entries intentionally absent until verified at build time.
- `argosy/services/alternatives_sourcing.py::verify_and_gate_proposal` — supersedes the old claim-trusting `gate_proposal`: verify each pick, keep only GREEN+estate-clean, bind candidates to the verifier-RESOLVED facts (never agent claims).
- **Codex-reviewed twice** (`codex_isin_checksum_verdict.txt`, `codex_verify_instrument_verdict*.txt`); all BLOCKER/HIGH findings fixed.

### Phase D — ETP-aware fleet debate
- `argosy/agents/alternatives_reviewers.py` — three reviewer lenses (`AltExposureStructureAnalyst`, `AltMacroDiversificationAnalyst`, `AltRiskLiquidityTaxAnalyst`) + `AlternativesFundManagerAgent`. Wrapper/structure prompts, NOT equity valuation (codex #4). FM selects from verified candidates **by symbol only** — cannot fabricate an instrument; can land `approve/cut/0_percent/insufficient_data`.
- Four roles registered in `base.py` (model=opus-4-7, effort=high, 16k budget).
- `argosy/orchestrator/flows/plan_synthesis/alternatives_phase.py::run_alternatives_phase` — hard gate first (no verified candidates ⇒ 0% without debate), ≥2 reviewer lenses required for a non-zero sleeve, FM sizes; final decision binds FM picks to verified objects, renormalises weights, computes **sourced** sleeve sigma, clamps to 4% cap. Pure (no DB).

### Phase E — engine + sigma + carry-forward
- `sigma_calibration.py::compute_alternatives_sigma` — sourced sigma from instrument asset classes (gold 0.16, BTC 0.70, …), replacing the fixed 0.268.
- `allocation_plan.py` — **HARDCODE REMOVED** (`_ALTERNATIVES_SLEEVE`, `ALTERNATIVES_TARGET_PCT`, gold/btc fracs, BTC cap, the assert). `build_target_allocation(alternatives_sleeve=None)` consumes a supplied decision: subtract-before-renorm, FI stays the sigma solver, the SOURCED sigma flows into the FI blend. None/0% ⇒ no alternatives class.
- `target_allocation_doc.py::_strip_stale_alternatives` — a CARRIED-FORWARD doc never preserves a stale, un-reverified sleeve (codex #6); folds the dropped % into cash.

### Phase E4 — synthesis integration
- `plan_synthesis/orchestrator.py::run_synthesis` runs `run_alternatives_phase` (best-effort, **never fatal** — on any failure the plan builds with 0% alternatives, never stale/unverified) and threads the decision through `resolve_target_allocation_json` → `build_plan_target_allocation_doc` → `build_target_allocation_doc` → engine. **Draft role unchanged — nothing promoted.**

---

## VERIFICATION STATUS
- Touched-file suites: **106 passed** (`test_instrument_verification / _alternatives_types / _alternatives_sourcer / _alternatives_reviewers / _alternatives_phase / _allocation_plan / _sigma_calibration / _target_allocation_doc`).
- Full suite (`pytest -m "not llm_eval"`): **running at handoff** — check `tmp_review/` / the task output; reconcile any reds.
- Live end-to-end run: `tmp_review/run_alternatives_phase_live.py` (source→verify→debate→decide; prints the decision + verification per instrument). Running at handoff.
- Codex money-math reviews: checksum + verify_instrument (×2) applied; **engine renorm/anchor review (`codex_engine_sleeve_verdict.txt`) was still running at handoff — read it and apply any blockers.**

## KNOWN FOLLOW-UPS (none blocking)
1. **Engine codex verdict** — confirm `codex_engine_sleeve_verdict.txt` has no blockers (subtract-before-renorm / anchor-hold for arbitrary (P,σ)).
2. **Amendment path** — `plan_amendment/workers.py` rebuilds with 0% alternatives (safe default); it does NOT preserve a previously-sourced sleeve. Decide: preserve prior sleeve vs re-source on amend.
3. **Registry breadth** — only gold ETCs seeded. The team can only hold instruments whose facts are verified; to give it a real BTC/commodity universe, verify + seed those ISINs (or build the deferred "live issuer-page verification for off-registry picks" so unknowns can be confirmed at run time instead of rejected).
4. **DB persistence of the decision** — the sleeve decision's rationale+violations persist inside `target_allocation_json`; a dedicated `agent_reports`/`decision_phases` record (codex #8) is not yet written.
5. **Glidepath sigma** — `sigma_glidepath` still maps the alternatives label to the class constant 0.268 for the redistribution glide; the FI solver uses the sourced sigma, but the glide series doesn't. Minor.

## DO NOT
- Promote (`role="current"`) autonomously — promotion is the user's act.
- Re-introduce a hardcoded sleeve or instrument list.
- Let an unverified instrument become a holding (registry-absent ⇒ rejected by design).
