# 2026-08-12 — North star in the SDD + repo/worktree hygiene

## Read first (in order)
1. This file (current state + what's actually open).
2. `git log --oneline -12`.
3. `docs/design/SDD.md` — now opens with `## North star` (new, this session).
4. `docs/handovers/2026-08-11-decision-loop-closure.md` — the decision-loop detail. **Its OPEN QUEUE is stale; see "Corrections" below.**

## Git state (authoritative)
- **master = `a71bd39`**, **ahead of origin by 2 (`4530660`, `a71bd39`) — UNPUSHED.** Both are docs/chore only. `git push` is blocked by the harness classifier → **Ariel runs `! git push origin master`**.
- Migrations head **`0101_fill_verdict_link`** — DB `db/argosy.db` confirmed at 0101. (Earlier docs said 0100; 0101 landed with seam 4.)
- Working tree **clean**. No worktrees. Branches: `master` + 4 `feat/stream-*` + 6 `worktree-agent-*`, all retained with unmerged commits (see "Branch inventory").

## Corrections to the 2026-08-11 handover (its open queue is out of date)
That file's ⚠️ RESTART CAVEAT and items 1–4 of its OPEN QUEUE are **all resolved**; the commits landed after it was written:
- **Restart caveat — MOOT.** The backend was already running `git_sha=b2e331c` before this session touched it.
- **#35 falsifier evidence-contract — SHIPPED** (`fbf2030`).
- **Seam 4, fill→verdict linkage — SHIPPED** (`0c4d692`, + migration 0101).
- **UI render for `how_our_calls_did` — SHIPPED** (`b2e331c`).
Do not re-do these. `CLAUDE.md`'s prior pointer text listed them as open; it has been corrected.

## What shipped this session
- **`4530660` — north star at the top of the SDD.** Ariel's wording, verbatim, as `## North star` above "Why Argosy exists", plus a tie-breaker line for design questions with no clean answer. This closes the gap the 2026-06-08 dynamic-allocation spec explicitly filed a follow-on docs task for (the north star had lived only in scattered specs + auto-memories). Also saved as auto-memory `project_argosy_north_star` and indexed in `MEMORY.md`.
- **`a71bd39` — repo hygiene.** domain_knowledge provenance refresh across 17 files (verified pure: every diff line is a `last_verified` / `retrieved` date, zero substantive change) + the 8 untracked 2026-08-08/09 handovers checked in.

## Housekeeping done (no code impact)
- **~11.4 GB freed.** Deleted `.tmp_aside` / `.tmp_d1` (6.3 GB) / `.tmp_verify_aside` + 9 loose `.tmp_*` files (~8 GB), and all `db/*.bak*` (3.4 GB, 19 files). Verified redundant first: `backups/` holds the daily full-DB chain (`argosy-20260726` → `argosy-20260812`). **`db/argosy.db.SAFETY_pre_repair.20260808T221356Z` was deliberately KEPT** — it is the pre-repair safety copy from the P1 restore, not a `.bak`.
- **11 worktrees removed + pruned.** Checked for the documented `ui/node_modules` junction hazard first — none existed (`stream-d` had a real copy), so no repeat of the `@babel/` wipe. Branch refs were left intact, so no commits were lost. Only the 2 fully-merged branches were deleted (`feat/opens-2026-07-11`, `feat/stream-d-managed-holdings-abstention`). The `.worktrees/feat-early-signals-a` husk that had been file-locked since July finally cleared.
- **Backend restarted** onto `a71bd39` — `/api/health`, `/api/portfolio/snapshot`, `/api/positions/thesis` all 200.
- **UI restarted** — it was simply not running (no crash, nothing broken). `http://localhost:1337/` returns 200, Next.js 16.2.4 / Turbopack, ready in 6.7s.
- **Backup run** — `scripts/backup_to_sibling.ps1` → `D:\Projects\financial-advisor-backup`, 3,647 files / 750 MB copied, 0 failed.

## OPEN QUEUE (priority)
1. **Push** — master is 2 ahead of origin. Ariel: `! git push origin master`.
2. **#24 Phase 3c enforcement** — the spine gate shipped warn-first and **DORMANT** (`f45acf1`); flipping it to enforce is behavior-changing and still deferred. Needs a deliberate decision, not a drive-by.
3. **Re-run the lean Sol confirms** self-verified during the codex outages (3d round-4 delta; verdict-authority round-2) now that codex is stable (model `gpt-5.5`).
4. **Branch inventory decision** — 10 branches carry unmerged commits with no worktree (below). Each is either worth merging, worth cherry-picking, or worth deleting; nobody has triaged them.

## Branch inventory (unmerged commits, no worktree)
| Branch | Unmerged | Tip subject |
|---|---|---|
| `feat/stream-a-data-integrity` | 3 | restore provenance liveness path; keep vintage enforce off |
| `feat/stream-b-bear-independence` | 3 | make independence semantic; choke trader prompt assembly |
| `feat/stream-c-prediction-ledger` | 1 | wip: make the prediction ledger actually score |
| `feat/stream-e-async-cache-loop` | 1 | wip: loop-safe bridge instead of `asyncio.run` |
| `worktree-agent-a10db172e619def77` | 1 | graduate mark-staleness so normal gaps degrade gracefully |
| `worktree-agent-a65837e8271f39123` | 1 | route six raw-TSV readers through one guarded book accessor |
| `worktree-agent-a7ad1ac05ea0314da` | 1 | make silent-success / fail-open job failures visible |
| `worktree-agent-a8c47c9e4b388501d` | 1 | label opaque symbol-less rows; stop live-DB test |
| `worktree-agent-aab67d6154b20533a` | 1 | stall alert must alert-not-throw + `busy_timeout` on sync engines |
| `worktree-agent-afb7cdd941018a1fc` | 1 | count present-but-unmanaged NVDA toward concentration % |

Note the last one overlaps the long-standing `_summarize_positions` unmanaged-NVDA issue from the 2026-08-09 handover — check whether that's already fixed on master before merging it.

## Restart recipes
**Backend** (kill the 4-proc chain first — the start script's idempotency guard refuses if a supervisor + busy port exist):
```powershell
Get-NetTCPConnection -State Listen -LocalPort 8000        # find the chain
Stop-Process -Id <top supervisor> -Force                  # children die with it; verify all 4 gone
$env:ARGOSY_EXPENSE_SAMPLES_ROOT = "D:\Google Drive\Family\Finances\Portfolio\Resources"
& "D:\Projects\financial-advisor\scripts\start_backend_detached.ps1" -Port 8000
Invoke-WebRequest http://127.0.0.1:8000/api/health        # expect 200
```
**UI** — must be launched detached; a double-background from bash loses the process:
```powershell
Start-Process cmd.exe -ArgumentList "/c","npm run dev" -WorkingDirectory "D:\Projects\financial-advisor\ui" `
  -RedirectStandardOutput D:\Projects\financial-advisor\tmp\ui_dev.log `
  -RedirectStandardError  D:\Projects\financial-advisor\tmp\ui_dev.err.log -WindowStyle Hidden
```

## Traps / discipline
- **Codex/Sol flaky** — model `gpt-5.5`, LEAN prompts finish, heavy ones get killed. Each money/decision unit: build → Sol → fix → commit. UI / read-projection: skip Sol per CLAUDE.md.
- **Never junction-link `<worktree>/ui/node_modules`** to main's — it has twice wiped main's `@babel/` scope on `git worktree remove`. Use `cd <worktree>/ui ; npm ci`.
- **`git worktree remove` does not delete commits** — branch refs survive, so removing worktrees is safe as long as you don't also `git branch -D`.
- Console is cp1252 — set `PYTHONIOENCODING=utf-8` for anything printing ₪ / → / Hebrew, and do durable side-effects BEFORE printing.
