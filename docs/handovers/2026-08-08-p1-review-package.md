# P1 book repair — review package (2026-08-08)

Self-contained brief for a reviewer who has not seen this work. Everything
needed to reproduce and judge it is here.

## The incident being repaired

On 2026-07-13 a Leumi-only TSV import **replaced the entire book** instead of
merging into it. Every non-Leumi account was erased: Schwab, Schwab 876, Aborad
— including the NVDA position. **$2,432.0k of $4,047.6k (59%) vanished from the
book.** Argosy has been computing concentration, US-situs estate exposure,
retirement gates, FI shock and NVDA sell quotas on the remaining 38 positions /
$1,615.6k ever since.

Production is **still in that truncated state**. Nothing here has been merged or
run against it.

## What the work is

Two halves that must land together:

1. **A data restore** — reconstruct the 46-position / $4,047.6k book by carrying
   forward the accounts the truncating feed never covered.
2. **A repair gate + ingest durability** — so the restore is safe to perform,
   provably reversible, and cannot be silently undone by the next feed.

## Where it lives

- Branch: `feat/stream-d-managed-holdings-abstention`
- Worktree: `D:\Projects\financial-advisor\.worktrees\stream-d-managed-holdings`
- Series: `2fd5a08` … `df1ce89` (8 commits, +2064 −82, 8 files) — **unmerged**
- master is at `0027459` and does not contain any of it

```
2fd5a08 WAL-safe timestamped pre-restore backups
8245fa7 gate the book repair on quiesce, checkpoint, verified backup
9a08282 stop dropping changed feeds and swallowing account-erasure
41a6be8 harden the restore gate and ingest digest (review round 1)
b929861 make the interloper check real, verify by content (round 2)
b63854a restore under a write lock; verify as equivalent databases (round 3)
44d730e make the value fingerprint injective across SQLite types (round 4)
df1ce89 cover the length-framing mutation the test missed (round 5)
```

## SAFETY RULE for the reviewer

`D:\Projects\financial-advisor\db\argosy.db` is the **real $2.4M database**.
Never open it for writing, never point the repair tool at it. Read-only
(`mode=ro` URI) inspection only. Work on temp copies. The repair tool will
refuse the live path without an explicit override flag — do not pass that flag.

## How to run the tests

```
cd D:\Projects\financial-advisor\.worktrees\stream-d-managed-holdings
D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" -q ^
  tests/test_backfill_restore_gate.py ^
  tests/test_backfill_restore_backup.py ^
  tests/test_ingest_durability.py
```

Expected: **34 passed**.

Two environment facts that will otherwise waste your time:
- `tests/test_api_phase4.py` **hangs the suite**. Do not run the full suite.
- `pytest-timeout` is **not installed**.

## Known pre-existing failures (NOT caused by this work)

Verified by stashing to the parent commit and re-running:
- `test_ingest_trail_writes_agent_reports` — invalid `AgentReport(attempt=...)` kwarg
- `test_nvda_shares_sold_ytd_populates_from_fills` — `3380 != 520`
- two more in `tests/test_plan_draft_api.py`, same `shares_sold_ytd = 3380` cause

## The gate, as it now stands

| # | Step | Mechanism |
|---|---|---|
| 1 | Designation | `--apply` requires `--staged-copy` XOR `--i-really-mean-the-live-db`. No basename guessing. `--staged-copy` can never authorize a database identified as live. |
| 2 | Quiescence | `PRAGMA data_version` across a settle window from ONE held-open connection, plus a `BEGIN EXCLUSIVE` probe. `--settle-seconds 0` refused; overrides unavailable for the live DB. |
| 3 | Checkpoint | `wal_checkpoint(TRUNCATE)` must report `busy=0` and a zero-length WAL, else refuse. |
| 4 | Backup + verify | Every row of every table hashed; values typed and length-framed; `sqlite_master` fingerprinted per object; unreadable tables fail closed. |
| 5 | Restore | Performed under `BEGIN IMMEDIATE` on the session's own connection. |

Measured on a 355MB copy of live: full `--apply` in **20.5s**, restoring 46
positions / $4,047.6k, 88 tables hashed and matching.

**Deliberate asymmetry to scrutinise:** the RESTORE is protected by exclusion (a
held write lock); the BACKUP is protected only by detection. Reason: `sqlite3`'s
backup API blocks indefinitely when the source connection is inside a write
transaction (measured — a probe hung and had to be killed). If you can show a
race-free way to hold a lock across the backup too, that is a finding.

## Review history — please do not just re-confirm these

Five independent adversarial rounds already ran against this code. **Fourteen
confirmed blockers**, all fixed; round five returned COMMIT AS-IS. The already-
found-and-fixed list, so you can spend your effort elsewhere:

- quiesce was a WAL-size heuristic (passed a DB committing 143 txns/window)
- `--settle-seconds 0` silently passed; incomplete checkpoint only logged
- `PRAGMA data_version` sampled on a fresh connection each time — inert
- TOCTOU window between the pre-restore check and the restore
- backup verification: latest-snapshot-only → row-counts-only → both defeatable
- TEXT vs BLOB collision, then the in-band tag collision that replaced it
- identity-only path guard, then a basename guard defeated by `production.sqlite`
- feed digest ignoring FX, allocations, real estate, pensions, cost basis, currency
- rejected feeds reaching plan synthesis, and a user-visible `today_shares=999`

## Where I would focus a fresh reviewer

1. **The restore itself, not just the gate.** Almost all review attention so far
   went to the gate. `backfill_restored_holdings_book()` in
   `argosy/services/holding_books.py` decides *what the restored book contains* —
   the account-coverage carry-forward logic, symbol identity/renames, and whether
   genuinely-sold positions (`NKE`, `RKT`, `SPCX`) stay sold. A wrong restore
   passes every gate check perfectly.
2. **Is 46 / $4,047.6k actually the right target?** It reconciles as 38 + 8
   positions and $1,615.6k + $2,432.0k, but that arithmetic came from my own
   analysis of the vanished rows. Re-derive it independently from
   `portfolio_snapshots` history rather than trusting the number.
3. **The unmanaged-holdings model.** NVDA is intentionally excluded from the
   managed sleeve but must still count for estate, FX and net worth. Migration
   `0097` plus `unmanaged_holdings` implements "excluded but present". Check the
   staleness split (quantity vs valuation) and that a stale mark cannot be
   published as a current number.
4. **The residual raw-TSV readers** (see below) — specifically whether any of
   them can put a rejected feed in front of a decision agent.

## Open design question (unresolved, needs the owner)

Six modules still call `parse_portfolio_tsv` directly, bypassing the ingest
guard entirely: `nvda_sales_history.py`, `cli/critique.py`, `cli/ingest.py`,
`orchestrator/loops/daily_brief.py`, `services/daily_brief_runner.py`,
`adapters/brokers/leumi_tsv.py`.

Two leaks were demonstrated and fixed at source (plan-synthesis Phase 3; and
`/draft/nvda-trajectory`, which showed `today_shares=999` from a rejected feed).
The rest were deliberately NOT point-fixed — the same root cause resurfacing in
three consecutive review rounds is the signal that it wants ONE trusted-book
accessor that consumers call instead of the parser. A reviewer's opinion on
scope here is welcome.

## Reproduction harness

Untracked scripts in `.tmp_d1/`: `probe_journal.py`, `wal_backup_demo.py`,
`stage_copy.py`, `safe_copy.py`, `rehearse_check.py`, `verify_backups.py`,
`refresh_no_truncate.py`. All open live read-only or operate on copies.

Full evidence, including retractions of earlier claims that later rounds
falsified: `docs/handovers/2026-08-08-p1-repair-rehearsal-evidence.md`.
