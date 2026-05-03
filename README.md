# Argosy

A multi-agent financial advisor system. Python orchestration + Claude Agent SDK + FastAPI + Next.js dashboard. Designed for sophisticated DIY investors; multi-tenant from day one.

> **Status:** Design phase. SDD approved. Implementation has not started.

## Documentation

- **[Software Design Document](docs/design/SDD.md)** — the canonical design reference. 16 sections + appendices. Open questions marked **OPEN-N** are deferred to resolution during build.
- **Diagram sources:** `docs/design/diagrams/*.drawio` (editable in [diagrams.net](https://app.diagrams.net)); SVG exports in `docs/design/diagrams/svg/`.
- **Reference repos** (cloned to `D:\Projects\financial-advisor-references\`): TradingAgents, FinRobot, TradingGoose. See SDD §16.1.

## What this is

- Cadence-first Python orchestration shell with TradingAgents-style decision mechanism inside it
- Phase 1: Advisor + human approval (mode B) on existing accounts; paper-mode default
- Phase 2: Limited autonomous account at IBKR Pro (size configurable, $1K initial), modes B+C; bounded by capital
- Productization-aware: all paths derive from `ARGOSY_HOME`; multi-tenant from day one

## What this is not

- Not high-frequency trading
- Not a regulated financial advisor (personal-use software; productized form is *infrastructure*, not advice)
- Not optimized for alpha — optimized for plan adherence, concentration reduction, tax efficiency, and audit trail

## Implementation phasing

| Phase | Window | Goal |
|---|---|---|
| 0 | Weeks 1-2 | Scaffold + dependencies + drawio diagrams |
| 1 | Weeks 3-6 | Intake interview + domain KB seed + plan-critique agent |
| 2 | Weeks 7-10 | Cadence loops + daily brief + dashboard v1 (paper-only) |
| 3 | Weeks 11-14 | Decision team (analysts → debate → trader → risk → fund manager) + tiers |
| 3.5 | Weeks 15-16 | **Mandatory paper-mode soak** |
| 4 | Weeks 17-20 | IBKR adapter + 1-click approval + live mode for T0/T1 in main accounts |
| 5 | Weeks 21-24 | Limited account autonomy + cooling-off + kill switch |
| 6 | Weeks 25+ | Productization (multi-tenant, hosted, billing) |

See SDD §13 for full details and exit gates.

## Repo layout (planned)

```
${ARGOSY_HOME}/
├── README.md                      # this file
├── argosy.toml                    # top-level config (paths, ports, keychain refs)
├── docs/
│   └── design/
│       ├── SDD.md                 # the design document
│       └── diagrams/
│           ├── *.drawio           # editable sources
│           └── svg/*.svg          # exported renders
├── argosy/                        # Python package (engine + agents)
│   ├── orchestrator/              # cadence loops
│   ├── agents/                    # one module per agent role
│   ├── adapters/
│   │   ├── brokers/               # IBKR, Schwab, Leumi
│   │   └── data/                  # yfinance, FRED, Finnhub, etc.
│   ├── state/                     # SQLite + DuckDB layer
│   └── api/                       # FastAPI app
├── ui/                            # Next.js + shadcn/ui app
├── tests/
│   ├── unit/
│   ├── integration/
│   └── agent_evals/               # snapshot tests per agent role
├── domain_knowledge/              # tax/, brokers/, asset_classes/, etc.
├── configs/
│   └── <user_id>/                 # per-user configs (multi-tenant)
│       ├── agent_settings.yaml
│       ├── user_context.yaml
│       ├── plan.yaml
│       ├── entitlements.yaml
│       └── branding.yaml
├── db/                            # SQLite database file
├── backups/                       # daily snapshots (path is configurable)
├── logs/                          # app + agent logs
└── secrets/                       # encrypted; master key in OS keychain
```

## Quick start (Phase 0)

Phase 0 is the bare scaffold: FastAPI backend, Next.js dashboard, SQLite + Alembic migrations, secrets via OS keychain. No agents, no broker, no Claude calls yet.

### Prerequisites

- Python 3.12+
- Node.js 20+ and npm
- [`uv`](https://github.com/astral-sh/uv) for Python dep management (`pip install --user uv`)

### First-time setup

```bash
# from the repo root
uv sync                          # creates .venv and installs Python deps
uv run alembic upgrade head      # apply DB migrations (creates db/argosy.db)

cd ui
npm install                      # install UI deps
```

### Run the stack (two terminals)

Terminal 1 — FastAPI backend on `http://localhost:8000`:

```bash
uv run uvicorn argosy.api.main:app --reload
```

Terminal 2 — Next.js dashboard on `http://localhost:1337`:

```bash
cd ui && npm run dev
```

### Phase 0 exit gate

Both servers up, browse to <http://localhost:1337>, and the home page shows the
**Health: OK** badge in green. The dashboard fetches `/api/health` (proxied to
the FastAPI backend), which in turn verifies the SQLite connection. If you see
the green badge, the full stack is wired correctly.

### Tests

```bash
uv run pytest -q
```

## Phase 1 quick start (intake + plan critique)

Phase 1 adds a CLI (`argosy ...`), an Israeli/US tax domain knowledge seed at `domain_knowledge/`, and two cross-cutting agents: the **intake** interview agent and the **plan-critique** agent. No cadences, no decision team, no broker yet.

### One-time setup

1. Apply migrations (creates the new Phase 1 tables — `plan_versions`, `plan_critiques`, `agent_reports`, `agent_reports_blobs`, plus `user_context.current_stage`):

   ```bash
   uv run alembic upgrade head
   ```

2. Set the Anthropic API key. Either of these is fine; the keychain entry takes priority if both are set:

   ```bash
   uv run argosy secrets set argosy.anthropic.api_key sk-ant-...
   # or, transient for shell sessions:
   set ANTHROPIC_API_KEY=sk-ant-...        # Windows cmd
   $env:ANTHROPIC_API_KEY = "sk-ant-..."  # PowerShell
   export ANTHROPIC_API_KEY=sk-ant-...     # bash/zsh
   ```

   If the key is missing the CLI prints actionable instructions and exits non-zero.

### Ingest your data

You provide two file paths — Argosy never hardcodes your Drive paths in code:

```bash
# Portfolio TSV (parses Leumi+Schwab+real-estate+pensions+allocations).
uv run argosy ingest tsv "<path-to-Family Finances Status - YY MMM.tsv>"

# Plan markdown (stored as a new plan_versions row).
uv run argosy ingest plan "<path-to-Jacobs_Wealth_Plan.md>" --version-label v2.0
```

`argosy ingest tsv` prints a summary and reports parse warnings; it does **not** write positions to the DB in Phase 1 (the holdings table arrives in Phase 2).

### Run the intake interview

```bash
uv run argosy intake --user-id ariel
```

The agent walks the SDD §6 six-stage flow one question at a time:

1. Identity & jurisdiction → 2. Goals & timeline → 3. Financial picture → 4. Brokerage connections → 5. Plan import & critique → 6. Operational preferences.

Type `/quit` to end early; progress (current_stage + accumulated YAML) is persisted to `user_context`.

### Produce a plan critique

```bash
uv run argosy critique --user-id ariel \
  --plan "<path-to-Jacobs_Wealth_Plan.md>" \
  --snapshot "<path-to-Family Finances Status - YY MMM.tsv>"
```

If `--plan` is omitted, the most recent ingested `plan_versions` row is used. The agent loads the relevant Israeli/US tax KB files, produces RED/YELLOW/GREEN findings with cited evidence, prints them, and saves the structured critique to `plan_critiques`.

### Tests

```bash
uv run pytest -q
```

The Phase 1 test suite mocks the Anthropic client; no live Claude calls happen in tests. The TSV/plan parser tests skip cleanly when the user's Google-Drive files are absent.

## Phase 2 quick start (cadences + daily brief + dashboard)

Phase 2 wires up the cadence orchestrator, three new analyst agents (news, macro, concentration), the Daily Brief loop, and a real three-tab dashboard (Home / Portfolio / Plan).

### One-time setup

1. Apply migrations (creates `cadence_state`, `daily_briefs`, and the three external-data caches):

   ```bash
   uv run alembic upgrade head
   ```

2. Set adapter API keys. FRED and Finnhub each have a free tier; both are optional but the daily brief is sparse without them. Same priority order as Phase 1 (keychain wins, env var fallback):

   ```bash
   uv run argosy secrets set argosy.fred.api_key <fred-key>
   uv run argosy secrets set argosy.finnhub.api_key <finnhub-key>
   # or transient env vars:
   #   FRED_API_KEY, FINNHUB_API_KEY
   ```

   If a key is missing, the affected adapter raises a clear `MissingAPIKeyError` at call time and the loop logs the failure but continues with reduced fidelity.

3. The first run of the orchestrator writes a default `agent_settings.yaml` under `configs/<user_id>/` (template lives at `configs/example/agent_settings.yaml`). Edit cadences, model overrides, and tier thresholds there.

### Run the cadence scheduler (foreground)

```bash
uv run argosy run --user-id ariel
```

The scheduler runs in the foreground; Ctrl-C stops it cleanly. Phase 2 wires only the **Daily Brief** loop (`0 9 * * *` Asia/Jerusalem by default). Other loops (minute / hour / weekly / monthly / quarterly / annual) are scheduled but their tick implementations land in Phase 3+.

### Trigger a one-shot Daily Brief

Useful for testing without waiting until 09:00:

```bash
uv run argosy brief --user-id ariel
```

This runs the news + macro + concentration analysts and re-runs the plan-critique against today's snapshot, persisting all four reports + a summary to `daily_briefs`. The `daily_brief.ready` WebSocket event fires; the dashboard's Home page subscribes and refreshes automatically.

### Browse the dashboard

```bash
# Terminal 1
uv run uvicorn argosy.api.main:app --reload
# Terminal 2
cd ui && npm install && npm run dev
```

Then open <http://localhost:1337>. Three tabs:

- **Home** — net worth, concentration scorecard, plan RED/YELLOW/GREEN badge, daily-brief teaser, last 10 agent runs.
- **Portfolio** — positions per account from the latest TSV, allocation vs target, FX header.
- **Plan** — rendered plan markdown + critique findings, with a **Re-critique now** button that POSTs to `/api/plan/critique`.

## Reference paper

Xiao et al. *TradingAgents: Multi-Agents LLM Financial Trading Framework.* [arXiv:2412.20138](https://arxiv.org/html/2412.20138v1).
