# Windows launcher

`scripts/ensure_servers.py` health-checks the local backend (8000) and UI (1337).
Healthy servers are left alone. Missing servers start silently; repeatedly
unresponsive, checkout-owned servers are restarted. Foreign port owners are
never terminated. Startup gets three minutes of grace. Active backend jobs defer
a restart, and attempts are bounded to three per service per thirty minutes.
Application/DB/research errors are not disguised as process failures.

Install/update for the current Windows user:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/install_servers_startup.ps1
```

This updates the existing `Argosy Backend Supervisor` scheduled task (old XML
saved under `tmp/startup-task-before-*.xml`) to check **both** servers at sign-in
and every five minutes while signed in, including after sleep/missed schedules.
It does not wake the PC or run analysis while the PC is off. The task retains its
historical name so there aren't competing old/new watchdogs.

The desktop `Argosy` shortcut runs the same launcher and then opens the dashboard.
The installer packages `ui/public/logo.png` as `tmp/argosy-logo.ico` and assigns
that app-logo icon to the shortcut. Reinstalling refreshes it without new artwork.
Five minutes is a **health-check interval**, not an investment-analysis interval:
two local HTTP probes on a healthy run, no LLM requests and no process restart.
This bounds detection of a stopped service to roughly five minutes while awake.
Windows Script Host and pythonw keep console windows hidden. The backend retains
its existing child-process supervisor. Normal backend startup can perform its
already-configured scheduler catch-up; the launcher calls no job, approval, or
trade execution endpoint.

```powershell
.venv/Scripts/python.exe scripts/ensure_servers.py
.venv/Scripts/python.exe scripts/ensure_servers.py --check-only
```

Status: `tmp/server_launcher_status.json`. History: `tmp/server_launcher_history.jsonl`.
Process output: `tmp/server_launcher.log` plus existing backend supervisor logs.

Verified 2026-09-19: live healthy backend/UI skipped; stopped UI started again with
HTTP 200 while backend remained unchanged; registered task completed with result 0;
desktop shortcut target and arguments verified. Twelve scoped launcher/supervisor
tests passed. Actual Windows reboot/sign-in and live hung-backend restart were not
forced for this verification.

## Existing decision/outcome screens

- `/inbox`: current proposed actions, not a complete historical ledger.
- `/decisions`: decision runs and agent-review detail.
- `/decisions/funnel`: routing/triage decisions, including dropped and blocked names.
- `/portfolio`: expand **Automatic self-evaluation vs S&P 500 — recommendations bought or not**.
- `/audit`: logged approvals, fills, overrides and other audit events.

These do not establish universal prediction coverage. The September 19 repair
restored 30/180/365-day clocks for all 32 actionable verdicts, including superseded
calls, and all 16 legacy proposals (separate provenance, not invented verdicts).
The daily evaluator uses the same idempotent repairs. All 70 eligible radar names
already carried a six-month clock. Expand **Historical proposal coverage** in the
Portfolio self-evaluation panel for original calls, dispositions and rationale.
The real evaluator run89938 scored and benchmarked34 newly repaired due calls;
seven still lack usable historical price evidence (provider symbol identity).
Across the entire existing scorecard,23 evaluations are now explicitly reported
unscorable (these7 plus16 older cases); this follow-on evidence-quality gap remains.
Clock completeness is not outcome completeness, and missing evidence is not a win.

Preview or apply the clock repair independently, without running the fleet:

```powershell
.venv/Scripts/python.exe scripts/repair_recommendation_clocks.py
.venv/Scripts/python.exe scripts/repair_recommendation_clocks.py --apply
```

The desktop/installed watchdog also recovered a real unresponsive backend at
06:33UTC September19; the following five-minute task completed successfully.

The
September 15 operator advisory's five fleet reviews are agent reports 6613–6617;
the final allocation given in the external chat is not an Argosy prediction or
approved order sheet. Do not silently convert it into one.
