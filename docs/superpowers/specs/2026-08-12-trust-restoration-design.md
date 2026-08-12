# Trust restoration — one pass to close the 2026-08-12 audit

**Status:** design, awaiting go/no-go from Ariel.
**Evidence base:** `docs/superpowers/INDEX.md` (94 docs audited against code).
**North star served:** *right, current, self-consistent; always-on; the user is not the expert.*

---

## 1. The opens are four root causes, not twenty items

Sorting the audit findings by *cause* rather than by symptom collapses them:

| Root cause | Findings it explains |
|---|---|
| **R1 — absence of a result is read as a passing result** | codex math gate `(None,None)`; whole-artifact reader `None ⇒ _reader_ok`; digest `ok` while skipped; `can_publish_plan` bypassed on exception; `/refine` invariants inert without `post_doc`; instage gate logs but never blocks; FX freshness advisory not hard; spine gate DORMANT |
| **R2 — built subsystems are not wired to each other** | unmanaged NVDA absent from analyst input; expense DB absent from plan burn; deconcentration optimizer not API-exposed; `/overview` has no page route; `discovery_pick` not a funnel source; `build_figure_registry` never called |
| **R3 — canonical numbers are not single-sourced** | NVDA 58% / 8% / 12%; derivation-first Slice 6; prose outside the derivation graph |
| **R4 — the system cannot speak** | SMTP unconfigured; 0 push subscribers; Discord dead but still shown as a live source; 59 undelivered proposals |

R1 is the keystone. It is also *why the others survived*: every one of R2/R3/R4 would have been caught by a mechanism that exists, if that mechanism had failed loudly instead of silently.

**Doctrine check.** Fixing R1 is not the whack-a-mole antipattern. It does not judge whether a decision is *good* — it asserts that a verifier which did not run cannot be counted as having passed. That is the inviolable-arithmetic/liveness floor, exactly where determinism belongs. Judgment stays with the fleet.

---

## 2. Phase 0 — `DID_NOT_RUN ≠ PASS` (the keystone)

**One shared contract**, `argosy/quality/verification.py`:

```python
class GateStatus(StrEnum):
    PASS = "pass"
    BLOCK = "block"
    DID_NOT_RUN = "did_not_run"      # timeout, missing kit, unconfigured, exception

@dataclass(frozen=True)
class GateOutcome:
    gate: str                 # "codex_math", "whole_artifact_reader", ...
    status: GateStatus
    detail: str               # why
    override_by: str | None = None   # explicit human override, never implicit
```

Rule: **for any promotion / publish / deliver decision, `DID_NOT_RUN` is treated as `BLOCK`.**

Call sites to convert (each its own commit, Sol-reviewed):

| Site | Today | After |
|---|---|---|
| `orchestrator.py:2657` | `_reader_ok = (_reader_verdict is None or …)` | `None` → `DID_NOT_RUN` → not approvable |
| `codex_second_opinion.py` | returns `(None,None)` fail-soft | returns `GateOutcome(DID_NOT_RUN)` |
| `plan.py:3839-3847` | `except:` → bare `evaluate_promotion` | exception → `DID_NOT_RUN` → refuse promote |
| `plan.py:5688` `/refine` | no `post_doc` ⇒ invariants inert | compute `post_doc`; absent ⇒ `DID_NOT_RUN` |
| `email_digest` | `status='ok'`, `send_status='skipped'` | unsent ⇒ job status `error` |
| `period_directive.py:205` | `fx_stale` advisory | stale ⇒ `DID_NOT_RUN` on FX-dependent legs |
| `instage_gate` | logs only | **stays warn-only** — deliberate; it is a lint, not a floor |

**Escape hatch, by design:** an operator may override any `DID_NOT_RUN`, but only via an explicit recorded `override_by` + reason persisted on the decision run. Silent override becomes impossible; deliberate override stays cheap.

**Verification receipt (the surface that makes this real).** Persist one row per gate per decision run and render it: *"This plan was approved with 5 of 6 gates passed; the coherence reader DID NOT RUN (codex timeout)."* Ariel should never again have to ask whether a check ran. Add to the plan draft header and `/admin/jobs`.

**Which agent should have caught this?** None — no agent owns "did the verifier execute." That is precisely why it needs the deterministic floor, and why the receipt must be visible rather than another silent check.

---

## 3. Phase 1 — reconnect the pipes (small, independent, high value)

Each is a wire between two things that already work.

| # | Fix | Where | Note |
|---|---|---|---|
| 1A | Unmanaged NVDA → ConcentrationAnalyst input | `plan_synthesis/inputs.py::_summarize_positions` (:1287/:1310) | **Check branch `worktree-agent-afb7cdd94` first — likely already written.** Precondition A for the regen. |
| 1B | Real spend → plan burn | `inputs.py:1006 _assemble_household_budget_payload` | Aggregate `ExpenseTransaction` over a stated window; keep `identity_yaml` as explicit fallback **with provenance in the payload** so the analyst knows which it got. Also fix SDD §6:544, which currently claims this already happens. |
| 1C | `/overview` page route + nav entry | `ui/src/app/overview/page.tsx` | Backend + 10 components already exist. Exercise `find_unauthorized_numbers` in e2e before shipping — it has never run against a live page. |
| 1D | Deconcentration optimizer → API + bind headline | `retirement.py`, `derived_cache.py` | Confirm whether the canonical CGT haircut is the optimizer's output or a static figure. If static, that alone can move the safe-retirement age by 1–2 years. |
| 1E | Cal / Amex / Diners parsers | `expense_ingest/parsers/` | Only if Ariel or Noga actually holds those cards — **ask before building**. |

## 4. Phase 2 — one number, one source (R3)

The 58/8/12 split is the north star's own sentence failing. Do **not** attempt full Slice 6 (routing all from-scratch synthesis through `PlanDecisionModel`) in this pass — that is a multi-session rewrite.

**Scoped fix:** bind only the figures with known live contradictions — NVDA cap, net worth, FI-crossing year, RSU retention — to canonical resolver nodes at *render* time, extending the existing `live_surfaces.py` cutover that already works for three of them. Add IPS prose to that set. Then add a cheap cross-surface assertion to the verification receipt: *if the same figure key renders two values anywhere, that is a `BLOCK`.*

That converts "self-consistent" from an aspiration into a gate, without rebuilding the generator.

## 5. Phase 3 — make the caps real (R3/safety)

`risk_preflight.py:180 check_concentration_cap` has no sector logic; the 35% tech and 15% single-name caps are prose. Minimal FM-OBJ-7 slice: an `instrument_classification` table (ticker → sector), a typed cap rule, and a sector check in preflight. Arithmetic floor — legitimate determinism. Skip the full `PlanPolicy` union for now.

## 6. Phase 4 — the system speaks again (R4)

- Configure `ARGOSY_SMTP_*`; make an unsent digest a **failed** job (Phase 0).
- Get one real push subscription registered end-to-end (`notification_subscriptions` = 0 today).
- Mark Discord **dead** in `source_reliability` so 434 stale predictions stop presenting as a live feed.
- Then drain the 59 open proposals — most are probably stale; that backlog is itself a signal nobody was being told.

## 7. Phase 5 — regenerate the plan, then dual-review

Depends on 0, 1A, 1B, 1D, 2. Sequence: fix → restart on new SHA → `POST /api/advisor/check-in` → blind math re-derivation must **PASS** (not `DID_NOT_RUN`) → present draft to Ariel → **never auto-accept**. Re-run `canonical_feasible_dual_track` on the true book.

Done means: a fresh plan whose NVDA weight, estate, net worth and burn all reconcile to the live book, with a receipt showing every gate ran.

---

## 8. Explicitly OUT of scope (name them, don't smuggle them in)

Each is a real gap and a separate goal: the **insurance agent fleet** (NOT_BUILT — currently a 10×-income heuristic); the **dynamic allocation owner** (`allocation_path.py` + `allocation_strategist`, absent — no owner for sequence-risk glide in the 47–57 bridge); **scoped re-synthesis** (every objection round costs a full 5-phase run); **IBKR / broker write path**; **bidirectional messaging**; **full Slice 6**. Of these, the dynamic allocation owner is the one most load-bearing for "earliest *safe* retirement" and should be the next goal after this pass.

## 9. Order, and why

```
Phase 0 (keystone)  →  Phase 1 wires  →  Phase 2 one-number  →  Phase 3 caps
                                      ↘  Phase 4 delivery (independent)
                                                     ↘  Phase 5 regen + review
```

Phase 0 first, because until gates fail closed, every later fix is unverifiable by the same means that already failed. Phase 4 can run in parallel — it touches nothing else. Phase 5 last, because it consumes all of it.

**Suggested granularity:** one branch, ~10–12 commits, money/decision units each Sol-reviewed per CLAUDE.md; UI and read-projections skip Sol.

## 10. How we know the pass worked

1. Kill codex, run a synthesis → the plan **refuses to promote** and the receipt names the gate. (Today it silently approves.)
2. Break SMTP → digest job goes **red**. (Today it goes green.)
3. Query NVDA cap from every surface → **one** number.
4. `/overview` opens and its numbers reconcile to `/plan`.
5. A regenerated plan's burn traces to `ExpenseTransaction` rows, not to `identity_yaml`.
6. A buy that would breach the 35% tech cap is **refused** at preflight.
