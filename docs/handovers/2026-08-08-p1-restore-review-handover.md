# Handover for adversarial review — the production book restore (2026-08-08)

Self-contained. A reviewer needs nothing else. Written to be attacked: every
claim below has a command that can falsify it, and §7 lists where I think I am
most likely wrong.

**This is a post-hoc review of a change already made to production.** The
restore is done. The question is no longer "should we?" but "is what landed
correct, and what did the executor miss?"

---

## 0. SAFETY RULE

`D:\Projects\financial-advisor\db\argosy.db` is the live database holding a real
~$4.2M book. **Read-only inspection only** — use
`sqlite3.connect("file:...?mode=ro", uri=True)`. Never run the repair tool at it
(it refuses without an explicit override; do not pass the override). The backend
is running and writing to it.

## 1. What was wrong

On 2026-07-13 a Leumi-only TSV import replaced the whole book instead of merging
per account. Schwab, Schwab 876 and Aborad were erased, NVDA included.
**$2,432.0k of $4,047.6k — 59% — disappeared.** For 26 days Argosy computed
concentration, US-situs estate exposure, retirement gates, FI shock and NVDA sell
quotas on 38 Leumi positions worth $1,615.6k.

## 2. What changed on production tonight

**Data.** Three new snapshots:

| id | positions | total | accounts | source |
|---|---|---|---|---|
| 49 (pre-existing) | 38 | $1,615.6k | 1 | truncated lineage |
| **50** | **46** | **$4,047.6k** | **4** | `backfill:last_coverage_restore` |
| 51 | 46 | $4,191.4k | 4 | self-refresh (see §6 finding 1) |
| 52 | 46 | $4,191.4k | 4 | self-refresh under master code |

Migration `0094 → 0097_unmanaged_holdings`, adding `unmanaged_holdings` and
`unmanaged_symbol_policy` (seeded with NVDA). One durable row: NVDA @ schwab,
10,940 sh, active.

**Code.** master fast-forwarded `dcf5e93 → 00c89f4`, 12 commits, 40 files,
+8394 −438. Backend restarted and serving `git_sha 00c89f4`.

**Rollback points**, both content-verified, either restores the pre-restore state:

```
db/argosy.db.SAFETY_pre_repair.20260808T221356Z   354,893,824 B  (independent, pre-migration)
db/argosy.db.bak_pre_restore.20260808T200547Z     354,922,496 B  (taken by the tool)
```

## 3. The restore logic, in one paragraph

`resolve_prior_positions_by_account_coverage()` walks snapshot history
newest-first, and the first time it sees an account it takes all of that
account's rows from that snapshot. The restored book is therefore a **union of
per-account time slices**: Leumi as of Aug 8, the other three as of Jul 13. The
principle is "coverage ≠ emptiness" — a feed that never mentions Schwab is silent
about Schwab, not evidence that it is empty. That is the exact inversion of the
bug. `backfill_restored_holdings_book()` then asserts 46 / $4,047.6k, rebuilds
the row, re-verifies after building, and rolls back on any mismatch, so it cannot
quietly produce a different book.

## 4. Claims, each with a falsifier

| # | Claim | How to falsify |
|---|---|---|
| 1 | Target is 46 / $4,047.6k | Re-derive from raw snapshot JSON, independently of `argosy` code — `.tmp_d1/rederive_target.py` does this and got 46 / $4,047.6k (38 Leumi + 8 carried; $1,615.6k + $2,432.0k) |
| 2 | Only 8 positions were carried | `.tmp_d1/rederive_target.py` prints them: NVDA 10,940sh/$2,307.9k, SGOV, SCHD, VOO, BMY, SCHG, two unnamed |
| 3 | The sort key doesn't matter here | Same script reconstructs under both `imported_at` and `snapshot_date` ordering — identical account→snapshot map and identical book |
| 4 | Schwab vs Schwab 876 aren't aliases | `.tmp_d1/review_queries.py` — `schwab` holds only NVDA, `schwab 876` holds six others |
| 5 | Backup is a true equivalent | Tool verified 88 tables, every row hashed, values type-tagged and length-framed, `sqlite_master` per object |
| 6 | Nothing else wrote during the repair | `data_version` stable over a 5s settle window, EXCLUSIVE probe acquired, restore under `BEGIN IMMEDIATE`, interloper checks clean post-backup and pre-restore |
| 7 | Self-refresh no longer re-truncates | Ran it twice post-restore: 46 positions and 4 accounts preserved both times |
| 8 | NVDA present but excluded | `load_total_book` → total 45/$4,186.0k, managed 44/$1,735.9k, NVDA in total, absent from managed, `degraded=False` |

Reproduction scripts are untracked in `.tmp_d1/` (all read-only or copy-based):
`rederive_target.py`, `review_queries.py`, `verify_restore.py`,
`check_flags_after_refresh.py`, `find_dropped.py`, `final_state.py`,
`verify_safety.py`.

## 5. Verification evidence

Tool output from the apply (13.3s):

```
quiesce: data_version stable at 2 over 5s and EXCLUSIVE lock acquired
checkpoint: wal_checkpoint(TRUNCATE) busy=0 log_pages=0; wal now 0B
backup written: db/argosy.db.bak_pre_restore.20260808T200547Z
backup verification: reconciled ... whole-DB: 88 tables, every row hashed and matching
interloper check (post-backup): no other connection committed (data_version 2)
interloper check (pre-restore, lock held): no other connection committed
restored: 46 positions / $4047.6k  accounts=[aborad, leumi, schwab, schwab 876]
```

Tests: **98 passed** (`test_backfill_restore_gate`, `test_backfill_restore_backup`,
`test_ingest_durability`, `test_stream_d_managed_holdings_abstention`) and
**69 passed** including master's `test_fail_loud_status.py`, on the rebased tree.
Rebase onto master was clean.

**The full suite has now completed, and it changes this section. See §5a.**

## 5a. THE MERGE REGRESSED 23 TESTS — read this before anything else

Full suite on master (excluding the two hanging files): **39 failed, 6,809
passed** in 3h14m. 38 of the 39 reproduce on a targeted re-run, so they are not
flakes. Classified by re-running the same 38 against the pre-merge commit
`dcf5e93` in an isolated worktree:

- **15 pre-existing** — fail on both sides. My earlier claim of "4 known
  pre-existing" was wrong; that baseline came from targeted files, never a full
  run. Retracted.
- **23 regressions** — passed before the merge, fail after it.

The regressions:

| cluster | count |
|---|---|
| `test_incremental_plan.py` | 10 |
| `test_retirement_safety_gates.py::TestNraEstateGate` | 5 |
| `test_decision_funnel_position_context.py` | 3 |
| `test_decision_funnel_sleeve_mandate.py` | 3 |
| `test_assembled_artifact.py` | 1 |
| `test_stream_d_managed_holdings_abstention.py::test_resolver_sell_shares_use_tradeable_denominator` | 1 |

**They share one root cause: the new fail-closed degraded-book rule.**
`MARK_STALE_DAYS = 0` means any mark not valued *today* must be live-repriced
before it can publish. In test conditions — fixtures dated `2026-06-01`, no live
quote function — the book is always degraded, so:

```
TotalBookDegraded: live reprice unavailable for current-money marks —
  stale_mark_unpriceable:NVDA@unknown (valued_as_of=2026-06-01);
  stale_mark_unpriceable:CSPX@unknown (valued_as_of=2026-06-01)
```

`incremental_plan.py:199` raises it; the NRA estate gate returns `FAIL` instead
of `PASS`; the decision-funnel packets lose `positions_summary` and
`user_constraints` entirely (`KeyError`), meaning stage-3 context silently
disappears rather than degrading visibly.

**Production is currently NOT degraded** — `load_total_book` on live returned
`degraded=False` because the live quote fetch succeeded. But the same strictness
is now running in production: on any day the reprice fails, plan synthesis will
raise, the estate gate will read FAIL, and funnel packets will drop context.
That may be the intended fail-loud behaviour, but it is a broad new failure mode
that landed tonight without being exercised against a quote outage.

**This is the strongest argument for reverting master**, and it is the reviewer's
call. Note the data restore is independent of the code: reverting `00c89f4` does
not un-restore the book. What it would cost is the ingest guard that stops the
next Leumi-only feed re-truncating it — so a revert should not be left standing
without that protection.

## 6. Findings discovered during execution

**1. `PYTHONPATH` pointed at a worktree.** `import argosy` resolved to
`.worktrees/stream-e-async-cache-loop/argosy` — code with none of the repair —
because a `PYTHONPATH` entry outranks the editable-install `.pth`. My first
self-refresh verification ran the wrong code and was redone under master. It was
process-scoped only (User and Machine empty; the supervisor uses
`powershell -NoProfile` with its own `$Root`), so production was never affected.
Reviewer should confirm that scoping independently — if it were persistent, the
backend would silently run a worktree against live money.

**2. Quantity provenance does not survive a refresh.** The restore correctly
stamped NVDA `observed_as_of = 2026-07-13`, its true quantity date. After
self-refresh, both the snapshot row and the durable row read `2026-08-08`. The
share count is still 10,940, but the system can no longer tell the quantity is 26
days old. `quantity_is_stale` still returned True in checks, so the guard is not
fully defeated — but the "never re-date carried quantities" property that the
restore deliberately preserved is undone one refresh later. **Unfixed.**

**3. A blank-symbol cash row is dropped from the total book.** `load_total_book`
returns 45 of 46 positions; the missing one is Leumi `Cash`, NIS, `symbol=''`,
**$17.66k**. `/api/portfolio/snapshot` agrees at 45. Net worth and estate
under-count that cash. Pre-existing unnamed-row behaviour, not caused by the
restore. **Unfixed.**

## 7. Where I would attack my own work

1. **Carry-forward rests on an owner assertion, not data.** The restore assumes
   nothing traded in the three dark accounts since Jul 13. Ariel confirmed that
   explicitly, and it is unverifiable from inside the system — there is no fills
   ledger for those accounts. If that assertion is wrong, the book is confidently
   wrong and every check above still passes. A broker statement would settle it.
2. **The 8 carried rows are stale marks, not just stale prices.** Reprice fixes
   price. It cannot fix a quantity that changed unobserved.
3. **Merged without a full-suite green** (§5).
4. **Finding 2 half-undoes a property the restore advertises.** I verified the
   guard still fires but did not fix the root cause, and I did not check every
   consumer that might read `observed_as_of` and now see a false date.
5. **The unnamed rows.** $69.0k in Aborad and $5.9k in Schwab 876 restored as
   opaque `-` symbols, and a third unnamed row ($17.66k) is silently dropped
   (finding 3). ~$92k of the book is either unidentified or invisible.
6. **NVDA is 58.5% of the book at a price fetched once.** Every downstream number
   is hostage to that single quote; I did not cross-check it against a second
   source.
7. **Only the gate was heavily reviewed.** Five adversarial rounds and 14 fixed
   blockers all targeted the *safety machinery*. The restore *logic* got one
   review pass (the note Ariel signed off) plus my independent re-derivation.

## 8. Known pre-existing failures — do not attribute these to this work

Verified pre-existing by stashing to the parent commit:
`test_ingest_trail_writes_agent_reports` (invalid `AgentReport(attempt=...)`);
`test_nvda_shares_sold_ytd_populates_from_fills` (`3380 != 520`); two more in
`tests/test_plan_draft_api.py`, same `shares_sold_ytd` cause. Environment:
`pytest-timeout` is not installed; `tests/test_api_phase4.py` and
`tests/test_api_jobs.py` both hang the suite.

## 9. Highest-value work still owed

**Re-derive the downstream numbers.** NVDA sell quotas, concentration, US-situs
estate exposure, retirement gates, FI shock — all last computed on a book missing
59% of its value, none recomputed yet. This is the actual point of the repair and
it is not done.

Then: findings 2 and 3; name the unnamed rows; tighten the walk sort key to
`(snapshot_date, imported_at)`; and the trusted-book accessor Ariel approved —
six modules still call `parse_portfolio_tsv` directly (`nvda_sales_history`,
`cli/critique`, `cli/ingest`, `daily_brief`, `daily_brief_runner`, `leumi_tsv`),
bypassing the ingest guard that now protects the book.

## 10. Prior documents

- `2026-08-08-foundation-commits-review-note.md` — the restore-logic review Ariel
  signed off, including the §3/§4/§5 answers
- `2026-08-08-p1-restore-complete.md` — the execution record
- `2026-08-08-p1-repair-rehearsal-evidence.md` — five adversarial rounds, 14
  blockers, and retractions of claims later falsified
- `2026-08-08-p1-review-package.md` — the pre-restore package
