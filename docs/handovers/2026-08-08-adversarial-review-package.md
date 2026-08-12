# Adversarial review package — 2026-08-08 fleet-integrity marathon

**You are an independent adversarial reviewer. Your job is to break this work, not
to bless it.** Re-derive every claim from the code and the database. Default to
skepticism. It is better to surface a real flaw than to agree.

This document is self-contained: it states what was found, what was changed, what
is **proven** versus merely **claimed**, and where the remaining attack surface
is. Companion document with the full narrative:
`docs/handovers/2026-08-08-fleet-integrity-five-streams.md`.

**Nothing has been merged.** `master` = `origin/master` = `4d97a73`, unchanged.
Migrations head on master is still **0094**. Five feature branches carry
committed work. **Production `db/argosy.db` has not been written to** by any of
this work.

---

## 0. How to review

### Environment

- Repo `D:\Projects\financial-advisor`; worktrees under `.worktrees\`.
- PowerShell: **`&&` does not chain — use `;`**.
- Python: `D:/Projects/financial-advisor/.venv/Scripts/python.exe`.
- Tests: `-m pytest -m "not llm_eval" <path>`.
- **Set `PYTHONIOENCODING=utf-8`** — the console is cp1252 and the data contains
  Hebrew tickers and `₪`.

### Three gotchas that cost hours this session

1. **The worktrees are NOT pip-installed.** A bare `python script.py` silently
   imports the **main repo's** `argosy` package, so branch-only modules appear
   not to exist. Insert the worktree root at `sys.path[0]`.
2. **Never name a scratch file `inspect.py`** — it shadows the stdlib and yields a
   baffling `module 'inspect' has no attribute 'get_annotations'`.
3. **`rg` / Grep skip `.worktrees/`** because it is gitignored. Pass `--no-ignore`.

### Rule for this review

**Do NOT write to the live `db/argosy.db`.** Read it with
`sqlite3.connect("file:db/argosy.db?mode=ro", uri=True)` or copy it first. The
production book is already damaged; corrupting it further is the worst available
outcome.

---

## 1. State at a glance

| Stream | Branch | HEAD | Migration | Status |
|---|---|---|---|---|
| A — vintage gate / provenance | `feat/stream-a-data-integrity` | `fb9523d` | 0095 | Round 7; enforcement deliberately **OFF**; blocked |
| B — bear independence | `feat/stream-b-bear-independence` | `5995bfe` | none | Round 10; a working exploit was found in each of the last three rounds |
| C — prediction ledger | `feat/stream-c-prediction-ledger` | `8812ea8` | 0096 | 6 blockers open; peer-relative HOLD grading not started |
| D — managed/unmanaged holdings | `feat/stream-d-managed-holdings-abstention` | `03b7692` | 0097 | Regressions cleared; **BLOCK** with 9 findings; round 7 in flight |
| E — async event-loop root cause | `feat/stream-e-async-cache-loop` | `9cc1d26` | none | 2 blockers open; least mature |

**Only two things actually shipped**, both trades, neither code: **TRLV proposal
#20 rejected** and **IOVA proposal #19 un-shadowed** into the approvable queue.

---

## 2. The incident that drives Stream D

Independently verified against `db/argosy.db`, read-only. **This is a regression,
not the deliberate NVDA exclusion.**

Snapshot **id 34** (2026-07-13) held **49 positions / $4,075.6k** across accounts
`schwab`, `schwab 876`, `Aborad`, `Leumi`. Snapshot **id 49** (2026-08-08) holds
**38 positions / $1,615.6k** — `Leumi` **only**. A Leumi-only TSV import replaced
the entire book instead of merging per account.

**Every non-Leumi position was erased: 8 positions, $2,432.0k.**

| Symbol | Account | Shares | Value |
|---|---|---|---|
| NVDA | schwab | 10,940 | $2,307.9k |
| `-` | Aborad | 3.0 | $69.0k |
| SGOV | schwab 876 | 200 | $20.1k |
| SCHD | schwab 876 | 400 | $13.0k |
| VOO | schwab 876 | 10 | $6.9k |
| `-` | schwab 876 | 5,893 | $5.9k |
| BMY | schwab 876 | 100 | $5.8k |
| SCHG | schwab 876 | 100 | $3.5k |

**Enumerate by POSITION, not by symbol.** A symbol-keyed diff collapses a ticker
held in two accounts and under-reports the damage — this caught me out on the
first pass (I initially reported 3 positions / $2,382.7k). SCHD, VOO, SCHG and
SGOV each legitimately exist in **both** `Leumi` and `schwab 876`.

NKE, RKT and SPCX also left the book but were **genuine sales** (proposals #12,
#9, #4, `executed_live`). They must never be resurrected by a restore.

**Consequence:** every NVDA concentration figure, US-situs estate number,
retirement safety gate and FI-shock value currently in the system was computed on
a book missing 59% of its value.

---

## 3. Evidence ledger — proven vs claimed

The single most useful thing a reviewer can do here is refuse to accept the
second column.

### Proven (I re-derived these myself, or a reviewer demonstrated them with a probe)

| Claim | Evidence |
|---|---|
| Backfill reconstructs the book | Dry-run on a fresh copy of live: **46 positions / $4047.6k**; apply gives `aborad` 1/$69.0k, `leumi` 38/$1615.6k, `schwab` 1/$2307.9k, `schwab 876` 6/$55.1k |
| Backfill is idempotent | Second apply prints `noop`, not duplicates |
| Carried rows keep their own date | `observed_as_of` stays `2026-07-13` on carried rows, `2026-08-08` on Leumi rows |
| Cross-account lots survive | SCHD/VOO/SCHG/SGOV present as separate rows in both accounts |
| Cross-account lots are **summed** correctly | Reviewer probe: `investable_k=600`, `tradeable_k=600`, `nvda_pct=50`, `estate_usd=300000`, safety gate agreeing |
| Genuine sales stay sold | Read-only reconstruction did not resurrect NKE, RKT or SPCX; synthetic sale merge removed NKE |
| Stream D regressions are cleared | Selection `-k "upload or snapshot or portfolio or wealth or holding"` = **1 failed, 435 passed** (was 16 failed / 420 passed one commit earlier) |
| The one remaining failure is not ours | `test_bug1_rest_upload_returns_400_for_max_without_card_last4` fails identically on master `4d97a73` |
| Stream D test edits are not test-gaming | Deploy-cash lambdas gained an optional `db=None` arg; three tests seed a policy row; no assertion weakened |
| Revert-detectors are real | Reverting the dashboard as-of fix, and separately the last-coverage merge, each failed the corresponding test |
| 0097 seeds policy rows | `alembic/versions/0097_unmanaged_holdings.py:123-135` |

### Claimed but NOT verified — treat as unknown

- **Nothing has been exercised against production.** The migrate-then-restore
  sequence has only ever run on copies. The live DB is still at migration 0094.
- Blockers 2 and 4 (stale marks; coverage metadata) have not been tested
  end-to-end against a running server.
- Stream A's headline provenance liveness (**37/38**) does not come from
  production gathering — a reviewer proved that.
- Stream B's `test_fleet_reliability.py` was **skipped** in round 10, which
  changed circuit-breaker semantics. Unverified regression risk.
- Stream E's `asyncio.run` migration audit produced no output at all.
- No stream has run the **full** suite. Note the full suite historically **hangs**
  at `tests/test_api_phase4.py`; use `pytest-timeout`.
- UI lint/typecheck not re-run after the latest Stream D commit.
- NVDA sell quotas have **not** been re-derived on the restored book.

---

## 4. Review targets, by stream

### D — managed/unmanaged holdings (`03b7692`) — highest value, real money

Verdict from the last review: **BLOCK**, 9 findings, all probe-backed. Round 7 is
in flight against them. Ordered by "what makes running the backfill on production
unsafe":

1. **CRITICAL — the pre-restore backup is not a safe SQLite backup.**
   `scripts/backfill_restored_holdings_book.py:72-82,152-156` uses
   `shutil.copy2`, which omits committed data still in the WAL. This is the
   backup we would depend on before touching production money. The live-DB guard
   is filename-only (bypassable via alias/hardlink), and repeated applies
   overwrite the same backup path.
2. **CRITICAL — surfaces use incompatible valuation clocks, and one is
   temporally mislabelled.** `wealth_dashboard.py:928-952`,
   `net_worth_bases.py:154-189`, `plan_numeric_resolver.py:589-617`,
   `portfolio.py:283-299`. Dashboard/resolver/net-worth pass
   `today=snapshot_date`; `/portfolio/snapshot` uses real today. The dashboard
   published the July book while `/portfolio/snapshot` degraded the same book and
   refused values, both reporting `snapshot_date=2026-07-13`; the dashboard says
   "as of today" and never renders `assumptions.as_of`. Worse, a missing durable
   holding can take a **current** quote while stamped with the **historical**
   date: `nvda_price=223.96` published as `valued_as_of=2026-07-13`.
3. **CRITICAL — same-shape updates are silently discarded.**
   `portfolio_snapshot_store.py:527-563`: `latest_matches_snapshot()` compares
   only source path, date and feed count. Probe: incoming `['CSPX','NEW']`
   dropped, stored stayed `['CSPX','NKE']`. **Pre-dates the branch** (on master at
   `portfolio_snapshot_store.py:221`) but the branch modifies it and it undermines
   the erasure fix.
4. **CRITICAL — new tenants get no integrity gate.** 0097 seeds policy rows only
   for users existing at upgrade; `argosy/tenancy/onboarding.py:91-100` creates
   none, so `load_explicit_policy_symbols()` is empty and a later NVDA erasure is
   accepted silently (probe: `policy=[]`, missing NVDA, `degraded=False`).
5. **HIGH — net worth publishes totals contradicted by its rows.**
   `net_worth_bases.py:167-189`: probe `position_sum_k=100` with
   `totals_json_k=1000` published **$1,000,000**.
6. **HIGH — money tests reach live quotes.** `snapshot_refresh.py:134-166`,
   `market_data_adapter.py:369-409`. The `$223.96` arrived through the production
   `default_quote_fn` into the yfinance adapter because the test pinned no quote.
7. **HIGH — the XLS upload path swallows the integrity rejection.**
   `xls_osh_pair.py:288-305,345-379` catches `SnapshotIngestRejected` in a broad
   `except Exception` and returns `[]`, so the upload reports success while the DB
   write was refused. **Pre-existing** — the branch does not touch
   `portfolio_ingest/`.
8. **MEDIUM — no-op backfills misreport carry provenance.** First apply reports
   `accounts_carried=['schwab']`; the second reports `[]` while values are still
   July-dated.
9. **MEDIUM, pre-existing — the ordinary snapshot upload is not
   admin-authenticated** (`portfolio.py:601-620` accepts a caller-supplied
   `user_id`). Owner's call whether a local-only API needs this.

**Attack this next:** the valuation-clock policy chosen in round 7 (is it
coherent across *every* surface, and is the as-of actually rendered?); whether the
WAL-safe backup is genuinely consistent under concurrent writes; and whether the
content-comparison replacing `latest_matches_snapshot` can still drop a real feed.

### A — vintage gate / provenance (`fb9523d`) — enforcement OFF

The 37/38 liveness headline does not come from the production gathering path.
Actionable deploy and plan paths remain bypassable; the SEC operation is not
deployment-ready; provenance and instrument-class checks can be spoofed.
**Do not switch the gate on while liveness is unproven** — a gate at 0% liveness
either blocks everything or, worse, fails open silently. Blocked on
`ARGOSY_SEC_CONTACT_EMAIL` (SEC refuses callers without a declared contact).

**Attack this:** find any actionable path — deploy-cash, plan synthesis, the
scheduled funnel — that reaches a user-facing recommendation without passing the
vintage gate.

### B — bear independence (`5995bfe`) — ten rounds, an exploit each of the last three

The recurring failure class is **fail-open**: agent-authored free text reaching
the trader prompt, premise claims that need no retrieval evidence, and a bear
that can skip independent retrieval while still being marked independent. Round
10 reframed it as a design problem: enforce *semantic* independence (substantive
grounded points, not merely a URL that was fetched) and funnel trader prompt
assembly through a **single sanitizing choke point**.

**Attack this:** try to get attacker-controlled text from an agent output into the
trader prompt, and try to earn the "independent" mark without doing real
retrieval. Also check `test_fleet_reliability.py`, which was skipped in the round
that changed circuit-breaker semantics.

### C — prediction ledger (`8812ea8`) — 6 blockers open

This stream exists because the evaluator reported "ok" while doing no work: 63% of
outcomes `unparseable`, 642 predictions unscored, real fleet decisions never
recorded as predictions, and the largest source (`discord_listener`) dead on an
expired token. Open blockers include survivorship-biased and mispriced backfill,
hindsight mutation of prediction versions, and an invisible scorecard.

**Not yet started:** the owner's HOLD-grading decision — grade a HOLD against
**the best available alternative in the same class**, with a tolerance band equal
to the **actual switching cost** (capital-gains tax on the embedded gain plus
spread). This is blocked on cost basis: the `lots` table is empty and only broker
`avg_price` exists, so the switching-cost band cannot be computed until a Schwab
cost-basis CSV is ingested.

**Attack this:** whether any published hit-rate is survivorship-biased, and
whether a HOLD that underperformed its class peer can be scored as a win.

### E — async event-loop root cause (`9cc1d26`) — least mature

1,065 `Queue is bound to a different event loop` errors in
`logs/app/application.log` are the root cause of a wide class of downstream
damage: frozen prices, unscored predictions, and the empty evidence bundles that
made holdings reviews return "HOLD, LOW confidence" almost uniformly. The cause
is `asyncio.run` creating new loops against a shared aiosqlite pool. Two blockers
remain (systemic empty-feed failures still produce healthy summaries; a
process-global counter cross-attributes failures between concurrent jobs) and the
migration audit produced nothing.

**Do not run E concurrently with D** — both modify `snapshot_refresh.py`.

**Attack this:** whether `run_coro_sync` can deadlock or silently time out, and
whether `/health` can report healthy while feeds are systematically empty.

---

## 5. Calibration — the overstatement pattern

Every round this session, hand-back reports claimed more than was true. A
reviewer should assume this continues.

- A round reported **"all six blockers closed, 75 passed"** while breaking **15
  tests that pass on master**. It had verified only its own test file.
- A round returned **"success" having committed nothing at all** — unchanged HEAD,
  empty diff. **Always check `git log` and `git diff --stat` before reading a
  report.**
- A round confidently reported an NVDA sell quota of **8,261 shares**; it was
  wrong (it hardcoded a 12% target and bypassed the resolver).
- A backfill printed `restored: 46 positions` **while its core purpose failed** —
  the NVDA unmanaged registration errored on a migration-0094 database and was
  logged as a warning, so NVDA would have returned counted as *managed*.

I also got things wrong and was corrected by evidence: I reported a **vanishing
position and lost money** when the real mechanism was a canonical alias being
written into the stored symbol (no money lost); and I hypothesised that the estate
discrepancy was an unmanaged-set wipe when it was live repricing. Both
corrections came from probes, not argument.

**Method that worked:** run the wider suite, establish the master baseline before
calling anything a regression, and re-derive money numbers from raw rows.

---

## 6. Merge hazards — resolve deliberately

- **Alembic linearization.** Three migrations all descend from **0094**: A's
  `0095_remediation_requests`, C's `0096_prediction_ledger_scorecard`, D's
  `0097_unmanaged_holdings`. Merging any two without stitching `down_revision`
  produces multiple heads. Pick an order and rewrite the chain.
- **A-vs-B facilitator conflict.** Both streams modify the facilitator's
  disagreement handling. A naive merge will drop one side's fields; both must
  survive.
- **D-vs-E on `snapshot_refresh.py`.** Both modify it; sequence them.
- **The data repair and the code fix must land together.** Restoring the rows
  without the merge fix means the next Leumi upload erases them again; merging the
  fix without the backfill leaves $2.432M missing. Either half alone is worse than
  useless.

---

## 7. Owner decisions and open asks

**Binding decisions already made (do not relitigate):**

- **NVDA is deliberately excluded** from the managed sleeve and is managed
  separately. It must be modelled as **unmanaged-but-present**, never as absent —
  it still counts for US-situs estate tax, FX and net worth.
- **HOLD is graded against the best available alternative in the same class**,
  with a tolerance band equal to the real switching cost (CGT on the embedded gain
  plus spread).
- **Provenance is split by instrument class** — SEC EDGAR for US-listed single
  names, fund-appropriate rules for ETFs, cash exempt.
- Cost basis will come from an **ingested Schwab cost-basis CSV**.

**Open asks on the owner:**

1. `ARGOSY_SEC_CONTACT_EMAIL` — SEC blocks callers without a declared contact;
   Stream A's provenance path cannot go live without it.
2. The **Schwab cost-basis CSV export**, which unblocks the `lots` table and
   therefore the switching-cost band in Stream C.
3. Whether the snapshot upload route should require admin authentication
   (finding 9) given the API is intended to be local-only.

---

## 8. Do not do these

- Do not write to the live `db/argosy.db`; do not run the backfill against it
  until the WAL-safe backup lands.
- Do not merge any stream to `master` without an independent review of the
  hand-back.
- Do not `--no-verify` past hooks. Commit signing is not configured in this repo;
  do not add it.
- Do not junction-link a worktree's `ui/node_modules` to main's — it has twice
  destroyed the main repo's `@babel/` scope. Run `npm ci` in the worktree.
- Do not publish a performance scorecard. The original **BLOCK** verdict stands
  and this session's findings strengthened it: prices, FX and the
  duplicate-snapshots-per-date ambiguity are all still broken.

---

## 9. Deliverable requested from the reviewer

A verdict per stream — **MERGE** / **MERGE-WITH-CHANGES** / **BLOCK** — then a
numbered list of findings. For each finding give the failure scenario, the exact
`file:line`, the severity, and **the probe output that demonstrates it**.

Distinguish explicitly between "I proved this is broken" and "I suspect this".
State what you could **not** verify. Report honest test counts for anything you
run, including the selection command used. And do not accept a claim because a
test passes — check that the test actually exercises the failure it purports to
cover.
