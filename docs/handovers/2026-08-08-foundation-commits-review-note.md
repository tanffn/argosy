# Review note — the four foundation commits (restore logic)

For Ariel, reviewing now. These are the commits that decide **what your restored
book actually contains**. My eight commits sit on top and only decide whether
the restore is performed *safely*. A perfect gate around a wrong restore still
gives you a wrong book, which is why this is the part worth your eyes.

```
10dfbbc  wip(holdings): model NVDA as unmanaged rather than absent      29 files +4270 −298
6c07ff9  fix(holdings): merge snapshot ingest per account                6 files +1028 −167
e6588bf  fix(holdings): last-coverage merge, restore backfill, 6 blockers 9 files +1020 −62
03b7692  fix(holdings): stop alias rewrite, restore dashboard math      10 files  +244 −61
```

Core file: `argosy/services/holding_books.py` (1,783 lines, new).
Migration: `0097_unmanaged_holdings.py`.

---

## 1. What the restore algorithm actually does

`resolve_prior_positions_by_account_coverage()` (line 236). Plain English:

> Walk the snapshot history newest-first. Group each snapshot's positions by
> account. The first time you see an account, take **all** of its rows from that
> snapshot and stop looking at that account. Stamp each carried row with the
> date of the snapshot it came from.

So the restored book is a **union of per-account time slices**: Leumi as of its
last feed, Schwab as of *its* last feed (July), Aborad as of its last, and so
on. Each account is internally consistent; the book as a whole is a composite of
different dates.

The principle underneath is "coverage ≠ emptiness": a feed that never mentions
Schwab is silent about Schwab, not evidence that Schwab is empty. That is the
exact inversion of the July bug, and I think the principle is right.

## 2. The one assumption that can bite you

**Carry-forward assumes nothing happened in an account while it was dark.**

If you sold or bought anything in Schwab after 2026-07-13, and no Schwab feed
has landed since, the restore will faithfully bring back the *pre-trade* rows as
though they were still held. The code cannot know otherwise — there is no fills
ledger to reconcile against.

This is the question I most want your answer to, because you are the only source
of truth for it: **has there been any activity in Schwab, Schwab 876 or Aborad
since mid-July that Argosy never ingested?** If yes, the restored book is stale
in a way no amount of verification will catch, and we should ingest a fresh
broker file for those accounts *before* restoring rather than after.

The known sales (`NKE`, `RKT`, `SPCX`) are only correctly treated as sales if
they happened in an account that a *later* snapshot covered. Within a covered
account, a symbol's disappearance is read as a sale (line 338). Across a dark
account, it is not visible at all.

## 3. Account identity — where a double-count could hide

`location_account_key()` (line 110) is just the lowercased, whitespace-collapsed
location string. `schwab 876` and `schwab` are deliberately **different
accounts** — that deliberateness is what stops a one-account feed retiring the
other's holdings.

The flip side: if the same real account has ever been written with two different
labels (`Schwab` vs `Schwab 999` vs a typo), the restore treats them as two
accounts and carries **both** forward — a silent double-count of real money.

Check it yourself, read-only:

```sql
SELECT DISTINCT json_extract(value, '$.location')
FROM portfolio_snapshots, json_each(positions_json)
WHERE user_id = 'ariel'
ORDER BY 1;
```

If that list has near-duplicates that are really the same account, tell me and I
will add them to the alias map before we restore.

## 4. A specific thing I have NOT verified (possible bug)

The history walk orders by **`imported_at` descending** (line 256), i.e. by when
the file was loaded — not by `snapshot_date`, i.e. what date the file describes.

Those usually agree. They disagree if an older file was ever imported late (a
backfill, a re-import of an archived TSV). In that case a late-imported *old*
file would count as an account's "most recent coverage" and beat a
correctly-dated newer one, restoring genuinely outdated rows.

I have not checked whether your history contains such an inversion. It is a
one-query answer and I will run it before any restore:

```sql
SELECT id, snapshot_date, imported_at, source_path
FROM portfolio_snapshots WHERE user_id = 'ariel'
ORDER BY imported_at DESC;
```

If `snapshot_date` is not monotonic in that ordering, the sort key is wrong and
should be `(snapshot_date, imported_at)`. Flagging it as an open question rather
than asserting it is fine.

## 5. The target numbers are hardcoded, and that is mostly good

```python
EXPECTED_RESTORED_POSITION_COUNT = 46
EXPECTED_RESTORED_USD_K = 4047.6   # tolerance ±0.5
```

The restore **asserts** the reconstruction hits these and refuses to write
otherwise (lines 1588–1597), then re-verifies after building the row and rolls
back on mismatch (1702–1716). It therefore cannot quietly produce some other
book: it either produces exactly this one or fails loudly.

That narrows your review to a single question: **is 46 positions / $4,047.6k the
right answer?** It reconciles as 38 surviving + 8 erased and $1,615.6k +
$2,432.0k — but both halves of that arithmetic came from my own analysis of the
vanished rows, so it is not independent. If you want, I can re-derive it from
snapshot history with a script you can read first.

Note also that $4,047.6k is a **sum of marks from different dates**, not today's
money. `MARK_STALE_DAYS = 0` means any stored mark must be re-priced live before
it is allowed to publish as a current figure, so the headline should be treated
as a reconstruction checksum rather than your net worth.

## 6. What is genuinely well-built here

Said plainly so the note isn't only doubts:

- **Fail-closed prerequisites.** Refuses to restore if migration `0097` tables
  are absent, because NVDA would come back as *managed* and distort sleeve math
  (line 1525). This caught a real silent failure during rehearsal.
- **Idempotent.** Re-running against an already-restored book returns `noop`
  without writing (line 1607).
- **Unmanaged sync is transactional with the restore.** If the durable
  unmanaged rows fail to write, the whole restore rolls back (line 1690).
- **Dates are never re-stamped.** Carried rows keep their own `observed_as_of`,
  so a July quantity cannot masquerade as an August observation.
- **Renames aren't sales.** One entry today, the Hebrew TA-200 fund; the earlier
  version of this rewrote the stored symbol and `03b7692` correctly reduced it
  to a merge key only.

---

## ANSWERS — §3, §4 and §5 now checked (read-only, against live)

Run with `.tmp_d1/review_queries.py` and `.tmp_d1/rederive_target.py`.

**§4 sort key — inversion exists, but is provably harmless here.** One inversion
in 49 snapshots: id 34 (`2026-07-13`) was imported before a row describing
`2026-06-29`. It changes nothing, because both orderings resolve every account
to the same source snapshot and the same book:

| ordering | leumi | schwab | schwab 876 | aborad | result |
|---|---|---|---|---|---|
| `imported_at` (shipped) | 49 (08-08) | 34 (07-13) | 34 (07-13) | 34 (07-13) | 46 / $4,047.6k |
| `snapshot_date` (alt) | 49 (08-08) | 34 (07-13) | 34 (07-13) | 34 (07-13) | 46 / $4,047.6k |

Identical. The sort key stays theoretically fragile — a late-imported old file
covering a dark account would beat a correctly-dated newer one — but it is not a
defect of *this* restore. Worth tightening later, not a blocker now.

**§3 aliasing — not aliases, genuinely two Schwab accounts.** `schwab` holds
exactly one position in every snapshot (NVDA); `schwab 876` holds six (BMY,
SCHD, SCHG, SGOV, VOO and one unnamed). No double-count. **Confirm you really do
have two separate Schwab accounts and this isn't one account written two ways —
that is the only version of this that costs money.**

**§5 target — independently confirmed.** Re-derived from raw snapshot JSON
without importing any `argosy` code, so it is a genuine second opinion rather
than a re-run of the same function:

| account | positions | value | as of |
|---|---|---|---|
| leumi | 38 | $1,615.6k | 2026-08-08 |
| schwab | 1 | $2,307.9k | 2026-07-13 |
| schwab 876 | 6 | $55.1k | 2026-07-13 |
| aborad | 1 | $69.0k | 2026-07-13 |
| **total** | **46** | **$4,047.6k** | mixed |

Matches the hardcoded `46 / $4,047.6k` exactly. Production today: 38 positions,
$1,615.6k, **leumi only** — missing $2,432.0k across 8 positions.

The eight rows coming back:

```
schwab      NVDA   10,940.00 sh   $2,307.9k
schwab 876  SGOV      200.00 sh      $20.1k
schwab 876  SCHD      400.00 sh      $13.0k
schwab 876  VOO        10.00 sh       $6.9k
schwab 876  (unnamed) 5,893.00 sh     $5.9k
schwab 876  BMY       100.00 sh       $5.8k
schwab 876  SCHG      100.00 sh       $3.5k
aborad      (unnamed)   3.00 sh      $69.0k
```

Two observations on that list. **NVDA alone is 57% of the restored book** at a
July 13 mark of ~$211/share — it must be repriced before any figure derived from
it is published, which `MARK_STALE_DAYS = 0` enforces. And **two positions are
unnamed** (`-`, $69.0k in aborad and $5.9k in schwab 876); they restore as
opaque rows, and $69.0k of unidentified holding is worth naming at some point.

## 7. My recommendation

Land and restore, **conditional on your answer to §2** — whether any un-ingested
activity happened in the dark accounts since 2026-07-13. §3, §4 and §5 are now
checked and clear; §2 is the only failure mode left that the machinery cannot
detect for itself, because it depends on facts that exist only in your broker
statements.

Concretely: if the Schwab / Schwab 876 / Aborad positions above are still what
you hold, the restore is correct and I would run it. If any of them has traded
since July 13, we should ingest a fresh broker file for those accounts first and
restore from that instead of carrying July forward.

A rollback point already exists and is verified:
`db/argosy.db.SAFETY_pre_repair.20260808T221356Z` — standalone, integrity ok,
all 86 tables matching live.
