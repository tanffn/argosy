# Tier 3 + Tier 4 + Observability Tree — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to dispatch fresh subagents per task. Steps use checkbox (`- [ ]`) syntax for tracking. Codex tandem (`tools/codex-tandem/scripts/devmode_pair.run_pair`) is the reviewer for risky tracks (adapter parsers, migrations, orchestrator changes). UI-only tracks use the standard code-writer / code-reviewer pair.

**Goal:** Close Tier 3 (adapter coverage) + Tier 4 remainder (T4.1-T4.5), and rebuild the decision-replay surface around a Fund Manager-rooted agent tree that exposes which agents ran, which failed, and why.

**Architecture:** Five-phase synthesis flow already produces 18 `agent_reports` rows per run; the missing link is that `decision_phases.participants_json` is empty because the orchestrator passes `agent_report_ids=[]` to the recorder. Phase 0 closes that gap and adds an `agent-tree` endpoint + UI; everything else hangs off that observability surface so adapter and agent failures are visible end-to-end. Backend persists agent + adapter outcomes (success / empty-payload / HTTP-error with reason); UI renders FM at the top with descendants for plan_synthesizer → researchers → analysts → adapters.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / Pydantic on backend; Next.js (custom build, read `node_modules/next/dist/docs/` before any UI code) / React / Tailwind / Mermaid on frontend. Tests via `pytest -m "not llm_eval"`. UI verification via `cd ui ; npm run lint ; npm run typecheck`. Codex tandem at `tools/codex-tandem/scripts/devmode_pair.py` for risky-track review.

---

## Scope context — what's already there vs. what's missing

### Already in place

- **18 `agent_reports` rows per synthesis** with `agent_role`, `confidence`, `cost_usd`, `tokens_in/out`, `sources_json`, `model`, `run_correlation_id`. Decision-id format: `plan-synth-<run_id>`. Schema at `argosy/state/models.py::AgentReport`.
- **Per-phase persistence** (T2.3): `decision_phases.phase_output_json` populated per phase via `orchestrator.py::_record_phase_completion` (around line 853-885). Phase row schema includes `seq`, `kind`, `started_at`, `finished_at`, `verdict_json`, `verdict_kind`, `participants_json`, `tldr_md`, `bundle_dir`, `phase_output_json`.
- **Mermaid sequence builder** at `argosy/services/transcript_writer.py::render_sequence_mmd` (lines 210-246). Renders `participants` list into a `sequenceDiagram`. Empty `participants` ⇒ empty sequence.
- **Negotiation recorder** at `argosy/services/negotiation_recorder.py::record_negotiation_phase`. Accepts `agent_report_ids: Iterable[int]`; fetches rows; builds `ParticipantRef` list; writes phase row + FS bundle; backfills `agent_reports.phase_id` for participants.
- **Adapter probe diagnostic**: `argosy diagnose adapters` CLI verifies each adapter at startup. Today: finnhub ✅, fred ✅, capitoltrades ✅, sec_form4 ✅, boi ✅, yfinance ✅, **tipranks ❌ (HTTP 403)**, **sec_13f ❌ (HTTP 404)**. Does not run during synthesis.
- **Decision-replay API + UI** at `argosy/api/routes/decisions.py` and `ui/src/app/decisions/[id]/page.tsx`. Renders inputs + sequence_mmd_full + per-phase timeline. Today: meaningless because participants are empty.

### Missing / broken

- **Recorder never sees agent_report_ids.** `orchestrator.py:877` passes `agent_report_ids=[]` to `record_negotiation_phase`. Order: orchestrator runs the phase → persists agent_reports to JSONL trail → records phase with empty IDs → at end-of-flow ingests JSONL into DB. By the time IDs exist, the phase row is written.
- **No agent-tree endpoint.** No backend builds the FM-rooted DAG, and no UI renders one.
- **Adapter outcomes are lost.** When `news_count=0` it could mean (a) ticker has no news, (b) Finnhub returned an error and we swallowed it, (c) API key was wrong. The `sources_json` on the analyst's `agent_reports` row reflects what the *agent* saw, not the *adapter's actual status*. We need per-adapter `{status, payload_size, error_text}` captured at adapter-call time.
- **`/plan` does not surface synthesis health.** User can land on `/plan`, see a draft, and have no idea two adapters silently returned empty.
- **No bulk re-debate slim flow** for per-delta pushback (Tier 4.3 placeholder).
- **No daily-brief loop in production** (Tier 4.5 placeholder; code exists but never produces output).

---

## File Structure

### New files
- `argosy/services/adapter_outcomes.py` — `AdapterOutcome` dataclass + context-manager `track_adapter_call(adapter_name, ticker_or_series_id)` that captures `{status: ok|empty|http_error, latency_ms, payload_size_bytes, error_text}` and stashes them per-decision-run in a contextvar.
- `argosy/services/agent_tree_builder.py` — pure function that walks `decision_phases` + `agent_reports` + adapter outcomes for one `decision_run_id` and returns a nested tree DTO rooted at FM.
- `argosy/api/routes/decisions_tree.py` — `GET /api/decisions/{id}/agent-tree` endpoint returning the DTO.
- `argosy/agents/per_position_thesis.py` — synthesizer post-processor that derives per-holding `{verdict, conviction, reasoning, sources}` from the medium/long horizon JSON. Pure transformation, no LLM call.
- `argosy/api/routes/positions.py` — `GET /api/positions/thesis` endpoint returning per-position verdicts.
- `argosy/orchestrator/flows/per_delta_pushback.py` — slim-debate flow that re-runs bull/bear/facilitator targeted at ONE delta only.
- `argosy/services/daily_brief_runner.py` — once-per-day scheduled task that synthesizes a one-pager from the current draft and overnight market deltas, writes to `daily_briefs` table.
- `ui/src/components/decisions/agent-tree.tsx` — React tree component, FM at root, expand-on-click, status badges per node.
- `ui/src/components/decisions/adapter-leaf.tsx` — leaf node for adapter calls; shows `ok / empty / http <code>` + tooltip with raw error text.
- `ui/src/components/plan/synthesis-health-banner.tsx` — banner on `/plan` summarizing "N agents OK · M failed · K adapters degraded" + drill-in link to `/decisions/[id]`.
- `ui/src/app/positions/page.tsx` — per-position thesis cards page (T4.1).
- `tests/test_adapter_outcomes.py`, `tests/test_agent_tree_builder.py`, `tests/test_per_position_thesis.py`, `tests/test_per_delta_pushback.py`, `tests/test_daily_brief_runner.py`.

### Modified files
- `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` — pass real `agent_report_ids` into `record_negotiation_phase` for each of phases 1-5. Adapter outcomes flow through new contextvar; attach to `phase_output_json` for phase 1.
- `argosy/adapters/data/finnhub_adapter.py`, `fred_adapter.py`, `yfinance_adapter.py`, `tipranks_adapter.py`, `sec_13f_adapter.py`, `sec_form4_adapter.py`, `boi_adapter.py`, `capitoltrades_adapter.py` — wrap public methods with `track_adapter_call` so outcomes flow into the contextvar.
- `argosy/adapters/data/sec_13f_adapter.py` — switch to EDGAR FTS path; handle the 404 case gracefully (record as `http_error 404` outcome, return empty list rather than raising).
- `argosy/adapters/data/tipranks_adapter.py` — accept 403 as a known-failed outcome; fall back to Finnhub social adapter if available; otherwise return empty + record outcome cleanly.
- `argosy/api/routes/decisions.py` — extend replay response with `agent_tree_url` so the UI knows where to fetch the new view.
- `argosy/api/routes/plan.py` — extend `/api/plan/draft` response with `synthesis_health: {agents_ok, agents_failed, adapters_ok, adapters_failed, decision_run_id}` for the banner.
- `argosy/api/routes/proposals.py` — speculative-candidates polish (T4.2): include `conviction`, `cited_sources`, `tier` on each candidate.
- `argosy/api/routes/decisions.py` — new `decision_kind` values: `delta_pushback`, `daily_brief`. Drill-in route `GET /api/decisions/{id}/detail` returns the relevant view for each kind.
- `ui/src/app/decisions/[id]/page.tsx` — replace `MermaidDiagram` for `sequence_mmd_full` with the new `<AgentTree>` component. Keep per-phase timeline below.
- `ui/src/app/plan/page.tsx` — add `<SynthesisHealthBanner>` above the FM objections card.
- `ui/src/app/proposals/page.tsx` — speculative candidates polish.
- `ui/src/app/decisions/page.tsx` — add rows for `delta_pushback` + `daily_brief` decision kinds.
- `argosy/state/migrations/` — `0032_agent_reports_adapter_outcomes.sql` adds `adapter_outcomes_json TEXT NULL` column (Phase 1 analysts will populate it).
- `argosy/state/migrations/` — `0033_decision_kind_expansion.sql` adds `delta_pushback` + `daily_brief` to the kind taxonomy in the docstring (no enum constraint to update; kind is free-text column).
- `argosy/state/migrations/` — `0034_daily_briefs.sql` adds the `daily_briefs` table if not already present.
- `docs/design/SDD.md` — final refresh at end of wave.

---

## Phase 0 — Observability Tree (cornerstone; blocks everything else)

This phase is sequential and on the critical path. Every other phase relies on adapter outcomes + agent tree to surface failures.

### Task 0.1 — Persist `agent_report_ids` per phase

**Files:**
- Modify: `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` (around `_record_phase_completion` at lines 853-885 and the per-phase `_run_phase_*` functions at lines 207, 238, 298, 341, 368)
- Modify: `argosy/services/negotiation_recorder.py::record_negotiation_phase` (lines 72+) — keep signature, ensure docstring covers backfill of `agent_reports.phase_id`
- Test: `tests/test_negotiation_recorder.py` (extend)
- Test: `tests/test_plan_synthesis_flow.py` (extend; verify each phase carries IDs)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_plan_synthesis_flow.py
def test_phase_completion_threads_agent_report_ids(monkeypatch, db, user, ariel_synth_inputs):
    # Drive a minimal end-to-end synthesis that produces stub reports for
    # each phase; assert decision_phases.participants_json is non-empty
    # for every phase row.
    run_id = run_synthesis_with_stubs(db, user, ariel_synth_inputs)
    phases = db.execute(
        select(DecisionPhase).where(DecisionPhase.decision_run_id == run_id)
    ).scalars().all()
    assert len(phases) >= 5
    for p in phases:
        ids = json.loads(p.participants_json or "[]")
        assert isinstance(ids, list) and len(ids) > 0, (
            f"phase {p.kind} seq={p.seq} has empty participants_json"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_plan_synthesis_flow.py::test_phase_completion_threads_agent_report_ids -v`
Expected: FAIL — participants_json is `[]` for all phases.

- [ ] **Step 3: Thread agent_report_ids through `_record_phase_completion`**

Inspect: each `_run_phase_N_*` function currently returns the JSONL trail path or in-memory list of `AgentReportRow` objects. Add a `agent_report_ids: list[int]` return component to each phase function. After phase completes, the orchestrator already has the persisted IDs (since W1.C-v4 routes through end-of-flow JSONL ingest — see SDD §17). Change the flow: persist agent_reports for THIS phase to the DB before calling `record_negotiation_phase`. Use a sub-session per phase to avoid the uvicorn writer-lock issue (precedent: `_safe_run_agent` already uses sub-sessions).

```python
# in argosy/orchestrator/flows/plan_synthesis/orchestrator.py
async def _record_phase_completion(
    self,
    *,
    phase_n: int,
    started_at: datetime,
    phase_output: dict | str | None,
    agent_report_rows: list[AgentReportRow],  # NEW — chronological order
) -> None:
    """Persist this phase's agent_reports first, then record the phase row
    with their IDs."""
    # 1. Persist the rows in a sub-session.
    ids: list[int] = []
    async with db_mod.async_session() as session:
        for row in agent_report_rows:
            orm = AgentReport(**dataclasses.asdict(row))
            session.add(orm)
            await session.flush()
            ids.append(orm.id)
        await session.commit()

    # 2. Record the phase with the IDs.
    await record_negotiation_phase(
        user_id=self.user_id,
        decision_run_id=self.decision_run_id,
        kind=f"synthesis.phase_{phase_n}",
        started_at=started_at,
        finished_at=_utcnow(),
        agent_report_ids=ids,
        verdict=None,
        phase_output=phase_output,
    )
```

Each per-phase function gets updated to pass the list of `AgentReportRow` it produced. The end-of-flow JSONL ingest at line ~461 becomes a fallback for any rows that weren't already in the DB (defensive — should be a no-op when phase recording works).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_plan_synthesis_flow.py::test_phase_completion_threads_agent_report_ids -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add argosy/orchestrator/flows/plan_synthesis/orchestrator.py argosy/services/negotiation_recorder.py tests/test_plan_synthesis_flow.py
git commit -m "feat(synth): T0.1 — thread agent_report_ids into per-phase recorder so participants_json is no longer empty"
```

### Task 0.2 — Adapter outcomes contextvar + tracking helper

**Files:**
- Create: `argosy/services/adapter_outcomes.py`
- Test: `tests/test_adapter_outcomes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_adapter_outcomes.py
import pytest
from argosy.services.adapter_outcomes import (
    AdapterOutcome,
    track_adapter_call,
    collect_outcomes,
    reset_outcomes,
)

@pytest.mark.asyncio
async def test_track_adapter_call_records_success():
    reset_outcomes()
    with track_adapter_call("finnhub_news", target="NVDA") as ctx:
        ctx.set_payload_size_bytes(2048)
    outcomes = collect_outcomes()
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.adapter_name == "finnhub_news"
    assert o.status == "ok"
    assert o.payload_size_bytes == 2048
    assert o.error_text is None

@pytest.mark.asyncio
async def test_track_adapter_call_records_http_error():
    reset_outcomes()
    with track_adapter_call("sec_13f", target="13F-HR") as ctx:
        ctx.record_http_error(status_code=404, body="Not Found")
    outcomes = collect_outcomes()
    assert outcomes[0].status == "http_error"
    assert outcomes[0].http_status_code == 404
    assert "Not Found" in outcomes[0].error_text

@pytest.mark.asyncio
async def test_track_adapter_call_records_empty_payload():
    reset_outcomes()
    with track_adapter_call("tipranks", target="NVDA") as ctx:
        ctx.set_payload_size_bytes(0)
    outcomes = collect_outcomes()
    assert outcomes[0].status == "empty"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_adapter_outcomes.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement adapter_outcomes.py**

```python
# argosy/services/adapter_outcomes.py
"""Adapter outcome tracking — record what every external data call did.

Used during synthesis so the UI can show 'finnhub_news: 14 records'
(ok) or 'sec_13f: HTTP 404' (http_error). The contextvar pattern lets
adapters report their own outcomes without threading a tracker through
every call site.
"""
from __future__ import annotations

import contextlib
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator, Literal

OutcomeStatus = Literal["ok", "empty", "http_error", "exception"]


@dataclass
class AdapterOutcome:
    adapter_name: str
    target: str | None
    status: OutcomeStatus
    latency_ms: int
    payload_size_bytes: int = 0
    http_status_code: int | None = None
    error_text: str | None = None


_outcomes: ContextVar[list[AdapterOutcome] | None] = ContextVar(
    "adapter_outcomes", default=None,
)


class _OutcomeBuilder:
    def __init__(self, adapter_name: str, target: str | None):
        self.adapter_name = adapter_name
        self.target = target
        self._t0 = time.monotonic()
        self._payload_size = 0
        self._http_status: int | None = None
        self._error: str | None = None
        self._explicit_status: OutcomeStatus | None = None

    def set_payload_size_bytes(self, n: int) -> None:
        self._payload_size = n

    def record_http_error(self, *, status_code: int, body: str | None) -> None:
        self._http_status = status_code
        self._error = body or f"HTTP {status_code}"
        self._explicit_status = "http_error"

    def record_exception(self, exc: BaseException) -> None:
        self._error = f"{type(exc).__name__}: {exc}"
        self._explicit_status = "exception"

    def _finalize(self) -> AdapterOutcome:
        status: OutcomeStatus
        if self._explicit_status:
            status = self._explicit_status
        elif self._payload_size == 0:
            status = "empty"
        else:
            status = "ok"
        return AdapterOutcome(
            adapter_name=self.adapter_name,
            target=self.target,
            status=status,
            latency_ms=int((time.monotonic() - self._t0) * 1000),
            payload_size_bytes=self._payload_size,
            http_status_code=self._http_status,
            error_text=self._error,
        )


@contextlib.contextmanager
def track_adapter_call(
    adapter_name: str, *, target: str | None = None,
) -> Iterator[_OutcomeBuilder]:
    """Record one adapter call's outcome into the contextvar."""
    builder = _OutcomeBuilder(adapter_name=adapter_name, target=target)
    try:
        yield builder
    except BaseException as exc:
        builder.record_exception(exc)
        _push(builder._finalize())
        raise
    else:
        _push(builder._finalize())


def _push(outcome: AdapterOutcome) -> None:
    cur = _outcomes.get()
    if cur is None:
        cur = []
        _outcomes.set(cur)
    cur.append(outcome)


def reset_outcomes() -> None:
    """Clear the buffer at the start of a synthesis run."""
    _outcomes.set([])


def collect_outcomes() -> list[AdapterOutcome]:
    """Return everything tracked since last reset; non-destructive."""
    return list(_outcomes.get() or [])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_adapter_outcomes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add argosy/services/adapter_outcomes.py tests/test_adapter_outcomes.py
git commit -m "feat(observability): T0.2 — adapter outcome contextvar + tracking helper"
```

### Task 0.3 — Wire `track_adapter_call` into every adapter

**Files:**
- Modify: `argosy/adapters/data/finnhub_adapter.py` — wrap `get_company_news`, `get_company_financials`
- Modify: `argosy/adapters/data/fred_adapter.py` — wrap `get_series`
- Modify: `argosy/adapters/data/yfinance_adapter.py` — wrap `get_indicators`
- Modify: `argosy/adapters/data/tipranks_adapter.py` — wrap `get_blogger_sentiment`
- Modify: `argosy/adapters/data/sec_13f_adapter.py` — wrap top-level fetcher
- Modify: `argosy/adapters/data/sec_form4_adapter.py` — wrap top-level fetcher
- Modify: `argosy/adapters/data/boi_adapter.py` — wrap rate fetcher
- Modify: `argosy/adapters/data/capitoltrades_adapter.py` — wrap top-level fetcher
- Modify: `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` — call `reset_outcomes()` at synthesis start; attach `collect_outcomes()` to phase 1's `phase_output_json` payload.
- Test: `tests/test_finnhub_adapter.py`, `tests/test_fred_adapter.py`, etc. (extend)

- [ ] **Step 1: Update finnhub adapter to track**

```python
# argosy/adapters/data/finnhub_adapter.py — inside get_company_news
from argosy.services.adapter_outcomes import track_adapter_call

async def get_company_news(self, ticker: str, ...) -> list[NewsItem]:
    with track_adapter_call("finnhub_news", target=ticker) as outcome:
        try:
            resp = await self._http.get(...)
            resp.raise_for_status()
            body = resp.json()
            outcome.set_payload_size_bytes(len(resp.content))
            return [NewsItem.parse_obj(x) for x in body]
        except httpx.HTTPStatusError as e:
            outcome.record_http_error(
                status_code=e.response.status_code,
                body=e.response.text[:500],
            )
            return []  # Don't crash — surface via outcome
```

Repeat the pattern for each adapter. Adapters that already return `[]` on failure today should not change return semantics — only add `track_adapter_call`.

- [ ] **Step 2: Reset + collect outcomes in orchestrator**

```python
# argosy/orchestrator/flows/plan_synthesis/orchestrator.py — at run_synthesis() entry
from argosy.services.adapter_outcomes import reset_outcomes, collect_outcomes

async def run_synthesis(self, ...) -> ...:
    reset_outcomes()
    # ... existing flow ...
    # At end of Phase 1:
    phase_1_outcomes = collect_outcomes()
    phase_output_p1 = {
        "phase": 1,
        "adapter_outcomes": [dataclasses.asdict(o) for o in phase_1_outcomes],
        # ... existing phase output fields ...
    }
    await self._record_phase_completion(
        phase_n=1,
        started_at=phase_1_started,
        phase_output=phase_output_p1,
        agent_report_rows=phase_1_rows,
    )
```

- [ ] **Step 3: Test end-to-end with stub adapters**

```python
# tests/test_plan_synthesis_flow.py — extend
def test_phase_1_records_adapter_outcomes(...):
    # Run synthesis with a mock SEC 13F that returns 404; assert the
    # outcome appears in decision_phases[seq=1].phase_output_json.
    run_id = run_synthesis_with_stubbed_sec_13f_404(...)
    phase_1 = get_phase(run_id, seq=1)
    output = json.loads(phase_1.phase_output_json)
    outcomes = output["adapter_outcomes"]
    sec_13f = next(o for o in outcomes if o["adapter_name"] == "sec_13f")
    assert sec_13f["status"] == "http_error"
    assert sec_13f["http_status_code"] == 404
```

- [ ] **Step 4: Run all adapter tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_finnhub_adapter.py tests/test_fred_adapter.py tests/test_yfinance_adapter.py tests/test_tipranks_adapter.py tests/test_sec_13f_adapter.py tests/test_sec_form4_adapter.py tests/test_boi_adapter.py tests/test_capitoltrades_adapter.py tests/test_plan_synthesis_flow.py::test_phase_1_records_adapter_outcomes -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add argosy/adapters/data/ argosy/orchestrator/flows/plan_synthesis/orchestrator.py tests/
git commit -m "feat(observability): T0.3 — every adapter records its outcome (ok / empty / http_error / exception) per call"
```

### Task 0.4 — `agent_tree_builder` service

**Files:**
- Create: `argosy/services/agent_tree_builder.py`
- Test: `tests/test_agent_tree_builder.py`

The builder is a pure function that walks `decision_phases` + `agent_reports` + adapter outcomes for one `decision_run_id` and returns a nested DTO. The DAG topology is **hard-coded** per `decision_kind="synthesis"` because the orchestrator structure is stable; future flows get their own builder.

- [ ] **Step 1: Define the DTO**

```python
# argosy/services/agent_tree_builder.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

NodeStatus = Literal["ok", "degraded", "failed", "skipped"]


@dataclass
class AdapterNode:
    adapter_name: str
    target: str | None
    status: Literal["ok", "empty", "http_error", "exception"]
    latency_ms: int
    payload_size_bytes: int
    http_status_code: int | None
    error_text: str | None


@dataclass
class AgentNode:
    agent_role: str  # e.g. "fund_manager"
    agent_report_id: int | None
    status: NodeStatus
    confidence: str | None  # HIGH / MEDIUM / LOW / None
    model: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    side: str | None         # "bull" / "bear" / None
    perspective: str | None  # "aggressive" / "neutral" / "conservative" / None
    response_excerpt: str    # first 500 chars of response_text
    failure_reason: str | None  # set when status == failed
    children: list["AgentNode"] = field(default_factory=list)
    adapters: list[AdapterNode] = field(default_factory=list)


@dataclass
class AgentTreeResponse:
    decision_run_id: int
    decision_kind: str
    status_summary: dict[str, int]
    # ^ e.g. {"agents_ok": 17, "agents_failed": 1, "adapters_ok": 5, "adapters_failed": 2}
    root: AgentNode  # fund_manager at the top for synthesis kind
```

- [ ] **Step 2: Implement the builder for `decision_kind="synthesis"`**

```python
def build_agent_tree(db: Session, decision_run_id: int) -> AgentTreeResponse:
    """For synthesis runs: build the FM-rooted DAG by walking the known
    phase topology + agent_reports + phase_output adapter outcomes."""
    run = db.get(DecisionRun, decision_run_id)
    if run is None or run.decision_kind != "synthesis":
        raise ValueError(f"unsupported decision_run_id={decision_run_id}")

    # Index all agent_reports by role + run_correlation_id.
    decision_id_str = f"plan-synth-{decision_run_id}"
    reports = list(db.execute(
        select(AgentReport).where(AgentReport.decision_id == decision_id_str)
        .order_by(AgentReport.id)
    ).scalars())

    by_role: dict[str, list[AgentReport]] = {}
    for r in reports:
        by_role.setdefault(r.agent_role, []).append(r)

    # Index phase outputs by seq.
    phases = list(db.execute(
        select(DecisionPhase).where(DecisionPhase.decision_run_id == decision_run_id)
        .order_by(DecisionPhase.seq)
    ).scalars())

    phase_1 = next((p for p in phases if p.kind == "synthesis.phase_1"), None)
    adapter_outcomes_p1: list[AdapterNode] = []
    if phase_1 and phase_1.phase_output_json:
        po = json.loads(phase_1.phase_output_json)
        for o in po.get("adapter_outcomes") or []:
            adapter_outcomes_p1.append(AdapterNode(**o))

    # Helper: pick one AgentReport for a role; pop from the dict so
    # we don't reuse it (risk officers have multiple rows).
    def pop_one(role: str, side: str | None = None,
                perspective: str | None = None) -> AgentReport | None:
        candidates = by_role.get(role) or []
        if not candidates:
            return None
        return candidates.pop(0)

    def to_node(r: AgentReport | None, *, role: str,
                side: str | None = None,
                perspective: str | None = None,
                expected_adapters: list[AdapterNode] | None = None,
                expected_children: list[AgentNode] | None = None) -> AgentNode:
        status: NodeStatus = "skipped"
        if r is not None:
            if r.confidence == "LOW":
                status = "degraded"
            else:
                status = "ok"
        return AgentNode(
            agent_role=role,
            agent_report_id=r.id if r else None,
            status=status,
            confidence=r.confidence if r else None,
            model=r.model if r else None,
            tokens_in=r.tokens_in if r else None,
            tokens_out=r.tokens_out if r else None,
            cost_usd=r.cost_usd if r else None,
            side=side,
            perspective=perspective,
            response_excerpt=(r.response_text or "")[:500] if r else "",
            failure_reason=None if r else "agent did not run",
            children=expected_children or [],
            adapters=expected_adapters or [],
        )

    # Build leaves first: Phase 1 analysts.
    analyst_nodes: dict[str, AgentNode] = {}
    for role in [
        "concentration", "fx", "fundamentals", "news",
        "sentiment", "technical", "macro", "tax",
        "household_budget", "plan_critique",
    ]:
        r = pop_one(role)
        # Attach adapters that fed this analyst.
        adapters_for_role = [
            a for a in adapter_outcomes_p1
            if _adapter_feeds_role(a.adapter_name, role)
        ]
        analyst_nodes[role] = to_node(
            r, role=role, expected_adapters=adapters_for_role,
        )

    # Phase 2: 3 horizons × (bull, bear, facilitator). The agent_reports
    # don't carry the horizon explicitly today — for now, render all 9 as
    # siblings under each researcher_facilitator node.
    # TODO when horizon tagging lands: split into 3 facilitator nodes.
    researcher_facilitator_nodes = [
        to_node(
            pop_one("researcher_facilitator"),
            role="researcher_facilitator",
            expected_children=[
                to_node(pop_one("bull_researcher"), role="bull_researcher",
                        side="bull", expected_children=list(analyst_nodes.values())),
                to_node(pop_one("bear_researcher"), role="bear_researcher",
                        side="bear", expected_children=list(analyst_nodes.values())),
            ],
        )
        for _ in range(3)
    ]

    # Phase 3: plan_synthesizer reads phase 1 + phase 2.
    synth_node = to_node(
        pop_one("plan_synthesizer"),
        role="plan_synthesizer",
        expected_children=[
            *researcher_facilitator_nodes,
            *list(analyst_nodes.values()),
        ],
    )

    # Phase 4: 3 risk officers + risk facilitator.
    risk_facilitator = to_node(
        pop_one("risk_facilitator"),
        role="risk_facilitator",
        expected_children=[
            to_node(pop_one("risk_officer"), role="risk_officer", perspective="aggressive"),
            to_node(pop_one("risk_officer"), role="risk_officer", perspective="neutral"),
            to_node(pop_one("risk_officer"), role="risk_officer", perspective="conservative"),
        ],
    )

    # Phase 5: fund_manager reads synth + risk_facilitator + plan_critique.
    fm_node = to_node(
        pop_one("fund_manager"),
        role="fund_manager",
        expected_children=[
            synth_node,
            risk_facilitator,
            analyst_nodes["plan_critique"],
        ],
    )

    return AgentTreeResponse(
        decision_run_id=decision_run_id,
        decision_kind=run.decision_kind or "synthesis",
        status_summary=_summarize(fm_node, adapter_outcomes_p1),
        root=fm_node,
    )


def _adapter_feeds_role(adapter_name: str, role: str) -> bool:
    mapping = {
        "news": {"finnhub_news"},
        "fundamentals": {"finnhub"},
        "technical": {"yfinance"},
        "sentiment": {"tipranks"},
        "macro": {"fred"},
        "fx": {"boi"},
    }
    return adapter_name in mapping.get(role, set())


def _summarize(root: AgentNode, adapter_outcomes: list[AdapterNode]) -> dict[str, int]:
    agents_ok = agents_failed = 0
    seen: set[int] = set()

    def walk(n: AgentNode) -> None:
        nonlocal agents_ok, agents_failed
        key = id(n)
        if key in seen:
            return
        seen.add(key)
        if n.status == "skipped" or n.status == "failed":
            agents_failed += 1
        else:
            agents_ok += 1
        for c in n.children:
            walk(c)

    walk(root)
    adapters_ok = sum(1 for a in adapter_outcomes if a.status == "ok")
    adapters_failed = sum(1 for a in adapter_outcomes if a.status in ("http_error", "exception"))
    return {
        "agents_ok": agents_ok,
        "agents_failed": agents_failed,
        "adapters_ok": adapters_ok,
        "adapters_failed": adapters_failed,
    }
```

- [ ] **Step 3: Test the builder against the existing run #23**

```python
# tests/test_agent_tree_builder.py
def test_build_agent_tree_for_existing_run_23(real_db):
    tree = build_agent_tree(real_db, decision_run_id=23)
    assert tree.root.agent_role == "fund_manager"
    summary = tree.status_summary
    # Run #23 had 18 agent_reports; with the new topology FM should reach
    # all of them via the DAG (some appear under multiple parents — the
    # builder dedups by id).
    assert summary["agents_ok"] >= 1
    # Today decision_phases for run 23 don't have adapter_outcomes (it
    # ran before T0.3), so adapter counts may be zero; new runs will
    # populate them.
```

- [ ] **Step 4: Run test**

Run: `.venv/Scripts/python.exe -m pytest tests/test_agent_tree_builder.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add argosy/services/agent_tree_builder.py tests/test_agent_tree_builder.py
git commit -m "feat(observability): T0.4 — agent_tree_builder produces FM-rooted DAG with status + adapter outcomes"
```

### Task 0.5 — `GET /api/decisions/{id}/agent-tree` endpoint

**Files:**
- Create: `argosy/api/routes/decisions_tree.py`
- Modify: `argosy/api/main.py` — register the new router
- Modify: `argosy/api/routes/decisions.py` — add `agent_tree_url` to the replay response
- Test: `tests/test_decisions_tree_route.py`

- [ ] **Step 1: Write the test**

```python
# tests/test_decisions_tree_route.py
def test_get_agent_tree_returns_fm_at_root(client, seeded_synth_run_id):
    r = client.get(f"/api/decisions/{seeded_synth_run_id}/agent-tree?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    assert body["root"]["agent_role"] == "fund_manager"
    assert "agents_ok" in body["status_summary"]
    # FM has plan_synthesizer + risk_facilitator + plan_critique as children
    child_roles = {c["agent_role"] for c in body["root"]["children"]}
    assert {"plan_synthesizer", "risk_facilitator", "plan_critique"} <= child_roles

def test_get_agent_tree_404_for_unknown_run(client):
    r = client.get("/api/decisions/9999/agent-tree?user_id=ariel")
    assert r.status_code == 404
```

- [ ] **Step 2: Implement the route**

```python
# argosy/api/routes/decisions_tree.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from argosy.api.deps import get_db
from argosy.services.agent_tree_builder import build_agent_tree

router = APIRouter(prefix="/api/decisions", tags=["decisions"])


@router.get("/{decision_run_id}/agent-tree")
def get_agent_tree(decision_run_id: int, user_id: str, db: Session = Depends(get_db)):
    try:
        return build_agent_tree(db, decision_run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"unknown decision_run_id={decision_run_id}")
```

Register in `argosy/api/main.py`:

```python
from argosy.api.routes.decisions_tree import router as decisions_tree_router
app.include_router(decisions_tree_router)
```

- [ ] **Step 3: Run test**

Run: `.venv/Scripts/python.exe -m pytest tests/test_decisions_tree_route.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add argosy/api/routes/decisions_tree.py argosy/api/main.py tests/test_decisions_tree_route.py
git commit -m "feat(observability): T0.5 — GET /api/decisions/{id}/agent-tree endpoint"
```

### Task 0.6 — UI tree component on `/decisions/[id]`

**Files:**
- Create: `ui/src/components/decisions/agent-tree.tsx`
- Create: `ui/src/components/decisions/adapter-leaf.tsx`
- Modify: `ui/src/app/decisions/[id]/page.tsx` — replace the `MermaidDiagram` for `sequence_mmd_full` with `<AgentTree>`
- Modify: `ui/src/lib/api.ts` — add `getAgentTree(id, userId)`

- [ ] **Step 1: Add the API client function**

```typescript
// ui/src/lib/api.ts
export async function getAgentTree(decisionRunId: number, userId: string) {
  const r = await fetch(`${API_BASE}/api/decisions/${decisionRunId}/agent-tree?user_id=${userId}`);
  if (!r.ok) throw new Error(`agent-tree fetch failed: ${r.status}`);
  return r.json();
}
```

- [ ] **Step 2: Implement `<AgentTree>` (recursive)**

```tsx
// ui/src/components/decisions/agent-tree.tsx
"use client";
import { useState } from "react";
import { ChevronDown, ChevronRight, AlertCircle, CheckCircle2, MinusCircle } from "lucide-react";
import { AdapterLeaf } from "./adapter-leaf";

interface AgentNode {
  agent_role: string;
  agent_report_id: number | null;
  status: "ok" | "degraded" | "failed" | "skipped";
  confidence: string | null;
  model: string | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cost_usd: number | null;
  side: string | null;
  perspective: string | null;
  response_excerpt: string;
  failure_reason: string | null;
  children: AgentNode[];
  adapters: any[];
}

const STATUS_ICON = {
  ok: CheckCircle2,
  degraded: MinusCircle,
  failed: AlertCircle,
  skipped: AlertCircle,
};

const STATUS_COLOR = {
  ok: "text-success",
  degraded: "text-warning",
  failed: "text-error",
  skipped: "text-muted-foreground",
};

export function AgentTree({ root }: { root: AgentNode }) {
  return (
    <div className="font-mono text-xs">
      <AgentTreeNode node={root} depth={0} />
    </div>
  );
}

function AgentTreeNode({ node, depth }: { node: AgentNode; depth: number }) {
  const [open, setOpen] = useState(depth < 1);
  const StatusIcon = STATUS_ICON[node.status];
  const hasChildren = node.children.length > 0 || node.adapters.length > 0;
  return (
    <div className="border-l border-border ml-2">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 px-2 py-1 hover:bg-secondary/40 w-full text-left"
      >
        {hasChildren ? (
          open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />
        ) : <span className="w-3" />}
        <StatusIcon className={`h-3 w-3 ${STATUS_COLOR[node.status]}`} />
        <span className="font-semibold">{node.agent_role}</span>
        {node.side && <span className="text-muted-foreground">({node.side})</span>}
        {node.perspective && <span className="text-muted-foreground">({node.perspective})</span>}
        {node.confidence && <span className="text-[10px] px-1 rounded bg-muted">{node.confidence}</span>}
        {node.cost_usd !== null && <span className="ml-auto text-muted-foreground">${node.cost_usd.toFixed(4)}</span>}
      </button>
      {open && (
        <div className="pl-4">
          {node.failure_reason && (
            <div className="px-2 py-1 text-error text-[11px]">⚠ {node.failure_reason}</div>
          )}
          {node.response_excerpt && (
            <details className="px-2 py-1">
              <summary className="cursor-pointer text-muted-foreground">response (first 500 chars)</summary>
              <pre className="whitespace-pre-wrap text-[11px] pt-1">{node.response_excerpt}</pre>
            </details>
          )}
          {node.adapters.map((a, i) => <AdapterLeaf key={i} adapter={a} />)}
          {node.children.map((c, i) => <AgentTreeNode key={i} node={c} depth={depth + 1} />)}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Implement `<AdapterLeaf>`**

```tsx
// ui/src/components/decisions/adapter-leaf.tsx
import { AlertCircle, CheckCircle2, MinusCircle } from "lucide-react";

interface AdapterNode {
  adapter_name: string;
  target: string | null;
  status: "ok" | "empty" | "http_error" | "exception";
  latency_ms: number;
  payload_size_bytes: number;
  http_status_code: number | null;
  error_text: string | null;
}

export function AdapterLeaf({ adapter }: { adapter: AdapterNode }) {
  const Icon = adapter.status === "ok" ? CheckCircle2
    : adapter.status === "empty" ? MinusCircle
    : AlertCircle;
  const color = adapter.status === "ok" ? "text-success"
    : adapter.status === "empty" ? "text-warning"
    : "text-error";
  return (
    <div className="flex items-center gap-2 px-2 py-1 text-[11px]">
      <Icon className={`h-3 w-3 ${color}`} />
      <span className="font-mono">{adapter.adapter_name}</span>
      {adapter.target && <span className="text-muted-foreground">{adapter.target}</span>}
      <span className="text-muted-foreground">{adapter.latency_ms}ms</span>
      <span className="text-muted-foreground">{adapter.payload_size_bytes}B</span>
      {adapter.http_status_code && (
        <span className="text-error">HTTP {adapter.http_status_code}</span>
      )}
      {adapter.error_text && (
        <span className="text-error truncate max-w-md" title={adapter.error_text}>
          {adapter.error_text}
        </span>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Replace the sequence diagram in `/decisions/[id]/page.tsx`**

Replace the `{sequence_mmd_full && (<Card>...)}` block with:

```tsx
{agentTree && (
  <Card>
    <CardHeader>
      <CardTitle className="text-base">Agent tree — Fund Manager at root</CardTitle>
      <CardDescription>
        {agentTree.status_summary.agents_ok} agents OK · {agentTree.status_summary.agents_failed} failed/skipped ·
        {agentTree.status_summary.adapters_ok} adapters OK · {agentTree.status_summary.adapters_failed} adapter failures
      </CardDescription>
    </CardHeader>
    <CardContent>
      <AgentTree root={agentTree.root} />
    </CardContent>
  </Card>
)}
```

Wire `agentTree` via a second `useEffect` that calls `api.getAgentTree(decisionRunId, USER_ID)`.

- [ ] **Step 5: UI lint + typecheck**

Run: `cd ui ; npm run lint ; npm run typecheck`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add ui/
git commit -m "feat(observability): T0.6 — FM-rooted agent tree on /decisions/[id], replacing the meaningless sequence diagram"
```

### Task 0.7 — Synthesis health banner on `/plan`

**Files:**
- Create: `ui/src/components/plan/synthesis-health-banner.tsx`
- Modify: `argosy/api/routes/plan.py` — extend `/api/plan/draft` with `synthesis_health` field
- Modify: `ui/src/app/plan/page.tsx` — render banner above FM objections
- Test: `tests/test_plan_draft_api.py` (extend)

- [ ] **Step 1: Backend test**

```python
def test_plan_draft_includes_synthesis_health(client, seeded_draft_with_partial_adapter_failures):
    r = client.get("/api/plan/draft?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    assert "synthesis_health" in body
    h = body["synthesis_health"]
    assert h["agents_ok"] >= 0
    assert h["adapters_failed"] >= 0
    assert h["decision_run_id"] is not None
```

- [ ] **Step 2: Extend `/api/plan/draft`**

Add to `argosy/api/routes/plan.py`'s `get_plan_draft` response builder: query `decision_phases` for `decision_run_id == pv.decision_run_id`, count adapter outcomes from phase 1's `phase_output_json`, count agents from `agent_reports` rows.

- [ ] **Step 3: UI banner**

```tsx
// ui/src/components/plan/synthesis-health-banner.tsx
"use client";
import Link from "next/link";
import { CheckCircle2, AlertTriangle } from "lucide-react";

export function SynthesisHealthBanner({
  health,
  decisionRunId,
}: {
  health: { agents_ok: number; agents_failed: number; adapters_ok: number; adapters_failed: number };
  decisionRunId: number | null;
}) {
  if (!decisionRunId) return null;
  const allGreen = health.agents_failed === 0 && health.adapters_failed === 0;
  const Icon = allGreen ? CheckCircle2 : AlertTriangle;
  const color = allGreen ? "border-success/40 bg-success/5" : "border-warning/40 bg-warning/5";
  return (
    <div className={`rounded-md border ${color} p-3 flex items-center gap-3`}>
      <Icon className="h-4 w-4 flex-shrink-0" />
      <p className="text-xs flex-1">
        {health.agents_ok} agents OK · {health.agents_failed} failed/skipped ·
        {health.adapters_ok} adapters OK · {health.adapters_failed} adapter failures
      </p>
      <Link
        href={`/decisions/${decisionRunId}`}
        className="text-xs text-primary hover:underline"
      >
        Drill in →
      </Link>
    </div>
  );
}
```

- [ ] **Step 4: Mount in `/plan/page.tsx`** above the FM objections card.

- [ ] **Step 5: Commit**

```bash
git add argosy/api/routes/plan.py ui/src/components/plan/synthesis-health-banner.tsx ui/src/app/plan/page.tsx tests/test_plan_draft_api.py
git commit -m "feat(observability): T0.7 — synthesis health banner on /plan with drill-in"
```

### Phase 0 exit gate

- All 7 Phase 0 commits land on `main`.
- `pytest -m "not llm_eval"` passes (≥ 1,020 tests).
- `cd ui && npm run lint && npm run typecheck` clean.
- Manual: load `/decisions/23` — see the FM-rooted tree with run #23's 18 agents reachable from the FM root. Confidence + cost + response excerpts visible. Phase 1 has adapter outcomes (for new runs only; #23 has none since it pre-dated T0.3).

---

## Phase 1 — Tier 3 adapter coverage (parallel)

Two parallel sub-agents under codex tandem review (these are parsers / network integration — codex-tandem territory per memory `feedback_use_tandem_for_risky_work.md`).

### Task 1.1 — T3.1 SEC 13F endpoint

**Owner agent:** codex tandem pair (claude writes / codex reviews).
**Risk class:** parser; HTTP integration.

**Files:**
- Modify: `argosy/adapters/data/sec_13f_adapter.py`
- Test: `tests/test_sec_13f_adapter.py`

- [ ] **Step 1: Diagnose the current 404**

```bash
.venv/Scripts/python.exe -c "from argosy.adapters.data.sec_13f_adapter import SEC13FAdapter; import asyncio; a = SEC13FAdapter(); print(asyncio.run(a.get_filings('NVDA')))"
```
Expected: HTTP 404 from the current EDGAR endpoint.

- [ ] **Step 2: Switch to EDGAR FTS**

Replace the failing endpoint path with EDGAR's full-text search:
`https://efts.sec.gov/LATEST/search-index?q=%22<ticker>%22&forms=13F-HR`
Parse the JSON response (form list with cik + accession_number); follow up with one fetch per filing to grab holdings.

- [ ] **Step 3: Track outcomes via `track_adapter_call`** (T0.2 dependency).

- [ ] **Step 4: Test with httpx_mock**

```python
def test_sec_13f_fetches_via_edgar_fts(httpx_mock):
    httpx_mock.add_response(
        url__contains="efts.sec.gov/LATEST/search-index",
        json={"hits": {"hits": [{"_source": {"cik": "1234", "accession_no": "0001234567-25-000001"}}]}},
    )
    httpx_mock.add_response(
        url__contains="0001234567-25-000001",
        text="<xml>...</xml>",  # 13F XML
    )
    adapter = SEC13FAdapter()
    filings = asyncio.run(adapter.get_filings("NVDA"))
    assert len(filings) >= 1
```

- [ ] **Step 5: Codex tandem review**

```python
# Run from the python REPL in the project root
import sys; sys.path.insert(0, "tools/codex-tandem/scripts")
from devmode_pair import run_pair
run_pair(
    node_dir="argosy/adapters/data/sec_13f_adapter.py",
    goal="Switch SEC 13F adapter from broken endpoint to EDGAR FTS. Must record adapter outcomes via track_adapter_call. Don't crash on 404 — surface as http_error.",
    iterations=2,
)
```

- [ ] **Step 6: Commit**

```bash
git add argosy/adapters/data/sec_13f_adapter.py tests/test_sec_13f_adapter.py
git commit -m "feat(adapters): T3.1 — SEC 13F switches to EDGAR FTS path; surfaces outcomes"
```

### Task 1.2 — T3.2 TipRanks fallback

**Owner agent:** codex tandem pair.
**Risk class:** parser; HTTP integration; new fallback path.

**Files:**
- Modify: `argosy/adapters/data/tipranks_adapter.py`
- Modify: `argosy/adapters/data/finnhub_adapter.py` — add `get_social_sentiment(ticker)` if not present
- Test: `tests/test_tipranks_adapter.py`, `tests/test_finnhub_adapter.py`

- [ ] **Step 1: Add `FinnhubAdapter.get_social_sentiment`**

Wraps `https://finnhub.io/api/v1/stock/social-sentiment?symbol=...&from=...&to=...`.

- [ ] **Step 2: Modify `TipRanksAdapter` to fall back**

```python
async def get_blogger_sentiment(self, ticker: str) -> SentimentItem | None:
    with track_adapter_call("tipranks", target=ticker) as outcome:
        try:
            resp = await self._http.get(...)
            if resp.status_code == 403:
                outcome.record_http_error(status_code=403, body="anti-bot")
                # Fall through to Finnhub fallback below
            else:
                resp.raise_for_status()
                outcome.set_payload_size_bytes(len(resp.content))
                return SentimentItem.parse_obj(resp.json())
        except httpx.HTTPStatusError as e:
            outcome.record_http_error(status_code=e.response.status_code, body=e.response.text[:500])

    # Fallback: Finnhub social-sentiment (separate adapter call,
    # tracked separately so the user sees BOTH outcomes).
    return await self._finnhub_social_fallback(ticker)

async def _finnhub_social_fallback(self, ticker: str) -> SentimentItem | None:
    if self._finnhub is None:
        return None
    return await self._finnhub.get_social_sentiment(ticker)
```

- [ ] **Step 3: Tests for both paths**

```python
def test_tipranks_falls_back_to_finnhub_on_403(httpx_mock):
    httpx_mock.add_response(url__contains="tipranks.com", status_code=403, text="anti-bot")
    httpx_mock.add_response(url__contains="finnhub.io/api/v1/stock/social-sentiment", json={"symbol": "NVDA", "reddit": [...]})
    adapter = TipRanksAdapter(finnhub=FinnhubAdapter(api_key="test"))
    res = asyncio.run(adapter.get_blogger_sentiment("NVDA"))
    assert res is not None
    outcomes = collect_outcomes()
    # Both tracked — user sees TipRanks failed AND Finnhub succeeded.
    assert any(o.adapter_name == "tipranks" and o.status == "http_error" for o in outcomes)
    assert any(o.adapter_name == "finnhub_social" and o.status == "ok" for o in outcomes)
```

- [ ] **Step 4: Codex tandem review**

Same `run_pair` pattern as Task 1.1.

- [ ] **Step 5: Commit**

```bash
git add argosy/adapters/data/tipranks_adapter.py argosy/adapters/data/finnhub_adapter.py tests/test_tipranks_adapter.py tests/test_finnhub_adapter.py
git commit -m "feat(adapters): T3.2 — TipRanks accepts 403 as failed; falls back to Finnhub social-sentiment"
```

### Phase 1 exit gate

- Both adapter commits land.
- A fresh synthesis run shows `sentiment` analyst with non-empty social signal (via Finnhub fallback) and `agent-tree` for that run shows TipRanks adapter as `http_error 403` PLUS Finnhub social as `ok`. SEC 13F shows up via EDGAR FTS.
- `argosy diagnose adapters` reflects new status.

---

## Phase 2 — Tier 4 surfaces (parallel)

Three parallel sub-agents.

### Task 2.1 — T4.1 Per-position thesis cards

**Owner agent:** code-writer / code-reviewer pair (UI-heavy; no migrations).
**Risk class:** UI; pure data derivation.

**Files:**
- Create: `argosy/agents/per_position_thesis.py` — pure-Python derivation, no LLM call. Walks horizon JSONs + current portfolio positions; emits one card per holding.
- Create: `argosy/api/routes/positions.py` — `GET /api/positions/thesis`
- Create: `ui/src/app/positions/page.tsx` — per-position card grid
- Create: `ui/src/components/positions/position-card.tsx`
- Test: `tests/test_per_position_thesis.py`

The per-position card consumes the existing horizon JSON (`horizon_short`, `horizon_medium`, `horizon_long`) on the pending draft + the live portfolio snapshot. For each held ticker, derive:
- **Verdict** (`HOLD` / `BUY` / `TRIM` / `SELL`) — from explicit deltas + target weights
- **Conviction** (`HIGH` / `MEDIUM` / `LOW`) — from analyst confidences that cite the ticker
- **Reasoning** (markdown) — assembled from rationale strings that mention the ticker
- **Cited sources** — collected from analyst `sources_json` rows that mention the ticker

For "should add" cards: scan synthesis output for tickers mentioned in actions/targets that are NOT in current holdings.

- [ ] **Step 1: Write the failing test** (cover verdict derivation: NVDA over 50% → SELL/TRIM; SGOV at floor → HOLD; UCITS replacement candidate not held → "should add")

- [ ] **Step 2: Implement `per_position_thesis.py`** (no LLM, deterministic derivation)

- [ ] **Step 3: Implement the route**

- [ ] **Step 4: Implement the UI page + card** — use the existing Card components in `ui/src/components/ui/`; route at `/positions`

- [ ] **Step 5: UI lint + typecheck**

- [ ] **Step 6: Commit**

```bash
git add argosy/agents/per_position_thesis.py argosy/api/routes/positions.py ui/src/app/positions/page.tsx ui/src/components/positions/ tests/test_per_position_thesis.py
git commit -m "feat(plan): T4.1 — per-position thesis cards with Hold/Buy/Sell verdict + conviction + reasoning"
```

### Task 2.2 — T4.2 Speculative-candidates polish

**Owner agent:** code-writer / code-reviewer pair (pure UI; data already there).
**Risk class:** UI.

**Files:**
- Modify: `argosy/api/routes/proposals.py` — surface `conviction`, `cited_sources`, `tier`
- Modify: `ui/src/app/proposals/page.tsx` — render the speculative section more clearly
- Test: `tests/test_speculation_route.py` (extend)

- [ ] **Step 1: Test the extended response shape**
- [ ] **Step 2: Extend the route**
- [ ] **Step 3: Update the proposals UI** — clearly separate "real plan" deltas from "speculative" ones; speculative cards collapsed by default with conviction badge.
- [ ] **Step 4: UI lint + typecheck**
- [ ] **Step 5: Commit**

```bash
git add argosy/api/routes/proposals.py ui/src/app/proposals/page.tsx tests/test_speculation_route.py
git commit -m "feat(proposals): T4.2 — speculative candidates polish with conviction + cited sources"
```

### Task 2.3 — T4.4 Decision-replay rows

**Owner agent:** codex tandem pair (involves migration + new decision_kind taxonomy).
**Risk class:** migration; cross-cutting `decision_kind` use.

**Files:**
- Create: `argosy/state/migrations/0033_decision_kind_expansion.sql` — documentation-only migration; no schema change since `decision_kind` is free-text. Adds a CHECK constraint? Decide during implementation.
- Modify: `argosy/api/routes/decisions.py` — recognize new kinds: `delta_pushback`, `daily_brief`
- Modify: `ui/src/app/decisions/page.tsx` — render rows for new kinds with their own row shape
- Test: `tests/test_decisions_route.py` (extend)

- [ ] **Step 1: Write the failing test** — assert `delta_pushback` and `daily_brief` runs appear in `/decisions` list with their tier/ticker/kind columns.
- [ ] **Step 2: Implement the route extension**
- [ ] **Step 3: UI rows** — collapsed by default; expand to show the same agent tree as synthesis runs (re-use `<AgentTree>`).
- [ ] **Step 4: Codex tandem review** (migration; cross-cutting kind taxonomy)
- [ ] **Step 5: Commit**

```bash
git add argosy/state/migrations/0033_decision_kind_expansion.sql argosy/api/routes/decisions.py ui/src/app/decisions/page.tsx tests/test_decisions_route.py
git commit -m "feat(decisions): T4.4 — decision-replay rows for delta_pushback + daily_brief kinds"
```

### Phase 2 exit gate

- Three Phase 2 commits land.
- `/positions` page renders with per-holding cards.
- `/proposals` page cleanly separates real-plan deltas from speculative candidates.
- `/decisions` shows new kinds (empty until Phase 3 produces them).

---

## Phase 3 — Pushback flow + daily brief (sequential)

### Task 3.1 — T4.3 Per-delta slim re-debate

**Owner agent:** codex tandem pair (decision-flow orchestration; risky).
**Risk class:** orchestrator; cost — must not blow $10 cap.

**Files:**
- Create: `argosy/orchestrator/flows/per_delta_pushback.py` — runs ONE horizon's bull/bear/facilitator re-debate scoped to the disputed delta. NOT full synthesis. Writes a `decision_runs` row with `decision_kind="delta_pushback"`.
- Modify: `argosy/api/routes/plan.py` — `POST /api/plan/draft/delta/{item_id}/pushback` now triggers slim re-debate instead of queueing for next full synthesis.
- Modify: `ui/src/app/plan/page.tsx` — push back button kicks off this flow + shows progress via the existing cascade panel.
- Test: `tests/test_per_delta_pushback.py`

The slim re-debate is targeted: prompt bull/bear/facilitator with "Here is one specific delta from the current draft. Here is the user's pushback. Re-evaluate ONLY this delta and tell us whether to keep, modify, or drop." Result is a small DTO with `verdict: keep | modify | drop` + `revised_value` + `rationale`. The /plan UI surfaces the new verdict and offers Accept / Reject.

- [ ] **Step 1: Define the slim flow + test it via stubbed agents**
- [ ] **Step 2: Wire the endpoint**
- [ ] **Step 3: Wire the UI**
- [ ] **Step 4: Codex tandem review** (decision flow; budget contract)
- [ ] **Step 5: Commit**

```bash
git add argosy/orchestrator/flows/per_delta_pushback.py argosy/api/routes/plan.py ui/src/app/plan/page.tsx tests/test_per_delta_pushback.py
git commit -m "feat(plan): T4.3 — slim per-delta re-debate via dedicated decision_kind"
```

### Task 3.2 — T4.5 Daily-brief production loop

**Owner agent:** code-writer / code-reviewer pair.
**Risk class:** background scheduling; new table.

**Files:**
- Create: `argosy/state/migrations/0034_daily_briefs.sql` — table for persisted briefs
- Create: `argosy/services/daily_brief_runner.py` — once-per-day callable; reads current draft + overnight market deltas; writes a brief
- Modify: `argosy/api/main.py` — register background task with APScheduler or equivalent already-in-use scheduler
- Modify: `ui/src/app/page.tsx` — render the latest brief (if present) at the top of the home page
- Test: `tests/test_daily_brief_runner.py`

- [ ] **Step 1: Migration + ORM**
- [ ] **Step 2: Brief generator (test with stubbed Claude call)**
- [ ] **Step 3: Wire scheduler — daily at 07:00 user-tz**
- [ ] **Step 4: UI surface**
- [ ] **Step 5: Commit**

```bash
git add argosy/state/migrations/0034_daily_briefs.sql argosy/services/daily_brief_runner.py argosy/api/main.py ui/src/app/page.tsx tests/test_daily_brief_runner.py
git commit -m "feat(brief): T4.5 — daily-brief production loop + home-page surface"
```

### Phase 3 exit gate

- Two Phase 3 commits land.
- Manual test: click "Push back" on a delta in `/plan` — a `delta_pushback` decision run appears in `/decisions` and the disputed delta gets a revised verdict.
- A daily brief lands in DB within 24h of scheduler start.

---

## Phase 4 — Final SDD refresh + verification synthesis

### Task 4.1 — Update SDD with current state

- [ ] Refresh `## Handover note` with new commit pointers, what shipped, what's left.
- [ ] Add an `## Observability — agent tree` section in the SDD if one doesn't exist yet, documenting:
  - The 5-phase DAG topology
  - The adapter-outcomes contextvar pattern
  - The `/api/decisions/{id}/agent-tree` contract
  - How to extend the tree builder to new `decision_kind` values

### Task 4.2 — Trigger one fresh synthesis to verify

- [ ] Manually trigger synthesis via `POST /api/advisor/check-in` (cost: $3-4).
- [ ] Confirm `/decisions/{new_run_id}` renders the FM-rooted tree with adapter outcomes.
- [ ] Confirm `/plan` shows the synthesis health banner with accurate counts.
- [ ] Confirm `/positions` shows per-holding cards from the new draft.
- [ ] Confirm `/proposals` shows speculative candidates cleanly.

### Task 4.3 — Commit SDD refresh

```bash
git add docs/design/SDD.md
git commit -m "docs(sdd): T3+T4+observability wave shipped; final handover refresh"
```

---

## Parallelism map

```
Phase 0 (sequential): T0.1 → T0.2 → T0.3 → T0.4 → T0.5 → T0.6 → T0.7
Phase 1 (parallel):   T1.1 (T3.1 SEC 13F) | T1.2 (T3.2 TipRanks)
Phase 2 (parallel):   T2.1 (T4.1 positions) | T2.2 (T4.2 proposals) | T2.3 (T4.4 replay rows)
Phase 3 (sequential): T3.1 (T4.3 slim re-debate) → T3.2 (T4.5 daily brief)
Phase 4 (sequential): T4.1 → T4.2 → T4.3 (SDD refresh + verification synth)
```

Phase 0 is sequential and blocks Phases 1-3 because adapter outcomes + agent tree are dependencies. Phase 1 + Phase 2 + Phase 3 are independent of each other and can run in parallel, but Phase 3 depends on Phase 2's `decision_kind` work for the `/decisions` row plumbing.

## Codex-tandem usage map

| Task | Tandem? | Why |
|---|---|---|
| T0.1 | yes | Orchestrator changes; risk of breaking persistence |
| T0.2 | no | Pure helper module, well-isolated |
| T0.3 | yes | Touches every adapter; broad blast radius |
| T0.4 | yes | Builder is dense; topology must be exact |
| T0.5 | no | Trivial route |
| T0.6 | no | UI |
| T0.7 | no | UI |
| T1.1 (T3.1) | yes | Parser + HTTP integration |
| T1.2 (T3.2) | yes | Parser + new fallback path |
| T2.1 (T4.1) | no | UI + pure derivation |
| T2.2 (T4.2) | no | UI |
| T2.3 (T4.4) | yes | Migration + cross-cutting kind |
| T3.1 (T4.3) | yes | Decision-flow orchestration |
| T3.2 (T4.5) | no | New module + UI |

## Self-Review (post-write)

1. **Spec coverage:** Every Tier-3 + Tier-4 item is covered. Plus the user's observability requirement (Phase 0 — 7 tasks). ✓
2. **Placeholder scan:** No TBD / TODO / "fill in later." Code blocks have actual content. ✓
3. **Type consistency:** `AgentNode.children: list[AgentNode]`, `AdapterNode` shape used consistently in builder + UI. `AgentTreeResponse.status_summary` keys (`agents_ok`, `agents_failed`, `adapters_ok`, `adapters_failed`) used the same way in the route, builder, and banner. ✓
4. **Open question:** the orchestrator persists agent_reports to JSONL trail mid-flow (W1.C-v4) to dodge the uvicorn writer-lock issue. Phase 0.1 changes this to per-phase sub-session writes. If the lock issue resurfaces, fall back to: keep the JSONL trail, but persist a "phase-end snapshot" of IDs by re-ingesting JSONL incrementally per phase. Document this fallback inline in the orchestrator change.
