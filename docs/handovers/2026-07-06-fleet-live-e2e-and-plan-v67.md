# Handover — 2026-07-06 · the fleet ran live end-to-end: root causes fixed, plan v67 current, real money deployed

**Branch:** `master` · **HEAD:** `6fe42a5` · tree clean. 21 commits this session (2fdc490..6fe42a5), all merged + touched-area tests green (full ~3.5h suite NOT run — advisable before the next big merge).
Read this first in a fresh session, then `git log --oneline -25`.

---

## 0. THE headline

The whole loop the project exists for ran **live, end-to-end, with real money**: fleet authors → blind team reviews → flags reach the client → client executes at the broker → fills reconcile into the book → verify-run audits the chain → its findings become fixes. Plan **v67 is current** (promoted through the no-override gate); Ariel deployed **$161,376 across 11 broker fills** against it plus a $20k SGOV cover-sale. Three prior "blockers" turned out to be root-causable bugs, all fixed (§1).

## 1. Root causes found + fixed (each was misdiagnosed before)

- **The "transient claude.exe exit-1 flake" was mostly deterministic** (`c207c4c`): `max_turns=1` starved Opus 4.8 + adaptive thinking of its continuation turn; the CLI prints "Error: Reached max turns (1)" to STDOUT (stderr empty = the "silent flake" fingerprint) and exits 1. Cap now 3. Proven: 4/4 fail before, 2/2 pass after with retries disabled. The shared outer reliability envelope (`argosy/services/fleet_reliability.py`: known-transient-only retries w/ long backoff, per-scope breaker, sync hard-timeout+kill) also exists for genuinely transient bursts — wired into consult analysts + deploy reviewers.
- **Both new inbox sinks were silently dead** (`a560431`): `ck_action_proposals_kind` (0055) rejected `stock_decision` + `deploy_team_flag`; both sinks swallowed the CHECK IntegrityError as a presumed dedup collision; fake-db unit tests stayed green. Migration 0077 relaxed the CHECK; sinks log swallowed errors; REAL-SCHEMA write tests added (`alembic_engine_at_head`).
- **Fleet cost telemetry under-billed every cached call** (`388ec75`): `_estimate_usd` scaled ~77k cache tokens to ~3. Fixed forward; history not restated. Also: **fleet sessions are now isolated from the developer's personal Claude Code config** (`setting_sources=[]` + tools mirror; 33.8k → 10.5k effective input tokens/call, ~69% cut; knob `anthropic.claude_code_isolated`, default true). `agent.run.finished` now logs `effective_input_tokens`; `thinking_tokens=0` is real (CLI doesn't emit the field), logged once at startup.
- **Trader/analyst "no fair-value anchor"** (`ac3925c`): Finnhub carries no price/share-count AND the fundamentals prompt whitelist dropped even the EPS it had. Anchors (price/EPS/shares/mcap/revenue/NI/FCF) now flow end-to-end; catch-all render so upstream fields can't be silently dropped again.

## 2. Capability shipped

- **Analysts have live web access** (`df40451`): news + fundamentals get WebSearch (conditional 1-3 searches, cite-every-URL, payload numbers stay arithmetic truth; WebFetch deliberately off). Sentiment falls back to news-derived input (labelled) instead of being skipped for non-held tickers. This closed the "fleet missed ELF tariffs / CELH Texas-AG probe" insight gap vs a web-connected single agent.
- **verify-run skill** (`ad75ba3`, `.claude/skills/verify-run/SKILL.md`): post-hoc audit of any live run — roster, silent-degrade sweep, groundedness re-derivation from `agent_reports.sources_json`, verdict consistency, flag→inbox delivery. Ariel's chosen verification surface over test sprawl ("only telemetry can show if the fleet is working right"). Used live twice; its 3 WARNs on the deploy all became fixes (`ba4fb2d`). Also: 23 noise test files deleted after a 574-file audit (`920b3f4`, audit at `tmp/test_noise_audit.md`).
- **Snapshot self-refresh** (`43fe57b`): Argosy re-prices its own book (quantities carried, live quotes w/ suffix/pence/plausibility guards, FX, independent totals). **TSV is OUTPUT-only — the client never exports anything for freshness** (Ariel, binding). `apply_fills_to_snapshot` (`f83a05f`) folds executed broker fills in (blended avg, conservation-verified). SnapshotRefreshJob registered `enabled=False` (manual Run-now).
- **Deploy funding breakdown** (`6fe42a5`): the deploy DTO now carries per-(account,currency) cash + deterministic `required_actions` ("convert ~NIS X → USD at Leumi before executing") — born from a live incident (§4).
- **Inbox hygiene**: flag dedup collisions refresh-in-place; flags the team re-reviews and clears **auto-supersede** (`c714abe`) — resolved items leave the client's checklist.

## 3. The plan chain: v64 → v67 (all promoted through the REAL gate, zero overrides)

- **v64** (high-growth sleeve) promoted after gate fixes (`7b5ca0d`): moonshot domicile carve-out (sleeve-scoped), refinement drafts validate headline numbers against the nearest synthesis ancestor's manifest (real validation, not exemption), inherited IPS/currency debt repaired, freshness cleared by the self-refreshed snapshot.
- **v66** (`8aa7e35`,`a78f998`,`e390320`,`79799a5`): **R1GR → IWQU** (13.93% → 5.11-6.5% NVDA; author picked QDVB, blind reviewer refuted, code-forced reconciliation → author conceded — adversarial provenance worked). **Gold OUT by MODEL** (σ-kernel + drag arithmetic: negative geometric contribution in every parameterization incl. gold's best case; Ariel's rule: gold is not banned, it carries the burden of proof — see memory). **NVDA target is ARGOSY's** ("Ariel chose 12" narrative retired; cap-derived: cap clears to ~9.5, 8 held as a deliberate ~1.5pp drift buffer, re-validated each synthesis). Double blind ACCEPT (codex + independent Fable reviewer) before promotion; their 3 text corrections applied first.
- **v67** (`53747c7`): **the moonshot sleeve is x10-first** (Ariel: "it should be the x10 sleeve, not maybe-x2"). `X10_SLEEVE_MANDATE` hard-encoded at every grading/filling surface; fleet re-sourced live (author + blind reviewer, divergences reconciled in code): **RXRX 18 / ACHR 16 / RGTI 14 / OKLO 13 / TEM 12 / IONQ 10 / ASTS 9 / INVZ 8**; exits NU/MELI/CRWD **and RKLB** ("re-rated its own asymmetry away") — held tiny fills migrate on rebalance, never force-sold.

## 4. Live execution + the funding incident (2026-07-06)

Ariel executed the v66/v67 deploy at Leumi: 11 fills, **$161,376.10** (CSPX 45sh, EXUS 800, IWQU 225, EIMI 250, FUSA 800, SPMV 125, IBTA 2000, RXRX 1500, OKLO 100, TEM 80 — moonshots asymmetry-first per v67; ASTS skipped by Ariel, prefers his existing SPCX/SpaceX). **Incident:** the deployable $171k spanned Leumi USD + Leumi NIS + Schwab, but all fills hit Leumi USD → −$16.4k. Ariel refused FX conversion (too slow) → **sold 200 SGOV @ ~$100.45** (near-zero tax, first tranche of the standing US-situs SGOV exit) → Leumi USD +$3.7k. Book: snapshots **9** (self-refresh) → **10** (fills-applied) → **11** (SGOV sale), conservation verified; closed-loop expectations armed (parse_warnings + marker row `action_proposals:44`) so the **next real ingest must reconcile** (8 new positions, CSPX 240sh/EIMI 650sh, SGOV@Leumi 850sh, SGOV fill price is a live-quote ESTIMATE pending the broker print). Flags 38/41/43 (CSPX/FUSA/IWQU) closed accepted-by-execution; 39/40/42 superseded.

## 5. Open items (next session)

1. **Bank DPYA reply** → IWDP→DPYA share-class swap + the $5k property top-up (message sent; if Leumi can't open DPYA, top up IWDP and record it as the sleeve's de-facto instrument).
2. **Residual cash ~$9.6k** (Schwab $5.9k + Leumi ~$3.7k) + incoming glide cash → next tranche, EXUS-first (biggest gap: 2.0% vs 14.3%).
3. **UI**: render `DeploymentPlanDTO.funding.required_actions` ABOVE the buy list (`ui/src/lib/api.ts` ~707, `ui/src/app/inbox/page.tsx`); authored buys also lack sleeve/rationale display (rationale came back empty in the DTO — check `authored_outcome_to_dto`).
4. **Queued fleet/plan work**: SGOV migration completion (850sh Leumi + Schwab lot → IB01/IBTA); the consolidation batch (XZEW/VOO/SPMO/QQQM/SCHG → CSPX/CNDX, gains offset vs META/RKT losses); cap-weight-vs-equal-weight US-core adjudication; AI-correlation as a moonshot-sourcing input.
5. **Fleet observations from verify-run** (team fixes, not gates): debate agents hit the tolerant-JSON recovery ~100% (output-schema drift in debate prompts); the hallucinated-sources detector compares source_ids, not content URLs.
6. **CELH pending_reevaluation** auto-retry should now yield a real verdict (anchors + WebSearch live). ACN=hold, ELF=do-not-initiate, CELH=insufficient-data (pre-fix).
7. `test_deploy_cash_route.py::test_deploy_cash_returns_tiered_plan` fails ONLY in the main env (dev-DB/quote-dependent tiers collapse) — passes in clean worktrees; needs an env-independence fix, not a code fix.
8. Full suite run; Israeli feed-less funds (TA-200/MSCI-World-MTF/IBI-STOXX) carry-only in self-refresh.

## 6. Discipline that worked (keep it)

Blind re-derivation at every level (reviewers get RAW facts incl. situs/domicile — never the author's claims: the MELI catch); **model over argument** for money judgments (gold, NVDA buffer); telemetry first when debugging (the max_turns root cause came from reproducing ONE call in isolation and reading stdout); verify-run after every live run; subagent-driven to save context (worktree isolation when two streams share base.py); **the team is the architecture** — every failure this session was fixed as inputs/reliability/plumbing, zero new judgment gates.
