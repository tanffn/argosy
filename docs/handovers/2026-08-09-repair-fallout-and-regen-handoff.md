# Handoff — post-restore repair fallout + plan regeneration (2026-08-09)

Self-contained handoff for the next LLM. A $2.4M/59% data-loss bug was repaired on
production; this covers what shipped, the corrected numbers, and the one deliverable
left (regenerate the plan).

> **UPDATE 2026-08-09 — CODEX IS RESTORED (supersedes the "codex is dead" framing below).**
> Ariel re-logged in (`~/.codex/auth.json` present; `codex login status` = "Logged in
> using ChatGPT"). **The model is `gpt-5.5`, NOT a literal `sol`** — a ChatGPT-account
> codex rejects `--model sol` with HTTP 400. Pass `--model gpt-5.5` or omit `--model`
> (account default = gpt-5.5). The fleet's synthesis second-opinion
> (`plan_synthesis/codex_second_opinion.py`) passes no `--model` → default gpt-5.5 →
> **works now**. So **regen precondition B no longer needs the in-harness substitute** —
> the real codex math-gate runs. (Keep the in-harness re-derivation as a belt-and-braces
> cross-check if you like, but codex is live.) Everywhere below that says "codex is dead"
> / "assume codex doesn't work" is now historical.

---

## 0. SAFETY + environment traps (each cost an hour)

- **`db/argosy.db` is the LIVE ~$4.2M book, backend actively writing.** Inspect
  READ-ONLY: `sqlite3.connect("file:db/argosy.db?mode=ro", uri=True)`.
- **Never** run `scripts/backfill_restored_holdings_book.py` at live; never pass its
  override. Rollback copies: `db/argosy.db.SAFETY_pre_repair.20260808T221356Z`,
  `db/argosy.db.bak_pre_restore.20260808T200547Z`, `db/argosy.db.bak_pre_datefix.20260809T144532`.
- `echo $PYTHONPATH` must be empty; `python -c "import argosy; print(argosy.__file__)"`
  must be `D:\Projects\financial-advisor\argosy\__init__.py` (a stray worktree on
  PYTHONPATH silently loads unrepaired code).
- `tests/test_api_phase4.py` + `tests/test_api_jobs.py` **hang the suite** — always
  `--ignore` both. `pytest-timeout` **is now installed** — use `--timeout=300`.
- Never pipe pytest through `Select-Object` (buffers for hours) — redirect `> out.txt 2>&1`
  and read the file. Prefer the Bash tool + script files over PowerShell inline python.
- Full suite ≈ 3h. Console is cp1252 → `PYTHONIOENCODING=utf-8`.

## 1. State at a glance

- **master = origin = `1f6ca68`.** Backend healthy on it (`git_sha 1f6ca68`).
- **Live book (snapshot 53):** 46 positions / **$4,191.4k** / 4 accounts (Leumi 38, schwab 1
  = NVDA, schwab 876 6, Aborad 1). Migration head **0097**.
- **NVDA:** 10,940 sh, `observed_as_of=2026-07-13` (date-repaired), managed=False, in the
  total book, out of the managed sleeve, ~58.5% of book.
- **Plans:** v92 = current (pre-restore, NVDA @ 0% — STALE), v94 = draft from run 284
  (also computed with NVDA excluded — do NOT accept).

## 2. What is DONE on master ("everything executable is done") — elaborated

Seven commits this session (newest first), all reviewed + tested before merge:

| commit | what it fixes |
|---|---|
| `1f6ca68` | **Concentration resolver** — unmanaged NVDA no longer emitted as `excluded`; `nvda_current_pct` now resolves ~58-60% (was `0.0 UNKNOWN`). *Resolver only — see §4 for the remaining analyst-input half.* |
| `fd76a23` | The one-row NVDA date-repair **script** (already applied to live) |
| `8963406` | **Stall detector** alerts-not-throws + a `create_sync_engine()` seam (WAL + busy_timeout=60s) that kills the `database is locked` kv_cache class |
| `b7ec83e` | **Mark-staleness graduated** `0→4` days (+ hard 14) so weekends/outages degrade gracefully instead of raising `TotalBookDegraded` (fixed 17 of the 23 merge regressions) |
| `40f74e3` | **Trusted-book accessor** `load_current_book_snapshot` + six raw-`parse_portfolio_tsv` bypasses migrated |
| `9b98085` | Human labels for the ~$98k of blank/`-` rows; gated the one test that hit the live money DB |
| `e70cdcb` | Two post-restore conservation bugs: dedupe collision (dropped $5,446.93 cash) + carried-quantity re-dating (disarmed the staleness guard) |

**Also done, not a commit:** the NVDA `observed_as_of` 2026-08-08→2026-07-13 repair was
**applied to production** (both `unmanaged_holdings` + snapshot; conservation intact;
staleness guard re-armed). Zombie run 283 cancelled. `pytest-timeout` installed.

## 3. The corrected numbers (the actual point of the repair)

Recomputed on the true book vs the corrupted Leumi-only book:

| metric | corrupted | true | 
|---|---|---|
| Net worth | $1.56M / ₪4.68M | **$4.07M / ₪12.20M** |
| NVDA % of investable | 0.00% | **~58.5–59.9%** (4.6× the 13% cap) |
| US-situs NRA-40% estate tail | ~$279k | **~$1.28M / ₪3.84M** |
| Retirement vs FI target | 39.6% | **103%** (needs full MC to confirm) |

Every plan surface (v92 current, v94 draft) still reflects the corrupted "NVDA 0%".

## 4. THE DELIVERABLE — regenerate the plan (codex is DEAD)

Two hard preconditions, then a codex-free regen:

**Precondition A — fix the ANALYST-INPUT plumbing (the resolver fix was only half).**
`1f6ca68` fixed the resolver (final rendered number). But the synthesis feeds the
ConcentrationAnalyst its book via `positions_summary`, built by
`argosy/orchestrator/flows/plan_synthesis/inputs.py::_summarize_positions`, which
**stays "focused on tradeable holdings"** (inputs.py ~:1283/:1328) — so the unmanaged
Schwab NVDA is **excluded from the analyst's input**. Proof: run 284 (post-restore)
still logged *"no NVDA position in the Leumi tradeable snapshot; current NVDA weight
0.0 UNKNOWN."* Until `_summarize_positions` (or a dedicated concentration input)
surfaces unmanaged-but-present NVDA, a fresh synthesis reasons on 0.0 again and the
resolver only cosmetically corrects the headline. **Fix this before firing any regen.**

**Precondition B — replace the codex math-gate (codex is dead, plan for it staying dead).**
The synthesis phase-4.5 codex "second opinion" (`argosy/orchestrator/flows/plan_synthesis/
codex_second_opinion.py`) is the blind headline-number audit that BLOCKs a plan whose
NVDA weight / estate / net-worth don't independently re-derive. With codex 401-dead it
returns `(None,None)` fail-soft — which is exactly how run 284/v94 got green-lit blind.
**Substitute: an in-harness blind re-derivation.** After the synthesis produces a draft,
dispatch a read-only `Agent(subagent_type="general-purpose")` that re-derives NVDA
weight, US-situs estate, and net worth from raw snapshot rows, and BLOCK the draft if it
diverges from the known-true (NVDA ~58-60%, estate ~$1.28M). This satisfies the
adversarial-math-gate doctrine without codex. (Optionally wire this substitute into
`codex_second_opinion.py` so future runs are gated by default.)

**Regen sequence:** fix Precondition A → restart backend on the new SHA → fire
`POST /api/advisor/check-in` (or the equivalent) → in-harness math audit (Precondition B)
→ present the draft to Ariel (do NOT auto-accept) with the corrected estate/quota/
retirement numbers. Also re-run the full sequence-aware retirement MC
(`canonical_feasible_dual_track`) on the true book.

## 5. Other open items (non-blocking)

- **6 decision-funnel tests red on master** (`test_decision_funnel_position_context`,
  `_sleeve_mandate`) — they hit the live DB and short-circuit because SOFI now has a
  settled verdict (from this session's fleet work → `verdict_defended` before stage-3).
  Live-DB test-isolation defect (same class as the one `9b98085` fixed). Isolate them
  (control the verdict state / use a fixture DB); NOT a source bug.
- **~40 other sync `create_engine(` sites** lack `busy_timeout` — migrate to
  `create_sync_engine()` (mechanical; `8963406` did the two hot ones).
- **Cleanup:** scratch worktrees under `.claude/worktrees/agent-*` (branches already
  cherry-picked), `.tmp_d1/` + `.tmp_*.txt`, `.worktrees/premerge-check`, and ~1GB of
  `db/argosy.db.SAFETY_*`/`bak_*` copies once you trust the restore.
- **Owner:** a fresh Schwab statement to close the carry-forward assertion (the restore
  assumes nothing traded in Schwab/Schwab 876/Aborad since 2026-07-13 — Ariel confirmed,
  but it's unverifiable from inside the system).

## 6. Do NOT / known corrections

- Do NOT revert the merge — the data restore is independent of the code, but a revert
  drops the ingest guard that stops the next Leumi-only feed re-truncating the book.
- Do NOT assume codex will run. It is 401-dead; the fleet's codex steps silently
  fail-soft. Use the in-harness substitute.
- Corrections from earlier claims: the silently-dropped cash lot was **$5,446.93** (the
  *second* blank Leumi row), NOT the $17.66k one; **v94** (run 284), not v93, is the
  latest draft; run **283 was a zombie** (cancelled) — the real synthesis was 284.

## 7. Design specs (future build, already reviewed) — not part of this repair

`docs/design/argosy_operating_model_spec.md` (the closed-loop system: evaluate every
holding / ETF vehicle-selection / prediction-learning loop) and
`docs/design/performance_scorecard_design.md` — both carry a BINDING adversarial-review
revisions block at the top. Separate track from the repair.
