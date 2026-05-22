# Wave A — `BaseAgent` API features upgrade

**Spec date:** 2026-05-22
**Status:** Approved for implementation
**Author:** Ariel + Claude (brainstorm)
**Plan to follow:** `docs/superpowers/plans/2026-05-22-baseagent-api-features-implementation.md` (TBD after spec review)
**Wave B (follow-on, separate spec):** Daily news cascade + codex live integration + cascade-aware Agent Activity UI

## 1. Goal

Adopt three Anthropic Messages API features in `BaseAgent` to (a) cut token cost ~30-50% on multi-agent decision flows via **prompt caching**, (b) replace Argosy's hand-rolled cite-every-claim discipline with native verifiable source attribution via the **Citations API**, and (c) lift reasoning quality on high-stakes Opus agents via **extended thinking**.

This is the foundation Wave B (daily news cascade with codex fact-checking) depends on — the codex fact-checker needs verifiable Citations spans, and the daily cascade needs caching to be cost-affordable.

## 2. Scope

### In scope

- Refactor `argosy/agents/base.py::_do_call` (around line 609) to:
  - Pass `system` as a list of content blocks, with `cache_control: {"type": "ephemeral"}` on shared boilerplate
  - Conditionally pass `thinking={"type": "enabled", "budget_tokens": N}` when `self.thinking_budget > 0`
  - Conditionally enable Citations on source-consuming agents (pass source docs as `document` content blocks with `citations: {"enabled": true}`)
- Per-role configuration tables: `DEFAULT_THINKING_BUDGET_BY_ROLE`, `DEFAULT_CITATIONS_BY_ROLE`
- Per-user override in `configs/<user_id>/agent_settings.yaml` (extends the existing per-role override pattern in SDD §A.2)
- Alembic migration `0026_agent_reports_api_telemetry` adding four columns to `agent_reports`: `cache_input_tokens`, `cache_creation_tokens`, `thinking_tokens`, `citations_json`
- Parse and persist new response fields from `messages.create` into `AgentReport`
- Cost calculation in `_estimate_usd` (around `BaseAgent`) accounts for cache-write vs cache-read tokens (cache writes are 1.25×, reads 0.1×)

### Out of scope (deferred)

- **Batch API** — relevant for `HouseholdCategorizerAgent` backfill and `DomainRefreshAgent` overnight refresh, not cascade. Separate later wave.
- **MCP server export** — exposing Argosy via MCP is a separate use case. Later wave.
- **Claude Managed Agents** (dreaming, multiagent orchestration) — would replace `argosy/orchestrator/flows/*`, too big to bundle. Evaluate after Wave B ships.
- **How citations flow between cascading agents** — e.g. when bull-researcher cites a source the bear-researcher should also know about. This is Wave B's job (the cascade redesign records cross-phase citation propagation). Wave A just makes the citations *available* per call.
- **Codex integration at runtime** — entirely Wave B.

## 3. Components

### 3.1 `BaseAgent._do_call` refactor (`argosy/agents/base.py:609-636`)

Current shape:
```python
msg = client.messages.create(
    model=self.model,
    system=system,           # plain string
    max_tokens=self.max_tokens,
    messages=messages_payload,
)
```

New shape:
```python
system_blocks = self._build_system_blocks(system)  # see 3.2
call_kwargs = {
    "model": self.model,
    "system": system_blocks,
    "max_tokens": self.max_tokens,
    "messages": messages_payload,
}
if self.thinking_budget > 0:
    call_kwargs["thinking"] = {"type": "enabled", "budget_tokens": self.thinking_budget}
msg = client.messages.create(**call_kwargs)
```

Citations enablement attaches at the `messages_payload` level — when `self.citations_enabled`, source documents the agent loaded (domain_knowledge files, plan markdown, news payloads, etc.) are passed as `document` content blocks with `citations: {"enabled": true}` instead of being inlined into the user message text. Today the `news-as-data` wrapping in `<news>...</news>` tags is replaced by proper document blocks.

### 3.2 `_build_system_blocks(system: str)` helper

Splits the system prompt at the boundary between the shared boilerplate (cite-every-claim, news-as-data, confidence-band — the part produced today by `BaseAgent`'s prompt-construction logic, exact method name to be confirmed during implementation) and the role-specific instructions (produced by each agent's `build_prompt`).

Returns:
```python
[
    {"type": "text", "text": <shared boilerplate>, "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": <role-specific>},
]
```

**Caching is default-on for all roles** (per design decision). Roles with very short prompts pay a trivial cache-write overhead (~$0.001) and gain nothing — the cost is negligible vs. the operational simplicity of "no per-role flag."

### 3.3 Configuration tables (new constants in `base.py`)

```python
DEFAULT_THINKING_BUDGET_BY_ROLE: dict[str, int] = {
    "bull_researcher":  4000,
    "bear_researcher":  4000,
    "trader":           8000,
    "fund_manager":     8000,
    "plan_synthesizer": 8000,
    "audit":            4000,
    # all other roles default to 0 (no extended thinking)
}

DEFAULT_CITATIONS_BY_ROLE: dict[str, bool] = {
    # External-source consumers
    "news_analyst": True, "fundamentals": True, "technical": True,
    "sentiment": True, "macro": True, "tax": True, "fx": True,
    "intake_extractor": True, "plan_distiller": True, "plan_critique": True,
    "concentration": True,
    # Synthesizers (attribute back to inputs)
    "bull_researcher": True, "bear_researcher": True,
    "trader": True, "fund_manager": True, "audit": True,
    "plan_synthesizer": True,
    # No-citation agents
    "advisor": False, "intake": False, "household_categorizer": False,
    "researcher_facilitator": False, "risk_facilitator": False,
    "domain_refresh": False, "watchlist": False,
}
```

These are merged with the per-user override in `configs/<user_id>/agent_settings.yaml`:
```yaml
agents:
  bear_researcher:
    thinking_budget: 6000        # override default 4000
    citations_enabled: true       # override default true (explicit)
```

### 3.4 Migration `0026_agent_reports_api_telemetry`

```sql
ALTER TABLE agent_reports
  ADD COLUMN cache_input_tokens    INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN thinking_tokens       INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN citations_json        TEXT;
```

- `cache_input_tokens` — tokens read from cache (cheap, ~10% of input price).
- `cache_creation_tokens` — tokens written to cache (1.25× input price, one-time per cache).
- `thinking_tokens` — extended-thinking tokens (count as output tokens for billing).
- `citations_json` — JSON array; one entry per citation. Shape:
  ```json
  [
    {
      "source_id": "domain_knowledge/tax/israel/capital_gains.md",
      "source_span_start": 1240,
      "source_span_end": 1389,
      "claim_text": "25% CGT on long-term capital gains for IL residents",
      "cited_quote": "The capital gains tax rate for individuals is 25%..."
    }
  ]
  ```
  `NULL` when `citations_enabled=false` or no citations were emitted.

### 3.5 `_estimate_usd` cost calculation update

The existing function multiplies `tokens_in * price_in_per_M + tokens_out * price_out_per_M`. New formula:

```python
base_input = tokens_in - cache_input_tokens - cache_creation_tokens
cost_input = (
    base_input            * price_in
    + cache_input_tokens   * price_in * 0.10   # cache reads
    + cache_creation_tokens * price_in * 1.25  # cache writes
)
cost_thinking = thinking_tokens * price_out
cost_output = tokens_out * price_out
total = (cost_input + cost_thinking + cost_output) / 1_000_000
```

`tokens_in` from the SDK already includes cached + uncached. Subtract to get `base_input`. Verified against the Anthropic pricing page semantics.

## 4. Data flow

```
agent.run(input)
  └─ build_prompt(input)
       ├─ shared boilerplate (cite-rules, news-as-data, confidence-band) ──┐
       │                                                                    │ cache_control:
       │                                                                    │   ephemeral
       └─ role-specific instructions ───────────────────────────────────────┘
                                                                            ▼
  └─ _do_call(system, messages):
        system_blocks = _build_system_blocks(system)        # 2-element list
        if citations_enabled:
            messages_payload = inject source docs as document blocks
        if thinking_budget > 0:
            call_kwargs["thinking"] = {...}
        msg = client.messages.create(**call_kwargs)
  └─ parse usage:
        cache_creation_input_tokens, cache_read_input_tokens   → AgentReport.cache_*
        thinking content blocks                                 → AgentReport.thinking_tokens
        citation content blocks                                 → AgentReport.citations_json
  └─ _estimate_usd(...)                                         → AgentReport.cost_usd
  └─ persist to agent_reports
```

## 5. Error handling

| Scenario | Behavior |
|---|---|
| Model doesn't support extended thinking (older Sonnet, Haiku) | Catch SDK error, retry once without `thinking` param, log warning, set `thinking_tokens=0`. Don't fail the agent run. |
| Citations response parsing fails (malformed citation block) | Log warning with the raw block JSON, persist `citations_json=NULL`. Don't fail the agent run. |
| Cache write fails or cache TTL expires mid-call | No-op — SDK transparently falls back to non-cached. `cache_creation_tokens=0`, `cache_input_tokens=0`. |
| `agent_settings.yaml` override is invalid (e.g. `thinking_budget=-100`) | Pydantic validation on load surfaces the error at startup, not at agent-call time. |

## 6. Testing strategy

### Unit tests (`tests/test_base_agent_api_features.py`)

For each combination of `(caching ∈ {hit, miss, mixed}, thinking ∈ {on, off}, citations ∈ {on, off})`:
- Build a mocked SDK `Message` response with the appropriate `usage.cache_*`, `content` blocks for thinking + citations.
- Assert `AgentReport` columns populated correctly.
- Assert `_estimate_usd` matches manual calculation.

8 combinations × 1 sub-case = 8 tests. Plus failure-mode tests (thinking not supported, citations parse fail). ~12 unit tests total.

### Integration tests (`tests/test_base_agent_api_features_live.py`, `@pytest.mark.llm_eval`)

One live test per role family using cheapest viable model:
- `analyst` family → use `news_analyst` against a sample news payload, verify Citations come back populated
- `researcher` family → use `bull_researcher` on a fixture decision, verify `thinking_tokens > 0`
- `trader` family → use `trader` on a fixture researcher debate, verify thinking + citations both populate
- `audit` family → verify thinking populates

4 live tests. Opt-in via `pytest -m llm_eval`.

### Cost-regression smoke test (`tests/test_decision_flow_cost_regression.py`)

Replay a fixture decision (e.g. NVDA T2 trade-flow scenario already in test fixtures) through `DecisionFlow` end-to-end. Assert:
- Total cost ≤ 70% of pre-upgrade baseline (recorded as `tests/fixtures/cost_baseline_pre_wave_a.json`)
- All 5 cascade phases complete (no regression in flow logic)

## 7. Telemetry & observability

The home-page `/api/agent-activity` endpoint returns the same shape as today plus four new fields per row: `cache_input_tokens`, `cache_creation_tokens`, `thinking_tokens`, `citations_count` (the length of `citations_json` if present, else 0). UI changes are minimal — the existing token/cost columns just become more accurate; an optional "cache hit %" column can be added later.

`/internal/health/full` exposes aggregate stats: cache hit ratio over last 24h, average thinking tokens per role, citation coverage percentage. Useful for tuning the per-role config.

## 8. Rollout plan

1. Land migration 0026 + new columns + `_estimate_usd` update + null defaults — no behavioral change yet. Tests pass.
2. Wire `cache_control` in `_do_call`. Run cost-regression smoke; expect 30-50% input-token reduction.
3. Wire `thinking` for the 6 high-stakes roles. Run integration tests.
4. Wire Citations for the 17 citation-enabled roles. Run integration tests.
5. Update `agent_settings.yaml` per-user override schema + validation.
6. SDD update: refresh §3 (agent fleet table — add "thinking budget" + "citations" columns), §8.5 (migration history adds 0026), and the confidence-band section (note that Citations now provides verifiable attribution, making the hand-rolled `cited_sources` field redundant for citation-enabled roles). Verify section numbers when editing — SDD has been refactored multiple times.

Each step lands as a separate commit on `main`. No feature flag — Citations + caching + thinking are all backward-compatible with existing AgentReport readers.

## 9. Success criteria

- All existing tests pass (1,020+ under `pytest -m "not llm_eval"`).
- Cost-regression smoke shows ≥30% total token cost reduction on the fixture decision.
- Live integration tests show Citations populated for at least one source-consuming agent with span offsets that match the source file.
- `agent_settings.yaml` per-user override successfully changes a single role's `thinking_budget` without affecting other roles.
- `/api/agent-activity` returns the new fields without breaking existing UI rendering.

## 10. Open questions deferred to implementation

- Should the SDK use the **Anthropic Python SDK** directly (current) or the **Claude API beta features SDK**? Need to check if Citations is GA or still beta in the current SDK version pinned in `pyproject.toml`. **Action:** Implementation plan Task 1 verifies SDK version + Citations availability before any code changes.
- Cache TTL is 5 minutes by default; longer TTLs (1h) are available at higher write cost. Stick with 5-min default for Wave A; revisit if cascade flows take longer than 5 min wall-clock (they don't today — full decision is ~90s).
- For roles that consume **multiple** source files (e.g. `bear_researcher` reads bull's prior output + domain_knowledge + news), all sources become separate `document` blocks. The implementation needs a stable convention for `source_id` — likely the relative path from `ARGOSY_HOME` for files, or a stable hash for ephemeral payloads like news.
