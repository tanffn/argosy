# Deployment Advisor P2 — Live Market Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the Deploy Cash surface market-aware and never-stale: pull live S&P/VIX/oil/USD-NIS/BoI-rate/inflation + verify NVDA price/share-count, flag any stale or internally-inconsistent feed, surface each read's age, and let the expert layer set size+math-driven pacing (lump vs DCA) per line — replacing P1's flat `timing="now"`.

**Architecture:** A new `argosy/services/deployment_market_context.py` assembles a `DeploymentMarketContext` (macro snapshot + freshness per field + NVDA verification), reusing the existing FRED/BoI/yfinance adapters and the `agent_reports` cached-read pattern (live when available on the `claude_code` backend, freshest cached otherwise, with age surfaced). `assemble_deployment_plan` gains an optional `market_context` param; when present it drives `order_and_explain`-style pacing and stamps `market_context_age` + staleness caveats. No live calls when the context is absent (P1 behavior preserved).

**Tech Stack:** Python + FastAPI + SQLAlchemy; reuses `argosy/adapters/data/{yfinance,fred,boi,finnhub}_adapter.py`, `argosy/services/fx`, `AgentReport`. UI: Next.js + Vitest.

---

## Grounding (verified interfaces — from the P2 infra scout)

- **Adapters** (`argosy/adapters/data/`): `FredAdapter.get_series(series_id)` (VIX=`VIXCLS`, oil=`DCOILWTICO`; **add** S&P=`SP500`, CPI=`CPIAUCSL`, BoI rate via FRED `IRSTCI01ILM156N` or BoI API); `BoiAdapter.get_usd_nis()`; `YFinanceAdapter.get_quote(ticker)` → `Quote(ticker, price, currency, timestamp_utc)`.
- **Cached reads:** `AgentReport(agent_role, response_text, created_at, user_id)`; latest = `select(AgentReport).where(role==..., user_id==...).order_by(desc(created_at)).limit(1)`. Roles: `"macro"`, `"fx"`, `"news"`.
- **FX:** `argosy.services.fx.rate(session, "USD", "NIS", date)` + `FxRate` table.
- **Live analyst invocation:** mirror `plan_synthesis/orchestrator.py::_safe_run_agent` + `BaseAgent.run_sync()` (claude_code backend, no API key).

## Pinned technical definitions (P2)

1. **Staleness gate (trust-data-feed doctrine):** flag a feed stale only on (a) age beyond a per-feed TTL, or (b) **demonstrable internal inconsistency** — never "feels wrong." The one hard consistency check: `abs(marketCap/shares - price) / price > 0.10` ⇒ inconsistent (the documented rule). Per-feed max ages: quotes 15 min, macro/FX 24 h, news 48 h (config constants `DEPLOY_FRESHNESS_MAX_AGE`). Stale ⇒ surfaced as a loud per-feed caveat + `is_stale` flag, NOT a hard block in P2 (advisory).
2. **Live-vs-cached:** try live (claude_code analyst run / direct adapter) first; on failure or no-session fall back to the freshest `agent_reports` cached read and surface its age in `market_context_age`. Never blank, never silently old.
3. **Lump-vs-DCA pacing (decision 11) — CODEX-REVIEW:** size+math driven. Small lines (`<= DCA_LUMP_THRESHOLD_USD`, default $5k) always deploy whole (`"now"`). Larger lines evaluate DCA-over-N-weeks where N scales with a volatility/valuation signal: `N = clamp(round(k * vix_zscore + m * pctile_above_52w_ma), 1, 8)` weeks (exact form pinned with codex during the slice — the inputs are VIX level vs its trailing mean + price percentile vs 52-week range). The expert layer chooses + explains; amounts are unchanged (engine-owned).
4. **NVDA verification:** fetch live quote + shares; verify consistency; surface the verified price + share count + age. If inconsistent or stale, flag loudly (the deploy list still renders — advisory).
5. **No magic numbers:** every macro figure carries its source + fetch time; thresholds are named config constants.

## File structure

**New:** `argosy/services/deployment_market_context.py` (assembler + `DeploymentMarketContext`, `DataFreshness`, `NvdaVerification` dataclasses + freshness/consistency helpers); `tests/test_deployment_market_context.py`.
**Modify:** `argosy/adapters/data/fred_adapter.py` mappings or a small `argosy/services/market_snapshot.py` helper for the missing series (S&P/BoI/CPI); `argosy/adapters/data/yfinance_adapter.py` (`get_quote` → optionally also shares/marketCap, or a new `get_quote_with_fundamentals`); `argosy/services/deployment_advisor.py` (`assemble_deployment_plan(..., market_context=None)` + pacing + age/staleness caveats); `argosy/services/contracts.py` (DTO: add `market_context` block + per-line `timing`/`pace_rationale` already present); `argosy/api/routes/portfolio.py` (`GET /deploy-cash` gains `?live=true` to assemble context); UI `api.ts` + `DeployCashCard.tsx` (render context + freshness + per-line pacing).

---

## Tasks

### Task 1: Freshness + market-context dataclasses
- [ ] **Test** (`tests/test_deployment_market_context.py`): construct `DataFreshness(field, fetched_at, age_seconds, source, is_stale)`, `NvdaVerification(price, shares, market_cap, consistent, note)`, `DeploymentMarketContext(snapshot: dict[str,float], freshness: tuple[DataFreshness,...], nvda: NvdaVerification|None, overall_age_label: str)`; assert field access + an `is_any_stale` property.
- [ ] **Run** → fail (module missing).
- [ ] **Implement** the frozen dataclasses + `DeploymentMarketContext.is_any_stale` (any freshness.is_stale or nvda not consistent).
- [ ] **Run** → pass. **Commit** `feat(deploy): market-context dataclasses (P2)`.

### Task 2: Freshness + NVDA-consistency helpers (CODEX money-adjacent)
- [ ] **Test:** `is_stale(age_seconds, max_age_seconds)` boundary; `nvda_consistency(price, shares, market_cap)` → True when `abs(market_cap/shares - price)/price <= 0.10`, False at 11% drift, and **None/flagged** when shares or market_cap missing (never silently "consistent").
- [ ] **Run** → fail. **Implement** `DEPLOY_FRESHNESS_MAX_AGE` (quotes 900s, macro/fx 86400s, news 172800s) + `is_stale` + `nvda_consistency`. **Run** → pass. **Commit** `feat(deploy): staleness + NVDA-consistency helpers (P2)`.

### Task 3: Missing market series (S&P index, BoI rate, CPI)
- [ ] **Test:** a `market_snapshot(session)` helper returns a dict with keys `sp500, vix, oil_wti, usd_nis, boi_rate, cpi_yoy` each paired with a `DataFreshness`; monkeypatch the FRED/BoI adapters so the test is deterministic (no network).
- [ ] **Run** → fail. **Implement** `argosy/services/market_snapshot.py` wiring `FredAdapter.get_series` for `SP500`/`CPIAUCSL`/BoI-rate series + `BoiAdapter.get_usd_nis`, each stamping a `DataFreshness` from the adapter's cache `fetched_at`. **Run** → pass. **Commit** `feat(deploy): market snapshot incl. S&P/BoI-rate/CPI (P2)`.

### Task 4: NVDA price+share verification
- [ ] **Test:** `verify_nvda(session)` returns `NvdaVerification` with price/shares/market_cap + consistency, monkeypatching the yfinance call; asserts inconsistent flagged when marketCap/shares diverges >10%.
- [ ] **Run** → fail. **Implement** the yfinance fundamentals fetch (extend `get_quote` or add `get_quote_with_fundamentals`) + `verify_nvda`. **Run** → pass. **Commit** `feat(deploy): NVDA price/share verification (P2)`.

### Task 5: Context assembler (live + cached fallback)
- [ ] **Test:** `assemble_deployment_market_context(session, *, allow_live)` — with monkeypatched live sources returns fresh context (age ~0); with live raising, falls back to the latest `agent_reports` macro/fx read and surfaces its age; sets `is_any_stale` correctly.
- [ ] **Run** → fail. **Implement** the assembler (live `market_snapshot` + `verify_nvda`; on failure read latest `AgentReport` per role, compute age, set freshness). **Run** → pass. **Commit** `feat(deploy): market-context assembler with cached fallback (P2)`.

### Task 6: Wire context into assemble_deployment_plan + pacing (CODEX REVIEW)
- [ ] **Test:** `assemble_deployment_plan(..., market_context=ctx)` stamps `market_context_age`, adds a staleness caveat when `ctx.is_any_stale`, and sets per-line `timing`: lines `<= $5k` → `"now"`; larger → `"DCA Nwk"` with a `pace_rationale`. Sum invariant + estate split unchanged. `market_context=None` ⇒ exact P1 behavior (timing "now", age None).
- [ ] **Run** → fail. **Implement** the pacing function (size threshold + the codex-pinned vol/valuation N) + thread `market_context` through. Add `pace_rationale` to `DeploymentLine` (+ DTO + UI type). **Run** → pass.
- [ ] **CODEX-TANDEM** review the pacing math (`tmp_review/codex_deploy_pacing_review.py`, `PYTHONIOENCODING=utf-8`): is the lump/DCA boundary + the N-weeks formula decision-grade? Fix blockers. **Commit** `feat(deploy): market-aware size+math pacing per line (P2)`.

### Task 7: Endpoint `?live=true` + DTO
- [ ] **Test** (`tests/test_deploy_cash_route.py`): `GET /deploy-cash?cash_usd=250000&live=true` (monkeypatch the assembler) returns `market_context` block (snapshot + freshness + nvda + overall_age_label) and per-line `timing`/`pace_rationale`; `live` omitted ⇒ P1-shape (no live calls).
- [ ] **Run** → fail. **Implement** the `live` query param + `DeploymentMarketContextDTO` + converter; route assembles context only when `live`. **Run** → pass. **Commit** `feat(deploy): /deploy-cash live market context (P2)`.

### Task 8: UI — market context + freshness + pacing
- [ ] **Test** (vitest): `DeployCashCard` renders the market-context strip (S&P/VIX/USD-NIS + each read's age), a loud staleness badge when `is_any_stale`, the NVDA-verified line, and per-line `timing` (now vs DCA Nwk) + pace rationale.
- [ ] **Run** → fail. **Implement** the UI (types in `api.ts`, a `MarketContextStrip` sub-component, `live` toggle wiring in the page). `npx tsc --noEmit` + `npm run lint` + vitest clean. **Commit** `feat(deploy): UI market-context strip + per-line pacing (P2)`.

### P2 exit check
- [ ] Touched-file backend tests + `test_deployment_advisor.py` + `test_deploy_cash_route.py` green.
- [ ] UI tsc + lint + vitest clean.
- [ ] **Live drive** `?live=true` against real data → `tmp_review/deploy_cash_p2_live.json`: real S&P/VIX/USD-NIS with ages, NVDA verified, larger lines show DCA pacing, sums still exact, NVDA still $0.
- [ ] Full suite green pre-merge.
- [ ] **Trust-doctrine:** every macro number sourced + aged; staleness loud; pacing explained; `live` omitted preserves P1 exactly.

---

## Self-review
- **Spec coverage (P2):** live S&P/VIX/oil/FX/BoI/inflation ✔ (T3,T5), geopolitical via cached news read ✔ (T5, partial — Finnhub is per-ticker; flagged), NVDA verify ✔ (T4), staleness gate ✔ (T2,T6), size+math DCA pacing ✔ (T6). Geopolitical/Iran is the weakest (no dedicated feed) — surfaced via the cached news analyst read + flagged as a known gap to harden in P3/P4.
- **Placeholders:** none; the one deferred specific is the exact N-weeks pacing formula, explicitly pinned via codex in T6 (not a placeholder — a reviewed money-math step).
- **Type consistency:** `DataFreshness`/`NvdaVerification`/`DeploymentMarketContext` field names identical across service, DTO, UI. `market_context`/`pace_rationale`/`timing` consistent end-to-end. `market_context=None` path preserves P1 exactly.
</content>
