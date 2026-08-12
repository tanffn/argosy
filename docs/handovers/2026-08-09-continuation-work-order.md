# Work order — continue the post-restore repair (2026-08-09, ~05:00 UTC)

You are picking up mid-repair. The $2.4M book truncation is **fixed on
production**; what remains is fallout from the fix plus one unreviewed
deliverable. Everything below was verified by me against the live database, not
taken from anyone's status report — where a prior report was wrong I say so.

---

## 0. RULES — read before running anything

**`db/argosy.db` is the live database holding a real ~$4.2M book.** Open it
read-only for inspection (`sqlite3.connect("file:...?mode=ro", uri=True)`). The
backend is running and writing to it. Never point
`scripts/backfill_restored_holdings_book.py` at it; it refuses without an
explicit override flag — do not pass that flag.

Two verified rollback points exist. Either restores the pre-restore state:

```
db/argosy.db.SAFETY_pre_repair.20260808T221356Z   354,893,824 B
db/argosy.db.bak_pre_restore.20260808T200547Z     354,922,496 B
```

**Environment traps that will cost you an hour each:**

1. **`PYTHONPATH` may point at a worktree.** Check it first — `echo $env:PYTHONPATH`.
   It was set to `.worktrees/stream-e-async-cache-loop`, which outranks the
   editable-install `.pth`, so `import argosy` silently loaded code with none of
   the repair. It cost me one invalid verification run. Process-scoped only (User
   and Machine are empty), so production is unaffected, but clear it in any shell
   you use. Confirm with:
   `python -c "import argosy; print(argosy.__file__)"` → must be
   `D:\Projects\financial-advisor\argosy\__init__.py`.
2. **`tests/test_api_phase4.py` and `tests/test_api_jobs.py` hang the suite.**
   Always `--ignore` both. `pytest-timeout` is NOT installed.
3. **Don't pipe pytest through `Select-Object`** — it buffers and you see nothing
   for hours. Redirect to a file: `... > out.txt 2>&1` then read the file.
4. **PowerShell heredocs and inline `python -c` break** on quotes, `COUNT(*)`,
   and `$`. Write a real script file instead. Commit messages via
   `git commit -F <file>`.
5. Full suite takes **3h14m** and the live quote paths are slow when a synthesis
   run is competing (`database is locked` on `kv_cache`).

## 1. Verified current state

**Data (live).** Snapshot 52: **46 positions, $4,191.4k, four accounts**
(leumi 38 / $1,615.6k, schwab 1 / $2,450.1k, schwab 876 6 / $56.7k, aborad 1 /
$69.0k). Migration `0097`. NVDA 10,940 sh, `managed=False`, one durable
`unmanaged_holdings` row, in the total book, out of the managed sleeve, ~58.5%
of the book. Dedupe conserves exactly (46→46, zero dollars lost).

**Code.** master `e70cdcb`. Backend running and healthy on that SHA.

**Runs.** Plan version **94** (`synth-2026-08-08-2200`) exists as a **draft**
from run **284**, computed post-restore. Run **283 is a stalled zombie** — see
item P4.

## 2. What was already done (don't redo it)

The restore itself, its safety gate (five adversarial rounds, 14 blockers),
ingest durability, and two conservation fixes. History:
`2026-08-08-p1-restore-complete.md`, `2026-08-08-p1-restore-review-handover.md`,
`2026-08-08-foundation-commits-review-note.md`,
`2026-08-08-p1-repair-rehearsal-evidence.md`.

---

# The work, in priority order

## P1 — Review plan v94. This is the actual deliverable.

Every plan number since 2026-07-13 was computed on a book missing 59% of its
value. v93 stored **NVDA at 0%**. Run 284 regenerated it on the restored book and
v94 now carries a glide:

```
Strategic single-stock (NVDA):  58.02% -> 45.51% -> 33.01% -> 20.50% -> 8.0%
```

against the 13% look-through cap. **Nobody has reviewed it.** It is `role=draft`,
`accepted_at=None`.

Do this:
1. Reconcile the 58.02% starting weight against the live book. My independent
   figure is **58.5%** ($2,450.1k / $4,191.4k). Find out why they differ — likely
   denominator choice (the $5,446.93 cash lot was still being dropped when v94
   was computed, pre-`e70cdcb`), but **confirm rather than assume**. If v94 was
   built on the leaky denominator, decide whether it needs regenerating.
2. Check the glide is internally consistent and the 13% look-through cap holds
   at each step.
3. Re-derive the other numbers that were wrong for 26 days and are still not
   recomputed: **US-situs estate exposure** (NVDA is US-domiciled and 58% of the
   book — this is the highest-stakes one), **retirement gates**, **FI shock**,
   **concentration**, **NVDA sell quotas**.
4. Present to Ariel for accept/reject. Do not auto-accept.

## P2 — 23 test regressions from the merge. Decide fix-forward vs revert.

**Status: OPEN, untouched.** Master has **38 failing tests** = 23 regressions +
15 pre-existing. I verified the 23 by re-running them against the pre-merge
commit `dcf5e93` in an isolated worktree: they passed there, they fail on
`00c89f4` and still fail on `e70cdcb`.

| cluster | count |
|---|---|
| `test_incremental_plan.py` | 10 |
| `test_retirement_safety_gates.py::TestNraEstateGate` | 5 |
| `test_decision_funnel_position_context.py` | 3 |
| `test_decision_funnel_sleeve_mandate.py` | 3 |
| `test_assembled_artifact.py` | 1 |
| `test_stream_d_...::test_resolver_sell_shares_use_tradeable_denominator` | 1 |

**One root cause: the fail-closed degraded-book rule.** `MARK_STALE_DAYS = 0`
requires every mark to be live-repriced before it can publish. Test fixtures are
dated `2026-06-01` with no quote function, so the book is always degraded:

```
TotalBookDegraded: live reprice unavailable for current-money marks —
  stale_mark_unpriceable:NVDA@unknown (valued_as_of=2026-06-01)
```

`incremental_plan.py:199` raises it; the NRA estate gate returns `FAIL` instead of
`PASS`; funnel packets lose `positions_summary` / `user_constraints` entirely
(`KeyError`) — stage-3 context **disappears silently rather than degrading
visibly**, which is the worst of the three symptoms and worth fixing on its own
merits.

**Production risk to assess before choosing:** the same strictness runs live.
Today `load_total_book` returns `degraded=False` only because the quote fetch
succeeds. Establish what happens during a real quote outage — if plan synthesis
raises and the estate gate reads FAIL, that may be intended fail-loud behaviour,
but it landed without ever being exercised against an outage. **Answer that
question before deciding.**

Options: fix forward (make the rule distinguish "no quote function available"
from "quote failed"), or revert. **If you revert, note the data restore is
independent — reverting does not un-restore the book — but you would lose the
ingest guard that stops the next Leumi-only feed re-truncating it. Do not leave a
revert standing without that protection.**

Repro: `.tmp_regressions.txt` holds the 23 ids.

```
python -m pytest -m "not llm_eval" -q -p no:randomly @(Get-Content .tmp_regressions.txt)
```

## P3 — One-row data repair: NVDA quantity date

Code is fixed (`e70cdcb`) so future refreshes preserve carried dates, but last
night's corruption is still in the data. `unmanaged_holdings` for NVDA reads
`observed_as_of = 2026-08-08`; the true date is **2026-07-13** (last Schwab
coverage). Until corrected, the 90-day staleness guard is disarmed for a position
worth $2.45M. Fix the durable row and the snapshot copy, then confirm
`quantity_is_stale` reflects the real 27-day age.

## P4 — Kill the zombie run and fix the stall alert

Run **283** has produced **zero agent reports in 7.5 hours** and is still
`status=running` (started 2026-08-08 21:05:27 UTC). It reuses phases 1–2 from run
**253**, which finished 2026-08-07 — **a day before the restore**, i.e. truncated-
book analysis. Even if it resumes, question whether its inputs are trustworthy;
run 284 already superseded it.

Argosy detected this itself and then failed to act: a `synthesis_stall_alert`
fired at 03:21 UTC and `jobs.open_job_run_failed` threw. **The stall detector
itself is broken** — that is the more valuable bug. Also present:
`database is locked` on `kv_cache` inserts under concurrency.

Mark 283 failed, then fix the alert path so a stalled run surfaces instead of
silently staying "running".

## P5 — Trusted-book accessor (Ariel approved this)

Six modules still call `parse_portfolio_tsv` directly, bypassing the ingest guard
that now protects the book: `nvda_sales_history`, `cli/critique`, `cli/ingest`,
`orchestrator/loops/daily_brief`, `services/daily_brief_runner`,
`adapters/brokers/leumi_tsv`. Two leaks were already demonstrated and fixed at
source (plan-synthesis Phase 3; `/draft/nvda-trajectory`, which was showing
`today_shares=999` from a rejected feed). Ariel chose to build one accessor and
migrate all six rather than continue point-fixing.

## P6 — Smaller items

- **Name the unnamed rows.** Four rows carry no symbol: Aborad "real estate"
  $69.0k, Leumi cash $17.66k, Leumi cash $5.45k, schwab 876 cash $5.89k. ~$98k is
  opaque.
- **Sort key.** `resolve_prior_positions_by_account_coverage` orders by
  `imported_at`; it should be `(snapshot_date, imported_at)`. Verified harmless
  today (both orderings produce the identical book) but fragile: a late-imported
  old file covering a dark account would win over a correctly-dated newer one.
- **15 pre-existing failures.** Independent of this work; includes 4 date-
  sensitive `test_plan_action_items` / `test_action_item_evidence` tests and the
  `AgentReport(attempt=...)` kwarg. Baseline: `.tmp_rerun_premerge.txt`.
- **A test reads the LIVE database.** The full suite's single ERROR was
  `test_market_context_assembler.py::TestCachedFallbackViaRealDB::test_overall_age_label_contains_nonzero_age`.
  It **passes in isolation** — the suite error was lock contention, because the
  synthesis run was writing to `db/argosy.db` at the same time (`database is
  locked` on `kv_cache`). So: not a regression, but a real hazard worth closing.
  A test that opens the production database can both flake under concurrency and,
  in principle, write to real money data. Point it at a fixture.

## 3. Corrections to earlier reports — do not trust these claims

- "4 known pre-existing failures" — **wrong**, it is 15. That baseline came from
  targeted files, never a full run.
- "$17.66k cash row dropped" — **wrong lot**. Two Leumi cash rows collided; the
  dropped one was **$5,446.93**.
- "Plan re-derivation in flight, run 283, ~1–2h" — **wrong**. 283 is stalled with
  zero output; the re-derivation completed as run **284** → plan **v94**.
- The 98-passed and 69-passed runs I reported were executed in a shell where
  `PYTHONPATH` pointed at the stream-E worktree. The worktree `conftest.py`
  probably won, but **treat those two numbers as unverified** and re-run if you
  depend on them.

## 4. Scratch scripts (untracked, all read-only or copy-based)

`.tmp_d1/`: `rederive_target.py` (independent restore-target derivation),
`review_queries.py` (account aliasing + sort-key check), `verify_restore.py`,
`verify_conservation.py` (network-free dedupe check), `check_flags_after_refresh.py`,
`find_dropped.py`, `final_state.py`, `verify_safety.py`, `check_run283*.py`,
`check_v94.py`, `refresh_after_restore.py` (WRITES — inserts a snapshot).
Result files: `.tmp_regressions.txt`, `.tmp_rerun_master.txt`,
`.tmp_rerun_premerge.txt`, `.tmp_master_suite.txt`.

Delete `.worktrees/premerge-check` when done: `git worktree remove .worktrees/premerge-check`.
