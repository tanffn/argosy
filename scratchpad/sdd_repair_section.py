from pathlib import Path

p = Path("D:/Projects/financial-advisor/docs/design/SDD.md")
s = p.read_text(encoding="utf-8")

ANCHOR = "## 17. Provenance & accountability"

SECTION = """## 16.5 Plan repair — deterministic self-heal, in place

A blocked draft is repaired **in place**, never regenerated. This section
exists because regeneration was tried nine times and does not converge.

### Why regeneration fails

Across nine review rounds on drafts 117-125, **not one blocking finding was a
wrong financial judgement**. Every one was a document-consistency defect: one
concept published as two typed digits — the NVDA cap as 12 and 13, the FI basis
as ILS 311,584 and ILS 300,000, the NVDA weight as 62.5% and 56.8%, the cash
sleeve as 8.4% and 9.1%, the sale programme as 8,918 and 9,417.

The plan is LLM-authored prose that must satisfy a machine-checked consistency
contract, and **nothing enforces that contract at authoring time**. A full
regeneration re-types every digit and therefore re-rolls every contradiction:
it fixes the ones that were named and seeds new ones elsewhere. The gate says
so itself — draft 125 carried 91 `fact_placeholder_protocol` findings, each of
the form *"literal X matches registry fact Y, emit the token instead"*, and
those are NON-blocking, so they accumulate until two of them disagree.

Two mechanisms follow from that, and they are complementary:

* **Construction rule (§20, planned):** reject every raw financial literal at
  phase-3 slice validation unless it is a registered `{{fact:key}}` token, and
  let the deterministic renderer insert digits after every slice passes. This
  prevents *unbound digits*.
* **Repair loop (this section):** fix coherence among already-bound statements
  — missing qualifiers, conditional-vs-unconditional actions, as-of labels,
  incomplete structured projections. This is still required after the
  construction rule lands, and covers legacy plans and manual amendments.

### The service

`argosy/services/plan_repair.py`:

```python
repair_plan_version(
    session, *, plan_version_id: int, trigger: RepairTrigger,
    allow_current: bool = False, include_bounded: bool = True,
) -> PlanRepairResult
```

Edits **one persisted `PlanVersion` in place** — it never creates another row.
Five handlers, applied in order, each returning a `HandlerReceipt`:

| handler | repairs |
|---|---|
| `registry_literals_to_fact_tokens` | literals matching a registry fact -> the token |
| `fi_affirmative_claim_qualifier` | "FI is reached" without a same-sentence FX qualifier |
| `allocation_full_projection` | allocation surfaces rebuilt from `TargetAllocationDoc` |
| `contradiction_policy` | one concept, two values (policy below) |
| `bounded_single_slice_qualifiers` | bucket-B prose integration, one slice, no new numbers |

### Contradiction policy

Never majority-vote the prose. Resolution depends on what the two values ARE:

* one token + one stale literal -> replace the loser with the token;
* both raw representations of one registered concept -> replace both with the token;
* different bases, timestamps, currencies or scenarios -> keep both, add
  deterministic labels;
* no single canonical owner, units disagree, or choosing a winner changes
  policy -> **refuse and escalate**.

A repair loop that resolves genuine contradictions by picking a side is a
machine for making broken plans look clean. Refusal is a first-class outcome:
`PARTIAL_NEEDS_AUTHORING` is an honest result, `REPAIRED` is not always
available.

### Guards — all four are load-bearing

1. **`role='current'` refuses** without an explicit `allow_current`, which is
   deliberately independent of `trigger`: saying "manual" does not confer
   authority to rewrite the accepted plan.
2. **Leakage** reverts a handler on a higher count OR a new fingerprint, and
   is *never* overridden. An artifact containing `[derivation pending]` must
   not ship.
3. **Per-handler `gate_delta`** — computed inside `guarded_apply`; any handler
   with a positive delta is reverted individually. Without this, one bad
   handler poisons the whole run and the failure is undiagnosable from outside.
4. **Aggregate abort** — if the complete deterministic gate regresses, the
   whole transaction raises `PlanRepairRefused` rather than persisting a
   degraded plan.

`plan_repair_attempts` (migration `0105`) is an immutable receipt per attempt:
base and result artifact sha256, before/after violation counts, affected
fields, and the instructions applied. A same-row mutation without an audit
receipt is not acceptable on this data.

### THE RENDERER DEFECT — read before touching any plan round trip

Diagnosing the first repair build surfaced something larger than the repair
loop. On plan 125, a **no-edit round trip** of the structured JSON through the
current renderer changes the already-persisted long-horizon appendices:

```text
plain re-render: 147 -> 158 violations
  fact_placeholder_protocol: 91 -> 101
  headline_numeric_source:    5 ->   6
```

Rendering a plan without editing it makes it worse. Every repair handler
inherited that +11, which is why the first build reported 147 -> 157 (11 added,
1 genuinely cleared). The revert path re-rendered as well, so **the rollback
wrote the regressed bytes back** — a guard that re-renders on rollback is not a
rollback.

Current mitigation: snapshot the exact persisted bytes, render only the
surfaces a handler names, and freeze appendices that do not round-trip cleanly.
That is a containment, not a fix. **Anything that round-trips a plan through
the renderer is exposed to this**, not just repair. The proper fix is a
surgical section-block projection into the existing appendix, or a separately
reviewed appendix migration.

### Measured behaviour (plan 125, full DB clone, 2026-08-25)

```text
147 -> 145   status: PARTIAL_NEEDS_AUTHORING   leakage: [] -> []
  registry_literals_to_fact_tokens  147->147  +0   applied 1
  fi_affirmative_claim_qualifier    147->146  -1   applied 3
  allocation_full_projection        146->145  -1   applied 16
  contradiction_policy              145->145  +0   applied 4
  bounded_single_slice_qualifiers   145->145  +0   applied 2
cleared: ips_allocation_sum 1->0, fi_fx_shock_sufficiency 1->0
refused: moonshot C3 disclosure; NVDA 8,918-ceiling vs 3,378-future-vests;
         two undated NVDA prices; unquantified per-decision cap
```

The 91 remaining `fact_placeholder_protocol` findings sit largely in the frozen
appendices and need the surgical projection above — the same-row handler must
not quietly regenerate appendix bytes to reach them.

### Operational notes

* Migration `0105` must be applied before repair runs outside a clone.
* A **reverted** receipt deliberately retains the candidate's gate numbers so
  the cause stays diagnosable. Any consumer reading `changed_surfaces` or
  `addressed` MUST also check `reverted`, or it will report a failed repair as
  a success.
* Dry-run against a DB clone, never live: `scratchpad/repair_dryrun.py`.

"""

assert s.count(ANCHOR) == 1, "anchor not found"
s = s.replace(ANCHOR, SECTION + ANCHOR, 1)
p.write_text(s, encoding="utf-8")

c = p.read_text(encoding="utf-8")
assert "## 16.5 Plan repair" in c
assert "THE RENDERER DEFECT" in c
assert "147 -> 158" in c
print("SDD updated:", len(c.splitlines()), "lines")
