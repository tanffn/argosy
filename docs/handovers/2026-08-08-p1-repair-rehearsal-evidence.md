# P1 repair — rehearsal evidence (2026-08-08)

Evidence package for the owner-defined repair gate. **Nothing in here touched
production.** The live `db/argosy.db` was opened read-only or copied; it remains
at migration `0094_expense_tag_rules` with the truncated book (snapshot 49, 38
positions / $1,615.6k, Leumi only).

Code under test: branch `feat/stream-d-managed-holdings-abstention` at `2fd5a08`
(D-1 only; findings 2-9 are parked in `stash@{0}` and were NOT in the tree).

---

## The gate, and what is proven

| # | Gate step | Status |
|---|---|---|
| 1 | Stop backend + scheduler; confirm no `-wal` growth for a settle window | **NOT DONE** — no automated preflight yet |
| 2 | `PRAGMA wal_checkpoint(TRUNCATE)` to fold WAL into main | **NOT DONE** — not yet implemented |
| 3 | Content-verified backup on the quiesced file | **MECHANISM PROVEN**; content-verification not yet enforced *inside* the script |
| 4 | Apply data restore + code fixes | **REHEARSED** on a copy of live |
| 5 | Content-verify the result | **PROVEN** on the rehearsal |
| 6 | Controlled re-enable; first self-refresh must not re-truncate | **PROVEN in rehearsal** (with injected quotes) |

---

## 1. Why `integrity_check` is disqualified — reproduced independently

Production is `journal_mode = wal`, `page_size 4096`, `wal_autocheckpoint 1000`.

On a throwaway WAL-mode database with two committed, un-checkpointed rows:

```
source after commit : 2 rows / 2313.7 / integrity=ok
source sidecars     : argosy_like.db=4096B, argosy_like.db-wal=12392B, argosy_like.db-shm=32768B

shutil.copy2 backup : UNUSABLE (no such table: holdings) integrity=ok
backup API          : 2 rows / 2313.7 / integrity=ok
```

The `copy2` "backup" is not merely missing rows — **the table does not exist**,
because the schema itself was still in the WAL while the main file was a 4KB
header. And it **still reports `integrity_check = ok`**. A structural check
blesses garbage, so verification must be content reconciliation.

Concurrent-writer behaviour of the backup API (a second connection committing 200
rows during the backup):

```
source after writer : 202 rows / 2513.7 / integrity=ok
concurrent backup   : 2 rows / 2313.7 / integrity=ok
```

Boundary semantic: **transactions committed before the backup begins are
guaranteed present; those committed during it may not be.** The result is always
internally consistent. That is acceptable *only* because gate step 1 quiesces the
writer first — without quiescing, "may not be" includes real money.

## 2. Safe copy of the real 354MB database

Source opened read-only; `sqlite3.Connection.backup`; **1.4 s**.

```
source size 354,893,824B -> copy 354,893,824B

            LIVE                          COPY
  integrity      ok                     ok                     MATCH
  alembic        0094_expense_tag_rules 0094_expense_tag_rules MATCH
  table_count    86                     86                     MATCH
  latest_snap    49                     49                     MATCH
  portfolio_snapshots 49 / 49            MATCH        proposals 20 / 20   MATCH
  predictions    1325 / 1325            MATCH    decision_runs 280 / 280  MATCH
  agent_reports  3196 / 3196            MATCH  position_stances 37 / 37   MATCH
  plan_versions  89 / 89                MATCH         verdicts 33 / 33   MATCH
```

## 3. Migration 0097 on the copy

```
0094_expense_tag_rules -> 0097_unmanaged_holdings

policy rows              : [('ariel', 'NVDA')]
unmanaged_holdings rows  : ('ariel', 'NVDA', 'schwab', 10940.0, '2026-07-13')
latest snapshot 49       : 38 positions / leumi $1615.6k
NKE / RKT / SPCX         : absent (correct — genuine sales)
```

The migration establishes the **durable unmanaged-but-present record** for NVDA
and seeds the policy row, but **does not restore the book**. The backfill does
that. Both are required.

## 4. Restore, on the migrated copy

```
DRY-RUN : 46 positions / $4047.6k accounts_covered=[aborad, leumi, schwab, schwab 876]
                                  accounts_carried=[aborad, schwab, schwab 876]
APPLY   : backup written: ...bak_pre_restore.20260808T145050Z
          restored: 46 positions / $4047.6k  new snapshot id=50
APPLY#2 : noop: 46 positions / $4047.6k  (distinct backup ...T145053Z)
```

Reconciles to the incident arithmetic: 38 + 8 positions, $1,615.6k + $2,432.0k.

## 5. Content reconciliation of the backups (the new verification standard)

Two distinct timestamped files, both standalone (no `-wal`/`-shm` sibling needed):

```
rehearsal_migrated.db.bak_pre_restore.20260808T145050Z  354,922,496B
rehearsal_migrated.db.bak_pre_restore.20260808T145053Z  354,947,072B
```

| | snapshot | positions | total | accounts | NVDA | row-hash |
|---|---|---|---|---|---|---|
| backup `...145050Z` (pre-restore) | 49 | 38 | $1615.6k | leumi | none | `551c8e52efb9ab32` |
| backup `...145053Z` (pre-noop) | 50 | 46 | $4047.6k | 4 accounts | 10,940 / $2307.9k | `bd9325ba280ae237` |
| current file (post-restore) | 50 | 46 | $4047.6k | 4 accounts | 10,940 / $2307.9k | `bd9325ba280ae237` |

Ordering is correct: the pre-restore backup holds exactly the state a rollback
would return to. `NKE`/`RKT`/`SPCX` absent in all three.

## 6. Does the first self-refresh re-truncate? — No

Full `refresh_portfolio_snapshot` against the restored copy:

```
BEFORE : 46 positions / $4047.6k :: aborad=1/$69.0k leumi=38/$1615.6k schwab=1/$2307.9k schwab 876=6/$55.1k
AFTER  : 46 positions / $4071.7k :: aborad=1/$69.0k leumi=38/$1616.1k schwab=1/$2331.0k schwab 876=6/$55.6k

positions lost : NONE          positions gained : NONE
NVDA after     : 10,940 sh / $2331.0k
accounts after : [aborad, leumi, schwab, schwab 876]
RE-TRUNCATED   : NO
repriced 39, carried 7, warnings []
```

Session rolled back; the staged copy was left unchanged.

**Honest limits of this proof.** Quotes and FX were injected deterministically
(prices taken from the snapshot, nudged 1%), because the property under test is
which *positions* survive, not what they are worth. More importantly: this
exercises the **refresh** path. **The original $2.432M loss was an INGEST**, and
the ingest-side protections (loud rejection of an account-erasing feed on both
the TSV and XLS routes, and the content-blind `latest_matches_snapshot`) are
still in flight. Do not read this as "the book is safe from re-truncation."

---

## UPDATE — items 1-3 closed in `8245fa7` (branch worktree)

The gate now runs *inside* the tool that does the writing, in order, on `--apply`:

```
APPLY reconstruction: 46 positions / $4047.6k accounts_covered=['aborad', 'leumi', 'schwab', 'schwab 876']
quiesce: WAL stable at 0 bytes over 2s
checkpoint: wal_checkpoint(TRUNCATE) busy=0 log_pages=0 checkpointed=0; wal now 0B
backup written: ...\fresh_gate.db.bak_pre_restore.20260808T150409Z
backup verification: reconciled: snapshot 49, 38 positions / $1615.6k, accounts={'leumi': 1615.6}, row_hash=551c8e52efb9ab32
restored: 46 positions / $4047.6k accounts=['aborad', 'leumi', 'schwab', 'schwab 876']
new snapshot id=50
```

The backup's `row_hash=551c8e52efb9ab32` was produced by the tool's own
`book_fingerprint`, and equals the value my independent external harness
computed for the same pre-restore state — two separately written
implementations agreeing.

A second `--apply` is a no-op and takes its own distinct timestamped backup,
which reconciles the *post*-restore state (`snapshot 50, 46 positions /
$4047.6k`, four accounts, `row_hash=bd9325ba280ae237`), so repeated runs never
clobber an earlier rollback point.

Guard, both directions: the real `db/argosy.db` is refused by identity; a staged
copy *named* `argosy.db` is now accepted with no override.

**Tests: 13 pass** via
`python -m pytest -m "not llm_eval" -q tests/test_backfill_restore_gate.py tests/test_backfill_restore_backup.py`.
Each fix carries a revert detector, and a mutation run proved every one bites:

| reverted fix | matching test |
| --- | --- |
| basename refusal re-added | fails |
| quiesce always reports idle | fails |
| backup verification blesses anything | fails |
| checkpoint downgraded to `PASSIVE` | fails |

Two corrections to this document's earlier claims, both found by writing those
tests:

- **`copy2` is not unconditionally unsafe.** SQLite checkpoints on
  last-connection close, so a *cleanly closed* database copies fine. The data
  loss requires a connection still holding un-checkpointed WAL content — which
  is exactly the production state with the backend running, but the earlier
  blanket phrasing was too strong. It also means gate step 2 (checkpoint first)
  is what actually removes this risk; the backup API is now belt-and-braces.
- **An unreadable backup was raising instead of failing verification.** The
  `copy2` artefact can lack the schema entirely, so `book_fingerprint` threw
  `OperationalError` rather than reporting a mismatch. Fixed to return "does not
  verify — no rollback point".

## UPDATE — ingest durability closed in `9a08282` (branch worktree)

Three silent paths that would have undone a repaired book:

- **`latest_matches_snapshot()` was content-blind.** Same `source_path`, same
  date, same row count ⇒ "already ingested", so a corrected re-export was
  dropped and the book kept stale values. Now compares a digest of the feed's
  economic content, stored as `totals_json.feed_row_hash`. A row with no digest
  (legacy, or written by the restore path, which is not a feed) is deliberately
  *not* a match: err toward a guarded write, never toward silently dropping.
- **The XLS/OSH pair path swallowed `SnapshotIngestRejected`** into a generic
  warning. It now returns an operator-visible `SNAPSHOT_INGEST_REJECTED` line
  naming what would have been erased, without breaking the pair.
- **Plan-synthesis input assembly swallowed the same rejection and then built
  inputs from the rejected feed** — the database was protected while the planner
  synthesised against the truncated book. Now discarded.

9 new tests; the mutation run confirms all four revert detectors bite
(content-blind matcher, digest-less row treated as a match, swallowed pair
rejection, "use the rejected feed anyway").

One correction to an assumption I carried into this work: I expected a
Leumi-only feed to be *rejected*. It is not, and should not be — the
per-account merge carries uncovered accounts forward, so a partial feed is no
longer destructive and there is nothing to refuse. The rejection path is for
feeds that claim an account and then drop its positions. The test now asserts
the real property (schwab survives and is recorded as carried).

Also confirmed pre-existing on this branch, unrelated to my changes (verified by
stashing and re-running at `8245fa7`): `test_nvda_shares_sold_ytd_populates_from_fills`
and `test_ingest_trail_writes_agent_reports` (an `AgentReport("attempt")` kwarg
error). Ruff on the three touched modules: 18 findings before, 18 after.

## UPDATE — five adversarial review rounds (`41a6be8` … `df1ce89`)

Every hand-back above was re-verified by an independent reviewer that did not
write the code. Five rounds, fourteen confirmed blockers. **Round five returned
COMMIT AS-IS.** The rounds are recorded because several of them falsified claims
made earlier in this very document.

### Retractions — evidence above that is now known to be wrong

- **`quiesce: WAL stable at 0 bytes over 2s` (the §UPDATE `8245fa7` block) proves
  nothing.** Comparing the `-wal` sidecar SIZE at two instants is not a liveness
  test: with `wal_autocheckpoint` active a steady writer holds the WAL at a
  constant size. Measured — 143 transactions committed while the sidecar sat at
  exactly 4152 bytes and the gate reported "quiesced". Read that line as a
  *defect*, not as evidence.
- **"a staged copy named `argosy.db` is now accepted with no override"** was the
  wrong lesson from the right complaint. Identity-only refusal cannot protect a
  deployment at a path the tool does not enumerate, and a basename rule was
  defeated by pointing it at `production.sqlite`. There is no guessing left now
  (see below).
- **The mutation table claiming "every one bites"** was overstated. A later round
  found five tests that still passed with the behaviour reverted, and one test
  in a subsequent round that missed removal of length framing. Both are fixed;
  the lesson is that "mutation-tested" is only as good as the mutations chosen.

### What each round found

| Round | Confirmed blockers | The one that mattered most |
|---|---|---|
| 1 | 5 | Quiesce gate was a size heuristic (above) |
| 2 | 4 | `PRAGMA data_version` sampled on a FRESH connection each time — inert; an intervening commit read `2 -> 2` |
| 3 | 4 | TOCTOU: restore only re-checked and hoped, leaving a real commit window |
| 4 | 1 | Fingerprint non-injective: TEXT `"abc"` == BLOB `b"\x00T\x00abc"` |
| 5 | 0 | — (COMMIT AS-IS; one non-blocking test gap, closed in `df1ce89`) |

### The gate as it now stands

1. **Designation.** `--apply` requires either `--staged-copy` or
   `--i-really-mean-the-live-db`. No basename heuristics; the tool never guesses.
   The two flags are deliberately different so a rehearsal cannot train the
   fingers that run the real repair, and `--staged-copy` can *never* authorize a
   database identified as live.
2. **Quiescence.** `PRAGMA data_version` sampled across a settle window from ONE
   held-open connection (the only form in which SQLite defines it), plus a
   `BEGIN EXCLUSIVE` probe for actual mutual exclusion. `--settle-seconds 0` is
   refused; on the live database the overrides do not exist at all.
3. **Checkpoint.** `wal_checkpoint(TRUNCATE)` must report `busy=0` and leave a
   zero-length WAL. An incomplete checkpoint is a failure, not a log line.
4. **Backup + verification.** Every row of every table is hashed, with values
   serialised under an explicit type tag and explicit length, so TEXT ≠ BLOB,
   `INTEGER 1` ≠ `REAL 1.0`, `NULL` ≠ `""`, and no value can impersonate the
   framing around it. `sqlite_master` is fingerprinted per object, so a changed
   view, index or trigger fails. An unreadable table fails closed.
5. **Restore under a lock.** `BEGIN IMMEDIATE` is taken on the session's own
   connection before the restore, so no other connection can write until commit.
   Verified independently with a real concurrency test: writers are blocked for
   the duration, and a failed lock leaves both source and backup intact.

**Why the backup is not also under that lock:** `sqlite3`'s backup API blocks
indefinitely when the source connection is inside a write transaction (measured
— the probe hung). So the backup window is covered by detection (the held-open
`InterloperWatch`) rather than exclusion, and the restore window is covered by
exclusion. That asymmetry is deliberate and is documented in the code.

### Cost on the real database

Full `--apply` against a 355MB copy of live: **20.5s**, including hashing every
row of all 88 tables twice. Restores to 46 positions / $4047.6k. Scaling was
independently checked as linear (20k rows 0.123s, 40k rows 0.243s).

### Residual, accepted

- **Raw-TSV readers outside the guard.** `nvda_sales_history.py`,
  `cli/critique.py`, `cli/ingest.py`, `daily_brief.py`,
  `daily_brief_runner.py` and `leumi_tsv.py` still call `parse_portfolio_tsv`
  directly, so a rejected feed can reach them. Two demonstrated leaks were
  fixed at source (plan-synthesis Phase 3, and `/draft/nvda-trajectory`, which
  was showing `today_shares=999` from a rejected feed). **The rest is a design
  item, not a point fix:** it wants ONE trusted-book accessor that consumers
  call instead of the parser. Fixing six call sites individually is the
  whack-a-mole antipattern. **Needs Ariel's decision.**
- **Pre-existing branch debt**, confirmed by stashing to the parent commit and
  re-running: `AgentReport("attempt")` kwarg error; `shares_sold_ytd = 3380`
  affecting three tests; `tests/test_api_phase4.py` hangs the suite.

## Still required before a production run

1. **Land on master** and independently re-verify there — this evidence is from
   a branch worktree. Data restore and code fix must land together.
2. **A quiesced window**: stop the backend and the scheduler, then
   `--apply --i-really-mean-the-live-db`. The tool will refuse if they are still
   running, which is the point.
3. After the repair: **re-derive** NVDA sell quotas, concentration, US-situs
   estate, retirement gates and FI shock. Every current figure was computed on
   the truncated book.

## Reproduce this

Harness scripts live in `.tmp_d1/` (untracked): `probe_journal.py`,
`wal_backup_demo.py`, `stage_copy.py`, `safe_copy.py`, `rehearse_check.py`,
`verify_backups.py`, `refresh_no_truncate.py`. All open live read-only or operate
on copies.
