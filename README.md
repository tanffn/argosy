# Argosy

A multi-agent financial advisor system. Python orchestration + Claude Agent SDK + FastAPI + Next.js dashboard. Designed for sophisticated DIY investors; multi-tenant from day one.

> **Status:** Implementation complete across Phases 0-7 (9 commits, 359 tests passing). Paper mode is the default execution mode; live trading requires the mandatory 2-week paper-mode soak per SDD §13.1.

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

## Getting started

Six steps to a running dev system. Each command is also documented in the phase-specific quick-starts further down — this section is the canonical first-time walkthrough.

### 1. Prerequisites

- Python 3.12+
- Node.js 20+ and npm
- [`uv`](https://github.com/astral-sh/uv) for Python dep management (`pip install --user uv`)
- (Phase 4+ live execution only) Interactive Brokers TWS Gateway, paper-trading port 7497 by default

#### About `uv` and `uv run`

`uv` is a fast Rust-based replacement for `pip + venv`. It manages the project's virtual environment at `.venv/` against `pyproject.toml` (declared deps) and `uv.lock` (pinned versions).

| Command | Purpose |
|---|---|
| `uv sync` | Install/refresh `.venv/` to match the lockfile |
| `uv add <pkg>` | Add a dependency to `pyproject.toml` and install it |
| `uv run <cmd>` | Run `<cmd>` inside the project venv (auto-syncs first; never produces stale-deps errors) |

Every command in this guide that begins with `uv run` (e.g. `uv run argosy intake`) is just running a command inside the project's virtual environment — equivalent to `.\.venv\Scripts\Activate` followed by the command. The `argosy` CLI itself is wired as a project entry point in `pyproject.toml`, so `uv run argosy <subcommand>` works without a separate install step.

### 2. Backend setup (one-time)

```bash
cd D:\Projects\financial-advisor
uv sync                              # install Python deps (~89 packages)
uv run alembic upgrade head          # apply DB migrations 0001..0007
```

### 3. Frontend setup (one-time)

```bash
cd ui
npm install                          # install Next.js + shadcn + react-markdown + next-auth
```

### 4. Anthropic backend & secrets

Argosy can talk to Claude through one of two backends, selected by `[anthropic] backend = ...` in `argosy.toml`:

| Backend | When to use | Auth | Cost lands on |
|---|---|---|---|
| **`claude_code`** *(default)* | You already have the `claude.exe` (Claude Code) CLI installed and authenticated | Inherited from your local Claude Code session — **no API key needed** | Your Claude Code subscription |
| **`api_key`** | You want metered pay-as-you-go API billing, or are running headless on a server without `claude.exe` | `sk-ant-...` key from <https://console.anthropic.com/settings/keys> stored in the OS keychain | Anthropic API account |

The default is `claude_code` so a fresh checkout works out of the box if you're already a Claude Code user. To switch to the API-key backend, edit `argosy.toml`:

```toml
[anthropic]
backend = "api_key"
keychain_key_name = "argosy.anthropic.api_key"
```

…or set the env var `ARGOSY_ANTHROPIC__BACKEND=api_key`. Then store the key:

```bash
uv run argosy secrets set argosy.anthropic.api_key sk-ant-...
```

Optional adapter keys (richer daily briefs; both backends use these the same way):

```bash
uv run argosy secrets set argosy.fred.api_key <fred-key>
uv run argosy secrets set argosy.finnhub.api_key <finnhub-key>
```

### 5. Run the stack (two terminals)

```bash
# Terminal 1 — backend on http://localhost:8000
uv run uvicorn argosy.api.main:app --reload

# Terminal 2 — frontend on http://localhost:1337
cd ui && npm run dev
```

Browse to <http://localhost:1337>. The home dashboard should render with a green **Health: OK** badge.

### 6. Onboard yourself (first-run)

```bash
uv run argosy intake --user-id ariel        # 6-stage interview (SDD §6.1)
uv run argosy ingest tsv "<portfolio.tsv>"  # import current positions
uv run argosy ingest plan "<plan.md>"       # import existing plan (optional)
uv run argosy critique --user-id ariel      # run plan-critique agent
uv run argosy brief --user-id ariel         # one-shot daily brief
```

After this, the orchestrator's cadence loops can run continuously:

```bash
uv run argosy run                            # foreground scheduler
```

For depth on each phase (decision flows, IBKR setup, Argonaut autonomy, productization), see the phase-specific quick-starts below or the [SDD](docs/design/SDD.md) §13.

## Repo layout

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

2. Pick an Anthropic backend (see Getting started §4 for the full table).

   - **`claude_code`** (default) — uses your local `claude.exe` session; no API key needed. Skip this step.
   - **`api_key`** — set `[anthropic] backend = "api_key"` in `argosy.toml`, then store the key. The keychain entry takes priority if both are set:

   ```bash
   uv run argosy secrets set argosy.anthropic.api_key sk-ant-...
   # or, transient for shell sessions:
   set ANTHROPIC_API_KEY=sk-ant-...        # Windows cmd
   $env:ANTHROPIC_API_KEY = "sk-ant-..."  # PowerShell
   export ANTHROPIC_API_KEY=sk-ant-...     # bash/zsh
   ```

   If `backend = "api_key"` and no key is found, the CLI prints actionable instructions and exits non-zero.

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

## Phase 3 quick start (decision team + tiers + proposals)

Phase 3 wires the full TradingAgents-pattern decision team: bull/bear
researchers + facilitator → trader → 3-perspective risk team + facilitator
→ fund manager. Adds the tier system (T0/T1/T2/T3), proposals queue,
rule-based risk preflight, weekly review loop, and the dashboard
**Proposals** tab.

### One-time setup

Apply migrations (creates `proposals`, `proposals_history`, `approvals`,
`decision_runs`):

```bash
uv run alembic upgrade head
```

### Trigger a one-shot decision flow

```bash
uv run argosy decide --ticker AAPL --tier auto --user-id ariel \
  --proposed-value 5000 --portfolio-value 100000
```

Tier flag accepts `auto` | `T0` | `T1` | `T2` | `T3`. With `auto`, the
resolver computes the tier from the value/portfolio rules in SDD §4.1
(plus the NVDA / plan-structural / concentration-cap overrides).

The decision-flow output is a `proposals` row plus the full reasoning
trail (one `agent_reports` row per agent invocation, all linked by the
parent `decision_runs` id).

### Review proposals

```bash
uv run argosy proposals list --user-id ariel              # all proposals
uv run argosy proposals list --status awaiting_human      # pending review
uv run argosy proposals approve <id> --user-id ariel      # 1-click approve
uv run argosy proposals approve <id> --second-factor      # required for T3
uv run argosy proposals reject <id>  --note "no go"
```

The dashboard's **Proposals** tab provides the same actions plus a
reasoning-trail expansion that pages through every agent-report row
that produced the proposal. The page subscribes to WebSocket events
`proposal.created` / `proposal.updated` and refreshes automatically.

### Tier override modes (SDD §4.4)

Edit `${ARGOSY_HOME}/configs/<user_id>/agent_settings.yaml`:

```yaml
tiers:
  override_mode: auto                  # default — use the resolver
  # override_mode: pinned:T2           # floor every decision at T2
  # override_mode: all-tier            # run the full T3 stack on everything
  # override_mode: per-decision-escalate  # UI button bumps single proposals
```

### Cadences added in Phase 3

- `weekly_review` — Sunday 18:00 default; full plan-critique re-pass +
  RED flagging via `weekly_review.flagged` WebSocket event
- `process_cooling` — every 60s; advances ripe `cooling` proposals
  (T2/T3 main → `awaiting_human`; limited+paper → auto-`executed_paper`)

Both auto-register when the scheduler starts (`uv run argosy run`).

### Tests

```bash
uv run pytest -q
```

Tests mock the Anthropic client; nothing in the test suite calls live
Claude. The 178+ tests cover tier resolution (every branch + override
modes), proposals state-machine (every legal transition + reject
illegal), risk preflight (every check + aggregator), the bull/bear
debate, the 3-perspective risk team, trader, fund manager, full
decision-flow happy paths for T0/T1/T2/T3, the proposals API, and the
two new cadence loops.

## Phase 4 quick start (IBKR adapter + execution + email approval)

Phase 4 wires the brokerage layer end-to-end: a `BrokerAdapter` Protocol,
the IBKR adapter (read+write through TWS Gateway), a Schwab read-only
CSV importer, a Leumi read-only TSV wrapper, the execution router, the
reconcile loop, and the email approval channel. Live execution is gated
to T0/T1 in main accounts via the queue+approve flow; T2/T3 still
requires explicit human approval via the dashboard.

### One-time setup

Apply migrations (creates `audit_log`, `lots`, `fills`, `pending_orders`):

```bash
uv run alembic upgrade head
```

### Run TWS Gateway in paper mode

1. Install [IBKR TWS Gateway](https://www.interactivebrokers.com/en/trading/tws.php) and run the **Paper Trading** instance.
2. In the gateway: **Configure → Settings → API → Settings**, enable
   "Enable ActiveX and Socket Clients", set the **Socket port** to
   **7497** (paper) or **7496** (live), and check "Read-Only API" off.
3. Allow connections from `127.0.0.1` only (default).
4. Optional per-account override: create
   `configs/<user_id>/ibkr_settings.yaml`:

   ```yaml
   accounts:
     limited:
       host: localhost
       paper_port: 7497
       live_port: 7496
       client_id: 1
       mode: paper
   ```

### Set IBKR credentials

The gateway handles the actual login; Argosy stores the IBKR username
for audit only:

```bash
uv run argosy secrets set argosy.ibkr.username <ibkr-user>
```

### Import a Schwab cost-basis CSV

1. From schwab.com → **Accounts → History → Cost Basis** export the CSV.
2. Persist the lots:

   ```bash
   uv run argosy lots import --broker schwab --path "<path>.csv" --user-id ariel
   ```

The CLI reports the row count. Dashboard `/audit` and `/api/lots` then
expose the imported rows. Re-importing appends; clear the `lots` table
manually before re-importing the same export.

### Send an approval email

1. Create `configs/<user_id>/email_settings.yaml`:

   ```yaml
   smtp_host: smtp.gmail.com
   smtp_port: 587
   smtp_username: argosy@example.com
   smtp_use_tls: true
   sender: argosy@example.com
   public_url: http://localhost:8000
   ```

2. Stash the SMTP password (and let Argosy auto-generate a token-signing
   key on first send):

   ```bash
   uv run argosy secrets set argosy.email.smtp_password <password>
   ```

3. Send for a specific proposal:

   ```bash
   uv run argosy email send-approval <proposal_id> --to you@example.com
   ```

The email contains two signed links — one approve, one reject — that
land on `/api/proposals/{id}/approve?token=...`. The endpoint verifies
the signature + expiry (24h), then **redirects** to the dashboard with
`?confirm=<id>&action=<approve|reject>&token=...`. The dashboard shows
a one-click confirm dialog (per SDD §10.2 anti-phishing rule).

### Execute a proposal

After a proposal is `approved` (via dashboard 1-click, CLI, or email
landing), trigger execution:

```bash
# Via the dashboard: visit /proposals and click "Execute now".
# Or via CLI:
uv run argosy execute <proposal_id> \
  --user-id ariel \
  --cash-available-usd 100000 \
  --max-position-usd 25000
```

The execution router re-runs the rule-based risk preflight (SDD §9.3),
calls `IBKRAdapter.place_order(paper=mode == "paper")`, and either:

- **paper**: writes a `PaperFill` row + audit_log entry; transitions
  the proposal to `executed_paper`.
- **live**: places the order via TWS Gateway, records a `pending_orders`
  row, transitions the proposal to `executed_live`. The reconcile loop
  (30s cadence during market hours) then writes `fills` rows as they
  arrive and updates `pending_orders.status`.

### Audit log

Every fill, approval, paper fill, broker error, and credential read
writes one row to `audit_log` (SDD §14.1). Browse via the new
**Audit** dashboard tab or `/api/audit`.

```bash
uv run argosy fills list --proposal <id>      # CLI fills view
```

### Tests

```bash
uv run pytest -q
```

Tests mock both `ib_insync` and `aiosmtplib`; nothing in the test suite
makes a live broker connection or sends real email. The Phase 4 suite
covers Protocol conformance for all three adapters, IBKR paper/live
symmetry, Schwab CSV parsing, the execution router (preflight pass /
hard-fail / paper / live paths), the reconcile loop (filled / partial /
cancelled / rejected), email token round-trip + tampering rejection,
and the new API routes.

## Phase 5 quick start (Argonaut limited-account autonomy)

Phase 5 wires the limited-account autonomous path: T0/T1 decisions auto-execute
inside a bounded IBKR Pro account (the **Argonaut**); T2/T3 still require
human approval, with a 24h cooling-off + analyst-delta + risk-preflight
re-check before T3 commits. Kill-switch (`ARGOSY_KILL=1`) is honored at
every auto-execute call.

### 1. Open an IBKR Pro account

IBKR Lite is unavailable to Israeli residents; IBKR Pro is the right product.
Fund it with the bounded amount you wrote into your plan (default $1,000).
Take note of the account id (e.g. `U1234567`).

### 2. Configure Argonaut in `agent_settings.yaml`

```yaml
limited_account:
  size_usd: 1000             # bound the agent's capital
  account_id: "U1234567"     # IBKR account id
  execution_mode: paper      # paper | live | queue_only — start at paper
  per_decision_max_pct: 20   # > 20% of acct → escalate one tier
  daily_loss_limit_pct: 5    # halts new trades when breached
```

Or use the dashboard: navigate to the **Argonaut** tab and click the mode
buttons. The page reads/writes the same YAML file via `/api/argonaut/mode`.

### 3. Set up T3 second-factor (TOTP or 1h delay)

```yaml
security:
  t3_second_factor: delay    # delay (recommended for solo) or totp
  delay_minutes: 60
```

For TOTP:

```bash
uv run argosy security totp setup       # prints provisioning URI
uv run argosy security totp verify 123456
```

Then approve T3 proposals with `X-TOTP-Code: <code>` in the API call (the
dashboard prompts for it inline).

### 4. The 4-week soak protocol

Per SDD §13, Phase 5's exit gate is **4 weeks of autonomous paper-mode
operation with no kill-switch trips**, plus T0/T1 auto-executions matching
what you would have approved manually 90%+ of the time. Don't switch to
`live` early.

  - Week 1: paper mode only; review every auto-execution daily.
  - Week 2: paper mode; check the audit_log for `auto_promoted=True` entries
    and verify each one was a decision you'd have approved.
  - Weeks 3-4: paper mode; run the agreement-rate analysis. If < 90%, fix
    the underlying agent prompts before going live.
  - End of week 4: flip to `live` mode (Argonaut tab → live button) only if
    the agreement-rate target is hit AND no kill-switch trips occurred.

The kill switch is your seatbelt: `ARGOSY_KILL=1` halts all new orders and
leaves the engine in read-only mode. Trip it at the first sign of trouble.

### CLI

```bash
uv run argosy argonaut status               # current account state
uv run argosy argonaut snapshot             # force a daily snapshot
uv run argosy argonaut mode paper           # toggle execution mode
uv run argosy security totp setup           # enroll TOTP
uv run argosy security totp verify <code>
```

### Tests

```bash
uv run pytest -q
```

Tests mock `ib_insync` and never make live broker calls. The Phase 5 suite
covers ArgonautAccount config + snapshots, the auto-execute path
(limited+T0/T1 vs T2/T3 vs main), account-scoped escalation re-check at
execution time, T3 cooling-off re-check (delta detection + preflight),
TOTP secret/verify/replay, the daily loss limit gate, and the new API
routes.

## Phase 6 quick start (productization)

Phase 6 promotes Argosy from a single-tenant developer tool to a hosted
multi-tenant service. Per-tenant SQLite databases, license / quota
enforcement, NextAuth, an opt-in telemetry pipeline, white-label
branding, and an admin CLI for tenant onboarding all land here.

### Hosted vs self-hosted

Argosy ships two deploy paths:

- **Hosted (Vercel + Fly):** `ui/` deploys to Vercel; the engine runs as
  a Fly.io app per tenant. See `docs/deploy/vercel.md` and
  `docs/deploy/fly.md`.
- **Self-hosted (Docker compose):** single host runs `argosy/engine` +
  `argosy/ui` containers from `docker-compose.yml`. See
  `docs/deploy/docker.md`.

The engine reads `ARGOSY_CORS_ORIGINS` (comma-separated) so the same
image serves localhost dev, Vercel-hosted UIs, and bespoke white-label
domains.

### Tenant onboarding

A new tenant is provisioned by an operator:

```bash
uv run argosy admin tenant create --user-id alice --email alice@example.com --plan pro
```

The command prints a JSON payload with a single-use **setup token**.
Send the token to the new tenant; they visit `/onboarding?token=...` to
complete first login (NextAuth credentials provider). The engine
creates `${ARGOSY_HOME}/tenants/alice/argosy.db` and seeds
`configs/alice/{entitlements,branding}.yaml`.

```bash
uv run argosy admin tenant list      # registry of provisioned tenants
```

### Entitlement plans

Per-tenant feature + quota gating lives in
`configs/<user_id>/entitlements.yaml`. The minimal form just records
the plan tier; the rest is resolved from the defaults:

```yaml
plan: pro     # free | pro | enterprise
# Optional overrides:
# features:
#   autonomous_mode: true
# limits:
#   monthly_decisions: 500
```

Plan tier defaults:

| Feature             | free | pro | enterprise |
|---------------------|:---:|:---:|:---:|
| `agent_fleet_full`  | -   | yes | yes |
| `domain_kb_custom`  | -   | yes | yes |
| `multi_account`     | -   | yes | yes |
| `autonomous_mode`   | -   | -   | yes |
| `live_execution`    | -   | -   | yes |

`/api/decisions/run`, `/api/argonaut/mode→live`, and
`/api/proposals/{id}/execute (live)` all enforce the matching feature.
Free-tier tenants additionally hit a 50/month decision count cap and
$5/month Claude spend cap; pro raises both, enterprise removes them.

### Watchdog

```bash
uv run argosy admin watchdog start --user-id alice
```

Polls the SDD §14.2 health signals (engine heartbeat, cadence loops,
broker, Claude error rate, monthly spend, disk, backup age) and emails
on threshold breach. The same data is exposed via
`GET /internal/health/full?user_id=...` (admin token required).

### Telemetry

Opt-in. Set `agent_settings.telemetry.enabled: true` and an
`endpoint:` URL; the client POSTs anonymized event records (sha256
hashed user id, no tickers / prices / plan content). For self-hosted
instances `POST /internal/telemetry` is a built-in receiver stub —
useful for engine-only observability without an external service.

### White-label branding

`configs/<user_id>/branding.yaml` overrides the app name + theme
tokens; the dashboard fetches `/api/branding` on mount and applies the
theme via `BrandingProvider`.

```yaml
app_name: Pilot Capital
theme:
  primary: "#0f172a"
  accent: "#a78bfa"
logo_url: /pilot/logo.svg
favicon_url: /pilot/favicon.ico
support_email: hello@pilot.example
```

### Cross-tenant isolation

- One SQLite file per tenant under `${ARGOSY_HOME}/tenants/<user_id>/argosy.db`.
- `argosy.tenancy.context.TenantContext` resolves the per-request
  user_id from the NextAuth JWT, an `X-Argosy-User` header, or the
  `user_id` query/body parameter.
- Setting `ARGOSY_TENANCY=per-tenant` flips `state.db.get_session()` to
  route each session to the tenant's DB.
- Cross-tenant access raises `CrossTenantAccessError`. The
  `tests/test_tenant_isolation.py` suite asserts that two tenants on
  one engine cannot read each other's positions / proposals / agent
  reports / fills / audit log / TOTP secret.

## Phase 7 — SDD completeness

Phase 7 closes the remaining SDD gaps so every feature listed in the
spec has a working scaffold:

### Five additional analyst agents

| Agent | Module | Default model |
|---|---|---|
| Fundamentals | `argosy.agents.fundamentals_analyst.FundamentalsAnalystAgent` | Sonnet |
| Technical    | `argosy.agents.technical_analyst.TechnicalAnalystAgent`    | Haiku  |
| Sentiment    | `argosy.agents.sentiment_analyst.SentimentAnalystAgent`    | Haiku  |
| Tax          | `argosy.agents.tax_analyst.TaxAnalystAgent`                | Sonnet |
| FX           | `argosy.agents.fx_analyst.FXAnalystAgent`                  | Haiku  |

Each follows the news / concentration analyst pattern (pydantic output,
mocked-Anthropic tests, citation gate). Tax requires a non-empty
`domain_knowledge/tax/...` citation set.

### Three cross-cutting agents

| Agent | Module | Cadence |
|---|---|---|
| Domain refresh | `argosy.agents.domain_refresh.DomainRefreshAgent` | Weekly + annual full pass |
| Audit          | `argosy.agents.audit_agent.AuditAgent`            | Weekly                    |
| Watchlist      | `argosy.agents.watchlist.WatchlistAgent`          | Daily                     |

### Five new cadence loops

| Loop | Class | Schedule |
|---|---|---|
| Minute         | `MinuteLoop`        | 60 s, market-hours-only |
| Hour           | `HourLoop`          | 60 min, 24/7            |
| Monthly cycle  | `MonthlyCycleLoop`  | `0 8 1 * *`             |
| Quarterly      | `QuarterlyLoop`     | enabled flag only       |
| Annual         | `AnnualLoop`        | `0 8 2 1 *`             |
| Backup         | `BackupLoop`        | `0 3 * * *`             |

`Scheduler.register_default_loops()` wires them all, gated on
`cadences.<name>.enabled` from `agent_settings.yaml`.

### Cost-cap pause enforcement

`argosy.orchestrator.cost_guard.CostGuard` checks current month's
`agent_reports.cost_usd` sum against
`agent_settings.cost.monthly_budget_usd × pause_at_pct / 100`. All loops
EXCEPT `daily_brief` and `process_cooling` consult the guard at tick
start. `POST /internal/cost-guard/override` (admin-token-gated) lifts
the pause for a window; the override is audit-logged.

### Daily backup automation

`BackupLoop` snapshots the SQLite DB to
`${ARGOSY_HOME}/backups/argosy-YYYYMMDD.db` (or
`agent_settings.backups.backups_dir`) using `sqlite3.backup`. Retention:
30 daily, 12 weekly (Sunday), 12 monthly (1st), indefinite annual. When
`agent_settings.backups.offsite_path` is set, Sunday backups also
`shutil.copy2` to that path.

### Four new UI screens

| Screen | Route | API endpoints |
|---|---|---|
| Agent activity | `/agents`     | `/api/agent-activity` |
| Domain KB      | `/domain-kb`  | `/api/domain-kb/{tree,file,review-queue,review/{id}/{approve,reject}}` |
| Intake wizard  | `/intake`     | `/api/intake/{turn,status}` |
| Settings       | `/settings`   | `GET /api/settings`, `PATCH /api/settings`, `POST /internal/cost-guard/override` |

Nav entries added; each page is a Client Component using shadcn/ui
patterns established in earlier phases.

## Reference paper

Xiao et al. *TradingAgents: Multi-Agents LLM Financial Trading Framework.* [arXiv:2412.20138](https://arxiv.org/html/2412.20138v1).
