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

## Reference paper

Xiao et al. *TradingAgents: Multi-Agents LLM Financial Trading Framework.* [arXiv:2412.20138](https://arxiv.org/html/2412.20138v1).
