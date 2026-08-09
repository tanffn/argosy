# Argosy Operating Model — one closed-loop system (spec for adversarial review)

**Status:** DESIGN ONLY — not implemented. Written to be broken by an independent
reviewer. Re-derive every data claim against `db/argosy.db` (read-only:
`sqlite3.connect("file:db/argosy.db?mode=ro", uri=True)`) and the code. It is
better to surface a real flaw than to agree.

**Companion (already on master):** `docs/design/performance_scorecard_design.md`
(the realized-return-vs-benchmark scorecard; this spec references it as
Component C rather than restating it).

---

## 1. Purpose — the one goal, and the one test

Argosy exists to **maximize the family's wealth and reach the earliest *safe*
retirement** ("retire from work, not from life"; conservatism that quietly costs
retirement-years is the anti-goal). The test applied to every component here:
*does it move a real dollar or protect one, and can we prove it?*

This spec unifies three asks into **one system**, because they are not separate
features — they are links in a single loop and each is worthless without the
others:

- **#2** — evaluate *every* holding (with the right question per asset type).
- **#3** — turn every decision into a testable bet, grade it against the
  alternative, and learn why we were right or wrong.
- **Proof** — measure the whole thing as realized return vs benchmark (Component
  C, the scorecard).

The system's historical failure mode (verified this session: a $2.4M/59% book
erasure and 1,065 masked async errors, both reported as success) is a *broken
arrow that still reported green*. The architecture below is built so that this
class of failure is structurally impossible to launder into a confident number —
not caught by a per-symptom detector, but blocked by a single contract that every
component must read through.

## 2. The closed loop (system architecture) — everything passes through the spine

The organizing principle of the whole system is **§2A: the validated event
spine.** A, B, C, and E do not read raw snapshots, raw ledgers, or raw metadata.
They read **only** validated spine records. A spine record cannot be constructed
unless every required provenance field is present and every referenced integrity
verdict passed. **A missing required field yields NO RESULT — not a "degraded
confidence" number.** That single rule is what collapses the four separate
laundering paths the audit found (dirty book, B/C divergence, zero-share flow,
silent unscorable skip) into one enforced gate.

```
   RAW INPUTS (snapshots, ledgers, fills, metadata, benchmark prices)
        │   each carries an integrity verdict (conservation gate, §3)
        ▼
 ┌───────────────────────── §2A. THE SPINE ─────────────────────────┐
 │  validated_snapshot (per-item-bound facts) + …_period (window)    │
 │  observed_decision → validated_decision → …_outcome (append-only) │
 │  immutable, machine-readable, fully-provenanced.                  │
 │  Construction FAILS LOUD if any required field is missing or any  │
 │  referenced integrity verdict did not pass. No partial records;   │
 │  no record is ever mutated after authoring.                       │
 └───────┬───────────────┬───────────────┬───────────────┬──────────┘
         ▼               ▼               ▼               ▼
   A. EVALUATE      B. RECORD +     C. SCORECARD    E. LEARN
   every holding    GRADE the bet   realized return  post-mortem →
   (per asset type) (testable at    vs benchmark     source weights,
                     birth)         + attribution    actionability gate

   A ─► (emits event) ─► spine ─► B grades at maturity ─► spine
   B + C read the SAME spine periods and MUST reconcile (§8) or fail loud
   E reads graded spine events only ─► feeds back into A's actionability gate
```

**Invariant:** every arrow reads from the spine, and every record used **as valid
input to a number** is either fully valid or absent. There is no third "present
but suspect" state that flows downstream as a number — suspicion resolves to
either a valid record or a loud quarantine. This is distinct from *observation*
records (§2A), which immutably capture what was seen — **including the reasons it
failed validation** — so a suspect or unscorable decision is never silently
dropped and never mislabeled as valid input; it is retained as an observation and
is simply not eligible to become a validated grading input. "Silent success" is
therefore not a display choice we have to police; it is a state the data model
cannot represent.

---

## 2A. The validated event spine (the single foundational contract)

The spine is a set of append-only, immutable, machine-readable record types — the
*only* inputs A/B/C/E may read. No spine record can be edited after authoring (the
audit found hindsight mutation of prediction versions; the spine forbids it
structurally). Two distinctions are load-bearing and were previously conflated
into single mutable records:

1. **Observation vs. validated-input.** An *observation* record immutably captures
   what was seen — **including the reasons it failed validation.** A *validated*
   record additionally attests that every field required to compute a number is
   present and every referenced integrity verdict passed. An observation that
   failed validation is never silently dropped (that would make coverage look
   cleaner than reality) and never mislabeled as valid input (that would launder a
   dirty book into a confident number). It is retained, immutably, as an
   observation carrying its failure reasons, and is simply not eligible to become a
   validated grading input.
2. **Event vs. outcome.** A decision's *terms* are frozen at authoring and never
   change. Its *grade* arrives later. These are therefore separate records: the
   graded outcome is **appended**, never written back into the event. "Scoring
   mutates the event" is forbidden by construction.

### `validated_snapshot` — the canonical point-in-time book (NEW)
The immutable, normalized position/account facts a component reads **instead of**
raw `portfolio_snapshots.positions_json`. A/B/C never dereference the raw JSON;
they read this record, which is A's canonical point-in-time input. One record per
snapshot that passed the integrity gate. Required fields — **all mandatory; any
absent ⇒ no `validated_snapshot` and no component evaluates that book state:**

- `snapshot_id` — the raw input row it normalizes.
- `positions[]` — normalized `{instrument, account, shares, price, price_as_of,
  currency, value_local, value_usd}` per position. These are the canonical facts A
  evaluates and B/C compute returns from.
- `accounts[]` — normalized `{account_id, custodian, cash_by_currency}`.
- `integrity_verdict_id` — a **passed** conservation verdict (§3). Failed or
  missing ⇒ no `validated_snapshot`.
- `per_item_integrity_binding` — a cryptographic commitment over **each**
  normalized position and **each** account individually (e.g. a Merkle root over
  the per-item facts, with the per-item hashes retained), stored in and attested by
  the record. This binding proves facts **did not change after commitment**; it does
  **not**, on its own, prove the committed facts were **complete or correct at
  ingest**. That limit is the residual sub-threshold hole and is closed by the two
  fields below (`item_source_binding`, `expected_set_completeness`). The per-item
  binding attests every position and account discretely; any later read whose facts
  do not hash-match the binding is a **detected corruption, not a silent pass**. A
  `validated_snapshot_period` or a decision may only be constructed against a
  `validated_snapshot` whose per-item binding verifies.
- `item_source_binding` — for **each** normalized position and account, a reference
  to an **INDEPENDENT source record** (a broker-signed export / source-manifest row
  carrying that item's shares/price/cash) that the normalized item was reconciled
  against at ingest. **This is the actual fix for the sub-threshold-corruption
  hole.** The conservation gate (§3) only catches shrink *beyond an aggregate
  threshold*, and the Merkle binding only proves post-commitment immutability — so an
  adapter that silently zeroes one 0.5%-weight position **before** the snapshot is
  built passes the aggregate gate and is then faithfully (and permanently) committed
  by the binding. Binding each item to an independent source record catches that
  ingest-time corruption, because the zeroed item no longer matches its signed
  source. A normalized item with **no** independent source record is
  `source:unbound` and cannot contribute to a proof-grade `validated_snapshot`.
- `expected_set_completeness` — a proof that **no position is silently absent**: the
  set of normalized positions/accounts equals the expected set derived from the
  independent source manifest (prior book membership + the manifest's own line
  items), with any expected-but-missing item enumerated explicitly. Aggregate
  immutability cannot see a position that was dropped *before* commitment; this field
  makes a silent omission a **named missing item**, not a clean-looking smaller book.
- `fx_snapshot_id` — the dated FX rows used for the NIS/USD split.

**HARD PREREQUISITE — independent source binding is a data-availability gate, not a
prose guarantee.** A `validated_snapshot` is **proof-grade only when every item
carries an `item_source_binding` and `expected_set_completeness` verifies against an
independent source manifest.** That manifest (broker-signed exports / a per-account
source-record feed) **does not exist for all accounts yet** (see §7). Until it does,
a book that lacks item-level source binding may produce **only a diagnostic
snapshot** — usable for the pre-spine diagnostic path (Component C §6 0a), never a
proof-grade `validated_snapshot` and never an input to a headline number, a
`validated_decision`, or learning. No prose closes this; the missing manifest is the
gate.

### `validated_snapshot_period` — one window over two `validated_snapshot`s
One canonical record per period boundary (t0→t1) of the liquid book. It
*references* facts (two `validated_snapshot`s), it does not restate them. Required
fields — **all mandatory; any absent ⇒ the period is not constructed and no
component produces a number for it:**

- `period_id`, `t0_validated_snapshot_id`, `t1_validated_snapshot_id` — the two
  canonical `validated_snapshot` records (each already integrity- and per-item-
  verified, above).
- `coverage_denominator` — count + value of positions in scope, and the count +
  value of any position that could **not** be evaluated (so coverage can never
  look cleaner than reality).
- `benchmark_version` — the exact benchmark/policy-index revision used.
- `flow_reconciliation_status` — see the strengthened definition immediately
  below; **every** share/cash delta must carry machine-verifiable provenance or the
  period is quarantined.
- `expected_event_set_completeness` — see the strengthened definition below;
  proof-grade periods require an **independent broker activity/transaction manifest**
  proving the intra-period EVENT SET is complete. Absent it, the period is diagnostic,
  not proof.
- `price_freshness` — max staleness of any `price_as_of` in the period; over
  threshold ⇒ no period.

**`flow_reconciliation_status` — provenance, not endpoints.** Snapshot endpoints
alone cannot *prove* a return: the t0/t1 values are identical whether a share was
bought at $100 near t0 or at $110 just before t1, and a `proposals` row carries no
fill price / timestamp / quantity-to-delta binding, so a stale proposal can
"bless" a corrupted deletion. Therefore this field must reference, for **every**
share/cash delta between t0 and t1, a machine-verifiable provenance record — an
exact reconciled **fill / vest / transfer / corporate-action ID carrying a dated
amount and price** — or the delta is unreconciled and the period is quarantined
fail-loud. **Proof-quality TWR requires dated flows or broker-authored
NAV/transaction data. Without them the period is a *diagnostic*, not proof — never
a headline "return".** (This is the shared contract the scorecard §2.2 reads.)

**`expected_event_set_completeness` — provenance of observed deltas is NOT proof the
intra-period EVENT SET is complete.** `flow_reconciliation_status` proves a dated
provenance record for every share/cash delta *that is visible in the endpoints*. It
cannot see an event that leaves the endpoints unchanged. **NET-ZERO activity is
invisible to endpoint deltas** — and that is the motivating failure: a position
**sold then repurchased within the period** (offsetting cash) leaves identical
begin/end shares and identical cash, so every endpoint / per-item-binding / delta
check passes while the system wrongly treats the position as **continuously held**.
That corrupts B's effective holding windows (§2A(b), §8) and C's selection
attribution (scorecard §2.5) on a period that otherwise looks *proof-valid*. This is
the period analogue of `validated_snapshot`'s `expected_set_completeness`: the latter
proves no *position* is silently absent from a book; this proves no *event* is
silently absent from a window. Therefore a **proof-grade** period requires an
**INDEPENDENT broker activity/transaction manifest** (or broker-authored
NAV/activity data) attesting the **complete set of intra-period events** — every
buy, sell, vest, transfer, and corporate action between t0 and t1 — not merely dated
provenance for the observed net deltas. **Even after `fills` are populated, a period
WITHOUT event-set completeness is DIAGNOSTIC, never proof**: populated fills prove
the deltas we *saw* are dated, but only a broker-authored event manifest proves
there were no *other* events (the net-zero round-trip) we never saw. A period whose
event set cannot be independently closed is quarantined to the diagnostic path (§9),
never a headline "return". (This is the shared contract the scorecard §2.2 reads.)

### The decision records — OBSERVED → VALIDATED → OUTCOME (three records)
A single mutable `validated_decision_event` was self-contradictory: this section
calls spine records fully-valid-or-absent and immutable, yet the old `scored_status`
field changed after authoring, and an unscorable decision was written as though it
were valid input. Split into three:

**(a) `observed_decision` — immutable, always written. This is the DECISION
OBSERVATION, NOT A's analysis.** It records that a decision occurred — a
BUY/SELL/TRIM/ADD, a HOLD re-affirmation, or an A2 vehicle switch, whether taken by
a human or emitted by an agent — capturing what was decided **and any
validation-failure reasons.** It is deliberately scoped as the *observation of a
decision*, **distinct from A's evaluative machinery** (the analyst/debate/trader
fleet, §4), which runs **only** on a `validated_snapshot` and never on raw input.
An unscorable or ambiguous decision lives **here** — recorded, never dropped, never
promoted to valid grading input.

**PRE-VALIDATION path — a dirty-book decision is COUNTED but NON-ACTIONABLE.** The
contradiction to resolve: §2/§3 forbid A from reading a raw snapshot and forbid a
spine record on a failed book, yet a decision made *against* a dirty/failed book
must still be recorded (dropping it is a coverage hole). Resolution, stated as a
hard rule:
1. **The decision is always observed.** An `observed_decision` is written with
   `observed_source_input_id` referencing the raw/diagnostic snapshot it was made
   against and `validation_status_at_birth = unvalidated:dirty-book`. It is counted
   in the coverage denominator (no coverage hole).
2. **A's evaluative machinery does NOT run on the raw book.** A's analysis fleet is
   never invoked on raw input; there is no code path that produces an *actionable A
   verdict* from a dirty book. If any evaluative output is produced against
   un-validated input (e.g. a diagnostic re-derivation), it is marked
   **`actionability = non-actionable`** and is inert.
3. **A dirty-book observation can never become actionable.** Because no
   `validated_snapshot` exists for a dirty book, **no `validated_decision` can be
   constructed** (the (b) record requires `input_validated_snapshot_id`), and the
   late-attachment identity constraint (below) rejects back-attaching a *different*
   clean book. A non-actionable observation and any non-actionable evaluative output
   **cannot feed B, C, E, or any proof / recommendation surface** — those consumers
   read `validated_decision` / `validated_decision_outcome` only. The observation is
   therefore *counted* (in coverage) yet *inert* (never a graded input, never a
   published number) unless and until the SAME book is cleaned to a
   `validated_snapshot` and a `validated_decision` is constructed from the
   birth-frozen terms (an external-fact promotion, (b)).

Fields:
- `observed_decision_id`, `authored_at`, `instrument`, `decision_kind`, `verdict`,
  `conviction`.
- `predictive_terms_at_birth` — the forward, hindsight-vulnerable terms
  (`target_band`, `alternative_at_birth`, `stop`, `falsifiers_json`,
  `revisit_triggers_json`, `evaluation_due_at`), **each frozen at authoring or
  written as an EXPLICIT null.** These are the terms that determine whether the bet
  can ever be graded, and they must be pinned **here**, at birth. **A predictive term
  that was absent at authoring is an explicit null, and an explicit null is
  permanent** — it is never filled in later. This is the fix for late-promotion
  hindsight: because these terms live on the immutable observation and cannot be
  edited, a target band cannot be back-filled on an Aug-30 read for an Aug-1 HOLD.
- `observed_source_input_id` — **NULLABLE.** A reference to whatever book state the
  decision was actually made against — a `validated_snapshot` when the book was
  clean, **or** a raw/diagnostic snapshot ID when it was not. This exists so a
  decision on a **failed (dirty) book is still observed**: `observed_decision` is
  always written, and a dirty book by definition has **no** `validated_snapshot`, so
  requiring `input_validated_snapshot_id` *here* was self-contradictory. The
  validated-snapshot reference is required only on the `validated_decision` record
  (below), never on the observation. This id also serves as the **birth-time input
  fingerprint** the late-promotion identity check reads (see `validated_decision`
  below): a decision may only be validated against the clean normalization of the
  *same* book it was actually authored on.
- `validation_status_at_birth` — `validated` | `unvalidated:<reason>` (e.g.
  `unvalidated:cost-basis-missing`, `unvalidated:target-band-absent`,
  `unvalidated:dirty-book`, `unvalidated:ambiguous-target`,
  `unvalidated:missing-predictive-term-at-birth`). This is an **immutable property of
  the observation recording its status AT AUTHORING**, **not** a mutable flag and
  **not** a live gradability signal. **CURRENT gradability is NOT read from this field
  and NEVER mutates it** — it is *derived* from the **existence of a valid
  `validated_decision` child** pointing at this observation. A late-arriving external
  fact (§2A(b)) that lets a `validated_decision` be constructed does **not** rewrite
  `validation_status_at_birth` (it stays `unvalidated:<reason>` forever, as an honest
  record of birth conditions); the observation simply *acquires* a validated child,
  and every consumer that asks "is this gradable now?" answers by looking for that
  child. This removes the old contradiction where an immutable `unvalidated` status
  either double-reported after a fact arrived or forced a forbidden mutation.

**(b) `validated_decision` — immutable terms, only when gradable.** Constructed
**only** when every field required to grade the bet is present. Missing any
required field ⇒ **no `validated_decision`** (the observation persists as
`unvalidated:<reason>`).

**Late attachment is allowed for MISSING FACTS, forbidden for MISSING PREDICTIONS.**
Two kinds of "missing input" are categorically different:
- A missing **fact** that exists independent of the decision (cost basis, a
  benchmark price, a clean book) can arrive later without hindsight risk — its value
  is not chosen with knowledge of the outcome. When it lands, a `validated_decision`
  may be constructed from the terms **already frozen at birth** and an outcome
  appended.
- A missing **predictive term** (`target_band`, `alternative_at_birth`, `stop`,
  falsifier, `evaluation_due_at`) is set with the decision and, if supplied late,
  would be chosen with hindsight. **Therefore: if any predictive term was an explicit
  null in `predictive_terms_at_birth` (i.e. absent at authoring), the decision can
  NEVER become a `validated_decision` — missing-at-birth = permanently unscorable,
  not retro-gradable.** Construction reads the frozen `predictive_terms_at_birth`
  ONLY; it may not accept a predictive term supplied after `authored_at`.

Required fields:
- `observed_decision_id` — the observation it validates. Its
  `predictive_terms_at_birth` must have **no explicit nulls** among the terms below,
  or no `validated_decision` is constructible.
- `instrument`, `decision_kind`, `verdict`, `conviction`.
- `input_validated_snapshot_id` — must reference a `validated_snapshot` whose
  per-item binding verified. **This field is required only here** (not on the
  observation, which may be authored on a dirty book — see `observed_source_input_id`
  above). **Late-attachment identity constraint (blocks attaching a different/later
  clean book).** When this is supplied late (the dirty book at birth was cleaned or a
  missing fact arrived), the referenced `validated_snapshot` MUST be the **validated
  normalization of the frozen `observed_source_input_id`** — i.e. it normalizes the
  *same* raw book state the decision was authored against (the birth-time input
  fingerprint must match). A `validated_snapshot` of a *different* or *later* book is
  **rejected**, so a clean book from another day can never be back-attached to launder
  a dirty-book decision into a graded one. **Corollary: a `nullable`/absent
  `observed_source_input_id` is non-promotable** — with no birth-time fingerprint to
  match, identity cannot be proven, so no `validated_decision` may attach a
  `validated_snapshot` to it; the observation stays permanently ungradable.
- `alternative_at_birth` — the best-in-class peer / index / vehicle this was chosen
  over, frozen at authoring (in `predictive_terms_at_birth`), plus the
  switching-cost tolerance band.
- `target_band`, `evaluation_due_at`, `stop` — the testable-at-birth terms (B1),
  copied from `predictive_terms_at_birth`. **Any that was null at birth ⇒ not a
  `validated_decision`, ever** (it stays an observation).
- `falsifiers_json`, `revisit_triggers_json` — the thesis-break conditions, frozen
  at birth.
- `cost_basis_completeness` — `full-lot` | `avg-price-only` | `none`. Grading
  logic reads this and **refuses** a tax-aware verdict unless `full-lot` (§5 B2, §4
  A2); a tax-dependent decision without full-lot basis stays an observation for the
  tax-aware grade.
- `metadata_freshness` — for A2 events, the age/source of the fee/tracking/AUM
  metadata the switch relied on; stale ⇒ the switch cannot be graded as proven.

**(c) `validated_decision_outcome` — append-only, exactly-once, fully-provenanced
grade.** Scoring attaches by **appending** an outcome record that references the
`validated_decision`; it **never mutates the event.** Fields:
- `outcome_id` — unique identity for this settled outcome (so E can dedupe and a
  retry cannot silently double-append).
- `validated_decision_id`, `scored_at`, `outcome_kind`, `vs_benchmark_delta`,
  `post_mortem_category`, `regime_tag`, `is_shadow`.
- **Full calculation provenance** — `evaluation_window_id` (the exact window IDs
  scored), `benchmark_version`, `exposure_mapping_version` (§8), and
  `calculator_version`. Every number the outcome asserts is reproducible from these.

**Exactly-once, enforced — not asserted.** "A `validated_decision` accrues exactly
one settled outcome" is a *guarantee the schema must enforce*, or a scoring-job retry
appends a second, differently-versioned outcome, both immutable, and E learns twice /
nondeterministically. Enforce it one of two ways:
- **Idempotency key (default):** a UNIQUE constraint on
  `(validated_decision_id, evaluation_window_id, benchmark_version,
  exposure_mapping_version, calculator_version)`. A retry with identical inputs is a
  no-op (same key); a *changed* input (new benchmark revision, new calculator) is a
  **new key**, which is not permitted to silently coexist — it must go through the
  supersession protocol below.
- **Explicit supersession:** to re-grade under a new version, append a new outcome
  carrying `supersedes_outcome_id` pointing at the prior settled outcome. **`is_current`
  is NOT a stored mutable flag on the outcome row** — flipping a prior outcome's stored
  `is_current` to false would *mutate* an append-only record (forbidden), and leaving it
  immutable would let two outcomes both claim current (the composite UNIQUE idempotency
  key does not prevent this — a changed calculator/benchmark is a *different* key, so
  both rows coexist legally). Instead, "current" is the row named by a **transactional mutable HEAD TABLE**, which
  is the **SOLE mandatory enforcement mechanism** — one row per `validated_decision`
  pointing at the current `outcome_id`. **A partial UNIQUE index over "un-superseded
  outcomes" is NOT used and is not implementable as stated:** "un-superseded" depends on
  the *absence* of another row whose `supersedes_outcome_id` points at this one, which an
  ordinary SQLite partial unique index cannot express; and a re-grade authored with
  `supersedes_outcome_id = NULL` would form a second disconnected root that such an index
  cannot forbid — leaving the head table merely *picking between* roots rather than
  *proving* a unique chain head. The head table proves uniqueness directly. Rules:
  - **The head table is the only mutable surface; outcome rows stay append-only/immutable.**
    E and every proof surface read `current outcome_id` **only** from the head table.
  - **One root per decision.** The first outcome for a `validated_decision` is authored
    with `supersedes_outcome_id = NULL` and **creates** that decision's head-table row in
    the same transaction. A second `supersedes_outcome_id = NULL` outcome for a decision
    that already has a head row is **rejected** — so no disconnected second root can form.
  - **Atomic compare-and-swap of the head.** A re-grade appends an outcome carrying
    `supersedes_outcome_id = <prior head outcome_id>` and, **in the same transaction**,
    CAS-advances the head row *from the prior head id to the new outcome_id*; the swap
    **fails if the head no longer equals the prior id** (a concurrent re-grade already
    moved it), so two racing re-grades cannot both win.
  - **Same-`validated_decision_id` linkage.** An outcome and its `supersedes_outcome_id`
    target MUST share the same `validated_decision_id` — a supersession may not cross
    decisions.
  - **Exactly one successor per superseded outcome** — a UNIQUE constraint on
    `supersedes_outcome_id` (no two outcomes may supersede the same prior outcome, so the
    chain cannot fork).
  - **Exactly one current head per decision** — guaranteed by the one-row-per-decision
    head table (PRIMARY KEY / UNIQUE on `validated_decision_id`) plus the CAS above.

  E and every proof surface read **only** the head-table `outcome_id`, so learning is
  deterministic even though every outcome row is append-only and never mutated.

A shadow (paper) bet accrues an outcome with `is_shadow=true` under the same rules.
Because the grade is a separate append, the outcome rows are never mutated, and the
current head is the single-mutable-head-table singleton rather than a flag stored on
each immutable row, hindsight edits to a prediction and double-counted /
double-current learning are both structurally impossible.

**Why this is the biggest change:** with the spine in place, "integrity floor is
an assumption" (old finding 1), "B and C might disagree" (finding 4), "zero-share
deletion reads as a flow" (finding 5), and "unscorable decisions vanish or get
mislabeled" (findings 2 & 6) are no longer separate problems. They are the same
problem — *a component read something that wasn't a fully-provenanced valid record,
or a record changed after the fact.* The split spine — observation vs. validated
input, event vs. appended outcome, and the per-item integrity binding — makes each
impossible by construction.

---

## 3. Component 0 — Integrity floor as an ENFORCED INPUT CONTRACT (must land first)

A decision on a wrong book is worse than none. The integrity floor is **not a
background assumption that A/B/C rely on** — it is an input contract those
components actively read and enforce. A/B/C/E take a `validated_snapshot` /
`validated_snapshot_period` or a `validated_decision` as input; those records
**cannot be constructed** unless every part of the three-part gate below is
satisfied. There is no code path in which A/B/C see a raw snapshot directly.

**What makes a `validated_snapshot` — the COMPLETE THREE-PART GATE (consistent with
§2A).** A proof-grade `validated_snapshot` requires all three, not any one alone:

- **(a) Aggregate conservation verdict** (§3 gate 1) — a **passed** per-snapshot
  `integrity_verdict` that blocks a write dropping an account/location/currency or
  shrinking value/count beyond a threshold vs the prior live row.
- **(b) Post-commitment per-item binding** (`per_item_integrity_binding`, §2A) — a
  cryptographic commitment over each normalized position and account. **This proves
  only that facts DID NOT CHANGE AFTER COMMITMENT; it does NOT, on its own, prove the
  committed facts were complete or correct at ingest.** A sub-threshold partial
  corruption injected *before* the snapshot is built (one position zeroed, one
  account's cash understated) passes both (a) and (b) and is then faithfully — and
  permanently — committed. (b) alone does **not** close that hole.
- **(c) Independent item/set reconciliation** (`item_source_binding` +
  `expected_set_completeness`, §2A) — each normalized item reconciled at ingest to an
  **independent broker-signed source record**, and the position/account set proven
  equal to the expected set derived from the independent broker manifest. **This is
  the part that actually catches ingest-time / pre-commitment corruption**, because a
  zeroed or dropped item no longer matches its signed source or the expected set.

**Without (c) — i.e. when the independent source manifest does not yet exist for an
account (§7) — the snapshot is DIAGNOSTIC-ONLY:** pre-commitment corruption is NOT
caught by the aggregate verdict and the Merkle binding alone. Such a book may back
the pre-spine diagnostic path (Component C §6 0a) but is **never** a proof-grade
`validated_snapshot`, never an input to a headline number, a `validated_decision`, or
learning. The Merkle binding is not the fix for sub-threshold corruption — (c) is.

Two gates produce the verdicts the spine references (both from the 2026-08-08
audit; the repair of the *current* corrupt book is owned by the parallel repair
work — this spec consumes the verdicts, it does not re-implement the repair):

- **Conservation gate before persist, on ALL snapshot write paths.** Block or
  quarantine a write that drops an account/location, drops a cash currency, or
  shrinks total value / position count beyond a threshold vs the prior live row,
  and emit a per-snapshot `integrity_verdict`.
  (`portfolio_snapshot_store.persist_snapshot` is currently "intentionally dumb —
  always writes"; this makes it emit a verdict every write.) **The aggregate
  threshold is not sufficient on its own** — a sub-threshold partial corruption
  (one position zeroed, one account's cash understated) can pass it, and the
  post-commitment per-item binding cannot catch it either (it proves only
  immutability *after* commitment, not completeness/correctness *at* ingest). The
  hole is closed only by part (c) above — `item_source_binding` +
  `expected_set_completeness` reconciled against the independent broker manifest
  (§2A). All three parts — (a) aggregate verdict, (b) per-item binding, (c)
  independent reconciliation — together are what a proof-grade `validated_snapshot`
  requires; absent (c) the book is diagnostic-only.
- **Job status derived from work done, not just "did the tick raise?"** A tick
  that returns with `adapter_errors>0` / zero-work / non-empty `errors[]` closes
  degraded/error and its outputs get a failed verdict.

**Enforcement, not dependency:** the previous draft said "A/B/C assume these
hold." That is deleted. A/B/C **read the verdict and refuse dirty input** — a
period or event whose verdict is failed or missing does not become a **VALIDATED**
record (`validated_snapshot` / `validated_snapshot_period` / `validated_decision`),
and a non-validated record produces no downstream number. **This blocks the
VALIDATED records only — it does NOT drop the observation.** Consistent with §2A,
the `observed_decision` (and its `observed_source_input_id`) is **always written**,
including for a failed or dirty book, carrying its `validation_status_at_birth`
(e.g. `unvalidated:dirty-book`). A failed verdict therefore blocks promotion to a
`validated_decision` / `validated_snapshot`, but the immutable observation is still
recorded — nothing is silently dropped and sanitized out of coverage. **This is the
pre-validation path of §2A(a): A's evaluative fleet never runs on the raw book, any
evaluative output produced against un-validated input is marked
`actionability = non-actionable` and cannot feed B/C/E or any proof/recommendation
surface, and the dirty-book observation is COUNTED (coverage) yet INERT (never a
graded input) — so no coverage hole opens and no raw read is licensed.** The failure
state is loud and visible in the coverage denominator, never a green cell over a
broken input and never a decision that quietly disappeared because its book failed.

---

## 4. Component A — Evaluate every holding (the right question per asset type)

"Evaluate every position" is three questions, not one. A evaluates the canonical
`validated_snapshot` facts (§2A), never raw `positions_json`, and every stance it
emits is written as an `observed_decision` — promoted to a `validated_decision`
only when it is gradable (§2A) — on a book that passed conservation and whose
per-item binding verified.

### A1. Single-name stock (category C) → "is the company thesis intact?" — EXISTS
The per-ticker decision fleet (`argosy/decisions/flow.py` via
`run_deep_decision`): analysts (fundamentals/news/sentiment/macro) → bull vs bear
debate → trader → a settled **verdict ∈ {BUY, ADD, HOLD, TRIM, SELL}** +
conviction + **falsifiers + typed revisit-triggers** (shipped this session, in
`verdicts.falsifiers_json` / `revisit_triggers_json`). Category C sets the
*target weight*, concentration cap, and estate/FX gates — it is not the thesis.
Falsifiers are FUNDAMENTAL-only, forward, and a trigger exists only if one reading
confirms it (else qualitative). **This piece works on clean data.** Coverage today
is single-names only.

### A2. ETF / fund (category C) → "is this the best *vehicle* for C?" — TO BUILD
A basket has no company thesis; a BUY/HOLD/SELL verdict is the wrong frame. Split
into two questions:

- **Allocation** (the plan's job, regime-robust, changes rarely): does category C
  still deserve its `target_pct`? Read from `plan_versions.target_allocation_json`
  (`classes[].target_pct`). Out of scope for A2 — it is Component-plan territory.
- **Vehicle selection (the new build):** is the held instrument X the best vehicle
  for C, or is there a **Y with the same exposure but a better score** on:
  1. **Cost** — total expense ratio.
  2. **Tracking** — tracking difference/error vs the category index.
  3. **Domicile / estate** — UCITS (Irish) vs US-domiciled → US-situs estate-tax
     exposure (a first-order concern for this family; see
     `feedback_canonical_allocation_ucits_preferred`).
  4. **Tax** — distributing vs accumulating; withholding treaty treatment.
  5. **Liquidity** — AUM, spread, volume.

- **MANDATORY equivalence gate — before any "switch" is even a candidate.** The
  existing exposure tags are too coarse to establish equivalence — VOO and
  equal-weight XZEW are both "Broad Index / US". Equivalence has **two independent
  sub-gates, both mandatory**; a candidate must clear BOTH:

  1. **INDEX-IDENTITY gate (discrete facts — checked first and independently).**
     Overlap and correlation do **not** imply same index: an ESG-screened or
     currency-hedged fund can post high overlap and high correlation in a quiet
     window yet carry a hidden screen/hedge/factor bet that only shows up in the
     tail. Therefore the candidate Y must match the held X on each of these as
     **discrete, individually-compared facts** — correlation/overlap may **not**
     proxy for any of them:
     - **index identity / methodology** (the exact tracked index and its rules);
     - **weighting method** (market-cap vs equal-weight vs fundamental);
     - **ESG / screens** (any exclusion or inclusion screen);
     - **replication method** (physical/full vs sampled vs synthetic/swap-based);
     - **hedge policy** (unhedged vs currency-hedged share class).
     **A mismatch on ANY one = rejected as a hidden factor bet, regardless of how
     high its overlap or correlation is.** This gate does not rank; it admits or
     rejects.
  2. **QUANTITATIVE gate (survivors of gate 1 only).** (a) **holdings overlap** ≥ a
     threshold on the top-N constituents and total weight, AND (b) **return
     correlation** ≥ a threshold over a common window. This is a *secondary*
     confirmation that two same-index funds actually track alike — never a
     substitute for the identity facts above.

  The five-axis cost/tracking/tax scoring runs *only on candidates that clear both
  sub-gates.* Equivalence is a hard precondition, not a fleet opinion.

- **Output:** a per-sleeve recommendation `{keep X}` or `{switch X→Y}`. The
  economic size of a switch is a **tax-aware switching cost** (realized CGT on the
  embedded gain + spread) compared to the annualized benefit (fee + tracking + tax
  saving). A switch clears only when benefit beats switching cost with margin —
  the same discipline as the HOLD-grading rule in B.

- **FATAL COST-BASIS BLOCK.** The tax-aware switching cost needs the **real
  lot-level embedded gain**. `lots` is empty and broker `avg_price` is *not* a
  substitute — it makes the after-tax math *appear* to work while being wrong.
  Therefore the **full "switch now" recommendation (with a tax-aware net-benefit
  number) is FATALLY BLOCKED until a real lot-level cost basis (Schwab CSV) lands.**
  It must not ship on `avg_price`. What *is* unblocked on existing data is
  **A2-phase-0: domicile-only flagging** — identify every US-situs ETF sleeve and
  flag its estate-tax exposure, *without* asserting a net-of-tax switch-now number
  **and without asserting that any specific UCITS ticker is an equivalent** (that
  claim needs the §4 index-identity gate and the equivalence datasets of §7, which
  do not yet exist). Phase-0 flags estate exposure and, at most, *candidate* UCITS
  alternatives with identity **unverified**; full A2 verifies equivalence and
  recommends.

- **Mechanism:** map each held ETF → category via `resolve_sleeve_label`
  (`instrument_plan_class.py`); build the same-category candidate universe; run
  each candidate through the equivalence gate; score survivors on the five axes;
  rank; emit keep/switch (phase-0 = flag only, no tax number). This is a
  *deterministic comparator over instrument metadata*, not an LLM thesis — the
  fleet's role is a final sanity-check on an already-equivalence-gated survivor,
  never the primary equivalence test.

### A3. NVDA (and any deliberately-unmanaged holding) → "present but unmanaged"
Never a managed BUY/SELL verdict, but **always** counted for concentration,
US-situs estate, FX, and net worth. Modelled `unmanaged-but-present`, never
absent. (Owner-binding; the parallel Stream D work.)

**Component A end state:** *every* position carries a current stance — a
company-thesis verdict (A1), a keep/switch-vehicle recommendation (A2), or an
unmanaged-but-present accounting (A3) — a plain-English reason, and the tripwires
that flip it. **Nothing is silently uncovered** (an unmapped/unclassified holding
is a visible "cannot evaluate" in the coverage denominator, never a silent skip).

---

## 5. Component B — Prediction + learning loop (turn every decision into a graded, learned bet)

### B1. Testable at birth — and RECORDED even when not yet scorable
Every actionable output of A (a BUY/SELL/TRIM/ADD, or an A2 switch) is written as
an `observed_decision` (§2A) and, when the terms below are all present, promoted to
a `validated_decision` that is *structured and falsifiable*:
- direction; expected outcome / **target band** (not a single price — a band, to
  avoid exact-tag noise); **timeframe** (`evaluation_due_at`); **stop**;
- the **falsifiers + revisit-triggers** from A1 (the thesis-break conditions);
- the **alternative-at-birth** — for a HOLD, the *best-in-class peer* and the
  **switching-cost tolerance band**; for an A2 switch, the vehicle it beat.
- **Frozen at authoring** — no hindsight edits to a prediction's terms; the grade
  arrives later as an appended `validated_decision_outcome`, never a mutation (the
  audit found hindsight mutation of prediction versions; the spine is append-only).

These are exactly the `validated_decision` required fields in §2A — `target_band`,
`evaluation_due_at`, and `stop` are among them, so a decision missing any of them
is not a `validated_decision`.

**Unscorable ≠ dropped, and unscorable ≠ validated.** The previous draft's "if it
cannot be scored, it is not saved" was itself a *new* silent skip; the draft after
that mislabeled the unscorable decision as a validated spine event with a mutable
flag. Corrected per the split model (§2A): a decision that cannot yet be scored (no
benchmark, no cost basis, ambiguous or absent target band) is recorded as an
**immutable `observed_decision` with `validation_status_at_birth = unvalidated:<reason>`**,
and **no `validated_decision` is constructed** for it. It counts in the coverage
denominator as "observed, not yet gradable." Nothing actionable ever vanishes and
nothing unscorable is ever presented as valid grading input; the reason is explicit
and auditable. **When the missing input is an external FACT (benchmark, cost basis,
a clean book), a `validated_decision` is constructed later from the terms already
frozen at birth and the grade is appended as a `validated_decision_outcome`. When the
missing input is a PREDICTIVE term absent at birth (target band, alternative, stop,
falsifier — an explicit null in `predictive_terms_at_birth`), the decision is
`unvalidated:missing-predictive-term-at-birth` and is PERMANENTLY unscorable —
never retro-graded, because a band supplied after the fact is hindsight, not a
prediction** (§2A(b)).

*Rationale:* the ledger's 63%-unparseable failure was predictions that were never
testable. The fix is to make them testable **and** to keep the untestable ones
visible-as-untestable — not to delete them.

### B2. Graded vs the alternative, at maturity — not in a vacuum
At `evaluation_due_at`, **or earlier when a falsifier/revisit-trigger fires**,
score the bet against its `alternative_at_birth`:
- **Directional bets:** outcome bands (`prediction_outcomes.outcome_kind`:
  target/stop/expired-±) **and** return **vs the sleeve's benchmark** — a "correct"
  long that lagged its index is **not** a win (this is the link to Component C).
- **HOLD:** graded against the **best available alternative in the same class**,
  win only if the held name beat the peer by more than the **actual switching
  cost** (CGT on embedded gain + spread).
- **A2 switches:** did the switched-to Y actually deliver the modeled fee/tracking/
  tax benefit net of the realized switching cost?

**FATAL COST-BASIS BLOCK (HOLD-vs-alternative grading).** The HOLD and A2-switch
grades both need the **real lot-level embedded gain** to compute the switching
cost honestly. `lots` is empty; `avg_price` makes the after-tax comparison *appear*
to work while being wrong. **Full tax-aware HOLD-vs-alternative grading is FATALLY
BLOCKED until an ingested Schwab lot-level cost-basis CSV lands.** Until then these
decisions remain `observed_decision`s with `validation_status_at_birth =
unvalidated:cost-basis-missing` (B1) — no `validated_decision` for the tax-aware
grade, not graded on `avg_price`, and not dropped. (Cost basis is an external FACT,
so when a real lot-level CSV lands a `validated_decision` may still be constructed
from the birth-frozen terms — gradability is derived from that child, not by editing
the observation's `validation_status_at_birth`.) Domicile-only flagging
(A2-phase-0) is the only tax-adjacent output unblocked before cost basis.

### B3. Learn — a post-mortem WITH exploration (no doom loop)
Each resolved bet gets a **categorized post-mortem** (an agent, fed the frozen
thesis + what actually happened): *thesis wrong / timing wrong / one-off event /
just market beta / data error*. That verdict feeds source weights, agent prompts,
and the **actionability gate** — but the gate is explicitly designed so it can
never entrench itself into permanent suppression:

- **The doom loop, named:** a naive gate that "only acts on the long side where the
  ledger has proven edge" would suppress every low-history BUY → those signals
  never trade → they never generate outcomes → they are suppressed forever. A
  biased base ("index-only") becomes self-fulfilling. This is unacceptable.
- **EXPLORATION — a signal earns proof without being allowed to trade first.**
  Suppressed and low-history signals are **shadow-scored on paper**: their would-be
  bets are recorded as `validated_decision`s and graded at maturity — via an
  appended `validated_decision_outcome` with `is_shadow=true` (§2A) — exactly like
  live bets, but with no capital at risk. A shadow track record is how a new
  or currently-out-of-favor signal *earns* the right to go live — the gate reads
  paper outcomes, not just realized ones.
- **Decay old priors.** Source weights decay with age so a stale historical prior
  cannot dominate forever; recent (post-fix) evidence is weighted over old.
- **Regime awareness.** A miss in one regime does not permanently condemn a signal;
  post-mortems are tagged by regime and the gate does not extrapolate a bear-market
  miss into a blanket suppression.
- **Counterfactuals required.** Every suppression must carry the counterfactual it
  is being tested against (what the shadow track would have earned), so "suppress"
  is a falsifiable claim, not a terminal state.
- **Minimum-N and quarantine of the contaminated prior.** The gate may suppress a
  signal class only after a **minimum N of cleanly-scored (post-fix) outcomes**;
  the pre-fix ledger (the 38% long hit-rate computed on only the ~40% that parsed —
  the unparseable ~60% are **not** missing-at-random) is **quarantined and excluded
  from the gate entirely**. A permanently-degraded evaluator **escalates to an
  owner action item**, not a quiet "degraded" job status.

### B4. Honest by construction (guards against the exact failures found)
- Score **all** bets incl. the ones that went nowhere (no survivorship bias).
- **Fail loud** if an evaluation batch scored zero (the evaluator "reported ok
  doing no work").
- Never persist a *settled* "unparseable" outcome for a transient fetch failure —
  retry; a systemic fetch regression must degrade the job, not silently settle the
  whole ledger.

---

## 6. Component C — Scorecard (the proof)

The single number that says whether the loop is net-positive: **realized
time-weighted return vs (a) the market and (b) our own policy indexed passively,
with attribution (allocation / selection / cash / FX) and the Information Ratio.**
Full design in `docs/design/performance_scorecard_design.md`. It reads
`validated_snapshot_period` records only (§2A). It closes the loop: B's per-bet
grading rolls up into C's realized-return truth, and C's selection-attribution
tells B/A whether active selection is *adding or destroying* wealth vs indexing.

---

## 7. Data foundation (grounded; gaps flagged)

| Need | Exists? | Where / gap |
|---|---|---|
| Book / positions time series | Partial | `portfolio_snapshots.positions_json` (symbol/shares/price/value, account); irregular, ~4.5mo, **and currently corrupt** (needs repair) |
| Per-name verdict + falsifiers | Yes | `verdicts` (falsifiers_json, revisit_triggers_json) — shipped this session |
| Sleeve/category mapping | Yes | `instrument_plan_classes` + `resolve_sleeve_label` |
| Target weight per category | Yes | `plan_versions.target_allocation_json` (`classes[].target_pct`) |
| Predictions ledger + outcomes | Yes, broken | `predictions`, `prediction_outcomes` (63% unparseable, hollow evaluator, survivorship) |
| **Validated event spine** | **NO** | **new — `validated_snapshot` + `validated_snapshot_period` + `observed_decision`/`validated_decision`/`validated_decision_outcome` (§2A); the foundational build** |
| **Independent source manifest** (per-item broker-signed export / source record for `item_source_binding` + `expected_set_completeness`) | **NO / Partial** | **gap for §2A.** Broker-signed exports exist for some accounts, not all. **HARD PREREQUISITE for a proof-grade `validated_snapshot`:** without per-item source binding a book yields a *diagnostic* snapshot only (Component C §6 0a), never a proof-grade record. Merkle immutability alone does NOT prove ingest-time completeness/correctness. |
| **Independent broker activity/transaction manifest** (complete intra-period EVENT set for `validated_snapshot_period.expected_event_set_completeness`) | **NO** | **gap for §2A period contract + scorecard §2.2.** A per-account broker activity/NAV feed listing *every* intra-period buy/sell/vest/transfer/corporate-action. **HARD PREREQUISITE for a proof-grade period:** dated provenance for observed net deltas does NOT prove the event set is complete — a net-zero round-trip (sell then rebuy) is invisible to endpoints. Even after `fills` populate, without this a period is *diagnostic* only, never a headline return. |
| **ETF metadata** (fee/domicile/tracking/AUM) | **Partial** | domicile + exposure exist in `instrument_reference.py`; **TER / tracking / dist-vs-acc / AUM-spread exist NOWHERE** — must source + persist `instrument_metadata` |
| **Exposure-allocation join** (`exposure_allocation`: position-day ownership + decision→exposure mapping + overlap precedence, versioned) | **NO** | **new — the persisted, versioned join B and C reconcile through (§8, scorecard §2.5). Without it, C-vs-B is comparing totals and fails forever (NVDA in C, not B).** |
| **A2 equivalence datasets** (holdings, index identity/methodology, weighting, ESG/screens, replication, hedge policy) | **NO** | **gap for the §4 A2 index-identity gate** — these discrete facts exist NOWHERE today; domicile/exposure tags are NOT a substitute. **Required prerequisite before any "equivalent" label can be asserted (incl. the first-dollar UCITS-equivalence check).** |
| **Same-category candidate ETF universe** | **NO** | **gap for A2** — curated per-category universe |
| **Cost basis** (for HOLD/switch grading) | **NO** | `lots` empty; needs Schwab cost-basis CSV — **fatally blocks** tax-aware A2 switch-now + HOLD grading (§4/§5) |
| Benchmark price history | Partial | live-fetch only; scorecard needs a durable `benchmark_prices` table |

---

## 8. Interfaces — how the components wire into one loop (via the spine)

- **A → spine → B:** every actionable verdict/switch is written as an
  `observed_decision`, and as a `validated_decision` (with its `alternative_at_birth`
  and provenance) once gradable (B1). The falsifiers authored in A1 *are* the
  thesis-break conditions B2 watches; the grade is an appended
  `validated_decision_outcome`, never a mutation.
- **B → E → A:** post-mortems (including shadow-scored exploration outcomes, B3)
  update source-weights + the actionability gate that A's fleet consults before
  issuing a BUY (closes the learning loop; the gate can never permanently
  self-suppress — §5 B3).
- **B ↔ C — an explicit reconciliation gate, NOT "same benchmark ⇒ they
  reconcile", and NOT "one aggregate equals the unweighted sum of the other".**
  B's per-bet grading and C's Brinson selection effect are two truth systems over
  the same book with different windows and weights; "they use the same benchmark so
  they agree" was proven false. The previous draft's fix — *defining* C's aggregate
  selection effect as **equal to the sum of B's per-bet vs-benchmark deltas** — is
  **also wrong, and mathematically invalid:** (i) B's evaluation windows overlap and
  a HOLD is re-affirmed repeatedly, so summing per-bet deltas double-counts the same
  exposure; (ii) an unmanaged holding (NVDA) has **no** B bet at all yet still drives
  C's selection effect; (iii) per-bet deltas are unweighted by position size while a
  portfolio selection effect is weight-weighted. An unweighted sum is therefore the
  wrong identity. **Correct relationship:** C and B are **not** defined as one being
  the sum of the other. Both derive from the **same canonical position/return
  primitives** in the `validated_snapshot`/`validated_snapshot_period` records, joined
  through **one persisted, versioned exposure-allocation record** (`exposure_allocation`,
  below) that makes the mapping explicit instead of implied. The pre-fix framing
  reconciled "weighted, window-aligned, mapping-resolved contributions" but never said
  where that mapping was persisted, who owned a position-day, how overlaps resolved, or
  which subset was compared — so comparing totals could only fail forever (NVDA is in C,
  not B). `exposure_allocation` and three named identities fix that.

  **`exposure_allocation` — the persisted, VERSIONED join (NEW).** Per period it stores,
  immutably and under an `exposure_mapping_version`: **position-day ownership** (for each
  position-day of exposure, which `validated_decision` — if any — governs it, and its
  effective holding window); the **decision-to-exposure mapping** (the shares by window
  each decision governs); and **overlap precedence** (the deterministic rule that resolves
  a position-day claimed by more than one decision — e.g. a re-affirmed HOLD superseded by
  a later TRIM — so no position-day is double-owned or silently orphaned).

  **CLOSED ownership classification — the mapper may NOT fail open.** Every position-day
  MUST carry exactly one of three classes (a closed set — there is no implicit
  "everything else is unmanaged" bucket, which is precisely how a coverage bug hid):
  - **`decision_owned`** — a `validated_decision` governs it (identity (i) below).
  - **`deliberately_unmanaged:<policy-id>`** — a positive, policy-cited exclusion (NVDA,
    A3; the policy id names *why* it is out of B's scope). Identity (ii) below.
  - **`expected_but_missing:<reason>`** — a holding that SHOULD have a governing decision
    but whose decision was lost / failed / omitted by the mapper (e.g. an AAPL mapping bug).
    **This class MUST NOT be silently reclassified as unmanaged.**

  Only `decision_owned` and `deliberately_unmanaged` may **publish or contribute to
  reconciliation**. **`expected_but_missing` MUST block reconciliation** (the gate cannot
  pass while any position-day is unexplained) **and MUST appear in a value-weighted
  coverage denominator**, so a mapper that loses a managed holding's decision can no longer
  launder it from (i) into (ii) — leaving total C unchanged while B is quietly compared to
  a *reduced* managed subset and the gate rubber-stamps its own coverage bug. Without the
  closed classification, exactly that fail-open path (AAPL moved (i)→(ii), reconciliation
  "passes") is undetectable; the third class makes the missing decision a **named blocker**,
  not an invisible reclassification.

  From the classification, three **explicitly-named, separate identities** are computed:
  **(i) managed / B-attributable selection** — selection effect over `decision_owned`
  position-days (the ONLY identity reconciled against B); **(ii) unmanaged selection** —
  selection effect over `deliberately_unmanaged` position-days ONLY (e.g. NVDA, A3; no B
  counterpart by design — **not** a catch-all for decisionless holdings); **(iii) total C
  selection = (i) + (ii)** — the proof-surface aggregate, publishable only when no
  `expected_but_missing` position-day remains.

  **Reconciliation checks identity (i) against B over aligned windows — NEVER total-C
  against B — and only after the coverage denominator shows zero `expected_but_missing`
  weight.** Comparing total-C to B was guaranteed to fail forever precisely because
  NVDA lives in C but not B; excluding (ii) from the comparison removes that built-in
  contradiction, while the closed classification prevents (ii) from absorbing a
  coverage bug. **One source of truth still governs
  learning:** B's per-bet vs-benchmark delta is the atomic number E's post-mortems
  and the actionability gate ever read; C's selection effect (identity iii) is the
  proof-surface aggregate, never an independent input to learning. That preserves the
  "exactly one number trains the gate" governance while dropping the false equality.
  **Divergence of identity (i) from B beyond a stated tolerance, after the
  `exposure_allocation` weights/windows/precedence are applied, is a fail-loud flag**
  that blocks publishing either number — never two coexisting numbers with no
  adjudication.
- **Spine underneath everything (§2A):** conservation + loud-failure + full
  provenance guarantee A/B/C/E read a true, fully-attributed book and can never
  mistake a failed run — or a missing field — for a clean one.

---

## 9. Build order (each stage makes the next honest)

Sequenced so that **learning comes AFTER the inputs that make grading honest** —
never before. The old order let the ledger "learn" before cost-basis and fills
existed, which would train the gate on unaudited grades.

1. **Enforced integrity contract + repair the book** (§3; parallel repair work is
   the precondition). Conservation verdicts start being emitted per write.
2. **The canonical decision/event spine** (§2A) — `validated_snapshot` (per-item
   bound) + `validated_snapshot_period`, and the three decision records
   (`observed_decision` → `validated_decision` → append-only
   `validated_decision_outcome`), immutable and fully-provenanced. Nothing
   downstream reads raw inputs after this.
3. **Cost-basis / fill ingestion** (Schwab lot-level CSV → `lots`; persist fills).
   This is what unblocks tax-aware grading; it must land *before* learning.
4. **A2-phase-0 — domicile-only estate flags** (existing data; no tax-number
   claim). The smallest real-dollar move (§ below).
5. **Full A2 vehicle selection** — equivalence gate → five-axis comparator →
   tax-aware switch-now (now unblocked by step 3), fleet sanity-check on survivors.
6. **B/C grading + learning, together** (B2/B3 + Component C attribution), driven
   off the shared spine with the §8 reconciliation gate. Learning's exploration/
   shadow-scoring runs here, on cleanly-scored post-fix outcomes only.

Component C's **early TWR** may be computed before the spine exists, but **only as
an explicitly non-headline diagnostic** — it may not be shown on any "are we
beating the market?" proof surface and may not feed learning. A **headline / proof**
return number requires validated spine periods (each with reconciled, provenance-
backed flows, §2A) and reconciliation with B under the §8 gate. This matches
Component C §4/§6: the diagnostic and the proof number are different surfaces, and
the diagnostic never gets promoted implicitly.

**Smallest thing that moves a real dollar first** — before the full scorecard or
full A2, and aligned to the reviewer's list:
1. the **enforced conservation gate** (a true book, verdicts emitted);
2. **complete holding coverage** (every position carries a stance; coverage
   denominator visible);
3. a **US-situs ETF estate inventory** (`_US_SITUS_TICKERS`) sizing the estate-tax
   tail;
4. a **known-UCITS-equivalent check** for each US-situs sleeve — but **only once the
   §4 A2 index-identity gate can run**, which requires the equivalence datasets
   (holdings, index identity/methodology, weighting, ESG/screens, replication, hedge
   policy) named in §7. Those datasets **do not exist today**, so this step does
   **not** ship on existing data. Asserting "known equivalent" from domicile/exposure
   tags alone would repeat finding 3 — labelling a UCITS ticker equivalent while it
   silently carries a different index, screen, or hedge. Until the datasets land,
   this step emits **at most a candidate-flag ("possible UCITS equivalent — identity
   unverified")**, never an "equivalent" label and never a switch.
5. a **lot-aware tax-cost estimate** for the flagged swaps — which is exactly why
   cost-basis ingestion (step 3 of the build order) precedes the switch-now
   recommendation.

Steps 1–3 ship on existing data (domicile + exposure exist; no tax-number claim,
no equivalence label). Step 4's **equivalence** label waits on the A2 equivalence
datasets (§7); the tax-aware *switch-now* number waits on cost-basis ingestion. The
estate-inventory flags are the true zero-new-data, first-order-dollar move; the
equivalence and switch steps have named data prerequisites and do **not** ship on
existing data.

## 10. Explicitly out of scope / deferred
- Rebuilding the plan/allocation engine (regime-robust IPS already exists).
- Auto-execution of switches/trades (propose-and-ask stays).
- NVDA managed verdicts (deliberately unmanaged).
- Intraday/tactical trading (long-hold investor).

## 11. Open questions for the adversarial reviewer
1. **Spine completeness:** are the required fields on `validated_snapshot` /
   `validated_snapshot_period` and the `observed_decision` → `validated_decision` →
   `validated_decision_outcome` split (§2A) sufficient that no component can produce
   a number on incomplete provenance? Is the per-item integrity binding the right
   mechanism for sub-threshold corruption? Name any field whose absence should block
   a result but currently would not.
2. **A2 equivalence thresholds:** what holdings-overlap % and return-correlation
   floor actually stop a factor bet (equal-weight, ESG, synthetic, hedged,
   different index) from passing as like-for-like?
3. **B2 benchmark fairness:** right per-sleeve benchmark, and is grading a HOLD vs a
   *single* best-in-class peer robust, or should it be vs a category median?
4. **Cost-basis block:** is fatally blocking tax-aware switch-now + HOLD grading on
   real lot-level basis (vs an `avg_price` interim) the right call, or too strict?
5. **Learning-loop safety:** does the exploration/shadow-scoring + decay + minimum-N
   + regime tagging (§5 B3) actually prevent the doom loop, or is there a residual
   path to permanent self-suppression?
6. **B↔C reconciliation:** is the weight-/window-/mapping-based reconciliation of
   C's selection effect against B's per-bet deltas (§8) — NOT an unweighted-sum
   equality — sound given overlapping windows, repeated HOLDs, and unmanaged NVDA?
   Is the fail-loud-on-divergence tolerance the right mechanism, or does it hide a
   real modeling disagreement?
7. **Is the loop honest end-to-end** given the audit? Point at any component that
   could still report success while its input was silently wrong or incompletely
   provenanced.
