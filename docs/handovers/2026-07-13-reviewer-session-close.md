# Handover — 2026-07-13 reviewer-session close (expenses + plan + positions marathon)

**Roles:** resident Claude Code session = REVIEWER (implementer works on
`feat/opens-2026-07-11`, hands back per block, announces commits, NEVER merges;
reviewer verifies evidence + merges on PASS). **Git at close:** master = origin =
branch = `d3f2a99`. Migrations head **0094**. Backend under supervisor
(`scripts/start_backend_detached.ps1`, reboot-proof via the "Argosy Backend
Supervisor" logon task); wake-catchup live.

## What shipped today (each reviewed + merged; see git log)

- **Scheduler**: sleep/wake missed-run catch-up + double-fire guards (`a34eab2`);
  supervisor idempotent + logon task (`552e6dc`).
- **Expenses UX**: refund strikethrough + Vacation fold + refund-aware netting;
  taxonomy-parent clustering (Car/Vacation labels, mig 0091/0092); server-side
  sort + clickable headers; **merchant→tag brush rules** (mig 0094, פז→Mazda
  seeded); amount sort uses NIS-equivalent so foreign rows interleave.
- **Ingest correctness**: Leumi custody-view rejection at sniff (shared
  `leumi_html.py`); interleaved-coverage gap-warning fix; **Isracard foreign
  charge fix** (use the card EUR charge, not the merchant ¥/฿ sticker; symbol→ISO;
  all live rows backfilled); **overlap dedup extended to Discount/Max rolling
  cards** (`de0f232`) + **78 duplicate rows cleaned from the live DB this session**
  (backup at `db/argosy.db.bak_pre_overlap_cleanup`; independently re-verified,
  0 non-installment cross-statement dups remain).
- **Portfolio / plan**: Block H **instrument→plan-class map** (mig 0093) — all 37
  held classified, **Unmapped 0%**; estate-toggle honored; verdict freshness;
  IB01 fixed to Cash. **IBTA→Cash plan edit** (v92 CURRENT): engine defaults
  changed (survives synthesis), Cash 10.1%, Short-duration sleeve retired.
  Phantom stance rows structurally fixed (universe gate, `b384481`).
- **Execution**: preflight hard-fail no longer cancels approved proposals
  (`e558d2e`, CMPS-15 scar); cash resolved from snapshot, fail-loud on missing.

## Open items / queues

- **CMPS**: proposal 15 `executed_live` (manually corrected — the router had
  cancelled it on a $0 stale-cash preflight). Fill verifies on next Leumi **094**
  cash-account upload. IWDP/DPYA (13/14) expire 07-17.
- **Plan v92 prose debt** (memory `project_plan_prose_numeric_trace_debt`): v92
  accepted via `?override_gate=true` — inherited horizon prose fails
  `headline_numeric_source` (₪12,045,522 / ₪209,389 don't trace) + 83 placeholder
  violations. **Fix at next FULL synthesis** (a scoped edit re-inherits + re-trips;
  any scoped-edit accept off this lineage needs the same override until then).
- **Falsifiers**: NOT populated. Genuinely a fleet decision-run job — settled
  verdicts (the only surface that shows falsifiers) also freeze the position from
  fleet re-review, so a cheap direct write is unsafe. **Fleet pass scheduled next
  month.** 48→37 stances after phantom fix; 35/37 LOW conviction (mechanical, not
  quality — resolves as verdicts get authored).
- **Instrument map follow-ups**: fleet enrichment (verify classifications + author
  `what_it_is`/`why_held` blurbs) queued next month. **Consolidation-review**
  flags: ACWD/FWRA/MSCI World (3 redundant all-world funds — owner leaning unwind,
  not add a Global sleeve); IUHC (lone sector bet — exit-review). `travel.work`
  Vacation-fold question open for owner.
- **Implementer queue**: Block E siblings (broker-reject + account-escalation
  still auto-cancel approved proposals); scheduler narrow double-fire window
  (both guards pass before the per-job lock); monthly pie still debit-only (yearly
  is refund-netted); general instrument-reclass API (option C, deferred); owner to
  add more Mazda tag-rules (fuel variant + insurance/maintenance merchants).
- **Strategy (owner, deferred by choice)**: plan stays as-is. Owner asked "is it
  too safe?" — answered NO (83% equity, concentration risk is the real exposure,
  not caution); growth lever if wanted = high-growth/moonshot sleeve size, not the
  defensive floor. Modeling a higher-growth variant offered, not built.

## Reviewer process note (watch this)

Two batches (IBTA engine commits; overlap-dedup `de0f232`/`9524188`) reached
master by **fast-forward when a later commit was merged on top** — they weren't
explicitly reviewed at merge time. Before pushing, run `git log master..<branch>`
and review the WHOLE range, not just the tip. Backend restart after any
route/parser change (in-process caches). Migration up-down-up probe runs against
the live dev DB — it drops/recreates the table, so re-seed any live rows it wipes.
