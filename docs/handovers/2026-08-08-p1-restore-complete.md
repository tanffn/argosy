# P1 — the $2.4M book restore is DONE on production (2026-08-08)

Owner authorized after reviewing `2026-08-08-foundation-commits-review-note.md`
(§2 no un-ingested activity, §3 two genuinely separate Schwab accounts).

## Outcome

Production went from **38 positions / $1,615.6k / Leumi only** to
**46 positions / $4,047.6k across all four accounts**, then repriced live to
**$4,191.4k**. The 26-day-old truncation is closed.

| snapshot | positions | total | accounts | source |
|---|---|---|---|---|
| 49 (before) | 38 | $1,615.6k | 1 | self-refresh (truncated lineage) |
| **50** | **46** | **$4,047.6k** | **4** | `backfill:last_coverage_restore` |
| 51 | 46 | $4,191.4k | 4 | self-refresh (reprice) |
| 52 | 46 | $4,191.4k | 4 | self-refresh (reprice, master code) |

NVDA is back as **present but unmanaged**: 10,940 sh, repriced from the stale
July mark of $210.96 to $223.96 = $2,450.1k, `managed=False`, with a durable
`unmanaged_holdings` row. It is **in** the total book (estate, FX, net worth) and
**out** of the managed sleeve (44 positions / $1,735.9k). 58.5% of the book.

## Sequence actually executed

1. Independent safety copy → `db/argosy.db.SAFETY_pre_repair.20260808T221356Z`,
   verified standalone: 86 tables, all row counts matching live, integrity ok.
2. Rebased the 12 commits onto master — **clean, no conflicts**.
3. Tests on the rebased tree: **98 passed** (gate + durability + stream-D) and
   **69 passed** including master's new `test_fail_loud_status.py`, confirming
   the repair composes with the fail-loud job-status contract rather than
   fighting it.
4. Quiesce verified: 0 python processes, nothing listening on 8000, WAL 0 bytes,
   supervisor task logon-triggered only (cannot self-fire).
5. `alembic upgrade head` on live: `0094 → 0097_unmanaged_holdings`.
6. Dry run: 46 / $4,047.6k, carried `aborad, schwab, schwab 876`.
7. **Apply, 13.3s.** Tool log: quiesce `data_version` stable over 5s +
   EXCLUSIVE acquired; `wal_checkpoint(TRUNCATE)` busy=0, WAL 0B; backup
   `db/argosy.db.bak_pre_restore.20260808T200547Z` verified whole-database
   (88 tables, every row hashed and matching); interloper check clean both
   post-backup and pre-restore under the held lock; restored, snapshot id=50.
8. Fast-forward merge to master → **`00c89f4`** (40 files, +8394 −438).
9. Self-refresh run twice: **no re-truncation**, all 4 accounts and 46 positions
   preserved; the second run under master's code also restored `managed=False`.
10. Backend restarted: `/api/health` → `status ok, db ok, git_sha 00c89f4`.
    `/api/portfolio/snapshot` returns all four accounts.

Two rollback points exist, both content-verified:
`SAFETY_pre_repair.20260808T221356Z` and `bak_pre_restore.20260808T200547Z`.

## Findings discovered DURING execution (not previously known)

**1. `PYTHONPATH` pointed at the stream-E worktree.** `import argosy` resolved to
`.worktrees/stream-e-async-cache-loop/argosy` — code with none of the repair —
because a `PYTHONPATH` entry outranks the editable-install `.pth`. My first
self-refresh verification therefore ran the WRONG code and had to be redone.
Scope check: it was **process-scoped only** (User and Machine both empty) and the
supervisor launches `powershell -NoProfile` with its own `$Root`, so production
was never affected. Worth knowing that any shell inheriting it silently runs a
worktree's code against the live DB.

**2. Quantity provenance was lost by the refresh path.** The restore correctly
stamped NVDA's `observed_as_of = 2026-07-13` (its true quantity date). After the
self-refresh, both the snapshot row and the durable `unmanaged_holdings` row read
`observed = 2026-08-08`. The share count is still right (10,940), but the system
can no longer tell the quantity is 26 days old — the "never re-date carried
quantities" property the restore was careful about does not survive a refresh.
`quantity_is_stale` did still return True during checks, so the guard is not
fully defeated, but this needs a follow-up.

**3. A blank-symbol cash row is dropped from the total book.** `load_total_book`
returns 45 of the 46 positions; the missing one is a Leumi `Cash` row, NIS,
`symbol=''`, **$17.66k**. `/api/portfolio/snapshot` shows the same 45. Net
worth / estate therefore under-count that cash. Pre-existing behaviour of the
unnamed-row handling, not caused by the restore.

## Still owed

- **Re-derive the downstream numbers** on the restored book: NVDA sell quotas,
  concentration, US-situs estate exposure, retirement gates, FI shock. All were
  computed on a book missing 59% of its value and none has been recomputed yet.
- Findings 2 and 3 above.
- Owner's deferred items: name the two unnamed rows ($69.0k Aborad, $5.9k
  Schwab 876); tighten the history walk sort key to `(snapshot_date,
  imported_at)` (verified harmless today — both orderings give the same book).
- The trusted-book accessor refactor (owner chose to build it): six modules still
  call `parse_portfolio_tsv` directly — `nvda_sales_history`, `cli/critique`,
  `cli/ingest`, `daily_brief`, `daily_brief_runner`, `leumi_tsv`.
- Pre-existing branch debt, verified as pre-existing by stashing to the parent:
  `AgentReport(attempt=...)` kwarg; `shares_sold_ytd = 3380` (3 tests);
  `tests/test_api_phase4.py` hangs the suite, and `tests/test_api_jobs.py` was
  observed to hang the same way.
