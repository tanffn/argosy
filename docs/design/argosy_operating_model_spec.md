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

### The SPINE PRODUCERS — THREE state machines, not one monolith (NEW)
Spine records are emitted by **exactly three producers, each in its own module** — and
by **nothing else.** The earliest draft named a single "materializer" that supposedly
emitted `validated_snapshot` + `validated_snapshot_period` + the contribution ledger
**in one transaction on the snapshot's verdict-pass.** That is an **unsatisfiable
temporal dependency**: contribution rows need a `governing_decision_id`, an exposure
allocation, an event manifest, and a benchmark version — and validated decisions,
event manifests, and benchmarks legitimately arrive **later** than the snapshot they
sit between. The round-2 draft split it into two (validator + a period finalizer that
wrote **both** `validated_snapshot_period` and `contribution_ledger`), but that
**re-coupled total TWR to attribution**: because the one finalizer wrote
`validated_snapshot_period` only after benchmark + exposure + governing decisions
froze, and total TWR reads `validated_snapshot_period`, TWR could not publish until
attribution inputs arrived — the "integrity-only TWR" claim was not operationally
true. The concerns are therefore split into **three** contracts with three state
machines, so the integrity period (what TWR reads) is finalized on integrity **alone**,
and attribution is a **separately-versioned** finalization layered on top:

- **Producer 1 — the SNAPSHOT VALIDATOR (`argosy/services/spine/snapshot_validator.py`,
  to build).** Raw snapshot → `integrity_verdict` → `validated_snapshot`. This is the
  **only** thing gated on snapshot-pass, and it runs **immediately** on pass. It writes
  `validated_snapshot` + its `per_item_integrity_binding` **only**; it does **not**
  touch `validated_snapshot_period`, `contribution_ledger`, or any attribution record.
- **Producer 2 — the INTEGRITY-PERIOD FINALIZER
  (`argosy/services/spine/integrity_period_finalizer.py`, to build).** The **sole**
  writer of `validated_snapshot_period` **and of the `integrity_period_head` table**
  (below). It finalizes a period on
  **integrity-of-the-book ONLY**: the two bounding `validated_snapshot`s, a **complete
  event manifest** (`expected_event_set_completeness`), **dated-flow reconciliation**
  (`flow_reconciliation_status`), and **price freshness**. It reads **no** benchmark,
  **no** governing decisions, and **no** exposure allocation. This is what total TWR
  reads → **TWR publishes on integrity alone, truly** (scorecard §4/§6). On finalizing a
  period it **CAS-advances `integrity_period_head`** for that boundary to the new
  `integrity_period_version` (monotonic `seq`, mirroring the verdict/attribution heads),
  so there is exactly one current integrity version per boundary. It never
  touches `contribution_ledger` or attribution.
- **Producer 3 — the ATTRIBUTION FINALIZER
  (`argosy/services/spine/attribution_finalizer.py`, to build).** The **sole** writer
  of `contribution_ledger` (+ its `linked_active_contribution`) and of the attribution
  head/version records below. It runs on a **separate, versioned
  `attribution_finalization` contract** (point 3 below) that fires only once the
  **benchmark version**, the **exposure ownership** (`exposure_mapping_version`, §8), and
  the **decision-completeness watermark** (the closed decision manifest, point 3b) are
  all frozen — layered **over** an already-finalized `validated_snapshot_period`, never
  on snapshot-pass and never on integrity-period finalization. Contribution rows are
  **never** emitted on a snapshot transition or an integrity-period finalization.
  **Every `attribution_finalization` row FK-binds the EXACT integrity-period version it
  was computed over** (`input_integrity_period_id` NOT NULL FK, point 3b), which is what
  couples the two version axes: when an integrity correction advances
  `integrity_period_head`, the prior attribution — computed over the superseded integrity
  version — is no longer served (the consumer view of point 3 drops it) until Producer 3
  re-finalizes over the new integrity version.

The concrete laundering paths both close: real code still reads raw `positions_json`
(`argosy/services/current_book.py:162` via `parse_positions_json`;
`decision_funnel/position_context.py`) and a **second direct snapshot writer** exists
(`holding_books.py:1865`). Both producers' contracts are stated as enforceable
mechanisms, not rules:

1. **Single writer per table, one transaction.** The snapshot validator is the
   **only** code with INSERT rights on `validated_snapshot`; the **integrity-period
   finalizer** is the **only** code with INSERT rights on `validated_snapshot_period`;
   the **attribution finalizer** is the **only** code with INSERT rights on
   `contribution_ledger`, `decision_contribution_map`, and the
   `attribution_finalization` / `attribution_finalization_head` tables. The validator
   reads a raw `portfolio_snapshots` row **plus**
   its `integrity_verdict` (below) and, **only if the CURRENT-HEAD verdict reads pass
   AND commits to exactly the bytes being materialized (finding 1, below)**, emits the
   normalized record and its per-item binding **in a single DB transaction**. A verdict
   that is pending, superseded, stale, content-mismatched, or failed yields NO
   `validated_snapshot`. There is no second code path that authors either kind of
   record.
2. **Content-hash-bound verdict check (the fix for stale/superseded pass).** A verdict
   is append-only, so "latest by `authored_at`" is a fragile query convention — ties
   are possible, and a stale pass `V1` could authorize bytes even after a fail `V2` was
   appended, because the old FK only checked that the *referenced* row said `pass`, not
   that it was **current** or that it assessed **these exact bytes**. Closed three ways:
   - **(a) `integrity_verdict.snapshot_content_hash`** — every verdict carries a hash
     over the **exact normalized snapshot bytes it assessed** (the canonicalized
     positions/accounts payload, not the raw JSON blob). A verdict is thus a commitment
     to *content*, not merely to a `snapshot_id`.
   - **(b) The validator computes the content-hash of the bytes it is about to
     materialize and binds `validated_snapshot.integrity_verdict_id` to the verdict
     whose `snapshot_content_hash` EQUALS that hash** — proving the bound verdict
     assessed exactly those bytes. A pass verdict over a *different* normalization can
     never authorize this snapshot.
   - **(c) Exactly-one-authoritative verdict, enforced.** An `integrity_verdict_head`
     table holds **one row per `snapshot_id`** (`PRIMARY KEY(snapshot_id)`,
     `current_verdict_id` FK). Appending any verdict for a snapshot CAS-advances that
     head row to the new verdict in the same transaction (fail-loud if the head moved
     concurrently); a re-evaluation appends a new row and moves the head, so a later
     `fail` becomes head and demotes the prior `pass`. Ties on `authored_at` are broken
     by a **monotonic `verdict_seq` (autoincrement integer), never a timestamp.** The
     insert trigger on `validated_snapshot` requires the referenced verdict be **ALL
     of**: (i) the **current head** for its `snapshot_id` (`integrity_verdict_head.
     current_verdict_id = validated_snapshot.integrity_verdict_id`), (ii) `result =
     'pass'`, and (iii) `snapshot_content_hash =` the validated_snapshot's own committed
     content-hash. A stale, superseded, or content-mismatched verdict fails the trigger.
3. **TWO separately-versioned finalization contracts — Producer 2's and Producer 3's
   state machines.** The single "period_finalization" of the round-2 draft is split so
   the integrity period (what TWR reads) never waits on attribution inputs.

   **3a. `integrity_period_finalization` — Producer 2's state machine (integrity ONLY).**
   A period is a record with lifecycle `open → integrity_inputs_frozen → integrity_finalized`,
   under an `integrity_period_version`. Its inputs are **integrity-of-the-book only**:
   - **`open`** — the period exists but cannot produce a number while ANY of these is
     missing: the two bounding `validated_snapshot`s, the independent **event manifest**
     (`expected_event_set_completeness`), the **dated-flow reconciliation**
     (`flow_reconciliation_status`), and **price freshness**. **No benchmark, no
     decisions, no exposure gate this state** — those are attribution inputs (3b), not
     integrity inputs. It is honestly `open`, never a silently-wrong frozen row.
   - **`integrity_inputs_frozen`** — snapshots the exact integrity input version-set
     (the two snapshot ids, the event-manifest id, the flow-reconciliation id).
   - **`integrity_finalized`** — Producer 2 emits **`validated_snapshot_period`** in one
     transaction and **CAS-advances `integrity_period_head`** for the boundary to this
     `integrity_period_version` in the same transaction, and records the frozen
     integrity-input-set on the period. This is the
     record total TWR reads; **it publishes on integrity alone** (scorecard §4/§6).

     **The `integrity_period_head` — one row per period boundary, CAS-advanced,
     monotonic.** A table `integrity_period_head(period_boundary_id PRIMARY KEY,
     current_integrity_period_id FK, seq)` names the **current** integrity version for
     each boundary — the concrete home of "the current `integrity_period_version`",
     mirroring `attribution_finalization_head` and `integrity_verdict_head`.
     `integrity_period_id` (= a specific `integrity_period_version` row) carries a
     **monotonic autoincrement `seq` (never `authored_at`)**. Every finalization
     CAS-advances the head from the prior id to the new one (fail-loud if the head moved
     concurrently), so two racing re-finalizations cannot both win.

     **Late-arrival / retry rule:** a corrected snapshot, event manifest, or flow record
     opens a **new `integrity_period_version`** over the same boundary, re-frozen and
     re-finalized, and **CAS-advances `integrity_period_head`** to it — superseding the
     prior via the head-table / supersession discipline of
     §2A(c). A finalized integrity period is immutable; correction is a new version.
     **Coupling to attribution (finding 1):** advancing the integrity head is exactly the
     event that renders any attribution computed over the prior integrity version
     UNAVAILABLE (point 3, consumer rule) and triggers a new attribution finalization
     over the fresh integrity version — the two version axes are never allowed to drift
     apart.

   **3b. `attribution_finalization` — Producer 3's state machine (layered OVER an
   `integrity_finalized` period).** Lifecycle `open → attribution_inputs_frozen →
   attribution_finalized`, under a monotonic `attribution_finalization_id` (below). It
   may not even open until the underlying `validated_snapshot_period` is
   `integrity_finalized`. **It also binds the EXACT integrity version it layers over —
   `input_integrity_period_id` NOT NULL FK to the `integrity_period_head`-current
   `integrity_period_version` at open — which is a first-class frozen input (below) and the
   coupling that finding 1 requires.** Its inputs are the **attribution** inputs only:
   - **`open`** — stays open while ANY of these is missing: the **`input_integrity_period_id`**
     (the current `integrity_period_head` version it computes over), the pinned
     **`benchmark_version`**, the versioned **exposure ownership**
     (`exposure_mapping_version`, §8), and the **decision-completeness manifest**
     (`decision_manifest`, below) certifying **every** governing decision whose effect
     window overlaps the period is in a terminal state. A missing governing decision
     keeps attribution `open`; the integrity period underneath is unaffected and TWR
     still publishes.
   - **DECISION MANIFEST / watermark (the fix for "completeness is unprovable" AND
     "completeness is a certificate over an unclosed source", finding 2).**
     Freezing the *currently-visible* set of governing decisions cannot prove none is
     missing — "all overlapping decisions terminal" is meaningless without a closed
     definition of what "all" is. Two mechanisms close it:
     - **(a) A monotonic DECISION-INGRESS SEQUENCE.** Every `observed_decision` is stamped
       with a durable, monotonic `ingress_seq` **at authoring** (autoincrement, never a
       timestamp), independent of validation. This is the closed source watermark: the
       decision_manifest is **closed at a specific `ingress_seq` watermark**, and certifies
       that **ALL `observed_decision`s with `ingress_seq <= watermark` AND an effect window
       overlapping the period are in a terminal state** — either a produced
       `validated_decision` (gradable) **or** `permanently-unscorable`
       (`unvalidated:missing-predictive-term-at-birth`, §2A(b)). Completeness is now provable
       **relative to the closed ingress watermark**, not "whatever was visible."
     - **(b) The manifest enumerates BOTH terminal sets in the window**, all bound to the
       `attribution_finalization_id`: the governing **`validated_decision_id`s** that grade,
       AND the **permanently-unscorable `observed_decision_id`s** (which have no validated
       ID yet are inside the completeness claim). Enumerating only validated IDs would be a
       certificate that silently omits the permanently-unscorable decisions its own claim
       covers; both sets are listed so the claim matches its enumeration.
     Producer 3 therefore requires this **closed, versioned `decision_manifest`**
     certificate before it may freeze: a producer-authored record, referenced by
     `attribution_finalization_id`, carrying `{ingress_seq_watermark,
     validated_decision_ids[], permanently_unscorable_observed_decision_ids[]}`. Until the
     manifest certifies completeness at its watermark, attribution **cannot finalize** — a
     currently-visible subset is not a proof of completeness. A
     decision **arriving after the watermark** (a higher `ingress_seq`) is not back-attached;
     it opens a **new `attribution_finalization_id`** (below) whose manifest re-certifies at
     a higher watermark over the larger set.
   - **`attribution_inputs_frozen`** — snapshots the exact attribution input version-set:
     **`input_integrity_period_id`**, `benchmark_version`, `exposure_mapping_version`,
     the `decision_manifest` id (with its `ingress_seq_watermark`), and the manifest's
     enumerated `validated_decision_id` **and** permanently-unscorable
     `observed_decision_id` sets. Only from this state may
     `contribution_ledger` rows be authored, and they are authored **against the frozen
     set** and stamped with the current `attribution_finalization_id`.
   - **`attribution_finalized`** — Producer 3 emits `contribution_ledger` (+
     `linked_active_contribution`) and `decision_contribution_map` in one transaction,
     every row FK-stamped with this `attribution_finalization_id`, and CAS-advances the
     head (below).

   **The `attribution_finalization_head` — one row per period boundary, CAS-advanced.**
   A table `attribution_finalization_head(period_id PRIMARY KEY,
   current_attribution_finalization_id FK, seq)` names the **current** attribution
   version for each integrity-period boundary. `attribution_finalization_id` carries a
   **monotonic autoincrement `seq` (never `authored_at`)**, exactly as the verdict/outcome
   heads (§2A(c), §3). The enumerated events that open a **new**
   `attribution_finalization_id` (higher `seq`) over the same period, re-run the manifest +
   freeze, and **CAS-advance the head from the prior id to the new one in the same
   transaction** (fail-loud if the head moved concurrently) are: a **late decision** (one
   arriving above the prior manifest's `ingress_seq` watermark), a **benchmark revision**,
   an **exposure re-map**, **and — the finding-1 addition — an integrity-period correction
   (a new `integrity_period_version`, i.e. `integrity_period_head` advanced).** Old
   `contribution_ledger` / `decision_contribution_map` rows are **never deleted or
   mutated** — they remain, FK-bound to the superseded `attribution_finalization_id`, but
   are **never selected**, because:
   - **Every C and B consumer VIEW filters to the current head AND requires the integrity
     axes to agree.** The view joins through
     `attribution_finalization_head` and selects only rows whose
     `attribution_finalization_id = current_attribution_finalization_id` for the period
     **AND whose finalization's `input_integrity_period_id` equals the boundary's current
     `integrity_period_head.current_integrity_period_id`.** This second clause is the
     **finding-1 version-dependency contract, expressed as a constraint on the view:** the
     instant an integrity correction advances the integrity head, the current attribution
     head — still computed over the *superseded* integrity version — no longer satisfies the
     equality, so it is **UNAVAILABLE (returns no rows / no attribution), never silently
     served against fresh TWR**, until Producer 3 re-finalizes a new
     `attribution_finalization_id` whose `input_integrity_period_id` matches the fresh
     integrity head and CAS-advances the attribution head. Stale attribution can therefore
     never be summed against a newer integrity period.
     Stale (superseded) and current rows are also **non-mixable**: a query can never
     sum a v1 row and a v2 row together.
   - **Economic-position-day uniqueness within the current finalization.** Because v1 and
     v2 carry **different `contribution_id`s** for the same economic position-day (account
     × instrument × day), a duplicated economic position-day is detectable: a UNIQUE
     constraint `(attribution_finalization_id, account_id, instrument_stable_id, date)` on
     `contribution_ledger` forbids two rows for the same economic position-day **within one
     finalization**, and the current-head filter guarantees exactly one finalization is
     ever summed — so no economic position-day is double-counted across versions.
4. **Enforcement is a DB constraint AND a single service boundary — not prose.**
   (a) `validated_snapshot.integrity_verdict_id` is a **NOT NULL FK** guarded by the
   three-part trigger of point 2(c) (current-head AND pass AND content-matching), so the
   database itself refuses a spine row over a non-authoritative or content-mismatched
   verdict. (b) All INSERTs to the spine tables are funnelled through the three producer
   modules; a repo-guard test fails the build if any file other than the snapshot validator writes
   `validated_snapshot`, any file other than the **integrity-period finalizer** writes
   `validated_snapshot_period` / `integrity_period_head`, any file other than the
   **attribution finalizer** writes
   `contribution_ledger` / `decision_contribution_map` / `attribution_finalization[_head]`.
   (c) **The raw-read guard is an AST/import ALLOW-LIST, not a helper-name grep (finding 4).**
   The earlier draft's guard only caught the single helper `parse_positions_json`, which is
   trivially bypassable — **~19 production files today decode `positions_json` directly** via
   `json.loads(...)` (e.g. `decision_funnel/position_context.py:55`,
   `decision_funnel/orchestrator.py:122`, `closed_loop.py:268`), completely invisible to a
   `parse_positions_json` grep. The guard is therefore restated as a static allow-list rule
   enforced by an AST/import check in CI, over **every** way `positions_json` can be
   dereferenced:
   - **Every dereference of `positions_json`** — `parse_positions_json(...)`,
     `json.loads(<snapshot>.positions_json)`, and any **ORM column access** to
     `PortfolioSnapshotRow.positions_json` — **outside the sanctioned spine-materializer
     boundary (the snapshot validator, Producer 1) or the explicitly-labelled pre-spine
     diagnostic path (§6 0a) is FORBIDDEN.** The check keys on the AST node (attribute
     access / call), not a function name, so renaming the accessor cannot slip past it.
   - **Every snapshot WRITE** — `session.add(PortfolioSnapshotRow(...))` / any INSERT of
     `PortfolioSnapshotRow` — **outside the single sanctioned raw-ingest writer is
     FORBIDDEN**, which catches the **second direct writer** (`holding_books.py:1865`) the
     helper-grep never saw.
   The allow-list names the sanctioned modules; the build fails on any dereference or write
   from an unlisted module. This replaces the bypassable single-function grep with a
   dependency rule the migration backlog (point 5) must clear before the guard turns on.
5. **Migration path (the ~19 raw decoders + the second writer must route through it BEFORE
   the guard turns on).** This is a required, enumerated cut-over, not an aspiration — and
   it is the explicit backlog the finding-4 AST allow-list (point 4c) blocks the build on:
   - **The two direct WRITERS.** `holding_books.py:1865` (the second snapshot writer) and
     `portfolio_snapshot_store.persist_snapshot` (the "intentionally dumb — always
     writes" path, §3) are demoted to **raw-ingest only**: they may land a raw
     `portfolio_snapshots` row and MUST emit an `integrity_verdict`, but they may
     **not** write a `validated_snapshot`. Until then the write allow-list lists exactly
     the single sanctioned raw-ingest writer, and CI fails on the un-migrated second writer.
   - **The ~19 direct DECODERS (the migration backlog, named).** Every current
     `json.loads(positions_json)` / `parse_positions_json` / `PortfolioSnapshotRow.
     positions_json` consumer that feeds a **proof-grade** number is repointed to read
     `validated_snapshot`. The direct-decode backlog to route is (per an AST sweep of
     `argosy/`): `services/holding_books.py`, `services/wealth_dashboard.py`,
     `services/retirement/safety_gates.py`, `services/net_worth_bases.py`,
     `services/raw_holdings_block.py`, `services/portfolio_snapshot_store.py`,
     `services/nvda_sales_history.py`, `services/nvda_projection.py`,
     `api/routes/wealth_dashboard.py`, `api/routes/plan.py`,
     `services/decision_funnel/orchestrator.py` (:122),
     `services/decision_funnel/position_context.py` (:55), `services/home_greeting.py`,
     `services/overview_assembler.py`, `services/action_item_evidence.py`,
     `services/closed_loop.py` (:268), `services/retirement/sigma_calibration.py`,
     `services/rsu_prevest_planner.py`, and `services/retirement/rebalancing.py`.
     Diagnostic-only surfaces (Component C §6 0a) may keep a raw read **only** from within
     an allow-listed diagnostic module behind the explicit diagnostic label.
   - **Post-materialization, ANY `positions_json` dereference on a proof surface — decode,
     helper, or ORM column access — is prohibited** and caught by the AST allow-list of
     point 4c, not by a helper-name grep. The prohibition is a test, not a comment; the
     guard turns on only once every module above has been migrated or explicitly
     allow-listed as diagnostic.

Until all three producers and their constraints land, the spine is design-only and no
number in this doc is proof-grade; the diagnostic path (§6 0a) is all that runs.

### `validated_snapshot` — the canonical point-in-time book (NEW)
The immutable, normalized position/account facts a component reads **instead of**
raw `portfolio_snapshots.positions_json`, **emitted solely by the snapshot validator
(Producer 1, above).** A/B/C never dereference the raw JSON; they read this record, which is A's
canonical point-in-time input. One record per snapshot that passed the integrity
gate. Required fields — **all mandatory; any absent ⇒ no `validated_snapshot` and no
component evaluates that book state:**

- `snapshot_id` — the raw input row it normalizes.
- `positions[]` — normalized per position, with **every field below bound** (the old
  binding covered only shares/price/cash, which let a faulty normalization pass while
  the derived value was wrong):
  `{instrument_stable_id, instrument_display, account_id, shares, price,
  price_as_of, currency, contract_multiplier, value_local, value_usd,
  fx_rate_id, corporate_action_lineage_id}`. Concretely:
  - `instrument_stable_id` — a **stable, non-symbol identifier** (ISIN / CUSIP /
    broker contract ID), not the display ticker. A ticker can be reused or renamed;
    the stable ID is what corporate-action lineage and the rename check (below) key
    on. A position with no resolvable stable ID is `identity:unbound` and cannot
    reach a proof-grade `validated_snapshot`.
  - `value_local` — **committed WITH its formula**: `value_local = shares × price ×
    contract_multiplier`, re-derived and checked inside the snapshot-validator transaction.
    Binding shares and price alone does **not** bind the product — a normalization bug
    that halves `value_usd` while shares/price look right (a wrong multiplier, a
    dropped multiplier for a futures/options-style contract) passes a shares/price
    binding but fails this one. `contract_multiplier` defaults to 1 for cash equities
    and MUST be explicit for anything else.
  - `value_usd` — `value_local` converted at a **VERSIONED FX rate** identified by
    `fx_rate_id` (a specific dated row of `fx_rates`, not an ambient "today's rate").
    The conversion is reproducible: given `value_local` and `fx_rate_id`, `value_usd`
    re-derives exactly, and a later FX correction is a new `fx_rate_id`, never a silent
    re-mark of a committed record.
  - `corporate_action_lineage_id` — links the position to any split/reverse-split/
    spinoff/merger that changed its share count or identity, so a corporate-action
    share change is never mistaken for a trade (scorecard §2.2) and a renamed line can
    be proven-continuous (below).
  These are the canonical facts A evaluates and B/C compute returns from.
- `accounts[]` — normalized `{account_id, custodian, cash_by_currency,
  broker_reported_account_total, account_total_reconciliation}`.
  `broker_reported_account_total` is the **account-level total the broker itself
  reports** (from the signed source record, below); `account_total_reconciliation`
  attests that the sum of the account's normalized positions + cash equals that broker
  total within a de-minimis rounding tolerance, or the account is
  `account:unreconciled` and cannot reach a proof-grade `validated_snapshot`.
  Per-item binding can be internally consistent yet collectively wrong (a whole
  account under-weighted proportionally); reconciling to the broker's own account total
  is the independent cross-foot that catches it.
- **Rename detection requires evidence, never equal-shares inference.** A symbol
  disappearing while a new symbol appears with equal shares is **not** treated as a
  rename on that coincidence — that heuristic is unsafe without stable IDs (two
  unrelated positions can carry equal shares). A rename/re-identification is accepted
  **only** when the two lines share the same `instrument_stable_id` (ISIN/CUSIP/contract
  carried across) **or** carry a `corporate_action_lineage_id` documenting the
  re-registration/merger. Absent that evidence, the vanished line is an
  `expected_but_missing` item (not a silent rename), which fails
  `expected_set_completeness` loud.
- `content_hash` — the hash over **this record's canonicalized normalized bytes**
  (the same normalization the conservation gate assessed; the `per_item_integrity_
  binding` Merkle root is computed over exactly these bytes). This is the value the
  integrity verdict must have committed to.
- `integrity_verdict_id` — a conservation verdict (§3) that is **all three of**:
  the **current head** for this `snapshot_id`, `result='pass'`, and whose
  `snapshot_content_hash` **equals** this record's `content_hash` (§2A point 2,
  enforced by trigger). A failed, missing, superseded, or content-mismatched verdict ⇒
  no `validated_snapshot`.
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

### `validated_snapshot_period` — one INTEGRITY window over two `validated_snapshot`s
The **sole output of the integrity-period finalizer (Producer 2, §2A)** — one canonical
record **per integrity-period finalization** of a boundary (t0→t1) of the liquid book,
named by `integrity_period_version` and tracked by that finalizer's own versioned head.
It is emphatically **not** "one record per boundary for all time": a corrected integrity
input opens a new `integrity_period_version` that supersedes the prior via the head
discipline (§2A point 3a), and **attribution has its own separate versioned head**
(`attribution_finalization_head`) layered on top — the two are not the same record and
not the same version axis. This record *references* facts (two `validated_snapshot`s), it
does not restate them, and it carries **integrity inputs ONLY** — **no `benchmark_version`,
no governing decisions, no exposure allocation** live here (those are attribution inputs,
frozen by Producer 3 on the `contribution_ledger`, §2A). This is exactly what makes total
TWR readable on integrity alone. Required fields — **all mandatory; any absent ⇒ the
period is not integrity-finalized and no component produces a number for it:**

- `period_id`, `integrity_period_version`, `t0_validated_snapshot_id`,
  `t1_validated_snapshot_id` — the version and the two canonical `validated_snapshot`
  records (each already integrity- and per-item-verified, above).
- `coverage_denominator` — count + value of positions in scope, and the count +
  value of any position that could **not** be evaluated (so coverage can never
  look cleaner than reality).
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

### `contribution_ledger` — the ONE canonical position-day ledger B and C both consume (NEW)
The single highest-leverage record. B (grade decision-window vs alternative) and C
(position-day Brinson selection) were two **independent** measures joined by an
"agree within tolerance" gate. **Sharing the same rows and the same `daily_capital_
weight` is necessary but NOT sufficient for an identity** — because naive
**geometric linking does not commute with grouping.** Concretely: one 50%-weight
position returning +10%/day for two days gives `link-then-weight = 0.5 × 21% = 10.5%`
but `weight-then-link = 1.05² − 1 = 10.25%` — same rows, same weight, a nonzero
residual purely from *ordering* the link and the grouping differently. So "both read
the same rows, therefore they reconcile" is still false, and a tolerance would be
masking exactly this linking-order noise. The fix is a **canonical ADDITIVE
attribution algorithm** whose per-id contributions **sum exactly** to the total, so
B-by-decision and C-by-class are two *sums* of the identical per-id numbers — equal by
construction with **zero** linking residual.

The **attribution finalizer** (Producer 3, §2A) emits, for each period, **one
`contribution_ledger` row per position-day** (per account × instrument × day within the
period), under the current `attribution_finalization_id`. Each row is immutable and carries:
- `contribution_id` — stable identity B and C both cite. **Distinct across attribution
  versions:** v1 and v2 of the same economic position-day carry **different**
  `contribution_id`s, which is what lets a duplicated economic position-day be detected
  rather than silently summed.
- `attribution_finalization_id` — **NOT NULL FK** to the `attribution_finalization`
  version that authored this row (§2A point 3b). Every B/C consumer VIEW filters to the
  row whose `attribution_finalization_id` equals the period's
  `attribution_finalization_head.current_attribution_finalization_id`, so **stale and
  current rows are never mixed in one sum.** A UNIQUE constraint
  `(attribution_finalization_id, account_id, instrument_stable_id, date)` forbids two
  rows for the same **economic position-day** within one finalization; combined with the
  current-head filter, no economic position-day is ever double-counted across versions.
- `account_id`, `instrument_stable_id` — stable (ISIN/CUSIP/contract) IDs, same as
  `validated_snapshot`.
- `date`, `period_id` — the position-day and its owning integrity period.
- `source_record_commitments` — the `validated_snapshot` per-item binding IDs (t0/t1)
  and the dated flow-provenance IDs (`flow_reconciliation_status`) this day's value
  rests on, so every number is traceable to committed source.
- `valuation` — `shares_held_that_day`, `price`, `contract_multiplier`,
  `value_local`, `fx_rate_id`, `value_usd` (same versioned-FX formula as §2A).
- `event_ids` — the intra-period event(s) (buy/sell/vest/transfer/corporate-action)
  bounding this position-day's effective holding window.
- `daily_capital_weight` — the position's share of the period's **canonical
  denominator** (the period's total invested capital that day). **This one
  denominator is the shared basis** — B's weighting and C's weighting are the same
  numbers because they read this field, not two independently-computed weights.
- `position_return` — this position-day's **simple return over its actual effective
  window** (`event_ids` bound it), so a share held only part of the day/period is
  returned only over the days held (the mid-period-buy case, scorecard §2.2).
- `benchmark_return` — the sleeve/policy benchmark's return for that position-day,
  at `benchmark_version`.
- `linked_active_contribution` — **THE canonical, additive per-id number** (below):
  this position-day's contribution to the period's total **linked active return**,
  computed by the smoothing algorithm so that `Σ linked_active_contribution` over all
  rows of a period **equals** the period's total linked active return exactly. B and C
  both read **this same field**; they never re-link independently.
- `ownership_class` + `governing_decision_id` — the CLOSED classification (§8):
  `decision_owned` (+ the `validated_decision_id` that governs the day),
  `deliberately_unmanaged:<policy-id>`, or `expected_but_missing:<reason>`.

**Return-linking is a NAMED ADDITIVE algorithm, computed once in the ledger — not left
to each consumer, and not naive geometric linking.** The attribution finalizer computes
`linked_active_contribution` using a **canonical logarithmic smoothing (Cariño /
Menchero linking)** under a recorded `linking_algorithm_version`: it distributes the
multi-period geometric compounding across the single-period arithmetic active
contributions with per-period smoothing coefficients such that the **sum of the
smoothed per-day, per-position active contributions equals the total geometrically-
linked active return** with **no residual**. This is the additive coordinate system in
which grouping commutes: because every consumer aggregates the *same* additive
`linked_active_contribution` values, `link-then-group` and `group-then-link` yield the
identical total regardless of grouping order — the 10.5%-vs-10.25% artifact above
cannot arise. FX is separated per §2.6 (NIS-basis minus USD-basis, both re-derived from
`fx_rate_id`), and any **cost/FX/interaction residual is booked as an explicit named
ledger line** (`residual_cost`, `residual_fx`, `residual_interaction`) that carries its
own `linked_active_contribution` share, not smeared into selection.

**Consequence for the B↔C gate (see §8):** the "agree within tolerance" gate is
**removed entirely** for the managed identity and replaced by a **shared-ledger
SUM-identity**. B's per-decision `vs_benchmark_delta` and C's per-class selection are
both defined as **`SUM(linked_active_contribution)` over a `contribution_id` set** —
just grouped differently (B by `governing_decision_id`, C by class). Neither is an
independently authored number. Because both are sums of the identical additive per-id
values, a nonzero residual between them is **impossible from linking**; any residual is
a **real ledger/mapping defect** (a mis-linked window, a double-owned position-day, a
lost decision) **localized to specific `contribution_id`s**, and the gate blocks on any
residual beyond de-minimis floating-point rounding. There is **no** remaining
"agree within tolerance" language for the managed identity.

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
- `ingress_seq` — a **durable, monotonic autoincrement integer stamped at authoring**
  (never a timestamp), assigned to **every** `observed_decision` regardless of validation
  status. This is the **decision-ingress watermark** the `decision_manifest` closes against
  (§2A point 3b, finding 2): a manifest certifies completeness for all `ingress_seq <=`
  its `ingress_seq_watermark`, so "all overlapping decisions terminal" is provable relative
  to a closed source rather than "whatever was visible." A decision authored later carries a
  higher `ingress_seq` and forces a new attribution finalization at a higher watermark.
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
- `equivalence_evidence` — **for A2 vehicle-switch events, the full reproducible
  record of the §4 equivalence gate, or no `validated_decision` for the switch.** The
  old schema omitted this, so the mandatory A2 gate was not reproducible from the
  spine — a switch could be graded without any record of *why* X and Y were deemed the
  same exposure. Required sub-fields, each frozen at authoring and each sourced from
  the named `instrument_metadata` store (§7), never inferred: `held_instrument_id`,
  `candidate_instrument_id` (stable IDs); `metadata_source` + `metadata_as_of` (the
  provider and date the facts were pulled); the **index-identity facts** (`index_id`
  / methodology, `weighting_method`, `esg_screens`, `replication_method`,
  `hedge_status`) for BOTH instruments; the **committed quantitative inputs** — the
  top-N holdings lists for X and Y and the aligned daily price series the numbers were
  computed from (referenced by content-committed IDs, so the result is reproducible,
  not asserted); the **quantitative results** (`holdings_overlap_pct` on the policy's
  top-N + total weight, `return_correlation` + `correlation_window` + `correlation_
  frequency`); and — crucially — the **`equivalence_policy_version`** whose floors were
  applied and the derived `overlap_gate_result` / `correlation_gate_result` /
  `index_identity_gate_result ∈ {pass, reject:<field>}`. **The thresholds are NOT
  authored on this record; they are read from the versioned `equivalence_policy` (§4)
  named by `equivalence_policy_version`** — the producer cannot record a `0` floor. A
  record whose `equivalence_policy_version` is unknown, whose committed inputs are
  absent, or whose `metadata_as_of` exceeds the policy TTL is not a proven switch.
  Grading (B2) reads this and **refuses** to grade a switch "proven" unless
  `index_identity_gate_result = pass`, the committed inputs are present, and the
  quantitative results met the **policy's** floors on within-TTL metadata. A switch
  whose `equivalence_evidence` is absent or references metadata that does not exist
  stays an `observed_decision`.

**(c) `validated_decision_outcome` — append-only, exactly-once, fully-provenanced
grade.** Scoring attaches by **appending** an outcome record that references the
`validated_decision`; it **never mutates the event.** Fields:
- `outcome_id` — unique identity for this settled outcome (so E can dedupe and a
  retry cannot silently double-append).
- `validated_decision_id`, `scored_at`, `outcome_kind`,
  `post_mortem_category`, `regime_tag`, `is_shadow`.
- **`vs_benchmark_delta` is NOT an independently-authored number — it is a DB-DERIVED
  AGGREGATE over the decision's committed `contribution_id` set.** The old schema let
  scoring *author* this delta, which meant B's persisted number had no enforced
  membership in the ledger and could drift from the position-days it supposedly
  summarized. Instead: a `decision_contribution_map(validated_decision_id,
  contribution_id, attribution_finalization_id)` table (FK to all three, and authored by
  the **attribution finalizer**, §2A) records **exactly which** ledger position-days a
  decision governs **within a given attribution finalization** (materialized from
  `exposure_allocation`, §8; a `contribution_id` may map to at most one `decision_owned`
  decision — enforced UNIQUE, so no double-ownership). Because a late decision or
  re-map opens a **new `attribution_finalization_id`**, the map is re-materialized under
  the new id and the old rows are superseded, not mutated. `vs_benchmark_delta` is then a
  **VIEW / enforced generated aggregate**: `SUM(contribution_ledger.linked_active_
  contribution)` over exactly that mapped set, **filtered to the current head — and, per
  finding 1, requiring the current attribution head's `input_integrity_period_id` to equal
  the boundary's current `integrity_period_head.current_integrity_period_id`.** The VIEW
  joins `attribution_finalization_head` and selects only `contribution_id`s and map rows
  whose `attribution_finalization_id = current_attribution_finalization_id` for the period
  **and whose finalization was computed over the current integrity version**, so a
  superseded finalization — or one stranded over a superseded integrity period after a
  correction — can never contribute (B's delta is simply UNAVAILABLE until re-finalized,
  never stale-served). It is the same current-head additive
  field C sums by class. B's number and C's number are therefore two GROUP-BYs of one
  current-head column, equal by construction; there is no authored scalar to drift and no
  stale/current mixing.
- **Full calculation provenance** — `evaluation_window_id` (the exact window IDs
  scored), `benchmark_version`, `exposure_mapping_version` (§8), `linking_algorithm_
  version`, and `calculator_version`. Every number the outcome asserts is reproducible
  from these plus the mapped `contribution_id` set.

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
  shrinks total value / position count **at or beyond** the threshold vs the prior
  live row, and emit a per-snapshot `integrity_verdict`.
  **The comparison is `≥`, not `>`.** The current guard rejects only a drop
  *strictly* beyond 50%, so an **exactly-half** erasure passes — the wrong boundary.
  The threshold is a **concrete, named policy value: reject at or beyond a 20% drop
  in total value OR position count vs the prior live row** (justification: a >20%
  single-snapshot shrink in a long-hold book with no dated executed sale is
  overwhelmingly a corruption, not a legitimate move; the July erasure was 59%).
  This is a *quarantine-and-alert* threshold, not a silent drop: a real large
  reconciled outflow clears by attaching dated flow provenance (§2A). The 20% figure
  is the single source of truth shared with the scorecard's partial-drop gate
  (§2.2), replacing that doc's unspecified "X".
  (`portfolio_snapshot_store.persist_snapshot` is currently "intentionally dumb —
  always writes"; this makes it emit a verdict every write.)

**The `integrity_verdict` record (NEW — the verdict the snapshot validator checks at
commit).** One immutable row per conservation evaluation, authored by the conservation
gate: `{integrity_verdict_id, verdict_seq, snapshot_id, snapshot_content_hash, result
∈ {pass, fail}, checks_json (which of drop-guard / currency-drop / account-drop /
count-drop fired), threshold_policy_version, authored_at}`. Two fields make it a
binding commitment, not a loose query target:
- **`snapshot_content_hash`** — a hash over the **exact normalized snapshot bytes the
  verdict assessed** (the canonicalized positions/accounts payload). The verdict is a
  commitment to *content*: a validated_snapshot may bind to it **only** when the
  content-hash of the bytes being materialized equals this field (§2A finding 1). A pass
  verdict over a different normalization cannot authorize these bytes.
- **`verdict_seq`** — a **monotonic autoincrement integer** used to order verdicts and
  break `authored_at` ties. Currency of a verdict is **never** decided by timestamp.

It is **append-only and never mutated** — a re-evaluation appends a new row (higher
`verdict_seq`) and CAS-advances the per-snapshot `integrity_verdict_head` row
(`PRIMARY KEY(snapshot_id)`, `current_verdict_id`) to it in the same transaction, so a
later `fail` demotes a prior `pass`. The snapshot validator binds **only** the verdict
that is the current head, `result='pass'`, AND content-hash-matching (§2A point 2). The
three-part insert trigger on `validated_snapshot` (current-head AND pass AND
content-matching) is what the database enforces against this row — a stale, superseded,
or content-mismatched verdict can never be observed as an authorizing `pass`. **The aggregate
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
  2. **QUANTITATIVE gate (survivors of gate 1 only) — reads a VERSIONED POLICY, not
     producer-chosen thresholds.** The floors are **not** free parameters the
     comparator may pick per run (a producer could record `overlap_threshold=0` and
     "pass" anything). They live in a **versioned `equivalence_policy`** record
     (identified by `equivalence_policy_version`, which is recorded verbatim into
     `equivalence_evidence`, §2A), with **concrete named minimums**:
     - **holdings overlap ≥ 90% by weight** on the **top-50 constituents** (the
       `top_n` definition is part of the policy), measured on committed holdings
       inputs;
     - **return correlation ≥ 0.99** on **daily** returns over a **3-year** trailing
       window (or the full common history if shorter, which itself downgrades the
       result to *candidate — insufficient history*);
     - **metadata TTL ≤ 90 days** — the holdings/index facts must be no staler than
       this or the gate yields *stale, cannot prove*.
     The gate **reads the policy** and compares against it; the producer supplies only
     the measured values and the **committed inputs** (the underlying top-N holdings
     lists for X and Y and the aligned daily price series) that those values were
     computed from, so the numbers are reproducible and the thresholds cannot be
     dialled down. Tightening a floor is a **new `equivalence_policy_version`**, never
     an in-run override. This is a *secondary* confirmation that two same-index funds
     actually track alike — never a substitute for the identity facts above.

  - **The floors are protected by DB constraints + a single-writer boundary — a
    zero-floor policy CANNOT be authored.** Reading the floor from a versioned record is
    not enough on its own: a service with policy-write access could still insert a new
    `equivalence_policy_version` with `overlap_floor = 0` and "pass" anything. The
    `equivalence_policy` table is therefore **IMMUTABLE / append-only behind a single
    policy-writer service boundary** (grep-gated in CI, mirroring the producer boundaries
    of §2A), and every row is guarded by **DB CHECK constraints** that make an
    out-of-range floor unrepresentable:
    - `CHECK(overlap_floor >= 0.90)`
    - `CHECK(correlation_floor >= 0.99)`
    - `CHECK(metadata_ttl_days <= 90)`
    Plus a **monotonic-tightening rule**, enforced by an insert trigger that reads the
    prior `equivalence_policy_version` (highest `policy_seq`): a successor may only make
    floors **STRICTER** — `CHECK(new.overlap_floor >= prior.overlap_floor)`,
    `CHECK(new.correlation_floor >= prior.correlation_floor)`,
    `CHECK(new.metadata_ttl_days <= prior.metadata_ttl_days)`. Versions are ordered by a
    monotonic `policy_seq` (never `authored_at`), exactly as the verdict/outcome heads.
    A row that would loosen any floor, or breach a constant floor, **fails the insert** —
    so no zero-floor (or any weaker) policy can exist for a producer to cite.

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

- **A2 phase-0 EXPLICITLY QUARANTINES the unverified UCITS-twin claims already in
  production.** Two live surfaces already assert equivalence with **no** index-identity
  evidence and must be **disabled/relabelled as "candidate — identity unverified" the
  moment A2 phase-0 ships**, not silently carried forward as fact:
  - `per_position_thesis.py:62` (`_US_DOMICILED_UCITS_SWAP`) hardcodes twin mappings
    (VOO→CSPX, SCHD→FUSA, VEA→EXUS, VNQ→DPYA, …) that drive TRIM/SELL cards purely on
    domicile, with no methodology/weighting/screen/replication/hedge check.
  - `allocation_plan.py:218` already **admits FUSA is not an exact SCHD twin** ("There
    is no exact SCHD twin in UCITS form … tilts slightly more mega-cap/quality-growth")
    — i.e. a self-declared *inexact* equivalence presented as a plan target.
  Until the §7 equivalence datasets exist and the §4 index-identity gate has run and
  written `equivalence_evidence` (§2A), A2 phase-0 treats **every** entry in that swap
  map and the FUSA/SCHD substitution as an **unverified candidate flag only** — never
  an "equivalent" label, never an auto-generated switch/TRIM/SELL rationale that claims
  like-for-like. This is a phase-0 deliverable: quarantine the existing claims, do not
  inherit them.

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
The parameters below are **concrete, named policy values** (versioned as
`learning_policy_version`), not adjectives — the earlier draft named these knobs but
set none, so "shadow scoring / decay / min-N / exploration" could not actually be
implemented or tested. Tune the numbers with evidence; the point is they are stated
and enforced, and the **suppressor never controls its own escape hatch**:

- **FORCED EXPLORATION QUOTA — bounded coverage, independent of the suppressor's
  scoring.** The quota must actually *guarantee* every suppressed class is revisited —
  a fixed 10% of slots cannot do that when the number of suppressed classes exceeds the
  quota slots (some class gets zero coverage). Two mechanisms, together, close it:
  1. **Adequate, coverage-driven sizing.** The exploration budget is **`max(10% of the
     cycle's candidate slots, |currently-suppressed classes| / K)`** rounded up — i.e.
     it **scales with the number of suppressed classes** so the slots are never fewer
     than needed to reach every class within the revisit bound.
  2. **Bounded round-robin over a PERSISTENT rotation cursor.** Exploration slots are
     filled by **round-robin over the suppressed classes in a stable, deterministic
     ordering**, so with `S` suppressed classes and `E` exploration slots per cycle
     **every suppressed class is revisited at least once within `⌈S / E⌉` cycles** (with
     the sizing above, `K` bounds this to at most `K` cycles). The rotation position is
     **durable state, not recomputed from scratch each run:** an
     `exploration_rotation_cursor` row (`{learning_policy_version, cursor_index,
     class_ordering_hash, updated_at}`) stores the cycle index and a hash of the
     **stable class ordering** (classes sorted by a fixed deterministic key —
     e.g. `class_id`, never insertion order), and is advanced and persisted every cycle.
     **This survives restart** so the rotation resumes where it left off rather than
     restarting at class 0 — without it, a process that restarts each cycle would forever
     re-select the first few classes and starve the tail. When the suppressed set changes,
     the cursor is carried over the re-sorted stable ordering (the `class_ordering_hash`
     detects the change) so newly-suppressed classes join the rotation without resetting
     progress on the rest.
  3. **Class → shadow-candidate GENERATOR (a suppressed class must yield an evaluable
     bet, or a logged gap).** Selecting a class is not enough; the cycle must produce an
     **evaluable shadow decision** for it. The generator, for a selected suppressed class,
     (i) draws the class's members from a **named candidate universe** (the
     `candidate_universe` store, a HARD PREREQUISITE added to §7 — the DB has **no**
     candidate table today, so this cannot run until it lands), (ii) applies the class's
     signal rule to pick the highest-ranked eligible member as the shadow candidate, and
     (iii) emits a shadow `observed_decision` → `validated_decision` for it, graded via an
     appended `is_shadow=true` outcome by the normal shadow pipeline (§2A).
  4. **NO-CANDIDATE record (the gap is visible, and rotation still advances).** When a
     selected class produces **no** eligible candidate that cycle (empty universe slice,
     all members ineligible, or missing data), the generator writes an immutable
     **`exploration_no_candidate` record** `{learning_policy_version, cycle_index,
     class_id, reason, observed_at}` and **advances the cursor anyway.** A class counts as
     **"revisited" only when it produced an evaluable shadow decision OR a logged
     `exploration_no_candidate`** — never merely by being *selected*. This keeps the
     revisit bound honest (a class cannot be silently skipped and counted as covered) and
     surfaces coverage gaps (a class that logs no-candidate every cycle is a visible data
     gap, not an invisible starvation).
  5. **CURSOR ADVANCE + EVIDENCE ARE ONE ATOMIC, CAS-GUARDED, IDEMPOTENT TRANSACTION
     (finding 3).** The three steps **read cursor → emit evidence (shadow
     `observed_decision`/`validated_decision` **or** `exploration_no_candidate`) → advance
     cursor** are **a SINGLE DB transaction**, never three separate writes. The failure the
     earlier draft left open: a crash *after* advancing the cursor but *before* writing the
     evidence would silently skip a class (it looks revisited but produced nothing), and two
     concurrent cycles reading the same `cursor_index` would both select the same class and
     duplicate its shadow bet. Closed by three properties, stated explicitly:
     - **Atomic:** the evidence record and the `exploration_rotation_cursor` advance
       **commit together or not at all.** A crash rolls back both — the class is neither
       counted as revisited nor left with an orphan cursor gap; the cycle simply re-runs it.
       "Revisited" is therefore true **only when the evidence row AND the cursor advance are
       committed in the same transaction.**
     - **CAS on the cursor seq (serializes concurrent cycles):** the advance is a
       compare-and-swap — `UPDATE exploration_rotation_cursor SET cursor_index = :next,
       ... WHERE cursor_index = :read_index AND class_ordering_hash = :read_hash`. If a
       concurrent cycle already advanced it, the swap matches zero rows and this cycle's
       transaction **fails loud and retries from the re-read cursor** — two cycles can never
       both consume the same cursor position, so no class is double-selected or duplicated.
     - **Idempotent / crash-safe:** the emitted evidence carries the deterministic
       `(learning_policy_version, cycle_index, class_id)` identity (a UNIQUE key on both the
       shadow `observed_decision` exploration-origin and `exploration_no_candidate`), so a
       retry of an already-committed cycle-step is a no-op rather than a second shadow bet.
     Only with all three does a class count as "revisited" exactly once, crash-safely, with
     the cursor and its evidence provably in lock-step.
  **Precise independence (resolving the earlier contradiction).** "Independent of the
  suppressor" does **not** mean blind to *which* classes are suppressed — it means the
  explorer **reads only the LIST of currently-suppressed classes** (published state,
  needed to guarantee coverage) and **never reads the suppressor's scoring / decision
  FUNCTION** (the weights or ranking that decided to suppress). The explorer cannot be
  vetoed or re-ranked by the suppressor: it deterministically rotates over the list, and
  its candidates are graded by the **normal shadow pipeline**, not by the suppressor. So
  a biased suppressor can put a class *on* the list but can neither keep it off the
  rotation nor veto its shadow evaluation — it cannot starve its own falsification.
- **SHADOW-SCORING BUDGET + always-available path back.** Every quota candidate (and
  every low-history signal) is **shadow-scored on paper**: its would-be bet is a
  `validated_decision` graded at maturity via an appended
  `validated_decision_outcome` with `is_shadow=true` (§2A), no capital at risk. The
  shadow budget is **uncapped for the forced-exploration quota** (a suppressed signal
  is *guaranteed* a shadow track) and rate-limited only for opportunistic extra
  shadows. **A suppressed signal therefore always retains a shadow path back to
  production** — suppression can never be terminal.
- **PROMOTION RULE (shadow → live) — explicit.** A shadow signal is promoted to live
  eligibility when, on **cleanly-scored post-fix** outcomes, it clears BOTH: (a)
  **minimum N = 30** matured shadow outcomes for that class (below), and (b) a
  vs-benchmark edge that is **statistically significant AFTER the multiple-testing
  correction** below. Demotion is symmetric: a live signal falling below the same
  corrected bar for N consecutive outcomes returns to shadow (not to permanent
  suppression).
- **MINIMUM-N = 30 cleanly-scored post-fix outcomes** before the gate may either
  suppress or promote a signal class. Below N, the class stays in forced exploration —
  never suppressed on thin evidence.
- **DECAY HALF-LIFE.** Source/signal weights decay with a **12-month half-life** (an
  outcome's weight is `0.5^(age_months/12)`), so a stale prior cannot dominate and
  recent post-fix evidence is weighted over old.
- **REGIME-SAMPLING POLICY.** Post-mortems are tagged by regime (§B3 `regime_tag`),
  and the gate requires the minimum-N to include **at least a stated minimum
  representation across regimes** (default: no single regime may supply >70% of the N,
  and at least 2 distinct regimes present) before a suppression generalizes; a
  single-regime (e.g. bear-market) miss cannot extrapolate to blanket suppression.
- **MULTIPLE-TESTING CORRECTION.** Because many signal classes are tested at once,
  edge significance uses a **Benjamini-Hochberg FDR control at q = 0.10** across the
  classes evaluated in a cycle (not per-class naive p<0.05), so a class is neither
  promoted nor suppressed on noise that survives only because many were tried.
- **Counterfactuals required.** Every suppression carries the counterfactual it is
  tested against (what the shadow track earned), so "suppress" is a falsifiable claim
  re-evaluated every cycle, not a terminal state.
- **Quarantine of the contaminated prior.** The pre-fix ledger (the 38% long
  hit-rate computed on only the ~40% that parsed — the unparseable ~60% are **not**
  missing-at-random) is **quarantined and excluded from the gate entirely**. A
  permanently-degraded evaluator **escalates to an owner action item**, not a quiet
  "degraded" job status.

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
| **Exposure-allocation join** (`exposure_allocation`: position-day ownership + decision→exposure mapping + overlap precedence, versioned) | **NO** | **new — the versioned join B and C reconcile through (§8, scorecard §2.5); its result is MATERIALIZED into the `contribution_ledger`'s `ownership_class` + `governing_decision_id`. Without it, C-vs-B is comparing totals and fails forever (NVDA in C, not B).** |
| **Contribution ledger** (`contribution_ledger`: one immutable position-day row B and C both consume — stable IDs, `attribution_finalization_id` FK, source commitments, versioned valuation/FX, event IDs, `daily_capital_weight`, `position_return`, `benchmark_return`, **`linked_active_contribution`** (additive Cariño/Menchero), ownership class, governing decision) | **NO** | **new — emitted by the ATTRIBUTION FINALIZER (Producer 3, §2A); the shared additive basis that makes B↔C a by-construction SUM-identity, not a tolerance (§8, scorecard §2.5). Every row FK-bound to its `attribution_finalization_id`; consumers filter to the current head, with a UNIQUE `(attribution_finalization_id, account_id, instrument_stable_id, date)` forbidding a duplicated economic position-day within a finalization.** |
| **Integrity-period head** (`integrity_period_head`: one-row-per-boundary `{period_boundary_id PK, current_integrity_period_id FK, seq}`, CAS-advanced, monotonic `seq`) | **NO** | **new (finding 1) — the concrete home of "the current `integrity_period_version`", written ONLY by the integrity-period finalizer (Producer 2, §2A).** Mirrors `attribution_finalization_head` / `integrity_verdict_head`. Advancing it on an integrity correction is the event that renders stale attribution UNAVAILABLE (the consumer view requires `attribution_finalization.input_integrity_period_id = current_integrity_period_id`) and triggers a new attribution finalization. |
| **Attribution finalization head + decision manifest** (`attribution_finalization` versions each FK-binding `input_integrity_period_id`, one-row-per-boundary `attribution_finalization_head` (CAS, monotonic `seq`), and the closed `decision_manifest` completeness certificate) | **NO** | **new — Producer 3's versioned state (§2A point 3b).** Each `attribution_finalization` FK-binds the exact `input_integrity_period_id` it was computed over (finding 1); consumers select the current attribution head ONLY IF that id equals the current `integrity_period_head`. The `decision_manifest` is **closed at an `ingress_seq_watermark`** over the monotonic decision-ingress sequence (finding 2 — every `observed_decision` gets a durable `ingress_seq` at authoring) and enumerates BOTH the governing `validated_decision_id`s AND the permanently-unscorable `observed_decision_id`s in the window, certifying every `ingress_seq <= watermark` decision with an overlapping effect window is terminal BEFORE attribution may freeze. A decision above the watermark, or an integrity-period correction, opens a new `attribution_finalization_id` that supersedes via the CAS head. Completeness is provable relative to the closed watermark, not "freeze what's visible." |
| **Spine producers + `integrity_verdict`** (THREE writers: snapshot validator → `validated_snapshot`; **integrity-period finalizer** → `validated_snapshot_period` + `integrity_period_head` (integrity inputs only); **attribution finalizer** → `contribution_ledger` + `decision_contribution_map` + `attribution_finalization[_head]`. Immutable pass/fail verdict, content-hash-committed, current-head-tracked) | **NO** | **new (§2A/§3).** Enforced by a three-part insert trigger on `validated_snapshot` (verdict is **current head** AND `result='pass'` AND `snapshot_content_hash` matches the committed bytes), a monotonic `verdict_seq` (never `authored_at`) for tie-breaking, an `integrity_verdict_head` table, and a per-table single-writer INSERT boundary. **Raw-read/write enforcement is an AST/import ALLOW-LIST, not a helper-name grep (finding 4):** every `positions_json` dereference (json.loads / `parse_positions_json` / `PortfolioSnapshotRow.positions_json` ORM access) and every `PortfolioSnapshotRow` write outside the sanctioned modules fails CI. **Total TWR reads the integrity-period finalizer's `validated_snapshot_period` → publishes on integrity alone; attribution/B↔C reads the attribution finalizer's current-head `contribution_ledger`.** Migration backlog = the ~19 direct decoders (e.g. `decision_funnel/position_context.py:55`, `decision_funnel/orchestrator.py:122`, `closed_loop.py:268`, `current_book.py:162`) + the second direct writer (`holding_books.py:1865`) — all routed through the spine before the guard turns on (§2A point 5). |
| **A2 equivalence datasets** (holdings, index identity/methodology, weighting, ESG/screens, replication, hedge policy) → the named **`instrument_metadata`** store | **NO** | **gap for the §4 A2 index-identity gate + the `validated_decision.equivalence_evidence` field (§2A).** `instrument_reference.py` carries only **coarse** `sector`/`region`/`estate_safe` — it classifies VOO and equal-weight XZEW both as "Broad Index / US" and has **no** methodology/weighting/ESG/replication/hedge fields. These discrete facts exist NOWHERE today; domicile/exposure tags are NOT a substitute. **`instrument_metadata` MUST be sourced and populated BEFORE A2 runs** — A2 reads it, freezes it into `equivalence_evidence`, and cannot assert any "equivalent" label (incl. the first-dollar UCITS check) until it exists. |
| **Same-category candidate ETF universe** | **NO** | **gap for A2** — curated per-category universe |
| **Learning candidate universe + exploration rotation state** (`candidate_universe`: per-class members the class→shadow-candidate generator draws from; `exploration_rotation_cursor`: durable rotation position; `exploration_no_candidate`: logged gaps) | **NO** | **new — HARD PREREQUISITE for §5 B3 forced exploration.** The DB has **no** candidate table today, so exploration cannot generate an evaluable shadow bet until `candidate_universe` lands. `exploration_rotation_cursor` persists the round-robin index across restarts (else early classes are re-selected forever); `exploration_no_candidate` records a "no-candidate-this-cycle" gap so a selected class still advances the cursor and the coverage gap is visible. A class counts as revisited only on an evaluable shadow decision OR a logged no-candidate. **read cursor → emit evidence → advance cursor is ONE atomic, CAS-guarded, idempotent transaction (finding 3, §5 B3.5):** evidence + cursor advance commit together (crash-safe), the advance is a CAS on `(cursor_index, class_ordering_hash)` so concurrent cycles serialize and cannot double-select, and a `(learning_policy_version, cycle_index, class_id)` UNIQUE key makes retries no-ops. |
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
  wrong identity. **Correct relationship — a shared-ledger IDENTITY, not two
  measures plus a tolerance.** C and B are **not** defined as one being the sum of the
  other, and they are **not** two independent computations checked for approximate
  agreement — and simply *sharing the same rows and weights is still not enough*,
  because naive geometric linking does not commute with grouping (a 50%-weight name at
  +10%/day for two days gives 10.5% link-then-weight vs 10.25% weight-then-link — a
  residual from ordering alone, §2A). The identity is made exact by a **canonical
  ADDITIVE attribution field.** Both **consume the same immutable `contribution_ledger`
  rows** (§2A) — via the current-head consumer view that also **requires the finalization's
  `input_integrity_period_id` to equal the boundary's current `integrity_period_head`**
  (finding 1, so stale attribution over a corrected integrity period is UNAVAILABLE, never
  served against fresh TWR) — the one canonical position-day ledger the **attribution
  finalizer** emits,
  carrying stable IDs, source-record commitments, the versioned valuation/FX formula,
  the bounding `event_ids`, the single `daily_capital_weight`, the `position_return`,
  the `benchmark_return`, and — decisively — the **`linked_active_contribution`**: the
  smoothed (Cariño/Menchero, under `linking_algorithm_version`) additive per-id number
  that **sums exactly** to the period's total linked active return. B's per-decision
  `vs_benchmark_delta` and C's per-class selection are both `SUM(linked_active_
  contribution)` — the SAME column, grouped by `governing_decision_id` vs by class.
  Because grouping a sum commutes, `link-then-group` and `group-then-link` are
  identical; cost/FX/interaction residuals are explicit ledger lines each carrying their
  own `linked_active_contribution` share (§2A). Because both numbers are GROUP-BYs of
  one additive column, **they reconcile by construction with zero linking residual** —
  the mapping is no longer "implied," "persisted somewhere," or "compared over some
  subset," and neither delta is independently authored (the decision's delta is a
  DB-derived aggregate over its `decision_contribution_map` set, §2A(c)).
  `exposure_allocation`'s
  ownership/precedence semantics are **materialized into the ledger's `ownership_class`
  + `governing_decision_id`** (it remains the versioned join spec below; the ledger is
  where its result is committed).

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
  **Any nonzero residual between identity (i) and B — beyond de-minimis
  floating-point rounding — is a REAL LEDGER ERROR, not tolerable linking noise.**
  Because both are aggregations of the same `contribution_id` set over the same
  `daily_capital_weight`, a residual can only mean a mis-linked window, a
  double-owned or orphaned position-day, or a lost decision; it is a **fail-loud flag
  localized to the offending `contribution_id`s** that blocks publishing either
  number — never a tolerance band that quietly absorbs a modeling disagreement, and
  never two coexisting numbers with no adjudication.
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
4. **A2-phase-0 — domicile-only estate INVENTORY/LABEL** (existing data; no
   tax-number claim, no equivalence label). This **does not move a dollar** — it is an
   inventory of exposure that already ships (§ below); the first actual dollar-mover is
   step 5, gated as named there.
5. **Full A2 vehicle selection** — equivalence gate → five-axis comparator →
   tax-aware switch-now (now unblocked by step 3), fleet sanity-check on survivors.
6. **B/C grading + learning, together** (B2/B3 + Component C attribution), driven
   off the shared spine with the §8 reconciliation gate. Learning's exploration/
   shadow-scoring runs here, on cleanly-scored post-fix outcomes only.

Component C's **early TWR** may be computed before the spine exists, but **only as
an explicitly non-headline diagnostic** — it may not be shown on any "are we
beating the market?" proof surface and may not feed learning.

**Two proof surfaces gated INDEPENDENTLY — total TWR is NOT blocked on the decision
ledger.** The earlier draft refused all headline numbers until B↔C reconciled, which
over-blocked: total-portfolio TWR and conservation are a property of the
snapshot/flow book and do **not** require every position-day to carry a gradable
decision. Split the gates:
- **Total-portfolio TWR (+ conservation)** publishes as a **headline** as soon as
  the **snapshot/flow INTEGRITY** gate passes — validated spine periods with per-item
  binding, dated-provenance flow reconciliation, event-set completeness, and price
  freshness (§2A). It does **not** wait on B↔C. It **must carry an explicit
  attribution-COVERAGE percentage** (the value-weighted share of the book whose
  position-days are `decision_owned` and successfully reconciled), so the number is
  honest about how much of it is skill-attributed vs merely measured.
- **MANAGED-selection attribution and learning** are the **only** things blocked by a
  B↔C `contribution_ledger` residual (§8). A ledger residual, or any
  `expected_but_missing` position-day weight, blocks the managed-selection number and
  E's learning — never the total-TWR headline. Total TWR simply reports lower
  attribution coverage until the ledger is clean.

This matches Component C §4/§6/§8: the diagnostic, the total-TWR headline, and the
managed-attribution proof are different surfaces with different gates, and the
diagnostic never gets promoted implicitly.

**What ships first — and an HONEST label of which steps move a dollar.** The earlier
draft called the phase-0 estate flag "the smallest real-dollar move." **That was
wrong: phase-0 moves no dollar.** US-situs labelling of existing exposure is **already
shipped** (`wealth_dashboard.py` / `retirement/safety_gates.py`, sizing the current
**~$518K of US-situs ETFs**); re-inventorying it changes no position. The actual first
dollar-mover is the **tax-aware UCITS switch (step 5)**, and it is gated on data that
does **not exist yet**. Stated honestly, aligned to the reviewer's list:
1. the **enforced conservation gate** (a true book, verdicts emitted) — infrastructure,
   moves no dollar;
2. **complete holding coverage** (every position carries a stance; coverage
   denominator visible) — infrastructure, moves no dollar;
3. a **US-situs ETF estate INVENTORY/LABEL** (`_US_SITUS_TICKERS`, ~$518K) sizing the
   estate-tax tail — **this is a label of existing exposure, NOT a trade.** It already
   partially ships; it does not move a dollar;
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
no equivalence label) and are all **labels/infrastructure that move no dollar.**
Step 4's **equivalence** label waits on the A2 equivalence datasets (`instrument_metadata`,
§7); step 5's tax-aware *switch-now* number waits on **both** cost-basis ingestion
(Schwab lot-level CSV → `lots`, build-order step 3) **and** those equivalence datasets.
**The ACTUAL first dollar-mover is step 5 — a verified US-situs→UCITS switch — and it
is gated on exactly two missing datasets: (a) lot-level cost basis (for the tax-aware
switching cost) and (b) the `instrument_metadata` equivalence facts (for the §4
index-identity gate).** Until both land, nothing here recommends a real trade;
everything that ships on existing data is inventory/label, not a dollar moved.

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
6. **B↔C reconciliation:** is the **shared additive `contribution_ledger` identity**
   (§8/§2A) — B and C both defined as `SUM(linked_active_contribution)` over the SAME
   position-day rows, grouped by decision vs by class, so grouping commutes and a
   residual is a real ledger/mapping error rather than tolerated linking noise — sound
   given overlapping windows, repeated HOLDs, and unmanaged NVDA? Is the
   Cariño/Menchero smoothing (`linking_algorithm_version`) plus explicit
   cost/FX/interaction residual lines enough to keep `Σ per-id = total linked active
   return` exact for every grouping, or is there a residual source the ledger does not
   name?
7. **Is the loop honest end-to-end** given the audit? Point at any component that
   could still report success while its input was silently wrong or incompletely
   provenanced.
