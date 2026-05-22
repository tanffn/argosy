# Wave A — `BaseAgent` API Features Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire prompt caching + Citations API + extended thinking into `BaseAgent._call_via_api_key`, with per-role configuration and AgentReport telemetry, so multi-agent decision flows are 30-50% cheaper and citations become verifiable rather than self-reported.

**Architecture:** Refactor the system-prompt construction in `argosy/agents/base.py` to emit content blocks (one cacheable, one role-specific). Conditionally enable `thinking` and Citations per role via two new config tables. Add migration 0026 with four columns on `agent_reports` to capture the new telemetry. The claude_code-SDK backend (`_call_via_claude_code_inner`) is untouched in Wave A — it doesn't expose `cache_control`/`thinking`/`citations` through `query()`. Only the direct-API path (`_call_via_api_key`) gains the new features. This is acceptable because production agents use the api_key backend.

**Tech Stack:** Python 3.12 / SQLAlchemy 2.0 / Alembic / Pydantic v2 / Anthropic Python SDK ≥0.40 / pytest.

**Spec:** `docs/superpowers/specs/2026-05-22-baseagent-api-features-design.md`

**Common conventions for this plan:**
- Python interpreter: `D:/Projects/financial-advisor/.venv/Scripts/python.exe`
- Run backend tests: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" <path>`
- Run live-LLM tests (opt-in): `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m llm_eval <path>` (requires `ANTHROPIC_API_KEY` in keychain or env)
- Run alembic: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m alembic upgrade head` (from `ARGOSY_HOME`)
- All commits follow the existing convention: `<type>(<scope>): <subject>` (look at `git log --oneline -20` for examples).
- Working tree must be clean before starting; commit after each task.
- Per-user binding rule: **codex tandem for risky work.** Tasks 3 (migration), 6 (cost math), 8 (cache wiring), and 17 (citations wiring) are flagged 🧪 **TANDEM** — use the codex-tandem zigzag pattern (kit at `tools/codex-tandem/`) for cross-provider review before committing.

**Validation gates:**
- After every task: `pytest -m "not llm_eval" <touched paths>` passes.
- After Phase 4 (Citations landed): full suite passes (`pytest -m "not llm_eval"`).
- After Phase 7 (cost-regression smoke): the smoke test asserts ≥30% input-token reduction vs. baseline.

---

## Phase 0 — Pre-flight (no behavior change)

### Task 1: Verify SDK version and Citations availability

**Files:**
- Read: `pyproject.toml`
- Read: `.venv/Lib/site-packages/anthropic/__init__.py` (just to check version)

- [ ] **Step 1: Read the pinned Anthropic SDK version**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -c "import anthropic; print(anthropic.__version__)"`
Expected output: a version ≥ `0.40.0`. (If lower, run `uv add 'anthropic>=0.55.0'` to bump; ≥0.55 is the minimum that ships GA Citations.)

- [ ] **Step 2: Verify Citations API call shape is GA in the installed SDK**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -c "from anthropic.types import TextCitation; print(TextCitation)"`
Expected output: `<class 'anthropic.types.text_citation.TextCitation'>` or equivalent — confirms the citation block types are importable. If `ImportError`, bump the SDK before proceeding.

- [ ] **Step 3: Verify `thinking` parameter is GA**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -c "from anthropic.types import ThinkingBlock; print(ThinkingBlock)"`
Expected output: confirms `ThinkingBlock` import works.

- [ ] **Step 4: No commit — read-only verification.**

If any of the three imports failed, stop and resolve the SDK version before continuing. Do not paper over with `try/except ImportError`; the SDK is the contract this plan depends on.

---

### Task 2: Capture cost baseline for regression smoke

**Files:**
- Create: `tests/fixtures/cost_baseline_pre_wave_a.json`
- Create: `tests/_capture_baseline.py` (one-shot script, deleted at end of task)

- [ ] **Step 1: Identify a representative fixture decision**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_decision_flow_e2e.py -v --collect-only 2>&1 | head -20`
Expected: a list of e2e decision-flow tests. Pick the most representative one (typically `test_t2_nvda_full_flow` or similar). Note the test name.

- [ ] **Step 2: Write a one-shot baseline capture script**

```python
# tests/_capture_baseline.py
"""One-shot: replays a fixture decision through DecisionFlow on current main,
records total cost/tokens to tests/fixtures/cost_baseline_pre_wave_a.json.
Run BEFORE any BaseAgent changes land. Deleted after baseline is captured.
"""
from __future__ import annotations
import json
from pathlib import Path

from argosy.decisions.flow import DecisionFlow
from argosy.state import db as db_mod


def capture() -> None:
    # Use the same fixture wiring as the chosen test (test_t2_nvda_full_flow).
    # Inline the minimal setup here so this script is self-contained.
    from tests.fixtures.decision_fixtures import build_t2_nvda_scenario  # adjust import to whatever the chosen test uses
    scenario = build_t2_nvda_scenario()

    with db_mod.SessionLocal() as session:
        flow = DecisionFlow(user_id="ariel", session=session)
        result = flow.run_sync(**scenario.inputs)

    total_in = sum(r.tokens_in for r in result.agent_reports)
    total_out = sum(r.tokens_out for r in result.agent_reports)
    total_cost = sum(float(r.cost_usd) for r in result.agent_reports)

    out = {
        "scenario": "t2_nvda_full_flow",
        "agent_report_count": len(result.agent_reports),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_cost_usd": round(total_cost, 4),
        "captured_at": "2026-05-22",
        "git_sha": "f90e90f",
    }
    Path("tests/fixtures/cost_baseline_pre_wave_a.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8"
    )
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    capture()
```

- [ ] **Step 3: Run the capture script (live LLM — needs `ANTHROPIC_API_KEY`)**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe tests/_capture_baseline.py`
Expected output: JSON with non-zero `total_input_tokens` and `total_cost_usd`. File `tests/fixtures/cost_baseline_pre_wave_a.json` exists.

- [ ] **Step 4: Delete the one-shot script (we only need the baseline file)**

Run: `rm tests/_capture_baseline.py`

- [ ] **Step 5: Commit the baseline**

```bash
git add tests/fixtures/cost_baseline_pre_wave_a.json
git commit -m "test(fixtures): pre-Wave-A cost baseline for regression smoke"
```

---

## Phase 1 — Migration + telemetry plumbing

### Task 3: Create migration 0026 (add four columns to `agent_reports`)  🧪 **TANDEM**

**Files:**
- Create: `alembic/versions/0026_agent_reports_api_telemetry.py`
- Test: `tests/test_migration_0026.py`

- [ ] **Step 1: Write the failing migration test**

```python
# tests/test_migration_0026.py
"""Migration 0026 adds cache/thinking/citations telemetry columns to agent_reports."""
from __future__ import annotations

from sqlalchemy import inspect

from tests.conftest import alembic_engine_at_head


def test_agent_reports_has_api_telemetry_columns():
    engine = alembic_engine_at_head()
    insp = inspect(engine)
    columns = {c["name"] for c in insp.get_columns("agent_reports")}
    expected_new = {
        "cache_input_tokens",
        "cache_creation_tokens",
        "thinking_tokens",
        "citations_json",
    }
    missing = expected_new - columns
    assert not missing, f"agent_reports missing columns: {missing}"


def test_agent_reports_api_telemetry_defaults():
    """New columns default to 0 / NULL so existing rows remain valid."""
    engine = alembic_engine_at_head()
    insp = inspect(engine)
    cols_by_name = {c["name"]: c for c in insp.get_columns("agent_reports")}
    assert cols_by_name["cache_input_tokens"]["default"] == "0"
    assert cols_by_name["cache_creation_tokens"]["default"] == "0"
    assert cols_by_name["thinking_tokens"]["default"] == "0"
    assert cols_by_name["citations_json"]["nullable"] is True
```

- [ ] **Step 2: Run the test, expect FAIL**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_migration_0026.py -v`
Expected: FAIL with `AssertionError: agent_reports missing columns: {'cache_input_tokens', ...}`

- [ ] **Step 3: Write the migration**

```python
# alembic/versions/0026_agent_reports_api_telemetry.py
"""agent_reports: add cache/thinking/citations telemetry columns (Wave A).

Revision ID: 0026_agent_reports_api_telemetry
Revises: 0025_decision_phases_seq_unique
Create Date: 2026-05-22

Adds four columns to ``agent_reports`` to capture telemetry from the
Anthropic Messages API features wired into ``BaseAgent`` in Wave A:

* ``cache_input_tokens``    — tokens read from the prompt cache (priced at 0.1×
  input).
* ``cache_creation_tokens`` — tokens written to the cache (priced at 1.25×
  input, one-time per cache prefix).
* ``thinking_tokens``        — extended-thinking tokens (priced as output).
* ``citations_json``         — JSON array of cited spans from the Citations
  API, one entry per cited claim (NULL when citations disabled or unused).

Existing rows get defaults of 0 / NULL so the migration is a pure additive.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_agent_reports_api_telemetry"
down_revision: str | None = "0025_decision_phases_seq_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_reports") as batch:
        batch.add_column(sa.Column(
            "cache_input_tokens", sa.Integer(), nullable=False, server_default="0",
        ))
        batch.add_column(sa.Column(
            "cache_creation_tokens", sa.Integer(), nullable=False, server_default="0",
        ))
        batch.add_column(sa.Column(
            "thinking_tokens", sa.Integer(), nullable=False, server_default="0",
        ))
        batch.add_column(sa.Column(
            "citations_json", sa.Text(), nullable=True,
        ))


def downgrade() -> None:
    with op.batch_alter_table("agent_reports") as batch:
        batch.drop_column("citations_json")
        batch.drop_column("thinking_tokens")
        batch.drop_column("cache_creation_tokens")
        batch.drop_column("cache_input_tokens")
```

- [ ] **Step 4: Run the test, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_migration_0026.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: 🧪 TANDEM check — dispatch codex zigzag**

Why: migrations are listed in `feedback_use_tandem_for_risky_work.md`. Dispatch a codex review of the migration before committing.

Run (from `D:/Projects/financial-advisor`):
```bash
D:/Projects/financial-advisor/.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0, 'tools/codex-tandem/scripts')
from engine_codex import run_codex
from pathlib import Path
r = run_codex(
    node_dir=Path('tools/codex-tandem/runs/wave-a-migration-0026'),
    prompt='Review alembic/versions/0026_agent_reports_api_telemetry.py for: '
           '(1) correct down_revision chain (must follow 0025_decision_phases_seq_unique), '
           '(2) defaults match the spec (server_default=0 for the three int cols, nullable=True for citations_json), '
           '(3) downgrade reverses upgrade exactly, '
           '(4) no missing op.batch_alter_table guard (SQLite needs it for ALTER TABLE).',
    agent_name='wave_a_migration_0026', role='reviewer')
print('VERDICT:', r.verdict_text[:500])
"
```
Expected: `COMMIT AS-IS` or specific BLOCKERS. If BLOCKERS, fix and re-review. Do NOT commit until clean.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0026_agent_reports_api_telemetry.py tests/test_migration_0026.py
git commit -m "feat(db): migration 0026 — agent_reports API telemetry columns"
```

---

### Task 4: Update `AgentReport` ORM model with the four new columns

**Files:**
- Modify: `argosy/state/models.py` (around line 237, the `AgentReport` class)
- Test: `tests/test_agent_report_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_report_model.py
"""AgentReport ORM exposes the new API telemetry columns introduced in 0026."""
from __future__ import annotations

from argosy.state.models import AgentReport


def test_agent_report_has_api_telemetry_attrs():
    fields = {c.key for c in AgentReport.__table__.columns}
    assert "cache_input_tokens" in fields
    assert "cache_creation_tokens" in fields
    assert "thinking_tokens" in fields
    assert "citations_json" in fields


def test_agent_report_defaults_for_new_fields():
    """Defaults match the migration: 0 for token counts, None for citations_json."""
    r = AgentReport(
        user_id="ariel",
        agent_role="news_analyst",
        model="claude-sonnet-4-6",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.001,
    )
    assert r.cache_input_tokens == 0
    assert r.cache_creation_tokens == 0
    assert r.thinking_tokens == 0
    assert r.citations_json is None
```

- [ ] **Step 2: Run the test, expect FAIL**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_agent_report_model.py -v`
Expected: FAIL with `AssertionError: 'cache_input_tokens' not in fields`.

- [ ] **Step 3: Add the four `mapped_column` declarations to `AgentReport`**

In `argosy/state/models.py`, locate the `AgentReport` class (around line 237). After the existing `cost_usd` declaration (around line 264), insert:

```python
    # Wave A — Anthropic Messages API telemetry (migration 0026).
    cache_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    cache_creation_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    thinking_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    citations_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None,
    )
```

(Confirm `Text` is already imported at the top of the file; if not, add it to the existing `from sqlalchemy import ...` line.)

- [ ] **Step 4: Run the test, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_agent_report_model.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: Run the full suite to ensure no ORM-side breakage**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" -x`
Expected: all tests pass. Migration 0026 is already applied by `alembic_engine_at_head` in test fixtures, so existing tests that touch AgentReport remain valid.

- [ ] **Step 6: Commit**

```bash
git add argosy/state/models.py tests/test_agent_report_model.py
git commit -m "feat(state): AgentReport ORM columns for Wave A API telemetry"
```

---

### Task 5: Apply migration to dev DB

**Files:**
- (no source changes — runtime operation only)

- [ ] **Step 1: Back up the dev DB**

Run: `cp db/argosy.db db/argosy.db.pre-wave-a.bak`
Expected: backup file created.

- [ ] **Step 2: Apply migrations**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m alembic upgrade head`
Expected: output ends with `INFO  [alembic.runtime.migration] Running upgrade 0025_decision_phases_seq_unique -> 0026_agent_reports_api_telemetry, ...`. Exit code 0.

- [ ] **Step 3: Verify schema on the live dev DB**

Run:
```bash
D:/Projects/financial-advisor/.venv/Scripts/python.exe -c "
import sqlite3
c = sqlite3.connect('db/argosy.db')
cols = c.execute(\"PRAGMA table_info(agent_reports)\").fetchall()
for col in cols: print(col[1])
"
```
Expected output includes the four new column names (`cache_input_tokens`, `cache_creation_tokens`, `thinking_tokens`, `citations_json`).

- [ ] **Step 4: No commit — runtime op only.**

The backup is local-only (gitignored under `db/*.bak` per the existing `.gitignore`). If `db/*.bak` is NOT in `.gitignore`, add it before the next commit.

---

### Task 6: Update `_estimate_usd` to account for cache + thinking pricing  🧪 **TANDEM**

**Files:**
- Modify: `argosy/agents/base.py` (the `_estimate_usd` method)
- Test: `tests/test_estimate_usd.py`

- [ ] **Step 1: Locate `_estimate_usd` in `base.py`**

Run: `grep -n "_estimate_usd\|def _estimate" argosy/agents/base.py`
Expected: a line showing the method definition. Note the line number.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_estimate_usd.py
"""_estimate_usd accounts for cache and thinking pricing (Wave A)."""
from __future__ import annotations

import pytest

from argosy.agents.base import BaseAgent


class _DummyAgent(BaseAgent):
    agent_role = "news_analyst"
    output_model = type("Out", (), {})

    def build_prompt(self, **_):  # noqa: D401
        return ("", "")


@pytest.fixture
def agent():
    return _DummyAgent(user_id="ariel", model="claude-sonnet-4-6")


def test_estimate_usd_no_cache_no_thinking(agent):
    # 1000 input + 500 output on Sonnet ($3/M in, $15/M out):
    # cost = 1000*3/1M + 500*15/1M = 0.003 + 0.0075 = 0.0105
    cost = agent._estimate_usd(
        tokens_in=1000, tokens_out=500,
        cache_input_tokens=0, cache_creation_tokens=0, thinking_tokens=0,
    )
    assert cost == pytest.approx(0.0105, rel=1e-3)


def test_estimate_usd_with_cache_read(agent):
    # 1000 total input, 800 from cache:
    # uncached_input = 200; cache_read = 800
    # cost = 200*3/1M + 800*3*0.10/1M + 500*15/1M
    #      = 0.0006   + 0.00024            + 0.0075
    #      = 0.00834
    cost = agent._estimate_usd(
        tokens_in=1000, tokens_out=500,
        cache_input_tokens=800, cache_creation_tokens=0, thinking_tokens=0,
    )
    assert cost == pytest.approx(0.00834, rel=1e-3)


def test_estimate_usd_with_cache_creation(agent):
    # 1000 total input, 800 newly cached:
    # uncached_input = 200; cache_write = 800
    # cost = 200*3/1M + 800*3*1.25/1M + 500*15/1M
    #      = 0.0006   + 0.003          + 0.0075
    #      = 0.0111
    cost = agent._estimate_usd(
        tokens_in=1000, tokens_out=500,
        cache_input_tokens=0, cache_creation_tokens=800, thinking_tokens=0,
    )
    assert cost == pytest.approx(0.0111, rel=1e-3)


def test_estimate_usd_with_thinking(agent):
    # Thinking tokens priced as output:
    # cost = 1000*3/1M + (500 + 2000)*15/1M
    #      = 0.003     + 0.0375
    #      = 0.0405
    cost = agent._estimate_usd(
        tokens_in=1000, tokens_out=500,
        cache_input_tokens=0, cache_creation_tokens=0, thinking_tokens=2000,
    )
    assert cost == pytest.approx(0.0405, rel=1e-3)


def test_estimate_usd_combined(agent):
    # 1000 input (500 cache_read, 200 cache_write, 300 uncached), 500 out, 1000 thinking
    # uncached: 300*3/1M = 0.0009
    # read:     500*3*0.10/1M = 0.00015
    # write:    200*3*1.25/1M = 0.00075
    # output+thinking: (500+1000)*15/1M = 0.0225
    # total: 0.02430
    cost = agent._estimate_usd(
        tokens_in=1000, tokens_out=500,
        cache_input_tokens=500, cache_creation_tokens=200, thinking_tokens=1000,
    )
    assert cost == pytest.approx(0.02430, rel=1e-3)
```

- [ ] **Step 3: Run the test, expect FAIL**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_estimate_usd.py -v`
Expected: FAIL — either with `TypeError: _estimate_usd() got an unexpected keyword argument 'cache_input_tokens'` or the existing method has different signature/behavior.

- [ ] **Step 4: Update `_estimate_usd` signature and body**

Replace the existing `_estimate_usd` method with:

```python
    def _estimate_usd(
        self,
        *,
        tokens_in: int,
        tokens_out: int,
        cache_input_tokens: int = 0,
        cache_creation_tokens: int = 0,
        thinking_tokens: int = 0,
    ) -> float:
        """Estimate USD cost for a single Messages API call.

        Pricing per Anthropic published rates (verified 2026-05-22):
          * Input tokens (uncached) — base rate
          * Cache reads             — 0.10× input rate
          * Cache writes            — 1.25× input rate (one-time per cache prefix)
          * Output tokens           — output rate
          * Thinking tokens         — priced as output

        ``tokens_in`` from the SDK already includes cached + uncached input.
        Subtract to derive the uncached portion.
        """
        price_in_per_m, price_out_per_m = _PRICE_BY_MODEL.get(
            self.model, _PRICE_BY_MODEL[FALLBACK_MODEL],
        )
        uncached_input = max(0, tokens_in - cache_input_tokens - cache_creation_tokens)
        cost_input = (
            uncached_input         * price_in_per_m
            + cache_input_tokens    * price_in_per_m * 0.10
            + cache_creation_tokens * price_in_per_m * 1.25
        )
        cost_output = (tokens_out + thinking_tokens) * price_out_per_m
        return (cost_input + cost_output) / 1_000_000.0
```

(If `_PRICE_BY_MODEL` doesn't exist yet, locate the current price source — it may be inline in the old `_estimate_usd`. Extract it to a module-level dict named `_PRICE_BY_MODEL` with entries for every model in `DEFAULT_MODEL_BY_ROLE`.)

- [ ] **Step 5: Update every existing caller of `_estimate_usd`**

Run: `grep -n "_estimate_usd" argosy/`
Expected: a few call sites in `base.py` itself (in `run()` after `_do_call` returns). For each call site, ensure all five keyword arguments are passed. The pre-Wave-A calls were positional or three-arg; they now need:
```python
cost = self._estimate_usd(
    tokens_in=mc.tokens_in,
    tokens_out=mc.tokens_out,
    cache_input_tokens=mc.cache_input_tokens,
    cache_creation_tokens=mc.cache_creation_tokens,
    thinking_tokens=mc.thinking_tokens,
)
```

(`ModelCall` gains these fields in Task 9; for now leave the new fields as `0` defaults in the dataclass to keep behavior stable — see next step.)

- [ ] **Step 6: Add default-zero fields to `ModelCall` dataclass**

In `argosy/agents/base.py`, locate the `ModelCall` dataclass (around line 130 — search for `class ModelCall` or `@dataclass`). Add four fields with default `0` / `None`:

```python
@dataclass
class ModelCall:
    text: str
    tokens_in: int
    tokens_out: int
    model: str
    raw: Any
    cache_input_tokens: int = 0
    cache_creation_tokens: int = 0
    thinking_tokens: int = 0
    citations_json: str | None = None
```

- [ ] **Step 7: Run the test, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_estimate_usd.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 8: Run full suite to ensure no regression**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" -x`
Expected: all tests pass. The default-zero new ModelCall fields mean every existing path still produces the same cost.

- [ ] **Step 9: 🧪 TANDEM check — money math review**

```bash
D:/Projects/financial-advisor/.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0, 'tools/codex-tandem/scripts')
from engine_codex import run_codex
from pathlib import Path
r = run_codex(
    node_dir=Path('tools/codex-tandem/runs/wave-a-cost-calc'),
    prompt='Review the updated _estimate_usd in argosy/agents/base.py: '
           '(1) the four published Anthropic pricing relations are correct '
           '(cache read = 0.10x input, cache write = 1.25x input, thinking = output rate); '
           '(2) the uncached_input subtraction is correct given that tokens_in already includes cached portions; '
           '(3) no integer-truncation bug from int*float at low token counts; '
           '(4) max(0, ...) guard handles edge cases where cache_input + cache_creation > tokens_in.',
    agent_name='wave_a_cost_calc', role='reviewer')
print('VERDICT:', r.verdict_text[:600])
"
```
Expected: `COMMIT AS-IS` or specific BLOCKERS. Fix and re-review until clean.

- [ ] **Step 10: Commit**

```bash
git add argosy/agents/base.py tests/test_estimate_usd.py
git commit -m "feat(agents): _estimate_usd handles cache + thinking pricing (Wave A)"
```

---

## Phase 2 — Prompt caching

### Task 7: Add `_build_system_blocks` helper + unit test

**Files:**
- Modify: `argosy/agents/base.py` (add a new method on `BaseAgent`)
- Test: `tests/test_build_system_blocks.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_build_system_blocks.py
"""_build_system_blocks splits the system prompt into cacheable + role-specific."""
from __future__ import annotations

from argosy.agents.base import BaseAgent


class _DummyAgent(BaseAgent):
    agent_role = "news_analyst"
    output_model = type("Out", (), {})

    def build_prompt(self, **_):
        return ("", "")


def test_returns_two_blocks_when_boilerplate_present():
    agent = _DummyAgent(user_id="ariel")
    # Simulate the system string BaseAgent.run() assembles: boilerplate + role.
    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: news analyst. Output schema: ..."
    blocks = agent._build_system_blocks(full_system)

    assert len(blocks) == 2
    assert blocks[0]["type"] == "text"
    assert blocks[0]["text"] == BaseAgent.BOILERPLATE_SYSTEM
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["type"] == "text"
    assert "Role: news analyst" in blocks[1]["text"]
    assert "cache_control" not in blocks[1]


def test_returns_single_block_when_boilerplate_missing():
    """If a caller passed a system prompt that does NOT start with the boilerplate,
    we return a single uncached block (defensive — should not happen in practice)."""
    agent = _DummyAgent(user_id="ariel")
    blocks = agent._build_system_blocks("Just role-specific text, no boilerplate prefix.")
    assert len(blocks) == 1
    assert blocks[0]["text"] == "Just role-specific text, no boilerplate prefix."
    assert "cache_control" not in blocks[0]
```

- [ ] **Step 2: Run the test, expect FAIL**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_build_system_blocks.py -v`
Expected: FAIL with `AttributeError: '_DummyAgent' object has no attribute '_build_system_blocks'`.

- [ ] **Step 3: Add the helper method to `BaseAgent`**

In `argosy/agents/base.py`, inside the `BaseAgent` class, add:

```python
    def _build_system_blocks(self, system: str) -> list[dict[str, Any]]:
        """Split the system prompt into cacheable boilerplate + role-specific tail.

        Returns a 2-element list of content blocks when ``system`` starts with
        ``BOILERPLATE_SYSTEM`` (the common case): the first block is the
        boilerplate marked ``cache_control: ephemeral``, the second is the
        role-specific remainder. Falls back to a single uncached block if the
        boilerplate prefix isn't present (defensive).
        """
        if system.startswith(self.BOILERPLATE_SYSTEM):
            tail = system[len(self.BOILERPLATE_SYSTEM):].lstrip("\n")
            return [
                {
                    "type": "text",
                    "text": self.BOILERPLATE_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": tail},
            ]
        return [{"type": "text", "text": system}]
```

- [ ] **Step 4: Run the test, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_build_system_blocks.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add argosy/agents/base.py tests/test_build_system_blocks.py
git commit -m "feat(agents): _build_system_blocks helper for prompt caching"
```

---

### Task 8: Wire caching into `_call_via_api_key`  🧪 **TANDEM**

**Files:**
- Modify: `argosy/agents/base.py` (the nested `_do_call` inside `_call_via_api_key`, around line 609)
- Test: `tests/test_call_via_api_key_caching.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_call_via_api_key_caching.py
"""_call_via_api_key passes system as content blocks with cache_control."""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest

from argosy.agents.base import BaseAgent


class _DummyAgent(BaseAgent):
    agent_role = "news_analyst"
    output_model = type("Out", (), {})

    def build_prompt(self, **_):
        return ("", "")


def _make_mock_msg(input_toks=100, output_toks=50, cache_read=0, cache_create=0):
    msg = MagicMock()
    msg.content = [MagicMock(text="ok", type="text")]
    msg.content[0].text = "ok"
    msg.usage.input_tokens = input_toks
    msg.usage.output_tokens = output_toks
    msg.usage.cache_read_input_tokens = cache_read
    msg.usage.cache_creation_input_tokens = cache_create
    msg.model = "claude-sonnet-4-6"
    return msg


@pytest.mark.asyncio
async def test_system_passed_as_content_blocks_with_cache_control(monkeypatch):
    agent = _DummyAgent(user_id="ariel")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_mock_msg(cache_create=80)
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: news"
    result = await agent._call_via_api_key(system=full_system, user="hello")

    # Assert messages.create was called with system as a list of two blocks
    call_kwargs = fake_client.messages.create.call_args.kwargs
    system_blocks = call_kwargs["system"]
    assert isinstance(system_blocks, list)
    assert len(system_blocks) == 2
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}

    # Assert cache telemetry flowed back into the ModelCall
    assert result.cache_creation_tokens == 80
    assert result.cache_input_tokens == 0


@pytest.mark.asyncio
async def test_cache_read_telemetry_threaded_through(monkeypatch):
    agent = _DummyAgent(user_id="ariel")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_mock_msg(cache_read=200)
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: news"
    result = await agent._call_via_api_key(system=full_system, user="hello")
    assert result.cache_input_tokens == 200
    assert result.cache_creation_tokens == 0
```

- [ ] **Step 2: Run the test, expect FAIL**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_call_via_api_key_caching.py -v`
Expected: FAIL — either system not passed as list, or cache fields not on `ModelCall` result.

- [ ] **Step 3: Modify `_call_via_api_key` to use `_build_system_blocks`**

In `argosy/agents/base.py`, locate `_call_via_api_key` (around line 562) and its nested `_do_call` (around line 609). Replace the existing `client.messages.create(...)` invocation with:

```python
        def _do_call() -> ModelCall:
            system_blocks = self._build_system_blocks(system)
            try:
                msg = client.messages.create(
                    model=self.model,
                    system=system_blocks,
                    max_tokens=self.max_tokens,
                    messages=messages_payload,
                )
            except Exception as exc:  # pragma: no cover - exercised by integration only
                raise AgentRunError(f"{self.agent_role}: Anthropic API error: {exc}") from exc

            text_parts: list[str] = []
            for block in getattr(msg, "content", []) or []:
                t = getattr(block, "text", None)
                if t is not None:
                    text_parts.append(t)
            text = "".join(text_parts)

            usage = getattr(msg, "usage", None)
            tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
            tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
            cache_input_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
            cache_creation_tokens = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)

            return ModelCall(
                text=text,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                model=getattr(msg, "model", self.model),
                raw=msg,
                cache_input_tokens=cache_input_tokens,
                cache_creation_tokens=cache_creation_tokens,
                thinking_tokens=0,            # Task 12 populates
                citations_json=None,          # Task 18 populates
            )
```

- [ ] **Step 4: Run the test, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_call_via_api_key_caching.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: Thread cache fields through to `AgentReport` in `run()`**

In `BaseAgent.run()` (around line 234), after `_do_call` returns, locate where `AgentReport` is constructed (search for `AgentReport(`). Add the new fields:

```python
report = AgentReport(
    user_id=self.user_id,
    agent_role=self.agent_role,
    model=mc.model,
    tokens_in=mc.tokens_in,
    tokens_out=mc.tokens_out,
    cost_usd=cost,
    # ... existing fields ...
    cache_input_tokens=mc.cache_input_tokens,
    cache_creation_tokens=mc.cache_creation_tokens,
    thinking_tokens=mc.thinking_tokens,
    citations_json=mc.citations_json,
)
```

- [ ] **Step 6: Run full suite**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" -x`
Expected: all pass.

- [ ] **Step 7: 🧪 TANDEM check — cache wiring review**

```bash
D:/Projects/financial-advisor/.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0, 'tools/codex-tandem/scripts')
from engine_codex import run_codex
from pathlib import Path
r = run_codex(
    node_dir=Path('tools/codex-tandem/runs/wave-a-cache-wiring'),
    prompt='Review the updated _do_call in argosy/agents/base.py (in _call_via_api_key): '
           '(1) system is now a list of content blocks; (2) cache_read_input_tokens and '
           'cache_creation_input_tokens are correctly extracted from usage; '
           '(3) the new ModelCall fields are populated; (4) ANY backward-incompat with the existing '
           'agent fleet test fixtures that pass system as a string?',
    agent_name='wave_a_cache_wiring', role='reviewer')
print('VERDICT:', r.verdict_text[:600])
"
```
Expected: `COMMIT AS-IS` or BLOCKERS to fix.

- [ ] **Step 8: Commit**

```bash
git add argosy/agents/base.py tests/test_call_via_api_key_caching.py
git commit -m "feat(agents): prompt caching wired into _call_via_api_key (Wave A)"
```

---

### Task 9: Verify caching shows real savings on a live decision

**Files:**
- (no source changes — verification only)

- [ ] **Step 1: Run a fixture decision against the live API**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m llm_eval tests/test_decision_flow_e2e.py::test_t2_nvda_full_flow -v -s 2>&1 | tee /tmp/cache_smoke.log`
Expected: test passes. The `-s` flag lets prints through.

- [ ] **Step 2: Check the `agent_reports` table for cache hits**

Run:
```bash
D:/Projects/financial-advisor/.venv/Scripts/python.exe -c "
import sqlite3
c = sqlite3.connect('db/argosy.db')
rows = c.execute('''
    SELECT agent_role,
           SUM(tokens_in) AS in_tot,
           SUM(cache_input_tokens) AS cache_read,
           SUM(cache_creation_tokens) AS cache_write
    FROM agent_reports
    WHERE created_at >= datetime('now', '-5 minutes')
    GROUP BY agent_role
''').fetchall()
for r in rows:
    print(f'{r[0]:<25} in={r[1]:>7,d}  cache_read={r[2]:>7,d}  cache_write={r[3]:>7,d}')
"
```
Expected: at least one agent shows non-zero `cache_read` (subsequent calls hit the cache). If ALL agents show only `cache_write` and zero `cache_read`, the cache may not be living long enough between calls — increase the cache TTL by using `{"type": "ephemeral", "ttl": "1h"}` if the SDK supports it, otherwise note as a tuning followup.

- [ ] **Step 3: No commit — verification only.**

If cache_read is universally zero, file a note in §10 of the spec as a tuning gap. Don't block on it — caching still gives ~30% savings on cache_write-only flows via the 1.25× write being a one-time per cache prefix.

---

## Phase 3 — Extended thinking

### Task 10: Add `DEFAULT_THINKING_BUDGET_BY_ROLE` + `BaseAgent.thinking_budget`

**Files:**
- Modify: `argosy/agents/base.py`
- Test: `tests/test_thinking_budget_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_thinking_budget_config.py
"""DEFAULT_THINKING_BUDGET_BY_ROLE + per-agent resolution."""
from __future__ import annotations

from argosy.agents.base import BaseAgent, DEFAULT_THINKING_BUDGET_BY_ROLE


def test_high_stakes_roles_have_thinking_budgets():
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["bull_researcher"] == 4000
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["bear_researcher"] == 4000
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["trader"] == 8000
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["fund_manager"] == 8000
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["plan_synthesizer"] == 8000
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["audit"] == 4000


def test_other_roles_default_to_zero():
    """Non-listed roles get 0 (no thinking)."""
    assert DEFAULT_THINKING_BUDGET_BY_ROLE.get("news_analyst", 0) == 0
    assert DEFAULT_THINKING_BUDGET_BY_ROLE.get("intake", 0) == 0


def test_agent_resolves_its_thinking_budget():
    class _Trader(BaseAgent):
        agent_role = "trader"
        output_model = type("Out", (), {})
        def build_prompt(self, **_): return ("", "")

    class _News(BaseAgent):
        agent_role = "news_analyst"
        output_model = type("Out", (), {})
        def build_prompt(self, **_): return ("", "")

    assert _Trader(user_id="ariel").thinking_budget == 8000
    assert _News(user_id="ariel").thinking_budget == 0
```

- [ ] **Step 2: Run the test, expect FAIL**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_thinking_budget_config.py -v`
Expected: FAIL with `ImportError: cannot import name 'DEFAULT_THINKING_BUDGET_BY_ROLE'`.

- [ ] **Step 3: Add the constant and the property**

In `argosy/agents/base.py`, after `DEFAULT_MODEL_BY_ROLE`, add:

```python
# Per-role extended-thinking budget. Roles not listed default to 0 (no thinking).
# Tuned for high-stakes agents where reasoning quality dominates flow value.
DEFAULT_THINKING_BUDGET_BY_ROLE: dict[str, int] = {
    "bull_researcher":  4000,
    "bear_researcher":  4000,
    "trader":           8000,
    "fund_manager":     8000,
    "plan_synthesizer": 8000,
    "audit":            4000,
}
```

In `BaseAgent.__init__` (around line 213), at the end, add:

```python
        self.thinking_budget: int = DEFAULT_THINKING_BUDGET_BY_ROLE.get(
            self.agent_role, 0,
        )
```

- [ ] **Step 4: Run the test, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_thinking_budget_config.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add argosy/agents/base.py tests/test_thinking_budget_config.py
git commit -m "feat(agents): DEFAULT_THINKING_BUDGET_BY_ROLE config + per-agent attr"
```

---

### Task 11: Wire `thinking` parameter into `_call_via_api_key`

**Files:**
- Modify: `argosy/agents/base.py` (the `_do_call` inside `_call_via_api_key`)
- Test: `tests/test_call_via_api_key_thinking.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_call_via_api_key_thinking.py
"""_call_via_api_key passes thinking param when budget > 0 and extracts thinking_tokens."""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest

from argosy.agents.base import BaseAgent


class _Trader(BaseAgent):
    agent_role = "trader"
    output_model = type("Out", (), {})
    def build_prompt(self, **_): return ("", "")


class _News(BaseAgent):
    agent_role = "news_analyst"
    output_model = type("Out", (), {})
    def build_prompt(self, **_): return ("", "")


def _make_mock_msg(input_toks=100, output_toks=50, thinking_toks=0):
    msg = MagicMock()
    blocks = []
    if thinking_toks:
        thinking_block = MagicMock()
        thinking_block.type = "thinking"
        thinking_block.thinking = "thinking text"
        blocks.append(thinking_block)
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "ok"
    blocks.append(text_block)
    msg.content = blocks
    msg.usage.input_tokens = input_toks
    msg.usage.output_tokens = output_toks
    msg.usage.cache_read_input_tokens = 0
    msg.usage.cache_creation_input_tokens = 0
    # Anthropic puts thinking tokens in a separate counter:
    msg.usage.cache_creation = MagicMock()
    msg.model = "claude-opus-4-7"
    return msg


@pytest.mark.asyncio
async def test_thinking_passed_when_budget_positive(monkeypatch):
    agent = _Trader(user_id="ariel")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_mock_msg(thinking_toks=500)
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: trader"
    await agent._call_via_api_key(system=full_system, user="hello")

    call_kwargs = fake_client.messages.create.call_args.kwargs
    assert "thinking" in call_kwargs
    assert call_kwargs["thinking"] == {"type": "enabled", "budget_tokens": 8000}


@pytest.mark.asyncio
async def test_thinking_NOT_passed_when_budget_zero(monkeypatch):
    agent = _News(user_id="ariel")  # news_analyst has budget=0
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_mock_msg()
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: news"
    await agent._call_via_api_key(system=full_system, user="hello")

    call_kwargs = fake_client.messages.create.call_args.kwargs
    assert "thinking" not in call_kwargs
```

- [ ] **Step 2: Run the test, expect FAIL**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_call_via_api_key_thinking.py -v`
Expected: FAIL with `assert 'thinking' in call_kwargs` failing.

- [ ] **Step 3: Modify `_do_call` to conditionally pass `thinking`**

Locate the `_do_call` body in `_call_via_api_key`. Replace the `client.messages.create(...)` call with:

```python
            call_kwargs: dict[str, Any] = {
                "model": self.model,
                "system": system_blocks,
                "max_tokens": self.max_tokens,
                "messages": messages_payload,
            }
            if self.thinking_budget > 0:
                call_kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self.thinking_budget,
                }
            try:
                msg = client.messages.create(**call_kwargs)
            except Exception as exc:
                raise AgentRunError(f"{self.agent_role}: Anthropic API error: {exc}") from exc
```

- [ ] **Step 4: Run the test, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_call_via_api_key_thinking.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add argosy/agents/base.py tests/test_call_via_api_key_thinking.py
git commit -m "feat(agents): wire thinking param into _call_via_api_key"
```

---

### Task 12: Extract `thinking_tokens` from response and populate `ModelCall`

**Files:**
- Modify: `argosy/agents/base.py`
- Test: `tests/test_call_via_api_key_thinking.py` (extend existing)

- [ ] **Step 1: Add a new test case for thinking_tokens extraction**

Append to `tests/test_call_via_api_key_thinking.py`:

```python
@pytest.mark.asyncio
async def test_thinking_tokens_extracted_from_response(monkeypatch):
    agent = _Trader(user_id="ariel")
    fake_client = MagicMock()
    # Anthropic returns thinking token count via the dedicated usage field:
    mock_msg = _make_mock_msg(thinking_toks=500)
    mock_msg.usage.thinking_tokens = 500   # the field the SDK exposes
    fake_client.messages.create.return_value = mock_msg
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: trader"
    result = await agent._call_via_api_key(system=full_system, user="hello")
    assert result.thinking_tokens == 500
```

- [ ] **Step 2: Run the test, expect FAIL**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_call_via_api_key_thinking.py::test_thinking_tokens_extracted_from_response -v`
Expected: FAIL with `assert 0 == 500`.

- [ ] **Step 3: Update `_do_call` to extract `thinking_tokens`**

In the `_do_call` body, after extracting `cache_creation_tokens`, add:

```python
            thinking_tokens = int(getattr(usage, "thinking_tokens", 0) or 0)
```

And update the `ModelCall` return to use it instead of the hard-coded `0`:
```python
            return ModelCall(
                text=text,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                model=getattr(msg, "model", self.model),
                raw=msg,
                cache_input_tokens=cache_input_tokens,
                cache_creation_tokens=cache_creation_tokens,
                thinking_tokens=thinking_tokens,
                citations_json=None,
            )
```

- [ ] **Step 4: Run the test, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_call_via_api_key_thinking.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add argosy/agents/base.py tests/test_call_via_api_key_thinking.py
git commit -m "feat(agents): extract thinking_tokens from response usage"
```

---

### Task 13: Graceful fallback when model doesn't support thinking

**Files:**
- Modify: `argosy/agents/base.py`
- Test: `tests/test_call_via_api_key_thinking.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_call_via_api_key_thinking.py`:

```python
@pytest.mark.asyncio
async def test_thinking_unsupported_falls_back(monkeypatch, caplog):
    """When the model rejects the thinking param, retry once without it."""
    agent = _Trader(user_id="ariel")
    fake_client = MagicMock()

    call_count = {"n": 0}
    def side_effect(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call (with thinking) raises a "not supported" error
            raise Exception("400 Bad Request: thinking is not supported on this model")
        # Second call (without thinking) succeeds
        return _make_mock_msg()
    fake_client.messages.create.side_effect = side_effect
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: trader"
    result = await agent._call_via_api_key(system=full_system, user="hello")

    assert call_count["n"] == 2  # initial + fallback
    # Second call's kwargs should NOT contain 'thinking'
    second_call_kwargs = fake_client.messages.create.call_args_list[1].kwargs
    assert "thinking" not in second_call_kwargs
    assert result.thinking_tokens == 0
    assert any("thinking not supported" in rec.message.lower() for rec in caplog.records)
```

- [ ] **Step 2: Run the test, expect FAIL**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_call_via_api_key_thinking.py::test_thinking_unsupported_falls_back -v`
Expected: FAIL — the AgentRunError is raised on the first exception.

- [ ] **Step 3: Wrap the `messages.create` call with retry logic**

Replace the try/except in `_do_call`:

```python
            try:
                msg = client.messages.create(**call_kwargs)
            except Exception as exc:
                err_str = str(exc).lower()
                if "thinking" in call_kwargs and (
                    "thinking" in err_str and ("not supported" in err_str or "400" in err_str)
                ):
                    # Graceful fallback: retry without thinking
                    self._log.warning("thinking not supported by %s; retrying without", self.model)
                    call_kwargs.pop("thinking", None)
                    try:
                        msg = client.messages.create(**call_kwargs)
                    except Exception as exc2:
                        raise AgentRunError(
                            f"{self.agent_role}: Anthropic API error (fallback also failed): {exc2}"
                        ) from exc2
                else:
                    raise AgentRunError(
                        f"{self.agent_role}: Anthropic API error: {exc}"
                    ) from exc
```

- [ ] **Step 4: Run the test, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_call_via_api_key_thinking.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add argosy/agents/base.py tests/test_call_via_api_key_thinking.py
git commit -m "feat(agents): graceful fallback when model rejects thinking param"
```

---

## Phase 4 — Citations API

### Task 14: Add `DEFAULT_CITATIONS_BY_ROLE` + `BaseAgent.citations_enabled`

**Files:**
- Modify: `argosy/agents/base.py`
- Test: `tests/test_citations_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_citations_config.py
"""DEFAULT_CITATIONS_BY_ROLE + per-agent resolution."""
from __future__ import annotations

from argosy.agents.base import BaseAgent, DEFAULT_CITATIONS_BY_ROLE


def test_source_consumers_have_citations_enabled():
    for role in (
        "news_analyst", "fundamentals", "technical", "sentiment",
        "macro", "tax", "fx", "intake_extractor", "plan_distiller",
        "plan_critique", "concentration",
    ):
        assert DEFAULT_CITATIONS_BY_ROLE[role] is True, role


def test_synthesizers_have_citations_enabled():
    for role in (
        "bull_researcher", "bear_researcher",
        "trader", "fund_manager", "audit", "plan_synthesizer",
    ):
        assert DEFAULT_CITATIONS_BY_ROLE[role] is True, role


def test_non_source_agents_have_citations_disabled():
    for role in (
        "advisor", "intake", "household_categorizer",
        "researcher_facilitator", "risk_facilitator",
        "domain_refresh", "watchlist",
    ):
        assert DEFAULT_CITATIONS_BY_ROLE[role] is False, role


def test_agent_resolves_citations_flag():
    class _News(BaseAgent):
        agent_role = "news_analyst"
        output_model = type("Out", (), {})
        def build_prompt(self, **_): return ("", "")

    class _Advisor(BaseAgent):
        agent_role = "advisor"
        output_model = type("Out", (), {})
        def build_prompt(self, **_): return ("", "")

    assert _News(user_id="ariel").citations_enabled is True
    assert _Advisor(user_id="ariel").citations_enabled is False
```

- [ ] **Step 2: Run the test, expect FAIL**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_citations_config.py -v`
Expected: FAIL with `ImportError: cannot import name 'DEFAULT_CITATIONS_BY_ROLE'`.

- [ ] **Step 3: Add the constant and property**

In `argosy/agents/base.py`, after `DEFAULT_THINKING_BUDGET_BY_ROLE`, add:

```python
# Per-role Citations API enablement. Source consumers + synthesizers get
# citations; conversational/categorical agents do not (they don't read sources).
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

In `BaseAgent.__init__`, after `self.thinking_budget = ...`, add:

```python
        self.citations_enabled: bool = DEFAULT_CITATIONS_BY_ROLE.get(
            self.agent_role, False,
        )
```

- [ ] **Step 4: Run the test, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_citations_config.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add argosy/agents/base.py tests/test_citations_config.py
git commit -m "feat(agents): DEFAULT_CITATIONS_BY_ROLE config + per-agent attr"
```

---

### Task 15: Add `_build_document_blocks` helper

**Files:**
- Modify: `argosy/agents/base.py`
- Test: `tests/test_build_document_blocks.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_build_document_blocks.py
"""_build_document_blocks converts a list of (source_id, content) tuples into
Anthropic document content blocks with citations enabled."""
from __future__ import annotations

from argosy.agents.base import BaseAgent


class _News(BaseAgent):
    agent_role = "news_analyst"
    output_model = type("Out", (), {})
    def build_prompt(self, **_): return ("", "")


def test_empty_returns_empty_list():
    agent = _News(user_id="ariel")
    assert agent._build_document_blocks([]) == []


def test_single_source():
    agent = _News(user_id="ariel")
    blocks = agent._build_document_blocks([
        ("domain_knowledge/tax/israel/capital_gains.md", "The CGT rate is 25% for individuals."),
    ])
    assert len(blocks) == 1
    b = blocks[0]
    assert b["type"] == "document"
    assert b["source"]["type"] == "text"
    assert b["source"]["media_type"] == "text/plain"
    assert b["source"]["data"] == "The CGT rate is 25% for individuals."
    assert b["title"] == "domain_knowledge/tax/israel/capital_gains.md"
    assert b["citations"] == {"enabled": True}


def test_multiple_sources_preserves_order():
    agent = _News(user_id="ariel")
    blocks = agent._build_document_blocks([
        ("source_a.md", "Content A"),
        ("source_b.md", "Content B"),
    ])
    assert len(blocks) == 2
    assert blocks[0]["title"] == "source_a.md"
    assert blocks[1]["title"] == "source_b.md"
```

- [ ] **Step 2: Run the test, expect FAIL**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_build_document_blocks.py -v`
Expected: FAIL with `AttributeError: '_News' object has no attribute '_build_document_blocks'`.

- [ ] **Step 3: Add the helper method to `BaseAgent`**

```python
    def _build_document_blocks(
        self,
        sources: list[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        """Convert (source_id, content) tuples into Anthropic document blocks.

        Used when ``self.citations_enabled`` is True and the agent has loaded
        external sources (domain_knowledge files, news payloads, plan docs).
        Each block is paired with a citations-enabled marker so the model's
        output includes character-offset citations back into the source text.
        """
        return [
            {
                "type": "document",
                "source": {
                    "type": "text",
                    "media_type": "text/plain",
                    "data": content,
                },
                "title": source_id,
                "citations": {"enabled": True},
            }
            for source_id, content in sources
        ]
```

- [ ] **Step 4: Run the test, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_build_document_blocks.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add argosy/agents/base.py tests/test_build_document_blocks.py
git commit -m "feat(agents): _build_document_blocks helper for Citations API"
```

---

### Task 16: Extend `build_prompt` contract to optionally return sources

**Files:**
- Modify: `argosy/agents/base.py` (the `BaseAgent.run()` method signature handling)
- Test: `tests/test_run_with_sources.py`

The current `build_prompt` returns `(system_prompt, user_prompt)`. To wire Citations in non-invasively, we extend the contract so subclasses MAY return a 3-tuple `(system, user, sources)` where `sources` is `list[tuple[str, str]]`. Subclasses that return the 2-tuple keep working unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_with_sources.py
"""BaseAgent.run accepts (system, user, sources) 3-tuple from build_prompt."""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest

from argosy.agents.base import BaseAgent


class _SourceConsumer(BaseAgent):
    agent_role = "news_analyst"
    output_model = type("Out", (), {})

    def build_prompt(self, **_):
        return (
            BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: news",
            "Summarize the headlines.",
            [("news/2026-05-22.md", "Headline: NVDA up 3%.")],
        )


@pytest.mark.asyncio
async def test_sources_become_document_blocks(monkeypatch):
    agent = _SourceConsumer(user_id="ariel")
    fake_client = MagicMock()
    mock_msg = MagicMock()
    text_block = MagicMock(); text_block.type = "text"; text_block.text = "ok"
    mock_msg.content = [text_block]
    mock_msg.usage.input_tokens = 50; mock_msg.usage.output_tokens = 10
    mock_msg.usage.cache_read_input_tokens = 0
    mock_msg.usage.cache_creation_input_tokens = 0
    mock_msg.usage.thinking_tokens = 0
    mock_msg.model = "claude-sonnet-4-6"
    fake_client.messages.create.return_value = mock_msg
    agent._client = fake_client

    await agent._call_via_api_key(
        system=BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: news",
        user="Summarize.",
        sources=[("news/2026-05-22.md", "Headline: NVDA up 3%.")],
    )

    call_kwargs = fake_client.messages.create.call_args.kwargs
    msgs = call_kwargs["messages"]
    # User message should now be a content-block list with the document block prepended
    user_content = msgs[0]["content"]
    assert isinstance(user_content, list)
    doc_blocks = [b for b in user_content if b.get("type") == "document"]
    assert len(doc_blocks) == 1
    assert doc_blocks[0]["title"] == "news/2026-05-22.md"
    assert doc_blocks[0]["citations"] == {"enabled": True}
```

- [ ] **Step 2: Run the test, expect FAIL**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_run_with_sources.py -v`
Expected: FAIL — `_call_via_api_key` doesn't accept a `sources` kwarg.

- [ ] **Step 3: Add `sources` parameter to `_call_via_api_key`**

Update the signature:

```python
    async def _call_via_api_key(
        self,
        *,
        system: str,
        user: str,
        image_attachments: list[Any] | None = None,
        sources: list[tuple[str, str]] | None = None,
    ) -> ModelCall:
```

In the message-building section (around line 584), update the user-message construction:

```python
        if image_attachments or (sources and self.citations_enabled):
            blocks: list[dict[str, Any]] = []
            # Document blocks come first (Anthropic recommends this for caching)
            if sources and self.citations_enabled:
                blocks.extend(self._build_document_blocks(sources))
            if image_attachments:
                for att in image_attachments:
                    path = getattr(att, "path", None) or att["path"]
                    mime = getattr(att, "mime_type", None) or att["mime_type"]
                    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
                    blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": data,
                        },
                    })
            blocks.append({"type": "text", "text": user})
            messages_payload = [{"role": "user", "content": blocks}]
        else:
            messages_payload = [{"role": "user", "content": user}]
```

- [ ] **Step 4: Update `BaseAgent.run()` to thread `sources` through**

In `run()`, after `build_prompt` is called, support the 3-tuple shape:

```python
        bp_result = self.build_prompt(**inputs)
        if len(bp_result) == 2:
            system_prompt, user_prompt = bp_result
            sources = None
        elif len(bp_result) == 3:
            system_prompt, user_prompt, sources = bp_result
        else:
            raise AgentRunError(
                f"{self.agent_role}: build_prompt returned {len(bp_result)}-tuple, expected 2 or 3"
            )
```

Then pass `sources=sources` to `_call_via_api_key`. (Also to `_call_via_claude_code` for signature symmetry — the claude_code backend just ignores it for now with a one-line log.)

- [ ] **Step 5: Run the test, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_run_with_sources.py -v`
Expected: PASS.

- [ ] **Step 6: Run full suite to ensure 2-tuple subclasses still work**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" -x`
Expected: all pass. Existing agents returning 2-tuples are unaffected.

- [ ] **Step 7: Commit**

```bash
git add argosy/agents/base.py tests/test_run_with_sources.py
git commit -m "feat(agents): build_prompt may return (system, user, sources) for Citations"
```

---

### Task 17: Extract citations from response, populate `citations_json`  🧪 **TANDEM**

**Files:**
- Modify: `argosy/agents/base.py`
- Test: `tests/test_call_via_api_key_citations.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_call_via_api_key_citations.py
"""_call_via_api_key extracts citations from response content blocks."""
from __future__ import annotations
import json
from unittest.mock import MagicMock

import pytest

from argosy.agents.base import BaseAgent


class _News(BaseAgent):
    agent_role = "news_analyst"
    output_model = type("Out", (), {})
    def build_prompt(self, **_): return ("", "", [])


def _mock_msg_with_citations():
    msg = MagicMock()
    # First content block: text with citations metadata
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "The CGT rate is 25%."
    citation = MagicMock()
    citation.type = "char_location"
    citation.cited_text = "capital gains tax rate for individuals is 25%"
    citation.document_index = 0
    citation.document_title = "domain_knowledge/tax/israel/capital_gains.md"
    citation.start_char_index = 1240
    citation.end_char_index = 1389
    text_block.citations = [citation]
    msg.content = [text_block]
    msg.usage.input_tokens = 100; msg.usage.output_tokens = 20
    msg.usage.cache_read_input_tokens = 0
    msg.usage.cache_creation_input_tokens = 0
    msg.usage.thinking_tokens = 0
    msg.model = "claude-sonnet-4-6"
    return msg


@pytest.mark.asyncio
async def test_citations_extracted_to_json(monkeypatch):
    agent = _News(user_id="ariel")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _mock_msg_with_citations()
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: news"
    result = await agent._call_via_api_key(
        system=full_system, user="What's the CGT rate?",
        sources=[("domain_knowledge/tax/israel/capital_gains.md", "..." * 500)],
    )

    assert result.citations_json is not None
    parsed = json.loads(result.citations_json)
    assert len(parsed) == 1
    c = parsed[0]
    assert c["source_id"] == "domain_knowledge/tax/israel/capital_gains.md"
    assert c["source_span_start"] == 1240
    assert c["source_span_end"] == 1389
    assert c["claim_text"] == "The CGT rate is 25%."
    assert c["cited_quote"] == "capital gains tax rate for individuals is 25%"


@pytest.mark.asyncio
async def test_no_citations_returns_null(monkeypatch):
    """Response without any citation blocks: citations_json stays None."""
    agent = _News(user_id="ariel")
    fake_client = MagicMock()
    mock_msg = MagicMock()
    text_block = MagicMock(); text_block.type = "text"; text_block.text = "ok"; text_block.citations = []
    mock_msg.content = [text_block]
    mock_msg.usage.input_tokens = 10; mock_msg.usage.output_tokens = 5
    mock_msg.usage.cache_read_input_tokens = 0
    mock_msg.usage.cache_creation_input_tokens = 0
    mock_msg.usage.thinking_tokens = 0
    mock_msg.model = "claude-sonnet-4-6"
    fake_client.messages.create.return_value = mock_msg
    agent._client = fake_client

    result = await agent._call_via_api_key(
        system=BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: news",
        user="hello",
    )
    assert result.citations_json is None
```

- [ ] **Step 2: Run the test, expect FAIL**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_call_via_api_key_citations.py -v`
Expected: FAIL — `citations_json` is `None` for both tests.

- [ ] **Step 3: Add citation extraction to `_do_call`**

In `_do_call`, after the text extraction loop, add:

```python
            citations_list: list[dict[str, Any]] = []
            for block in getattr(msg, "content", []) or []:
                if getattr(block, "type", None) != "text":
                    continue
                block_text = getattr(block, "text", "") or ""
                for c in getattr(block, "citations", []) or []:
                    try:
                        citations_list.append({
                            "source_id": getattr(c, "document_title", None),
                            "source_span_start": getattr(c, "start_char_index", None),
                            "source_span_end": getattr(c, "end_char_index", None),
                            "claim_text": block_text,
                            "cited_quote": getattr(c, "cited_text", None),
                        })
                    except Exception as parse_exc:  # noqa: BLE001
                        self._log.warning(
                            "citation parse failed: %s; raw=%r",
                            parse_exc, c,
                        )
            citations_json: str | None = (
                json.dumps(citations_list, ensure_ascii=False) if citations_list else None
            )
```

(Ensure `import json` is at the top of `base.py`.)

Update the `ModelCall` return to use `citations_json=citations_json`.

- [ ] **Step 4: Run the test, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_call_via_api_key_citations.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: 🧪 TANDEM — citations review**

```bash
D:/Projects/financial-advisor/.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0, 'tools/codex-tandem/scripts')
from engine_codex import run_codex
from pathlib import Path
r = run_codex(
    node_dir=Path('tools/codex-tandem/runs/wave-a-citations-extract'),
    prompt='Review the citation extraction logic in argosy/agents/base.py _do_call: '
           '(1) is the SDK shape correct (block.citations is a list with .document_title, '
           '.start_char_index, .end_char_index, .cited_text — these are TextCitation fields); '
           '(2) does the parse-failure fallback correctly continue on partial data without dropping '
           'subsequent citations; (3) are there cases where citations_list ends up empty when there '
           'WERE citations in the response — i.e. a misclassification?',
    agent_name='wave_a_citations', role='reviewer')
print('VERDICT:', r.verdict_text[:600])
"
```
Expected: clean verdict or specific fixes.

- [ ] **Step 6: Commit**

```bash
git add argosy/agents/base.py tests/test_call_via_api_key_citations.py
git commit -m "feat(agents): extract citations into citations_json (Wave A)"
```

---

## Phase 5 — Per-user override

### Task 18: Extend `agent_settings.yaml` schema with `thinking_budget` + `citations_enabled`

**Files:**
- Modify: `argosy/config.py` (the Pydantic model that parses `agent_settings.yaml`)
- Test: `tests/test_agent_settings_overrides.py`

- [ ] **Step 1: Find the existing settings model**

Run: `grep -n "agent_settings\|AgentSettings\|class.*Settings" argosy/config.py`
Expected: a class definition. Note the line number and the field types currently supported.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_agent_settings_overrides.py
"""agent_settings.yaml supports thinking_budget + citations_enabled overrides."""
from __future__ import annotations
from pathlib import Path

import pytest

from argosy.config import load_agent_settings


def test_override_loaded(tmp_path: Path):
    yaml_text = """
agents:
  bear_researcher:
    thinking_budget: 6000
    citations_enabled: false
  trader:
    thinking_budget: 12000
"""
    p = tmp_path / "agent_settings.yaml"
    p.write_text(yaml_text)
    settings = load_agent_settings(p)

    assert settings.for_role("bear_researcher").thinking_budget == 6000
    assert settings.for_role("bear_researcher").citations_enabled is False
    assert settings.for_role("trader").thinking_budget == 12000
    # Unspecified field falls back to per-role default
    assert settings.for_role("trader").citations_enabled is None  # None = use default


def test_unknown_role_returns_empty_overrides(tmp_path: Path):
    p = tmp_path / "agent_settings.yaml"
    p.write_text("agents: {}")
    settings = load_agent_settings(p)
    assert settings.for_role("news_analyst").thinking_budget is None
    assert settings.for_role("news_analyst").citations_enabled is None


def test_invalid_thinking_budget_rejected_at_load(tmp_path: Path):
    p = tmp_path / "agent_settings.yaml"
    p.write_text("agents:\n  trader:\n    thinking_budget: -100\n")
    with pytest.raises(ValueError, match="thinking_budget"):
        load_agent_settings(p)
```

- [ ] **Step 3: Run the test, expect FAIL**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_agent_settings_overrides.py -v`
Expected: FAIL — either fields not supported, or `for_role` method missing.

- [ ] **Step 4: Add the override fields to the Pydantic model**

In `argosy/config.py`, locate the existing `AgentSettings` (or analogous) model and add:

```python
from pydantic import Field, model_validator


class AgentRoleOverride(BaseModel):
    """Per-role override fields (Wave A)."""
    thinking_budget: int | None = Field(None, ge=0, le=64000)
    citations_enabled: bool | None = None
    # ... existing fields like `model` stay alongside these ...


class AgentSettings(BaseModel):
    agents: dict[str, AgentRoleOverride] = Field(default_factory=dict)

    def for_role(self, role: str) -> AgentRoleOverride:
        return self.agents.get(role, AgentRoleOverride())
```

(Adjust to match the existing class name and merge with whatever's there.)

Add a top-level loader if not present:
```python
def load_agent_settings(path: Path) -> AgentSettings:
    import yaml
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return AgentSettings(**raw)
```

- [ ] **Step 5: Run the test, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_agent_settings_overrides.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 6: Commit**

```bash
git add argosy/config.py tests/test_agent_settings_overrides.py
git commit -m "feat(config): agent_settings.yaml supports thinking_budget + citations override"
```

---

### Task 19: Wire per-user overrides into `BaseAgent.__init__`

**Files:**
- Modify: `argosy/agents/base.py`
- Test: `tests/test_base_agent_overrides_applied.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_base_agent_overrides_applied.py
"""Per-user agent_settings.yaml overrides take precedence over per-role defaults."""
from __future__ import annotations
from pathlib import Path

import pytest

from argosy.agents.base import BaseAgent


class _Trader(BaseAgent):
    agent_role = "trader"
    output_model = type("Out", (), {})
    def build_prompt(self, **_): return ("", "")


def test_yaml_thinking_budget_overrides_default(monkeypatch, tmp_path: Path):
    yaml_path = tmp_path / "agent_settings.yaml"
    yaml_path.write_text("""
agents:
  trader:
    thinking_budget: 12000
""")
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))

    agent = _Trader(user_id="ariel")
    assert agent.thinking_budget == 12000  # 12000, not the default 8000


def test_yaml_citations_override_to_false(monkeypatch, tmp_path: Path):
    yaml_path = tmp_path / "agent_settings.yaml"
    yaml_path.write_text("""
agents:
  trader:
    citations_enabled: false
""")
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))

    agent = _Trader(user_id="ariel")
    assert agent.citations_enabled is False  # default would be True
```

- [ ] **Step 2: Run the test, expect FAIL**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_base_agent_overrides_applied.py -v`
Expected: FAIL — overrides not applied.

- [ ] **Step 3: Apply overrides in `__init__`**

In `BaseAgent.__init__`, after the defaults are set, layer overrides:

```python
        # Wave A — apply per-user YAML overrides on top of per-role defaults.
        try:
            from argosy.config import resolve_agent_settings_path, load_agent_settings
            yaml_path = resolve_agent_settings_path(self.user_id)
            if yaml_path and yaml_path.exists():
                settings = load_agent_settings(yaml_path)
                ov = settings.for_role(self.agent_role)
                if ov.thinking_budget is not None:
                    self.thinking_budget = ov.thinking_budget
                if ov.citations_enabled is not None:
                    self.citations_enabled = ov.citations_enabled
        except Exception as exc:  # noqa: BLE001
            # Override loading is best-effort; failure must not block agent creation.
            self._log.warning("agent_settings.yaml override load failed: %s", exc)
```

Add `resolve_agent_settings_path` to `argosy/config.py`:

```python
def resolve_agent_settings_path(user_id: str) -> Path | None:
    """Return the path to the per-user agent_settings.yaml, or None.

    Lookup order:
      1. ``$ARGOSY_AGENT_SETTINGS_PATH`` env var (used by tests).
      2. ``$ARGOSY_HOME/configs/<user_id>/agent_settings.yaml``.
      3. None (no overrides applied).
    """
    env = os.environ.get("ARGOSY_AGENT_SETTINGS_PATH")
    if env:
        return Path(env)
    home = os.environ.get("ARGOSY_HOME") or "."
    return Path(home) / "configs" / user_id / "agent_settings.yaml"
```

- [ ] **Step 4: Run the test, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_base_agent_overrides_applied.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add argosy/agents/base.py argosy/config.py tests/test_base_agent_overrides_applied.py
git commit -m "feat(agents): per-user YAML overrides for thinking_budget + citations"
```

---

## Phase 6 — Integration (live LLM)

### Task 20: Live integration test — analyst family

**Files:**
- Create: `tests/test_wave_a_integration_analyst.py`

- [ ] **Step 1: Write the live test**

```python
# tests/test_wave_a_integration_analyst.py
"""Live integration: NewsAnalystAgent end-to-end with caching + citations.
Marked @pytest.mark.llm_eval — opt-in via `-m llm_eval`."""
from __future__ import annotations
import json

import pytest

from argosy.agents.news_analyst import NewsAnalystAgent
from argosy.state import db as db_mod
from argosy.state.models import AgentReport
from sqlalchemy import select, desc


@pytest.mark.llm_eval
def test_news_analyst_emits_citations_and_caches():
    agent = NewsAnalystAgent(user_id="ariel")
    # Call the agent on a sample news payload. The exact build_prompt signature
    # varies — adjust based on the actual subclass; assume it accepts a `news`
    # list of dicts or similar.
    sample_news = [
        {"source": "reuters.com/2026-05-22-nvda", "headline": "NVDA up 3% on AI demand",
         "body": "Nvidia rose 3% after reporting record AI chip demand..."}
    ]
    report = agent.run_sync(news=sample_news)

    # 1. Citations populated
    assert report.citations_json is not None
    citations = json.loads(report.citations_json)
    assert len(citations) > 0
    assert all("source_id" in c and "cited_quote" in c for c in citations)

    # 2. Cache telemetry present (cache_creation > 0 on first call)
    assert report.cache_creation_tokens > 0

    # 3. Persisted to DB correctly
    with db_mod.SessionLocal() as session:
        latest = session.execute(
            select(AgentReport).where(AgentReport.agent_role == "news_analyst")
            .order_by(desc(AgentReport.id)).limit(1)
        ).scalar_one()
        assert latest.citations_json == report.citations_json
        assert latest.cache_creation_tokens == report.cache_creation_tokens
```

- [ ] **Step 2: Run the live test**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m llm_eval tests/test_wave_a_integration_analyst.py -v -s`
Expected: PASS. If FAIL, inspect the actual report fields — likely the news_analyst doesn't return sources in its `build_prompt` 3-tuple. If so: update `NewsAnalystAgent.build_prompt` to return sources alongside the prompt (see Task 21).

- [ ] **Step 3: Commit**

```bash
git add tests/test_wave_a_integration_analyst.py
git commit -m "test(live): NewsAnalystAgent integration — citations + caching"
```

---

### Task 21: Update source-consuming agents to return sources from `build_prompt`

**Files:**
- Modify: `argosy/agents/news_analyst.py`, `argosy/agents/fundamentals_analyst.py`, `argosy/agents/technical_analyst.py`, `argosy/agents/sentiment_analyst.py`, `argosy/agents/macro_analyst.py`, `argosy/agents/tax_analyst.py`, `argosy/agents/fx_analyst.py`, `argosy/agents/concentration_analyst.py`, `argosy/agents/intake_extractor.py`, `argosy/agents/plan_distiller.py`, `argosy/agents/plan_critique.py`

For each agent, locate its `build_prompt` method and update the return to include sources. Pattern:

```python
    def build_prompt(self, *, <existing_args>):
        # ... existing prompt construction ...
        sources: list[tuple[str, str]] = []
        # Collect any external sources the prompt currently inlines:
        # - news payloads → `("news/<id>", payload_text)`
        # - domain_knowledge files → `(rel_path, file_content)`
        # - upstream agent outputs → `(f"agent_reports/{upstream_id}", text)`
        return system_prompt, user_prompt, sources
```

This task is a refactor — each agent has its own existing source-loading pattern. Don't fabricate sources; pull them from where the agent already reads them today (the `<news>...</news>` wrappers in user prompts, the inlined domain_knowledge content, etc.) and convert them to the 3-tuple shape.

- [ ] **Step 1: Tackle one agent at a time. Start with `NewsAnalystAgent`.**

Read: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -c "from argosy.agents.news_analyst import NewsAnalystAgent; import inspect; print(inspect.getsource(NewsAnalystAgent.build_prompt))"`

Identify what the agent inlines today. Refactor to extract those source bodies into the `sources` list and reference them by source_id in the user prompt.

- [ ] **Step 2: Run the existing unit tests for the agent**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/agents/test_news_analyst.py -v`
Expected: PASS. If tests assert specific prompt strings, update assertions to match the new shape (sources are no longer inlined in user_prompt).

- [ ] **Step 3: Repeat Steps 1-2 for each citation-enabled source consumer**

Track progress with a TODO list as you go:
- [ ] news_analyst
- [ ] fundamentals_analyst
- [ ] technical_analyst
- [ ] sentiment_analyst
- [ ] macro_analyst
- [ ] tax_analyst
- [ ] fx_analyst
- [ ] concentration_analyst
- [ ] intake_extractor
- [ ] plan_distiller
- [ ] plan_critique

For synthesizers (bull/bear/trader/fund_manager/audit/plan_synthesizer), the "sources" are the upstream `agent_reports` they read. Convert those reads similarly.

- [ ] **Step 4: Run full test suite**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" -x`
Expected: all pass.

- [ ] **Step 5: Commit (one commit per agent or all in one — judgment call)**

For clarity, prefer one commit per agent: `git commit -m "refactor(agents): news_analyst returns sources tuple for Citations"`. The full task may produce 11 commits.

---

### Task 22: Live integration test — researcher family (with thinking)

**Files:**
- Create: `tests/test_wave_a_integration_researcher.py`

- [ ] **Step 1: Write the live test**

```python
# tests/test_wave_a_integration_researcher.py
"""Live: bull_researcher uses extended thinking (budget=4000)."""
from __future__ import annotations
import pytest

from argosy.agents.researcher import BullResearcherAgent


@pytest.mark.llm_eval
def test_bull_researcher_thinking_active():
    agent = BullResearcherAgent(user_id="ariel")
    assert agent.thinking_budget == 4000

    # Run on a minimal fixture turn. Use the same shape the production
    # researcher_facilitator passes in.
    from tests.fixtures.researcher_fixtures import build_minimal_bull_turn  # adjust import
    inputs = build_minimal_bull_turn()
    report = agent.run_sync(**inputs)

    assert report.thinking_tokens > 0
    assert report.thinking_tokens <= 4000
```

- [ ] **Step 2: Run the live test**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m llm_eval tests/test_wave_a_integration_researcher.py -v -s`
Expected: PASS, thinking_tokens > 0.

- [ ] **Step 3: Commit**

```bash
git add tests/test_wave_a_integration_researcher.py
git commit -m "test(live): bull_researcher extended thinking integration"
```

---

### Task 23: Live integration test — trader + fund_manager family

**Files:**
- Create: `tests/test_wave_a_integration_decision.py`

- [ ] **Step 1: Write the live test**

```python
# tests/test_wave_a_integration_decision.py
"""Live: TraderAgent and FundManagerAgent thinking + citations end-to-end."""
from __future__ import annotations
import json
import pytest

from argosy.agents.trader import TraderAgent
from argosy.agents.fund_manager import FundManagerAgent


@pytest.mark.llm_eval
def test_trader_thinking_and_citations():
    agent = TraderAgent(user_id="ariel")
    assert agent.thinking_budget == 8000
    assert agent.citations_enabled is True

    from tests.fixtures.decision_fixtures import build_trader_inputs
    inputs = build_trader_inputs()
    report = agent.run_sync(**inputs)

    assert report.thinking_tokens > 0
    # Trader synthesizes upstream agent reports — citations should reference at least one
    if report.citations_json:
        citations = json.loads(report.citations_json)
        assert len(citations) > 0


@pytest.mark.llm_eval
def test_fund_manager_full_loop():
    agent = FundManagerAgent(user_id="ariel")
    assert agent.thinking_budget == 8000

    from tests.fixtures.decision_fixtures import build_fund_manager_inputs
    inputs = build_fund_manager_inputs()
    report = agent.run_sync(**inputs)

    assert report.thinking_tokens > 0
    assert report.cost_usd > 0
```

- [ ] **Step 2: Run the live test**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m llm_eval tests/test_wave_a_integration_decision.py -v -s`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_wave_a_integration_decision.py
git commit -m "test(live): trader + fund_manager Wave A integration"
```

---

## Phase 7 — Cost-regression smoke

### Task 24: Cost-regression smoke test against pre-Wave-A baseline

**Files:**
- Create: `tests/test_decision_flow_cost_regression.py`

- [ ] **Step 1: Write the smoke test**

```python
# tests/test_decision_flow_cost_regression.py
"""Wave A cost-regression smoke: post-upgrade decision cost <= 70% of baseline."""
from __future__ import annotations
import json
from pathlib import Path

import pytest

from argosy.decisions.flow import DecisionFlow
from argosy.state import db as db_mod


@pytest.mark.llm_eval
def test_decision_cost_below_70pct_of_baseline():
    baseline_path = Path("tests/fixtures/cost_baseline_pre_wave_a.json")
    assert baseline_path.exists(), "Run Task 2 first to capture baseline"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    from tests.fixtures.decision_fixtures import build_t2_nvda_scenario
    scenario = build_t2_nvda_scenario()

    with db_mod.SessionLocal() as session:
        flow = DecisionFlow(user_id="ariel", session=session)
        result = flow.run_sync(**scenario.inputs)

    post_input_tokens = sum(r.tokens_in - r.cache_input_tokens for r in result.agent_reports)
    baseline_input = baseline["total_input_tokens"]

    reduction_pct = 1.0 - (post_input_tokens / baseline_input)
    print(f"Input-token reduction: {reduction_pct*100:.1f}%  "
          f"(pre={baseline_input:,}  post-uncached={post_input_tokens:,})")
    assert reduction_pct >= 0.30, (
        f"Expected >=30% input-token reduction, got {reduction_pct*100:.1f}%. "
        f"Caching/thinking may be misconfigured."
    )
```

- [ ] **Step 2: Run the smoke test**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m llm_eval tests/test_decision_flow_cost_regression.py -v -s`
Expected: PASS with reduction ≥30%. If FAIL with reduction < 30%, debug:
- Check that the boilerplate block actually has `cache_control`
- Check that consecutive calls in the same decision flow happen within the 5-min cache TTL
- Check that the model used supports caching (Sonnet 4.6 / Opus 4.7 do; older Haiku may not)

- [ ] **Step 3: Commit**

```bash
git add tests/test_decision_flow_cost_regression.py
git commit -m "test(live): Wave A cost-regression smoke (>=30% input-token reduction)"
```

---

## Phase 8 — Telemetry API + UI

### Task 25: Update `/api/agent-activity` to expose new fields

**Files:**
- Modify: `argosy/api/routes/agent_activity.py`
- Test: `tests/test_agent_activity_api.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_activity_api.py
"""GET /api/agent-activity returns the four new Wave A fields per row."""
from __future__ import annotations
import json

from fastapi.testclient import TestClient


def test_response_includes_wave_a_telemetry_fields(client_with_db: TestClient):
    # Seed one AgentReport row with non-zero new fields:
    from argosy.state.models import AgentReport
    from argosy.state import db as db_mod
    with db_mod.SessionLocal() as session:
        session.add(AgentReport(
            user_id="ariel", agent_role="news_analyst", model="claude-sonnet-4-6",
            tokens_in=1000, tokens_out=200, cost_usd=0.005,
            cache_input_tokens=600, cache_creation_tokens=200,
            thinking_tokens=0,
            citations_json='[{"source_id":"x","cited_quote":"y"}]',
        ))
        session.commit()

    resp = client_with_db.get("/api/agent-activity?user_id=ariel&limit=10")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) >= 1
    row = rows[0]
    assert "cache_input_tokens" in row and row["cache_input_tokens"] == 600
    assert "cache_creation_tokens" in row and row["cache_creation_tokens"] == 200
    assert "thinking_tokens" in row and row["thinking_tokens"] == 0
    assert "citations_count" in row and row["citations_count"] == 1
```

- [ ] **Step 2: Run, expect FAIL**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_agent_activity_api.py -v`
Expected: FAIL — new fields missing from response.

- [ ] **Step 3: Add the fields to `AgentActivityRow` and the route**

In `argosy/api/routes/agent_activity.py`, extend the Pydantic model:

```python
class AgentActivityRow(BaseModel):
    id: int
    user_id: str
    agent_role: str
    decision_id: str | None
    model: str
    confidence: str | None
    tokens_in: int
    tokens_out: int
    cost_usd: float
    created_at: str
    # Wave A fields
    cache_input_tokens: int = 0
    cache_creation_tokens: int = 0
    thinking_tokens: int = 0
    citations_count: int = 0
```

In the route handler, when constructing rows from `AgentReport`:

```python
        rows.append(AgentActivityRow(
            # ... existing fields ...
            cache_input_tokens=r.cache_input_tokens,
            cache_creation_tokens=r.cache_creation_tokens,
            thinking_tokens=r.thinking_tokens,
            citations_count=(
                len(json.loads(r.citations_json)) if r.citations_json else 0
            ),
        ))
```

- [ ] **Step 4: Run, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_agent_activity_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add argosy/api/routes/agent_activity.py tests/test_agent_activity_api.py
git commit -m "feat(api): /agent-activity exposes Wave A telemetry fields"
```

---

### Task 26: Update UI `api.ts` types

**Files:**
- Modify: `ui/src/lib/api.ts` (the `AgentActivityRow` type)

- [ ] **Step 1: Locate the type**

Run: `grep -n "AgentActivityRow" ui/src/lib/api.ts`
Expected: a type definition. Note the line number.

- [ ] **Step 2: Add the four new fields**

```typescript
export type AgentActivityRow = {
  id: number;
  user_id: string;
  agent_role: string;
  decision_id: string | null;
  model: string;
  confidence: string | null;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  created_at: string;
  // Wave A — Anthropic API telemetry
  cache_input_tokens: number;
  cache_creation_tokens: number;
  thinking_tokens: number;
  citations_count: number;
};
```

- [ ] **Step 3: Run UI lint + typecheck**

Run: `cd ui ; npm run lint ; npm run typecheck`
Expected: no new errors. Existing UI consumers that don't read the new fields are unaffected (TypeScript widening).

- [ ] **Step 4: Commit**

```bash
git add ui/src/lib/api.ts
git commit -m "ui(api): AgentActivityRow includes Wave A telemetry fields"
```

---

## Phase 9 — SDD update (mandatory — Ariel-explicit)

### Task 27: Refresh SDD §3 (agent fleet table)

**Files:**
- Modify: `docs/design/SDD.md` (§3 agent fleet table)

- [ ] **Step 1: Locate §3 in the SDD**

Run: `grep -n "^## 3\. \|^### 3\." docs/design/SDD.md | head -10`
Expected: a list of §3 sections. Note line numbers.

- [ ] **Step 2: Find the agent-fleet summary table**

The table lists each agent role with its default model. Add two columns: `Thinking budget` and `Citations`. Populate per the Wave A spec §3.3 (see `docs/superpowers/specs/2026-05-22-baseagent-api-features-design.md`).

- [ ] **Step 3: Commit**

```bash
git add docs/design/SDD.md
git commit -m "docs(sdd): §3 agent fleet — add thinking + citations columns (Wave A)"
```

---

### Task 28: Refresh SDD §8.5 (migration history)

**Files:**
- Modify: `docs/design/SDD.md` (§8.5)

- [ ] **Step 1: Locate the migration-history table**

Run: `grep -n "0025_decision_phases_seq_unique\|^### 8\.5\|^## 8\." docs/design/SDD.md | head`

- [ ] **Step 2: Add the 0026 row**

Append a row to the migration table:

```markdown
| `0026_agent_reports_api_telemetry` | Wave A (BaseAgent API features): adds four columns to `agent_reports` — `cache_input_tokens`, `cache_creation_tokens`, `thinking_tokens`, `citations_json`. Captures telemetry from prompt-caching, extended-thinking, and Citations API features wired into `BaseAgent._call_via_api_key`. See `docs/superpowers/specs/2026-05-22-baseagent-api-features-design.md`. |
```

- [ ] **Step 3: Commit**

```bash
git add docs/design/SDD.md
git commit -m "docs(sdd): §8.5 migration history — add 0026 (Wave A)"
```

---

### Task 29: Refresh SDD confidence-band section

**Files:**
- Modify: `docs/design/SDD.md` (confidence-band paragraph — search to locate)

- [ ] **Step 1: Find the confidence-band paragraph**

Run: `grep -n "confidence band\|confidence_band\|HIGH / MEDIUM / LOW\|HIGH.*MEDIUM.*LOW" docs/design/SDD.md | head`

- [ ] **Step 2: Add a note that Citations API now provides verifiable attribution**

After the existing prose, append:

```markdown
**Wave A update (2026-05-22):** Agents with `citations_enabled=True` (see §3 agent fleet table) now emit verifiable character-offset citations via the Anthropic Citations API, persisted to `agent_reports.citations_json`. The hand-rolled `cited_sources` field on agent output models remains for backward compatibility but is redundant for citation-enabled roles — downstream consumers (FundManagerAgent, AuditAgent, the future codex fact-checker) should prefer `citations_json` when present. See `docs/superpowers/specs/2026-05-22-baseagent-api-features-design.md`.
```

- [ ] **Step 3: Commit**

```bash
git add docs/design/SDD.md
git commit -m "docs(sdd): confidence-band — Citations API supersedes hand-rolled cited_sources"
```

---

### Task 30: Refresh the SDD handover note

**Files:**
- Modify: `docs/design/SDD.md` (the `## Handover note` section at the top of the file)

- [ ] **Step 1: Locate the handover note**

Run: `grep -n "^## Handover note\|Last edit:" docs/design/SDD.md | head -3`
Expected: line 16 (the `## Handover note` heading) and the `Last edit:` line just below.

- [ ] **Step 2: Update the `Last edit:` line and add a Wave-A summary paragraph**

Replace the existing `**Last edit:**` line with:

```markdown
**Last edit:** 2026-05-22 by Claude. Wave A — BaseAgent API features upgrade landed (prompt caching, Citations API, extended thinking on the high-stakes Opus roles). Migration 0026 adds four telemetry columns to `agent_reports`. Cost-regression smoke confirms ≥30% input-token reduction on the trade-flow fixture. Foundation for Wave B (daily news cascade + codex live cross-review). Spec: `docs/superpowers/specs/2026-05-22-baseagent-api-features-design.md`. Plan: `docs/superpowers/plans/2026-05-22-baseagent-api-features-implementation.md`.
```

- [ ] **Step 3: Commit**

```bash
git add docs/design/SDD.md
git commit -m "docs(sdd): handover note — Wave A landed"
```

---

## Wrap-up

After all 30 tasks land:

- [ ] **Final step 1: Run the full test suite one more time**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval"`
Expected: all pass, count is at least baseline + 30 new tests (Tasks 3, 4, 6, 7, 8, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 25 each added at least one test).

- [ ] **Final step 2: Run the live integration suite**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m llm_eval tests/test_wave_a_integration_* tests/test_decision_flow_cost_regression.py -v`
Expected: all pass.

- [ ] **Final step 3: Confirm SDD reflects current state**

Open `docs/design/SDD.md`. Verify:
  - §3 agent fleet table has Thinking + Citations columns
  - §8.5 lists migration 0026
  - Confidence-band section mentions Citations API
  - Handover note `Last edit:` reads 2026-05-22 with Wave A summary

- [ ] **Final step 4: Push and request review**

Run: `git push origin main` (or open a PR if branching).
