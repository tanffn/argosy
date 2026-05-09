# Household Expenses & Cash-Flow Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a household expenses subsystem that ingests Leumi bank statements + four credit-card statements, correlates bank credit-card-payment lines to itemized card statements, categorizes transactions (hybrid: issuer-seeded + cache + LLM with confidence ≥ 0.85), detects anomalies, and feeds a `HouseholdBudgetReport` into plan synthesis. This plan covers **EX1 (ingest core)** in full TDD detail; EX2 (anomaly + advisor), EX3 (plan integration), and EX4 (UI) are outlined and will get their own detailed plans when reached.

**Architecture:** Bottom-up TDD. Ground-truth oracle (pandas-only, parser-independent) is built first and becomes the test target every parser must satisfy. Each issuer parser is a pure function. The orchestrator assembles `parse → register source → persist statement → persist transactions → correlate → split refunds → categorize non-refunds → match refunds to prior debits` into one idempotent pipeline keyed on `user_files.id`. The LLM categorizer runs in batches of 50 with a confidence threshold of 0.85; below threshold lands in `expense_review_queue` (built in EX2). Conservation tests (row-count exact, debit/credit sums within ₪1, parsed total within ₪50 of issuer-declared) are non-negotiable and run on every real sample.

**Tech Stack:** Python 3.12, SQLAlchemy 2 (typed `Mapped[T]`) + alembic, FastAPI, pydantic v2, pandas + openpyxl + lxml (HTML-as-xls for Leumi), pytest with `pytest -m "not llm_eval"` as the default, Claude Agent SDK (Sonnet for `household_categorizer`), structlog.

**Spec:** `docs/superpowers/specs/2026-05-09-household-expenses-design.md` — read it first; this plan implements §3–§16 and §17.1 of that spec.

**Source samples** (gitignored; tests opt-in via env var):
- `D:\Google Drive\Family\Finances\Portfolio\Resources\2025\Leumi\leumi_2025_Osh.xls`
- `D:\Google Drive\Family\Finances\Portfolio\Resources\2026\Leumi\leumi_2026_May_Osh.xls`
- `D:\Google Drive\Family\Finances\Portfolio\Resources\2025\1266\1266_*_2025.xlsx` (12 months)
- `D:\Google Drive\Family\Finances\Portfolio\Resources\2026\1266\1266_*_2026.xlsx` (4 months)
- `D:\Google Drive\Family\Finances\Portfolio\Resources\2025\6225\<Mon>.xlsx` (12 months)
- `D:\Google Drive\Family\Finances\Portfolio\Resources\2026\6225\<Mon>.xlsx` (5 months)

The conservation test sets `ARGOSY_EXPENSE_SAMPLES_ROOT=D:\Google Drive\Family\Finances\Portfolio\Resources` and `pytest.skip`s when unset — so CI without the data is fine, but the developer machine that has the data MUST pass.

---

## Files this wave (EX1) creates or modifies

**Create:**
- `alembic/versions/0021_household_expenses.py`
- `argosy/services/expense_ingest/__init__.py`
- `argosy/services/expense_ingest/types.py`
- `argosy/services/expense_ingest/normalize.py`
- `argosy/services/expense_ingest/sniff.py`
- `argosy/services/expense_ingest/parsers/__init__.py`
- `argosy/services/expense_ingest/parsers/leumi_osh.py`
- `argosy/services/expense_ingest/parsers/isracard.py`
- `argosy/services/expense_ingest/parsers/max.py`
- `argosy/services/expense_ingest/parsers/cal.py` (stub — `NotImplementedError`)
- `argosy/services/expense_ingest/parsers/amex.py` (stub)
- `argosy/services/expense_ingest/parsers/diners.py` (stub)
- `argosy/services/expense_ingest/issuer_seed.py`
- `argosy/services/expense_ingest/registry.py`
- `argosy/services/expense_ingest/persistence.py`
- `argosy/services/expense_ingest/correlator.py`
- `argosy/services/expense_ingest/refund_matcher.py`
- `argosy/services/expense_ingest/category_resolver.py`
- `argosy/services/expense_ingest/orchestrator.py`
- `argosy/services/expense_ingest/taxonomy_seed.py`
- `argosy/agents/household_categorizer.py`
- `argosy/agents/household_categorizer_types.py`
- `argosy/api/routes/expenses.py`
- `argosy/cli/expenses_admin.py`
- `tests/expense_ground_truth.py` (utility, not `test_*`)
- `tests/test_migration_0021.py`
- `tests/test_expense_normalize.py`
- `tests/test_expense_sniff.py`
- `tests/test_expense_parsers_unit.py`
- `tests/test_expense_parsers_ground_truth.py`
- `tests/test_expense_registry.py`
- `tests/test_expense_persistence.py`
- `tests/test_expense_correlator.py`
- `tests/test_expense_refund_matcher.py`
- `tests/test_expense_category_resolver.py`
- `tests/test_expense_orchestrator.py`
- `tests/test_expense_pipeline_invariants.py`
- `tests/test_expense_routes.py`
- `tests/test_household_categorizer_e2e.py` (`@pytest.mark.llm_eval`)
- `tests/fixtures/expenses/leumi_osh_minimal.xls`
- `tests/fixtures/expenses/isracard_minimal.xlsx`
- `tests/fixtures/expenses/max_minimal.xlsx`

**Modify:**
- `argosy/state/models.py` — add 6 ORM classes
- `argosy/agents/base.py` — `DEFAULT_MODEL_BY_ROLE['household_categorizer'] = 'sonnet'`
- `argosy/api/main.py` — register the new router
- `argosy/api/events.py` — register new event names in the docstring
- `argosy/cli/__init__.py` — wire `expenses` subcommand
- `configs/<user_id>/agent_settings.yaml` (sample) — `expenses` block
- `pyproject.toml` — add `lxml` if not already present (needed for `pd.read_html`)
- `.gitignore` — add `tests/fixtures/expenses/__samples_root` symlink (if used)

---

## Conventions worth knowing before you start

- **`BaseAgent.__init__` requires `user_id` as a kwarg.** Tests instantiate `HouseholdCategorizerAgent(user_id="ariel")`. The original specs forgot this on every wave; don't repeat.
- **Migrations are linear.** Latest is `0020_decision_phases`. New revision is `0021_household_expenses` with `down_revision = "0020_decision_phases"`.
- **DB-backed tests use `alembic_engine_at_head`** (defined in `tests/conftest.py`); FastAPI-backed use `client_with_db`. Don't reinvent these.
- **Live LLM tests** mark with `@pytest.mark.llm_eval` and gate via `argosy.agents.base._llm_backend_available()`. Default suite is `pytest -m "not llm_eval"`.
- **Sync↔async bridging:** when a sync function needs to emit a WebSocket event, use `argosy.api.events.publish_event_threadsafe(name, payload)`.
- **catalog_upload is the single byte-blob entry.** Every uploaded statement flows through `argosy/services/file_catalog.py::catalog_upload`. Don't write a parallel ingest path.
- **Idempotency is not optional.** The orchestrator runs on `user_files.id`; re-running on the same `id` produces zero new rows.
- **Run python in the repo via** `D:/Projects/financial-advisor/.venv/Scripts/python.exe` (the venv is preinstalled). Tests run via `pytest` from the project root (the venv's bin is on PATH after `activate`).
- **PowerShell quirks:** `&&` doesn't chain; use `;` or `if ($?) { ... }`. Bash via Git-Bash works for POSIX scripts.

---

## Task list — EX1 (Ingest Core)

Tasks are sequential within a phase but a few are independent and can be parallelized. The phases are:

- **Phase A — Schema** (Tasks 1–3)
- **Phase B — Pure functions** (Tasks 4–9): types, normalize, ground-truth oracle, three parsers, sniff
- **Phase C — Stateful glue** (Tasks 10–13): registry, persistence, correlator, refund matcher
- **Phase D — Categorization** (Tasks 14–17): issuer seed map, agent, resolver, orchestrator
- **Phase E — Surfaces** (Tasks 18–25): REST routes, WS events, CLIs, config
- **Phase F — Verification** (Tasks 26–30): pipeline invariants, ground-truth on real samples, live LLM eval, end-to-end backfill smoke, agent_settings + register router + commit

---

## Phase A — Schema

### Task 1: Migration 0021 — `0021_household_expenses` (6 new tables)

**Files:**
- Create: `alembic/versions/0021_household_expenses.py`
- Create: `tests/test_migration_0021.py`

- [ ] **Step 1: Write the failing schema test**

Create `tests/test_migration_0021.py`:

```python
"""Schema assertions after migration 0021 (household expenses, 6 tables)."""

from sqlalchemy import inspect


def _columns(engine, table):
    insp = inspect(engine)
    return {c["name"]: c for c in insp.get_columns(table)}


def _indexes(engine, table):
    insp = inspect(engine)
    return {i["name"]: i for i in insp.get_indexes(table)}


def test_0021_creates_expense_sources(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "expense_sources")
    for name in ("id", "user_id", "kind", "issuer", "external_id",
                 "display_name", "cardholder_name", "active", "created_at"):
        assert name in cols, f"expense_sources missing column {name}"
    assert cols["user_id"]["nullable"] is False
    assert cols["cardholder_name"]["nullable"] is True


def test_0021_creates_expense_statements(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "expense_statements")
    for name in ("id", "user_id", "source_id", "file_id", "period_start",
                 "period_end", "charge_date", "declared_total_nis",
                 "parsed_total_nis", "parser_name", "parser_version",
                 "status", "parse_error", "ingested_at"):
        assert name in cols, f"expense_statements missing column {name}"


def test_0021_creates_expense_transactions(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "expense_transactions")
    for name in ("id", "user_id", "statement_id", "source_id",
                 "occurred_on", "posted_on", "merchant_raw",
                 "merchant_normalized", "amount_nis", "amount_orig",
                 "currency_orig", "direction", "tx_type", "reference",
                 "category_id", "category_source", "category_confidence",
                 "is_card_payment", "matched_statement_id", "refund_of_id",
                 "raw_row_json", "ingested_at"):
        assert name in cols, f"expense_transactions missing column {name}"


def test_0021_creates_expense_categories(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "expense_categories")
    for name in ("id", "user_id", "slug", "label_en", "label_he",
                 "parent_id", "is_excluded_from_spend", "is_inflow",
                 "display_order"):
        assert name in cols
    assert cols["user_id"]["nullable"] is True  # NULL = system-default rows


def test_0021_creates_merchant_category_cache(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "merchant_category_cache")
    for name in ("id", "user_id", "merchant_pattern", "is_regex",
                 "category_id", "source", "confidence", "hit_count",
                 "last_hit_at", "created_at"):
        assert name in cols


def test_0021_creates_expense_review_queue(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "expense_review_queue")
    for name in ("id", "user_id", "kind", "status", "payload_json",
                 "related_tx_id", "related_source_id", "user_note",
                 "created_at", "resolved_at"):
        assert name in cols


def test_0021_indexes_are_present(alembic_engine_at_head):
    tx_idx = _indexes(alembic_engine_at_head, "expense_transactions")
    have = set(tx_idx.keys())
    assert any("occurred_on" in n for n in have), \
        f"expected occurred_on index on expense_transactions; have {have}"
    assert any("merchant_normalized" in n for n in have)
    cache_idx = _indexes(alembic_engine_at_head, "merchant_category_cache")
    assert any("merchant_pattern" in n for n in cache_idx.keys())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_migration_0021.py -v`
Expected: All seven tests FAIL — tables don't exist.

- [ ] **Step 3: Write the migration**

Create `alembic/versions/0021_household_expenses.py`:

```python
"""household expenses subsystem (Wave EX1 — six new tables).

Revision ID: 0021_household_expenses
Revises: 0020_decision_phases
Create Date: 2026-05-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_household_expenses"
down_revision: str | None = "0020_decision_phases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "expense_sources",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),         # bank | card
        sa.Column("issuer", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("cardholder_name", sa.String(128), nullable=True),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "kind", "external_id",
                            name="uq_expense_sources_user_kind_external"),
    )

    op.create_table(
        "expense_statements",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source_id", sa.Integer,
                  sa.ForeignKey("expense_sources.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("file_id", sa.Integer,
                  sa.ForeignKey("user_files.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("period_end", sa.Date, nullable=False),
        sa.Column("charge_date", sa.Date, nullable=True),
        sa.Column("declared_total_nis", sa.Numeric(12, 2), nullable=True),
        sa.Column("parsed_total_nis", sa.Numeric(12, 2), nullable=False),
        sa.Column("parser_name", sa.String(32), nullable=False),
        sa.Column("parser_version", sa.String(16), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),       # parsed | failed | partial
        sa.Column("parse_error", sa.Text, nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "source_id", "period_start", "period_end",
                            name="uq_expense_statements_user_source_period"),
    )
    op.create_index("ix_expense_statements_user_period_end",
                    "expense_statements", ["user_id", "period_end"])

    op.create_table(
        "expense_categories",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=True),                                # NULL = system-default
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("label_en", sa.String(64), nullable=False),
        sa.Column("label_he", sa.String(64), nullable=False),
        sa.Column("parent_id", sa.Integer,
                  sa.ForeignKey("expense_categories.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("is_excluded_from_spend", sa.Boolean, nullable=False,
                  server_default=sa.false()),
        sa.Column("is_inflow", sa.Boolean, nullable=False,
                  server_default=sa.false()),
        sa.Column("display_order", sa.Integer, nullable=False,
                  server_default="0"),
        sa.UniqueConstraint("user_id", "slug", name="uq_expense_categories_user_slug"),
    )

    op.create_table(
        "expense_transactions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("statement_id", sa.Integer,
                  sa.ForeignKey("expense_statements.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source_id", sa.Integer,
                  sa.ForeignKey("expense_sources.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("occurred_on", sa.Date, nullable=False),
        sa.Column("posted_on", sa.Date, nullable=True),
        sa.Column("merchant_raw", sa.String(512), nullable=False),
        sa.Column("merchant_normalized", sa.String(512), nullable=False),
        sa.Column("amount_nis", sa.Numeric(12, 2), nullable=False),
        sa.Column("amount_orig", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency_orig", sa.String(3), nullable=True),
        sa.Column("direction", sa.String(8), nullable=False),    # debit | credit
        sa.Column("tx_type", sa.String(16), nullable=False),     # regular | standing_order | installment | refund
        sa.Column("reference", sa.String(64), nullable=True),
        sa.Column("category_id", sa.Integer,
                  sa.ForeignKey("expense_categories.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("category_source", sa.String(32), nullable=True),
        sa.Column("category_confidence", sa.Numeric(3, 2), nullable=True),
        sa.Column("is_card_payment", sa.Boolean, nullable=False,
                  server_default=sa.false()),
        sa.Column("matched_statement_id", sa.Integer,
                  sa.ForeignKey("expense_statements.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("refund_of_id", sa.Integer,
                  sa.ForeignKey("expense_transactions.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("raw_row_json", sa.Text, nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_expense_transactions_user_occurred_on",
                    "expense_transactions", ["user_id", "occurred_on"])
    op.create_index("ix_expense_transactions_user_merchant_normalized",
                    "expense_transactions", ["user_id", "merchant_normalized"])
    op.create_index("ix_expense_transactions_user_category_occurred_on",
                    "expense_transactions",
                    ["user_id", "category_id", "occurred_on"])

    op.create_table(
        "merchant_category_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("merchant_pattern", sa.String(512), nullable=False),
        sa.Column("is_regex", sa.Boolean, nullable=False,
                  server_default=sa.false()),
        sa.Column("category_id", sa.Integer,
                  sa.ForeignKey("expense_categories.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source", sa.String(16), nullable=False),      # issuer_seed | llm | user
        sa.Column("confidence", sa.Numeric(3, 2), nullable=False),
        sa.Column("hit_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "merchant_pattern", "is_regex",
                            name="uq_merchant_category_cache"),
    )
    op.create_index("ix_merchant_category_cache_user_merchant_pattern",
                    "merchant_category_cache",
                    ["user_id", "merchant_pattern"])

    op.create_table(
        "expense_review_queue",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="open"),                         # open | acknowledged | resolved | dismissed
        sa.Column("payload_json", sa.Text, nullable=False),
        sa.Column("related_tx_id", sa.Integer,
                  sa.ForeignKey("expense_transactions.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("related_source_id", sa.Integer,
                  sa.ForeignKey("expense_sources.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("user_note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_expense_review_queue_user_status_created",
                    "expense_review_queue",
                    ["user_id", "status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_expense_review_queue_user_status_created",
                  table_name="expense_review_queue")
    op.drop_table("expense_review_queue")
    op.drop_index("ix_merchant_category_cache_user_merchant_pattern",
                  table_name="merchant_category_cache")
    op.drop_table("merchant_category_cache")
    op.drop_index("ix_expense_transactions_user_category_occurred_on",
                  table_name="expense_transactions")
    op.drop_index("ix_expense_transactions_user_merchant_normalized",
                  table_name="expense_transactions")
    op.drop_index("ix_expense_transactions_user_occurred_on",
                  table_name="expense_transactions")
    op.drop_table("expense_transactions")
    op.drop_table("expense_categories")
    op.drop_index("ix_expense_statements_user_period_end",
                  table_name="expense_statements")
    op.drop_table("expense_statements")
    op.drop_table("expense_sources")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_migration_0021.py -v`
Expected: All seven tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add alembic/versions/0021_household_expenses.py tests/test_migration_0021.py
git commit -m "feat(db): migration 0021 — six tables for household expenses subsystem"
```

---

### Task 2: ORM models for the six tables

**Files:**
- Modify: `argosy/state/models.py` (append six classes)
- Test: `tests/test_migration_0021.py` (already covers schema; add ORM-round-trip)

- [ ] **Step 1: Write a failing ORM round-trip test**

Append to `tests/test_migration_0021.py`:

```python
def test_0021_orm_round_trip(alembic_engine_at_head):
    """Insert + read each new ORM class to confirm models match the schema."""
    from sqlalchemy.orm import Session
    from datetime import date

    from argosy.state.models import (
        ExpenseSource, ExpenseStatement, ExpenseTransaction,
        ExpenseCategory, MerchantCategoryCache, ExpenseReviewQueue,
        User, UserFile,
    )

    with Session(alembic_engine_at_head) as s:
        s.add(User(id="ariel", plan="free"))
        s.flush()
        s.add(UserFile(
            user_id="ariel", sha256="a" * 64,
            original_name="x.xls", sanitized_name="x.xls",
            mime_type="application/vnd.ms-excel", kind="other",
            size_bytes=1, storage_path="/tmp/x.xls", source="chat_attachment",
        ))
        s.flush()
        cat = ExpenseCategory(slug="food.groceries", label_en="Groceries",
                              label_he="מצרכי מזון")
        s.add(cat)
        s.flush()
        src = ExpenseSource(user_id="ariel", kind="card", issuer="isracard",
                            external_id="1266", display_name="Isracard 1266",
                            cardholder_name="ariel")
        s.add(src)
        s.flush()
        stmt = ExpenseStatement(
            user_id="ariel", source_id=src.id, file_id=1,
            period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
            charge_date=date(2026, 4, 15), parsed_total_nis=3319.44,
            parser_name="isracard", parser_version="0.1.0", status="parsed",
        )
        s.add(stmt)
        s.flush()
        tx = ExpenseTransaction(
            user_id="ariel", statement_id=stmt.id, source_id=src.id,
            occurred_on=date(2026, 4, 8), merchant_raw="NETFLIX.COM",
            merchant_normalized="netflix.com", amount_nis=69.90,
            direction="debit", tx_type="standing_order", raw_row_json="{}",
        )
        s.add(tx)
        s.add(MerchantCategoryCache(
            user_id="ariel", merchant_pattern="netflix.com",
            category_id=cat.id, source="user", confidence=1.00,
        ))
        s.add(ExpenseReviewQueue(
            user_id="ariel", kind="uncategorized",
            payload_json='{"merchant_normalized": "x"}',
        ))
        s.commit()
        assert s.query(ExpenseTransaction).count() == 1
        assert s.query(ExpenseReviewQueue).filter_by(status="open").count() == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_migration_0021.py::test_0021_orm_round_trip -v`
Expected: ImportError (classes not yet defined).

- [ ] **Step 3: Add the six ORM classes**

Append to `argosy/state/models.py` (after the last existing class):

```python
class ExpenseSource(Base):
    """A bank account or credit card the user has registered for expense ingest.

    Cardholder is metadata only — household aggregation is the unit; spend rolls
    to a single pool regardless of `cardholder_name`. ``kind`` distinguishes
    bank current accounts from credit cards; ``external_id`` is the card last-4
    or bank account number, stable across months.
    """

    __tablename__ = "expense_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    issuer: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    cardholder_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "kind", "external_id",
                         name="uq_expense_sources_user_kind_external"),
    )


class ExpenseStatement(Base):
    """A single uploaded statement file's metadata. Idempotent on
    (user_id, source_id, period_start, period_end).
    """

    __tablename__ = "expense_statements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("expense_sources.id", ondelete="CASCADE"),
        nullable=False
    )
    file_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user_files.id", ondelete="RESTRICT"),
        nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    charge_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    declared_total_nis: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    parsed_total_nis: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    parser_name: Mapped[str] = mapped_column(String(32), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False)
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "source_id", "period_start", "period_end",
                         name="uq_expense_statements_user_source_period"),
    )


class ExpenseCategory(Base):
    """Hierarchical taxonomy. user_id NULL = system-default row (copied per user
    on first ingest). is_excluded_from_spend marks rows that render but don't
    aggregate as 'real spending' (transfers, investments, taxes paid).
    """

    __tablename__ = "expense_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    label_en: Mapped[str] = mapped_column(String(64), nullable=False)
    label_he: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("expense_categories.id", ondelete="SET NULL"),
        nullable=True
    )
    is_excluded_from_spend: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_inflow: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("user_id", "slug", name="uq_expense_categories_user_slug"),
    )


class ExpenseTransaction(Base):
    """One transaction row, persisted from a parsed statement.

    Aggregation rules:
      real_spending(month) = SUM(amount_nis) WHERE direction='debit'
                             AND category.is_excluded_from_spend = FALSE
                             AND category.is_inflow = FALSE
                             AND is_card_payment = FALSE
      real_income(month)   = SUM(amount_nis) WHERE direction='credit'
                             AND category.is_inflow = TRUE
    Refunds offset within their inherited category via refund_of_id.
    """

    __tablename__ = "expense_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    statement_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("expense_statements.id", ondelete="CASCADE"),
        nullable=False
    )
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("expense_sources.id", ondelete="CASCADE"),
        nullable=False
    )
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    posted_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    merchant_raw: Mapped[str] = mapped_column(String(512), nullable=False)
    merchant_normalized: Mapped[str] = mapped_column(String(512), nullable=False)
    amount_nis: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    amount_orig: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency_orig: Mapped[str | None] = mapped_column(String(3), nullable=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    tx_type: Mapped[str] = mapped_column(String(16), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("expense_categories.id", ondelete="SET NULL"),
        nullable=True
    )
    category_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    category_confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    is_card_payment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    matched_statement_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("expense_statements.id", ondelete="SET NULL"),
        nullable=True
    )
    refund_of_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("expense_transactions.id", ondelete="SET NULL"),
        nullable=True
    )
    raw_row_json: Mapped[str] = mapped_column(Text, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class MerchantCategoryCache(Base):
    """Per-user cache mapping a normalized merchant pattern to a category.
    User overrides (source='user') always win; LLM results (source='llm')
    only persist when confidence ≥ 0.85.
    """

    __tablename__ = "merchant_category_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    merchant_pattern: Mapped[str] = mapped_column(String(512), nullable=False)
    is_regex: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("expense_categories.id", ondelete="CASCADE"),
        nullable=False
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_hit_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "merchant_pattern", "is_regex",
                         name="uq_merchant_category_cache"),
    )


class ExpenseReviewQueue(Base):
    """Anomalies + uncategorized rows pending user review.
    Built by the anomaly detector (EX2) and the orchestrator (EX1, for
    uncategorized rows).
    """

    __tablename__ = "expense_review_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    related_tx_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("expense_transactions.id", ondelete="SET NULL"),
        nullable=True
    )
    related_source_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("expense_sources.id", ondelete="SET NULL"),
        nullable=True
    )
    user_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

If `Decimal`, `Date`, `Numeric`, `Boolean`, or `Text` aren't already imported at the top of `models.py`, add them:

```python
from decimal import Decimal
from datetime import date  # if not already imported
from sqlalchemy import Boolean, Date, Numeric, Text  # extend the existing tuple
```

- [ ] **Step 4: Run the round-trip test**

Run: `pytest tests/test_migration_0021.py -v`
Expected: All eight tests (seven schema + one round-trip) PASS.

- [ ] **Step 5: Commit**

```powershell
git add argosy/state/models.py tests/test_migration_0021.py
git commit -m "feat(orm): six SQLAlchemy classes for household expenses subsystem"
```

---

### Task 3: Default-taxonomy seed (system-default rows + per-user copy)

**Files:**
- Create: `argosy/services/expense_ingest/__init__.py` (empty package marker)
- Create: `argosy/services/expense_ingest/taxonomy_seed.py`
- Test: `tests/test_expense_taxonomy_seed.py`

- [ ] **Step 1: Create the package directory**

```powershell
New-Item -ItemType Directory -Path argosy/services/expense_ingest
New-Item -ItemType File -Path argosy/services/expense_ingest/__init__.py
```

(or `mkdir` + `touch` in Bash)

- [ ] **Step 2: Write failing seed tests**

Create `tests/test_expense_taxonomy_seed.py`:

```python
"""Tests for default expense-category taxonomy seeding."""

from __future__ import annotations

from sqlalchemy.orm import Session

from argosy.services.expense_ingest.taxonomy_seed import (
    DEFAULT_TAXONOMY,
    seed_system_defaults,
    seed_user_categories,
)
from argosy.state.models import ExpenseCategory, User


def test_default_taxonomy_has_required_top_levels():
    slugs = {entry.slug for entry in DEFAULT_TAXONOMY}
    # Top-level rules from the spec §4.2
    for s in ("food.groceries", "dining_out.restaurants",
              "income.salary", "transfers.internal_transfer",
              "investments.broker_buy_us", "uncategorized"):
        assert s in slugs, f"taxonomy missing slug {s}"


def test_food_is_groceries_only():
    """Per user direction: 'restaurants should not be under food'."""
    food_children = {e.slug for e in DEFAULT_TAXONOMY
                     if e.slug.startswith("food.")}
    assert food_children == {"food.groceries"}, food_children


def test_dining_out_is_top_level_with_restaurants():
    do_slugs = {e.slug for e in DEFAULT_TAXONOMY
                if e.slug.startswith("dining_out.")}
    assert "dining_out.restaurants" in do_slugs
    assert "dining_out.takeout" in do_slugs


def test_excluded_categories_marked_correctly():
    by_slug = {e.slug: e for e in DEFAULT_TAXONOMY}
    for s in ("transfers.internal_transfer",
              "investments.broker_buy_us",
              "investments.retirement_contrib",
              "taxes.income_tax_paid"):
        assert by_slug[s].is_excluded_from_spend, f"{s} should be excluded"
    assert by_slug["food.groceries"].is_excluded_from_spend is False


def test_inflow_categories_marked_correctly():
    by_slug = {e.slug: e for e in DEFAULT_TAXONOMY}
    for s in ("income.salary", "income.rsu_vest_proceeds",
              "income.bonus", "income.child_benefit",
              "income.interest_credit", "income.other_recurring_income"):
        assert by_slug[s].is_inflow
    assert by_slug["food.groceries"].is_inflow is False


def test_seed_system_defaults_is_idempotent(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        seed_system_defaults(s)
        s.commit()
        n1 = s.query(ExpenseCategory).filter(
            ExpenseCategory.user_id.is_(None)).count()
        seed_system_defaults(s)
        s.commit()
        n2 = s.query(ExpenseCategory).filter(
            ExpenseCategory.user_id.is_(None)).count()
        assert n1 == n2 == len(DEFAULT_TAXONOMY)


def test_seed_user_categories_copies_from_defaults(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        s.add(User(id="ariel", plan="free"))
        seed_system_defaults(s)
        s.commit()
        seed_user_categories(s, "ariel")
        s.commit()
        n_user = s.query(ExpenseCategory).filter_by(user_id="ariel").count()
        n_sys = s.query(ExpenseCategory).filter(
            ExpenseCategory.user_id.is_(None)).count()
        assert n_user == n_sys
        # Re-running is a noop
        seed_user_categories(s, "ariel")
        s.commit()
        assert s.query(ExpenseCategory).filter_by(user_id="ariel").count() == n_user
```

- [ ] **Step 3: Run to verify it fails**

Run: `pytest tests/test_expense_taxonomy_seed.py -v`
Expected: ImportError on `argosy.services.expense_ingest.taxonomy_seed`.

- [ ] **Step 4: Implement the seeder**

Create `argosy/services/expense_ingest/taxonomy_seed.py`:

```python
"""Default household-expense taxonomy + seeding helpers.

The taxonomy is system-default (user_id=NULL) on first run; per-user copies
are made lazily by ``seed_user_categories`` on first ingest. Per-user rows
let the user customize labels without touching defaults shared by other tenants.

Aggregation rules (canonical, used by /api/expenses/monthly-summary):
    real_spending(month) = SUM(amount_nis) WHERE direction='debit'
                           AND is_excluded_from_spend = FALSE
                           AND is_inflow = FALSE
                           AND is_card_payment = FALSE
    real_income(month)   = SUM(amount_nis) WHERE direction='credit'
                           AND is_inflow = TRUE
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from argosy.state.models import ExpenseCategory


@dataclass(frozen=True)
class TaxonomyEntry:
    slug: str
    label_en: str
    label_he: str
    parent_slug: str | None = None
    is_excluded_from_spend: bool = False
    is_inflow: bool = False
    display_order: int = 0


DEFAULT_TAXONOMY: list[TaxonomyEntry] = [
    # === INFLOWS ===
    TaxonomyEntry("income", "Income", "הכנסות",
                  is_inflow=True, display_order=0),
    TaxonomyEntry("income.salary", "Salary", "משכורת", "income",
                  is_inflow=True, display_order=1),
    TaxonomyEntry("income.rsu_vest_proceeds", "RSU vest proceeds",
                  "תמורת מימוש RSU", "income", is_inflow=True, display_order=2),
    TaxonomyEntry("income.bonus", "Bonus", "בונוס", "income",
                  is_inflow=True, display_order=3),
    TaxonomyEntry("income.child_benefit", "Child benefit",
                  "קצבת ילדים", "income", is_inflow=True, display_order=4),
    TaxonomyEntry("income.interest_credit", "Interest credited",
                  "ריבית זכות", "income", is_inflow=True, display_order=5),
    TaxonomyEntry("income.other_recurring_income", "Other recurring",
                  "הכנסה שוטפת אחרת", "income", is_inflow=True, display_order=6),

    # === HOUSING ===
    TaxonomyEntry("housing", "Housing", "דיור", display_order=10),
    TaxonomyEntry("housing.mortgage", "Mortgage", "משכנתא", "housing",
                  display_order=11),
    TaxonomyEntry("housing.property_tax", "Property tax (arnona)",
                  "ארנונה", "housing", display_order=12),
    TaxonomyEntry("housing.utilities_electric", "Electricity", "חשמל",
                  "housing", display_order=13),
    TaxonomyEntry("housing.utilities_water_gas", "Water & gas",
                  "מים וגז", "housing", display_order=14),
    TaxonomyEntry("housing.internet_phone", "Internet & phone",
                  "אינטרנט וטלפון", "housing", display_order=15),
    TaxonomyEntry("housing.home_maintenance", "Home maintenance",
                  "תחזוקת בית", "housing", display_order=16),
    TaxonomyEntry("housing.furniture", "Furniture", "ריהוט", "housing",
                  display_order=17),

    # === FOOD (groceries only — restaurants live under dining_out) ===
    TaxonomyEntry("food", "Food (groceries)", "מזון (מצרכים)",
                  display_order=20),
    TaxonomyEntry("food.groceries", "Groceries", "מצרכי מזון", "food",
                  display_order=21),

    # === DINING OUT (top-level, NOT under food) ===
    TaxonomyEntry("dining_out", "Dining out", "אכילה בחוץ", display_order=22),
    TaxonomyEntry("dining_out.restaurants", "Restaurants",
                  "מסעדות", "dining_out", display_order=23),
    TaxonomyEntry("dining_out.takeout", "Takeout", "טייק אווי",
                  "dining_out", display_order=24),
    TaxonomyEntry("dining_out.coffee_bars", "Coffee/bars",
                  "בתי קפה ובארים", "dining_out", display_order=25),

    # === TRANSPORTATION ===
    TaxonomyEntry("transportation", "Transportation", "תחבורה",
                  display_order=30),
    TaxonomyEntry("transportation.fuel", "Fuel", "דלק", "transportation",
                  display_order=31),
    TaxonomyEntry("transportation.public_transit", "Public transit",
                  "תחבורה ציבורית", "transportation", display_order=32),
    TaxonomyEntry("transportation.parking", "Parking", "חניה",
                  "transportation", display_order=33),
    TaxonomyEntry("transportation.car_insurance", "Car insurance",
                  "ביטוח רכב", "transportation", display_order=34),
    TaxonomyEntry("transportation.car_maintenance", "Car maintenance",
                  "תחזוקת רכב", "transportation", display_order=35),
    TaxonomyEntry("transportation.taxi_rideshare", "Taxi / rideshare",
                  "מונית/שיתופי-נסיעה", "transportation", display_order=36),

    # === HEALTHCARE ===
    TaxonomyEntry("healthcare", "Healthcare", "בריאות", display_order=40),
    TaxonomyEntry("healthcare.health_insurance", "Health insurance",
                  "ביטוח בריאות", "healthcare", display_order=41),
    TaxonomyEntry("healthcare.pharmacy", "Pharmacy", "בית מרקחת",
                  "healthcare", display_order=42),
    TaxonomyEntry("healthcare.dental", "Dental", "טיפולי שיניים",
                  "healthcare", display_order=43),
    TaxonomyEntry("healthcare.doctors", "Doctors", "רופאים", "healthcare",
                  display_order=44),
    TaxonomyEntry("healthcare.medical_other", "Medical other",
                  "רפואה אחר", "healthcare", display_order=45),

    # === INSURANCE OTHER ===
    TaxonomyEntry("insurance_other", "Other insurance", "ביטוחים אחרים",
                  display_order=50),
    TaxonomyEntry("insurance_other.life", "Life", "ביטוח חיים",
                  "insurance_other", display_order=51),
    TaxonomyEntry("insurance_other.home", "Home", "ביטוח דירה",
                  "insurance_other", display_order=52),
    TaxonomyEntry("insurance_other.umbrella", "Umbrella", "ביטוח-על",
                  "insurance_other", display_order=53),
    TaxonomyEntry("insurance_other.other", "Other", "אחר",
                  "insurance_other", display_order=54),

    # === CHILDCARE / EDUCATION ===
    TaxonomyEntry("childcare_education", "Childcare & education",
                  "טיפול בילדים וחינוך", display_order=60),
    TaxonomyEntry("childcare_education.daycare", "Daycare", "מעון/גן",
                  "childcare_education", display_order=61),
    TaxonomyEntry("childcare_education.tuition", "Tuition", "שכר לימוד",
                  "childcare_education", display_order=62),
    TaxonomyEntry("childcare_education.after_school", "After school",
                  "צהרון/חוגים", "childcare_education", display_order=63),
    TaxonomyEntry("childcare_education.education_materials",
                  "Education materials", "ציוד לימודי",
                  "childcare_education", display_order=64),
    TaxonomyEntry("childcare_education.kids_activities",
                  "Kids activities", "פעילויות ילדים",
                  "childcare_education", display_order=65),

    # === SUBSCRIPTIONS ===
    TaxonomyEntry("subscriptions", "Subscriptions", "מנויים",
                  display_order=70),
    TaxonomyEntry("subscriptions.streaming", "Streaming", "סטרימינג",
                  "subscriptions", display_order=71),
    TaxonomyEntry("subscriptions.software", "Software", "תוכנה",
                  "subscriptions", display_order=72),
    TaxonomyEntry("subscriptions.gym", "Gym", "חדר כושר",
                  "subscriptions", display_order=73),
    TaxonomyEntry("subscriptions.news", "News", "חדשות",
                  "subscriptions", display_order=74),
    TaxonomyEntry("subscriptions.other_subscription", "Other subscription",
                  "מנוי אחר", "subscriptions", display_order=75),

    # === DISCRETIONARY ===
    TaxonomyEntry("discretionary", "Discretionary", "הוצאות בחירה",
                  display_order=80),
    TaxonomyEntry("discretionary.shopping_clothing", "Clothing",
                  "לבוש והנעלה", "discretionary", display_order=81),
    TaxonomyEntry("discretionary.shopping_other", "Shopping (other)",
                  "קניות אחרות", "discretionary", display_order=82),
    TaxonomyEntry("discretionary.entertainment", "Entertainment",
                  "בידור", "discretionary", display_order=83),
    TaxonomyEntry("discretionary.hobbies", "Hobbies", "תחביבים",
                  "discretionary", display_order=84),
    TaxonomyEntry("discretionary.gifts_to_others", "Gifts",
                  "מתנות לאחרים", "discretionary", display_order=85),
    TaxonomyEntry("discretionary.charity", "Charity", "צדקה",
                  "discretionary", display_order=86),

    # === TRAVEL ===
    TaxonomyEntry("travel", "Travel", "נסיעות", display_order=90),
    TaxonomyEntry("travel.flights", "Flights", "טיסות", "travel",
                  display_order=91),
    TaxonomyEntry("travel.hotels", "Hotels", "מלונות", "travel",
                  display_order=92),
    TaxonomyEntry("travel.vacation_other", "Vacation (other)",
                  "חופשה (אחר)", "travel", display_order=93),

    # === PERSONAL ===
    TaxonomyEntry("personal", "Personal", "אישי", display_order=100),
    TaxonomyEntry("personal.personal_care", "Personal care",
                  "טיפוח אישי", "personal", display_order=101),

    # === FINANCIAL (fees only — interest income is in income.*) ===
    TaxonomyEntry("financial", "Financial fees", "עמלות פיננסיות",
                  display_order=110),
    TaxonomyEntry("financial.bank_fees", "Bank fees", "עמלות בנק",
                  "financial", display_order=111),
    TaxonomyEntry("financial.fx_fees", "FX fees", "עמלות מט\"ח",
                  "financial", display_order=112),
    TaxonomyEntry("financial.interest_paid_other", "Interest paid",
                  "ריבית חובה", "financial", display_order=113),

    # === EXCLUDED FROM SPEND ===
    TaxonomyEntry("transfers", "Transfers", "העברות",
                  is_excluded_from_spend=True, display_order=200),
    TaxonomyEntry("transfers.internal_transfer", "Internal transfer",
                  "העברה פנימית", "transfers",
                  is_excluded_from_spend=True, display_order=201),
    TaxonomyEntry("transfers.paybox_to_household", "PayBox to household",
                  "פייבוקס למשק בית", "transfers",
                  is_excluded_from_spend=True, display_order=202),
    TaxonomyEntry("transfers.atm_cash_withdrawal", "ATM cash withdrawal",
                  "משיכת מזומן", "transfers",
                  is_excluded_from_spend=True, display_order=203),

    TaxonomyEntry("investments", "Investments", "השקעות",
                  is_excluded_from_spend=True, display_order=210),
    TaxonomyEntry("investments.broker_buy_us", "Broker buy (US)",
                  "קנייה ברוקר חו\"ל", "investments",
                  is_excluded_from_spend=True, display_order=211),
    TaxonomyEntry("investments.broker_buy_il", "Broker buy (IL)",
                  "קנייה ברוקר ישראלי", "investments",
                  is_excluded_from_spend=True, display_order=212),
    TaxonomyEntry("investments.retirement_contrib", "Retirement contribution",
                  "הפקדה לפנסיה", "investments",
                  is_excluded_from_spend=True, display_order=213),
    TaxonomyEntry("investments.keren_hishtalmut_contrib",
                  "Keren hishtalmut contribution",
                  "הפקדה לקרן השתלמות", "investments",
                  is_excluded_from_spend=True, display_order=214),
    TaxonomyEntry("investments.savings_deposit", "Savings deposit",
                  "פקדון/חיסכון", "investments",
                  is_excluded_from_spend=True, display_order=215),

    TaxonomyEntry("taxes", "Taxes", "מסים",
                  is_excluded_from_spend=True, display_order=220),
    TaxonomyEntry("taxes.income_tax_paid", "Income tax paid",
                  "תשלום מס הכנסה", "taxes",
                  is_excluded_from_spend=True, display_order=221),
    TaxonomyEntry("taxes.social_security_paid", "Social security paid",
                  "תשלום ביטוח לאומי", "taxes",
                  is_excluded_from_spend=True, display_order=222),

    # === SPECIAL ===
    TaxonomyEntry("uncategorized", "Uncategorized", "לא מסווג",
                  display_order=900),
]


def seed_system_defaults(session: Session) -> None:
    """Idempotent: insert one row per TaxonomyEntry as user_id=NULL."""
    existing = {
        c.slug for c in session.query(ExpenseCategory).filter(
            ExpenseCategory.user_id.is_(None)
        ).all()
    }
    by_slug: dict[str, ExpenseCategory] = {}
    # First pass — top-level rows; SQLAlchemy needs IDs flushed before children
    for entry in DEFAULT_TAXONOMY:
        if entry.parent_slug is None and entry.slug not in existing:
            cat = ExpenseCategory(
                user_id=None, slug=entry.slug,
                label_en=entry.label_en, label_he=entry.label_he,
                is_excluded_from_spend=entry.is_excluded_from_spend,
                is_inflow=entry.is_inflow,
                display_order=entry.display_order,
            )
            session.add(cat)
            by_slug[entry.slug] = cat
    session.flush()
    for c in session.query(ExpenseCategory).filter(
        ExpenseCategory.user_id.is_(None)
    ).all():
        by_slug[c.slug] = c
    # Second pass — children
    for entry in DEFAULT_TAXONOMY:
        if entry.parent_slug is not None and entry.slug not in existing:
            parent = by_slug[entry.parent_slug]
            cat = ExpenseCategory(
                user_id=None, slug=entry.slug,
                label_en=entry.label_en, label_he=entry.label_he,
                parent_id=parent.id,
                is_excluded_from_spend=entry.is_excluded_from_spend,
                is_inflow=entry.is_inflow,
                display_order=entry.display_order,
            )
            session.add(cat)


def seed_user_categories(session: Session, user_id: str) -> None:
    """Copy system-default categories into user-scoped rows. Idempotent."""
    existing = {
        c.slug for c in session.query(ExpenseCategory).filter_by(
            user_id=user_id
        ).all()
    }
    if existing:
        # If the user already has any rows, assume seeded; the cache test
        # (n_user == n_sys after re-run) will catch a mid-stream gap.
        return
    sys_rows = session.query(ExpenseCategory).filter(
        ExpenseCategory.user_id.is_(None)
    ).order_by(ExpenseCategory.display_order).all()
    sys_by_id: dict[int, ExpenseCategory] = {c.id: c for c in sys_rows}
    new_by_slug: dict[str, ExpenseCategory] = {}
    # Top-level
    for c in sys_rows:
        if c.parent_id is None:
            user_c = ExpenseCategory(
                user_id=user_id, slug=c.slug,
                label_en=c.label_en, label_he=c.label_he,
                is_excluded_from_spend=c.is_excluded_from_spend,
                is_inflow=c.is_inflow, display_order=c.display_order,
            )
            session.add(user_c)
            new_by_slug[c.slug] = user_c
    session.flush()
    # Children
    for c in sys_rows:
        if c.parent_id is not None:
            parent_slug = sys_by_id[c.parent_id].slug
            user_c = ExpenseCategory(
                user_id=user_id, slug=c.slug,
                label_en=c.label_en, label_he=c.label_he,
                parent_id=new_by_slug[parent_slug].id,
                is_excluded_from_spend=c.is_excluded_from_spend,
                is_inflow=c.is_inflow, display_order=c.display_order,
            )
            session.add(user_c)
            new_by_slug[c.slug] = user_c
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_expense_taxonomy_seed.py -v`
Expected: All seven tests PASS.

- [ ] **Step 6: Commit**

```powershell
git add argosy/services/expense_ingest/__init__.py argosy/services/expense_ingest/taxonomy_seed.py tests/test_expense_taxonomy_seed.py
git commit -m "feat(expenses): default taxonomy + system/user seeding (food vs dining_out split per spec)"
```

---

## Phase B — Pure functions

### Task 4: Pipeline pydantic types

**Files:**
- Create: `argosy/services/expense_ingest/types.py`
- Test: `tests/test_expense_types.py`

- [ ] **Step 1: Write the type tests**

Create `tests/test_expense_types.py`:

```python
"""Pydantic types for the expense-ingest pipeline."""

from datetime import date

import pytest

from argosy.services.expense_ingest.types import (
    NormalizedTransaction, ParseResult, StatementMeta, SourceHint,
    GroundTruth, ParserName,
)


def test_normalized_transaction_minimal():
    tx = NormalizedTransaction(
        occurred_on=date(2026, 4, 8),
        merchant_raw="NETFLIX.COM",
        merchant_normalized="netflix.com",
        amount_nis=69.90,
        direction="debit",
        tx_type="standing_order",
        raw_row={"foo": "bar"},
    )
    assert tx.amount_nis == 69.90


def test_direction_is_constrained():
    with pytest.raises(Exception):
        NormalizedTransaction(
            occurred_on=date(2026, 4, 8),
            merchant_raw="x", merchant_normalized="x",
            amount_nis=1, direction="something",  # invalid
            tx_type="regular", raw_row={},
        )


def test_tx_type_is_constrained():
    with pytest.raises(Exception):
        NormalizedTransaction(
            occurred_on=date(2026, 4, 8),
            merchant_raw="x", merchant_normalized="x",
            amount_nis=1, direction="debit",
            tx_type="bogus",  # invalid
            raw_row={},
        )


def test_parse_result_round_trip():
    txs = [NormalizedTransaction(
        occurred_on=date(2026, 4, 8),
        merchant_raw="x", merchant_normalized="x",
        amount_nis=10, direction="debit", tx_type="regular",
        raw_row={},
    )]
    pr = ParseResult(
        statement=StatementMeta(
            period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
            charge_date=date(2026, 4, 15),
            declared_total_nis=10, parsed_total_nis=10,
        ),
        transactions=txs,
    )
    assert pr.statement.declared_total_nis == 10
    assert len(pr.transactions) == 1


def test_ground_truth_optional_declared():
    gt = GroundTruth(row_count=5, sum_debits_nis=100, sum_credits_nis=0,
                     declared_total_nis=None)
    assert gt.declared_total_nis is None


def test_parser_name_enum_values():
    assert ParserName.LEUMI_OSH == "leumi_osh"
    assert ParserName.ISRACARD == "isracard"
    assert ParserName.MAX == "max"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_expense_types.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the types**

Create `argosy/services/expense_ingest/types.py`:

```python
"""Shared pydantic types for the expense-ingest pipeline.

Parsers return ``ParseResult``; the orchestrator persists those into
``ExpenseStatement`` + ``ExpenseTransaction`` ORM rows. ``GroundTruth`` is
the parser-independent oracle (used only by tests in §17.1 of the spec).
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ParserName(StrEnum):
    LEUMI_OSH = "leumi_osh"
    ISRACARD = "isracard"
    MAX = "max"
    CAL = "cal"
    AMEX = "amex"
    DINERS = "diners"


Direction = Literal["debit", "credit"]
TxType = Literal["regular", "standing_order", "installment", "refund"]
SourceKind = Literal["bank", "card"]


class NormalizedTransaction(BaseModel):
    """One transaction, parser-output. Persistence is in the orchestrator."""

    occurred_on: date
    posted_on: date | None = None
    merchant_raw: str
    merchant_normalized: str
    amount_nis: float                          # always positive
    amount_orig: float | None = None
    currency_orig: str | None = None           # 'USD' / 'EUR' / None
    direction: Direction
    tx_type: TxType
    reference: str | None = None
    issuer_category: str | None = None         # raw ענף when issuer provides one
    raw_row: dict[str, Any] = Field(default_factory=dict)


class StatementMeta(BaseModel):
    period_start: date
    period_end: date
    charge_date: date | None = None            # 'לחיוב ב-' for cards
    declared_total_nis: float | None = None    # issuer-stated footer total
    parsed_total_nis: float                     # sum of our parsed rows


class SourceHint(BaseModel):
    """Parser's best guess at which source the file is from. Used by the
    orchestrator to register the source on first sight (or match an existing
    one). Not all parsers can fill all fields.
    """

    kind: SourceKind
    issuer: str                                  # 'leumi' | 'isracard' | 'max' | …
    external_id: str                             # last-4 (cards) / account # (banks)
    cardholder_name: str | None = None
    display_name: str | None = None              # may be filled by orchestrator


class ParseResult(BaseModel):
    statement: StatementMeta
    transactions: list[NormalizedTransaction]
    source_hint: SourceHint | None = None       # None for parsers that can't infer


class GroundTruth(BaseModel):
    """Parser-independent ground truth — computed directly from raw cells.

    See ``tests/expense_ground_truth.py`` for the per-issuer oracle functions.
    """

    row_count: int
    sum_debits_nis: float
    sum_credits_nis: float
    declared_total_nis: float | None
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_expense_types.py -v`
Expected: All six tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add argosy/services/expense_ingest/types.py tests/test_expense_types.py
git commit -m "feat(expenses): pydantic types for ingest pipeline"
```

---

### Task 5: Merchant normalization

**Files:**
- Create: `argosy/services/expense_ingest/normalize.py`
- Test: `tests/test_expense_normalize.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_expense_normalize.py`:

```python
"""Merchant-name normalization (key for merchant_category_cache lookups)."""

import pytest

from argosy.services.expense_ingest.normalize import normalize


@pytest.mark.parametrize("inp,out", [
    ("NETFLIX.COM", "netflix.com"),
    ("  שופרסל בע\"מ  ", "שופרסל בע\"מ"),       # trim only
    ("PAYPAL *VENDOR_X", "vendor_x"),            # foreign prefix stripped
    ("SQ *Coffee Shop", "coffee shop"),
    ("WWW.NAME-CHEAP.COM*ABCD", "name-cheap.com"),  # WWW prefix + trailing-id
    ("מלאנוקס טכנו-י", "מלאנוקס טכנו"),          # Leumi -י suffix stripped
    ("רמי לוי תשלום 3/12", "רמי לוי"),            # installment marker stripped
    ("ביט שלם 1 מתוך 6", "ביט"),                  # alt installment marker
    ("multiple   spaces   here", "multiple spaces here"),
])
def test_normalize_examples(inp, out):
    assert normalize(inp) == out


def test_normalize_handles_empty():
    assert normalize("") == ""
    assert normalize("   ") == ""


def test_normalize_handles_unicode_nfkc():
    # Composed vs decomposed Hebrew should normalize the same
    composed = "שלום"
    # NFKC normalization is mostly transparent for Hebrew; assert idempotent
    assert normalize(composed) == normalize(normalize(composed))


def test_normalize_does_not_strip_short_digits():
    # Pure-digit blocks of length < 4 are kept (e.g., 'CARREFOUR 24')
    assert normalize("CARREFOUR 24") == "carrefour 24"


def test_normalize_strips_long_trailing_digit_block():
    assert normalize("VENDOR 12345") == "vendor"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_expense_normalize.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement normalization**

Create `argosy/services/expense_ingest/normalize.py`:

```python
"""Merchant-name normalization. Idempotent. The output is the cache key."""

from __future__ import annotations

import re
import unicodedata

# Hebrew installment markers. Both phrasings show up across issuers.
_INSTALLMENT_HE = re.compile(r"\bתשלום\s+\d+\s*(?:/|מתוך|מ-)\s*\d+\b")
_INSTALLMENT_MORE = re.compile(r"\bשלם\s+\d+\s+מתוך\s+\d+\b")

# Cards sometimes append a 4+-digit transaction sequence to the merchant string.
_TRAILING_DIGITS = re.compile(r"\s+\d{4,}\s*$")

# Leumi current-account abbreviates merchants and tags them with '-י'
_LEUMI_SUFFIX = re.compile(r"-י\s*$")

# Foreign-merchant prefixes used by acquirers (PayPal, Square, etc.)
_FOREIGN_PREFIX = re.compile(
    r"^(?:PAYPAL|SQ|SP|TST|WWW(?:\.[A-Z0-9-]+)?)\s*\*?\s*",
    re.IGNORECASE,
)


def normalize(s: str) -> str:
    """Normalize a merchant string into a stable cache key.

    Lowercases (Latin only — Hebrew has no case), strips installment markers,
    trailing transaction-sequence digit blocks (≥4 digits only),
    Leumi's '-י' suffix, foreign-acquirer prefixes, and excess whitespace.

    Idempotent: ``normalize(normalize(s)) == normalize(s)``.
    """
    if s is None:
        return ""
    s = s.strip()
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = _INSTALLMENT_HE.sub("", s)
    s = _INSTALLMENT_MORE.sub("", s)
    s = _FOREIGN_PREFIX.sub("", s)
    s = _LEUMI_SUFFIX.sub("", s)
    s = _TRAILING_DIGITS.sub("", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_expense_normalize.py -v`
Expected: All ~12 parametrized cases PASS.

- [ ] **Step 5: Commit**

```powershell
git add argosy/services/expense_ingest/normalize.py tests/test_expense_normalize.py
git commit -m "feat(expenses): merchant-name normalization (Hebrew + Latin + foreign prefixes)"
```

---

### Task 6: Ground-truth oracle (parser-independent)

This is the critical TDD-foundation task. The oracle is what every parser must satisfy. Build it BEFORE any parser. Per spec §17.1.1 — pandas-only, three issuer-specific functions.

**Files:**
- Create: `tests/expense_ground_truth.py` (utility, not collected as a test)
- Test: `tests/test_expense_ground_truth.py` (verifies the oracle itself by running it on real samples and asserting plausible-output)

- [ ] **Step 1: Write the failing oracle test**

Create `tests/test_expense_ground_truth.py`:

```python
"""Sanity tests for the parser-independent ground-truth oracle.

These tests skip if ARGOSY_EXPENSE_SAMPLES_ROOT is not set, so CI without
the data passes silently. On a developer machine with the data present
they MUST pass — the oracle is foundational.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.expense_ground_truth import (
    leumi_oracle, isracard_oracle, max_oracle,
)

SAMPLES = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")
pytestmark = pytest.mark.skipif(
    not SAMPLES, reason="ARGOSY_EXPENSE_SAMPLES_ROOT unset"
)


def _samples_root() -> Path:
    return Path(SAMPLES)


def test_leumi_oracle_runs_on_2026_may():
    p = _samples_root() / "2026" / "Leumi" / "leumi_2026_May_Osh.xls"
    if not p.exists():
        pytest.skip(f"sample missing: {p}")
    gt = leumi_oracle(p)
    assert gt.row_count > 0
    assert gt.sum_debits_nis > 0
    # Bank statements have no declared footer total
    assert gt.declared_total_nis is None


def test_isracard_oracle_runs_on_card_1266_apr_2026():
    p = _samples_root() / "2026" / "1266" / "1266_04_2026.xlsx"
    if not p.exists():
        pytest.skip(f"sample missing: {p}")
    gt = isracard_oracle(p)
    assert gt.row_count > 0
    assert gt.sum_debits_nis > 0
    assert gt.declared_total_nis is not None
    # The footer total must reconcile with our independent column-sum:
    diff = abs(gt.sum_debits_nis - gt.sum_credits_nis - gt.declared_total_nis)
    assert diff < 50.00, (
        f"Isracard oracle: column sums {gt.sum_debits_nis} debit "
        f"{gt.sum_credits_nis} credit do not reconcile to declared "
        f"{gt.declared_total_nis} (diff {diff})"
    )


def test_max_oracle_runs_on_card_6225_apr_2026():
    p = _samples_root() / "2026" / "6225" / "Apr.xlsx"
    if not p.exists():
        pytest.skip(f"sample missing: {p}")
    gt = max_oracle(p)
    assert gt.row_count > 0
    assert gt.sum_debits_nis > 0
    assert gt.declared_total_nis is not None
    diff = abs(gt.sum_debits_nis - gt.sum_credits_nis - gt.declared_total_nis)
    assert diff < 50.00


def test_isracard_april_2026_has_known_total():
    """Hard-coded sanity: this exact file's footer total is 3319.44 NIS."""
    p = _samples_root() / "2026" / "1266" / "1266_04_2026.xlsx"
    if not p.exists():
        pytest.skip(f"sample missing: {p}")
    gt = isracard_oracle(p)
    assert abs(gt.declared_total_nis - 3319.44) < 0.01, (
        f"declared total drifted from known 3319.44; got {gt.declared_total_nis}"
    )


def test_max_april_2026_has_known_total():
    """Hard-coded sanity: this exact file's footer total is 654.88 NIS."""
    p = _samples_root() / "2026" / "6225" / "Apr.xlsx"
    if not p.exists():
        pytest.skip(f"sample missing: {p}")
    gt = max_oracle(p)
    assert abs(gt.declared_total_nis - 654.88) < 0.01
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_expense_ground_truth.py -v`
Expected: ImportError on `tests.expense_ground_truth`.

- [ ] **Step 3: Implement the oracle**

Create `tests/expense_ground_truth.py`:

```python
"""Parser-independent ground-truth oracle for expense statement files.

Reads the raw spreadsheet cells via pandas alone — completely unaware of
``argosy.services.expense_ingest``. The conservation tests in
``tests/test_expense_parsers_ground_truth.py`` use these functions as the
source of truth: parser output must match within tolerance.

If this module has a bug, it must be obvious from reading. Keep it simple.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class GroundTruth:
    row_count: int
    sum_debits_nis: float
    sum_credits_nis: float
    declared_total_nis: float | None


_NIS_NUM = re.compile(r"[-+]?[\d,]+\.?\d*")


def _to_float(x) -> float:
    """Robust 'is this a number' converter. NaN/None/blank → 0.0."""
    if x is None:
        return 0.0
    try:
        return float(x)
    except (TypeError, ValueError):
        s = str(x).replace(",", "").replace("₪", "").replace("$", "").strip()
        try:
            return float(s)
        except ValueError:
            return 0.0


def leumi_oracle(path: Path) -> GroundTruth:
    """Leumi current-account ('Osh') HTML-as-xls export.

    The file is an HTML document; pandas.read_html returns multiple tables.
    Transactions live in the largest table. The header row contains
    תאריך | תאריך ערך | תיאור | אסמכתא | בחובה | בזכות | היתרה בש"ח | הערה.
    """
    tables = pd.read_html(path, encoding="utf-8")
    tx = max(tables, key=lambda t: t.shape[0])
    # Skip the two header-ish rows the export emits before data
    data = tx.iloc[2:].copy()
    # Drop blank separator rows (no date in col 0)
    data = data[data[0].notna()]
    debits = sum(_to_float(v) for v in data[4])    # column 4 = בחובה
    credits = sum(_to_float(v) for v in data[5])   # column 5 = בזכות
    return GroundTruth(
        row_count=int(len(data)),
        sum_debits_nis=round(debits, 2),
        sum_credits_nis=round(credits, 2),
        declared_total_nis=None,
    )


def isracard_oracle(path: Path) -> GroundTruth:
    """Isracard ``פירוט עסקאות`` export.

    Sheet header at row 12; data from row 13. Header columns:
    תאריך רכישה | שם בית עסק | סכום עסקה | מטבע עסקה |
    סכום חיוב | מטבע חיוב | מס' שובר | פירוט נוסף.

    The declared total appears at row 4 col 7 in NIS.
    """
    df = pd.read_excel(path, sheet_name="פירוט עסקאות", header=None)
    declared_str = str(df.iat[4, 7])
    declared_match = _NIS_NUM.search(declared_str.replace(",", ""))
    declared = float(declared_match.group()) if declared_match else None

    # Row 12 is the header; rows 13+ are transactions until first blank.
    data = df.iloc[13:].copy()
    data = data[data[0].notna()]
    # Stop at first blank row (Isracard sometimes has notes after)
    first_blank = data[data[0].isna()].index.min() if data[0].isna().any() else None
    if first_blank is not None:
        data = data.loc[: first_blank - 1]

    debits = 0.0
    credits = 0.0
    for _, row in data.iterrows():
        tx_amount = _to_float(row[2])              # סכום עסקה
        # סכום חיוב in column 4. If the original currency is NIS, that's our
        # number directly; if foreign (e.g., USD), col 4 still shows foreign,
        # so we fall back to סכום חיוב = col 4 with currency col 5 check.
        charge_nis = _to_float(row[4]) if str(row[5]).strip() == "₪" else None
        amount = charge_nis if charge_nis is not None else tx_amount
        if tx_amount < 0 or amount < 0:
            credits += abs(amount)
        else:
            debits += abs(amount)
    return GroundTruth(
        row_count=int(len(data)),
        sum_debits_nis=round(debits, 2),
        sum_credits_nis=round(credits, 2),
        declared_total_nis=declared,
    )


def max_oracle(path: Path) -> GroundTruth:
    """Max card export. Sheet name starts ``לאומי לישראל`` and ends with the
    account number. Row 0 has the title; row 2 has the declared total in
    a sentence like "...654.88 ₪". Row 3 is the header; rows 4+ are data.
    """
    xl = pd.ExcelFile(path)
    sheet = next(s for s in xl.sheet_names if s.startswith("לאומי לישראל"))
    df = pd.read_excel(path, sheet_name=sheet, header=None)
    header_row_idx = 3
    declared_str = str(df.iat[2, 0]).replace(",", "")
    declared_match = _NIS_NUM.search(declared_str.split(":")[-1])
    declared = float(declared_match.group()) if declared_match else None

    data = df.iloc[header_row_idx + 1 :].copy()
    data = data[data[0].notna()]
    # Stop at first row whose date column is non-numeric/non-date — Max
    # appends a 'full-info-on-website' note after the data block.
    debits = 0.0
    credits = 0.0
    n = 0
    for _, row in data.iterrows():
        try:
            charge = _to_float(row[3])             # col 3 = סכום חיוב
        except Exception:
            break
        # Trailer note has '0' or NaN in col 3; rely on col 0 being a date.
        if pd.isna(row[0]):
            continue
        n += 1
        if charge < 0:
            credits += abs(charge)
        else:
            debits += abs(charge)
    return GroundTruth(
        row_count=n,
        sum_debits_nis=round(debits, 2),
        sum_credits_nis=round(credits, 2),
        declared_total_nis=declared,
    )
```

- [ ] **Step 4: Run tests**

With samples on disk:
```powershell
$env:ARGOSY_EXPENSE_SAMPLES_ROOT = "D:\Google Drive\Family\Finances\Portfolio\Resources"
pytest tests/test_expense_ground_truth.py -v
```
Expected: All five tests PASS. Without the env var: all skip.

- [ ] **Step 5: Commit**

```powershell
git add tests/expense_ground_truth.py tests/test_expense_ground_truth.py
git commit -m "feat(expenses-tests): parser-independent ground-truth oracle (Leumi/Isracard/Max)"
```

---

### Task 7: Leumi parser (TDD against oracle)

**Files:**
- Create: `argosy/services/expense_ingest/parsers/__init__.py`
- Create: `argosy/services/expense_ingest/parsers/leumi_osh.py`
- Create: `tests/fixtures/expenses/leumi_osh_minimal.xls` (synthesized HTML, 5 rows)
- Test: `tests/test_expense_parsers_unit.py` (Leumi-specific cases)
- Test: `tests/test_expense_parsers_ground_truth.py` (oracle conservation)

- [ ] **Step 1: Build the synthetic Leumi fixture**

Create `tests/fixtures/expenses/leumi_osh_minimal.xls` (HTML disguised as `.xls` — write with `Write` tool):

```html
<HTML xmlns="http://www.w3.org/TR/REC-html40" dir="RTL"><head><meta charset="UTF-8"></head>
<body>
<table><tr><td>בנק לאומי</td></tr><tr><td>מס' חשבון: 882-447452/80</td></tr></table>
<table><tr><td>היתרה ₪ 1000</td></tr></table>
<table>
<tr><td>תנועות בחשבון</td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
<tr><td>תאריך</td><td>תאריך ערך</td><td>תיאור</td><td>אסמכתא</td><td>בחובה</td><td>בזכות</td><td>היתרה בש"ח</td><td>הערה</td><td></td></tr>
<tr><td>15/04/2026</td><td>15/04/2026</td><td>ל.מאסטרקרד(יש)</td><td>1266</td><td>3319.44</td><td>0</td><td>61131.90</td><td></td><td></td></tr>
<tr><td>15/04/2026</td><td>15/04/2026</td><td>כרטיסי אשראי-י</td><td>8547</td><td>654.88</td><td>0</td><td>57287.40</td><td></td><td></td></tr>
<tr><td>10/04/2026</td><td>10/04/2026</td><td>מקס איט פיננ-י</td><td>34685</td><td>9748.85</td><td>0</td><td>64451.34</td><td></td><td></td></tr>
<tr><td>01/05/2026</td><td>01/05/2026</td><td>מלאנוקס טכנו-י</td><td>61307</td><td>0</td><td>25990.40</td><td>83553.80</td><td></td><td></td></tr>
<tr><td>20/04/2026</td><td>20/04/2026</td><td>קצבת ילדים-י</td><td>13104</td><td>0</td><td>276.00</td><td>57563.40</td><td></td><td></td></tr>
</table>
</body></HTML>
```

- [ ] **Step 2: Write the failing parser test**

Create `tests/test_expense_parsers_unit.py`:

```python
"""Per-issuer parser unit tests against synthetic fixtures."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"


def test_leumi_parser_returns_5_rows():
    from argosy.services.expense_ingest.parsers.leumi_osh import parse
    result = parse(FIXTURES / "leumi_osh_minimal.xls")
    assert len(result.transactions) == 5


def test_leumi_parser_separates_debits_and_credits():
    from argosy.services.expense_ingest.parsers.leumi_osh import parse
    result = parse(FIXTURES / "leumi_osh_minimal.xls")
    debits = [t for t in result.transactions if t.direction == "debit"]
    credits = [t for t in result.transactions if t.direction == "credit"]
    assert len(debits) == 3
    assert len(credits) == 2


def test_leumi_parser_keeps_card_payment_reference():
    from argosy.services.expense_ingest.parsers.leumi_osh import parse
    result = parse(FIXTURES / "leumi_osh_minimal.xls")
    card_pay = next(t for t in result.transactions
                    if "מאסטרקרד" in t.merchant_raw)
    assert card_pay.reference == "1266"
    assert card_pay.amount_nis == pytest.approx(3319.44)
    assert card_pay.direction == "debit"


def test_leumi_parser_normalizes_dash_yod_suffix():
    from argosy.services.expense_ingest.parsers.leumi_osh import parse
    result = parse(FIXTURES / "leumi_osh_minimal.xls")
    salary = next(t for t in result.transactions
                  if "מלאנוקס" in t.merchant_raw)
    assert "מלאנוקס טכנו-י" == salary.merchant_raw
    assert "מלאנוקס טכנו" == salary.merchant_normalized


def test_leumi_parser_statement_metadata():
    from argosy.services.expense_ingest.parsers.leumi_osh import parse
    result = parse(FIXTURES / "leumi_osh_minimal.xls")
    assert result.statement.period_start == date(2026, 4, 10)
    assert result.statement.period_end == date(2026, 5, 1)
    assert result.statement.declared_total_nis is None
    assert result.statement.charge_date is None
```

- [ ] **Step 3: Run to verify it fails**

Run: `pytest tests/test_expense_parsers_unit.py -v -k leumi`
Expected: ImportError on the parser module.

- [ ] **Step 4: Implement the Leumi parser**

Create `argosy/services/expense_ingest/parsers/__init__.py` (empty package marker), then `argosy/services/expense_ingest/parsers/leumi_osh.py`:

```python
"""Parser for Leumi 'Osh' (current-account) HTML-disguised-as-xls export."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd

from argosy.services.expense_ingest.normalize import normalize
from argosy.services.expense_ingest.types import (
    NormalizedTransaction, ParseResult, ParserName, StatementMeta,
)

PARSER_VERSION = "0.1.0"


def _to_float(x) -> float:
    if x is None:
        return 0.0
    try:
        return float(x)
    except (TypeError, ValueError):
        s = str(x).replace(",", "").strip()
        try:
            return float(s)
        except ValueError:
            return 0.0


def _parse_dmy(s) -> date | None:
    """Leumi uses DD/MM/YYYY format."""
    if pd.isna(s):
        return None
    if isinstance(s, datetime):
        return s.date()
    return datetime.strptime(str(s).strip(), "%d/%m/%Y").date()


def parse(path: Path) -> ParseResult:
    """Parse a Leumi current-account HTML export.

    Tables: typically 3. Transactions live in the largest. Header row
    is at index 1 (within that table) carrying the Hebrew column names.
    Data rows start at index 2; we drop blanks (no date in col 0).
    """
    tables = pd.read_html(path, encoding="utf-8")
    tx_table = max(tables, key=lambda t: t.shape[0])
    data = tx_table.iloc[2:].copy()
    data = data[data[0].notna()]

    txs: list[NormalizedTransaction] = []
    for _, row in data.iterrows():
        d = _parse_dmy(row[0])
        if d is None:
            continue
        descr = str(row[2]).strip()
        ref = None if pd.isna(row[3]) else str(row[3]).strip()
        debit = _to_float(row[4])
        credit = _to_float(row[5])
        amount = debit if debit > 0 else credit
        direction = "debit" if debit > 0 else "credit"
        txs.append(NormalizedTransaction(
            occurred_on=d,
            posted_on=_parse_dmy(row[1]),
            merchant_raw=descr,
            merchant_normalized=normalize(descr),
            amount_nis=amount,
            direction=direction,
            tx_type="regular",  # Leumi doesn't distinguish
            reference=ref,
            issuer_category=None,
            raw_row={
                str(i): (None if pd.isna(v) else str(v))
                for i, v in enumerate(row)
            },
        ))

    if not txs:
        raise ValueError(f"Leumi parser produced 0 rows from {path}")

    parsed_total = sum(
        t.amount_nis for t in txs if t.direction == "debit"
    )
    return ParseResult(
        statement=StatementMeta(
            period_start=min(t.occurred_on for t in txs),
            period_end=max(t.occurred_on for t in txs),
            charge_date=None,
            declared_total_nis=None,
            parsed_total_nis=parsed_total,
        ),
        transactions=txs,
        source_hint=None,  # bank-account ID extraction is in registry
    )
```

- [ ] **Step 5: Run unit tests**

Run: `pytest tests/test_expense_parsers_unit.py -v -k leumi`
Expected: All five tests PASS.

- [ ] **Step 6: Write the conservation test against the real samples**

Create `tests/test_expense_parsers_ground_truth.py`:

```python
"""Conservation tests: parser output must match the ground-truth oracle.

These tests skip without ARGOSY_EXPENSE_SAMPLES_ROOT. On a developer
machine with the samples present, ALL parametrized cases must pass.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.expense_ground_truth import (
    leumi_oracle, isracard_oracle, max_oracle,
)

SAMPLES = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")
pytestmark = pytest.mark.skipif(
    not SAMPLES, reason="ARGOSY_EXPENSE_SAMPLES_ROOT unset"
)


def _root() -> Path:
    return Path(SAMPLES)


def _all_existing(*patterns) -> list[Path]:
    out: list[Path] = []
    root = _root()
    for sub in patterns:
        for p in root.glob(sub):
            if p.is_file():
                out.append(p)
    return out


@pytest.fixture(scope="module")
def leumi_samples():
    paths = _all_existing("**/Leumi/leumi_*.xls")
    if not paths:
        pytest.skip("no Leumi samples present")
    return paths


def test_leumi_parser_conservation(leumi_samples):
    from argosy.services.expense_ingest.parsers.leumi_osh import parse
    for p in leumi_samples:
        truth = leumi_oracle(p)
        result = parse(p)
        debits = sum(t.amount_nis for t in result.transactions
                     if t.direction == "debit")
        credits = sum(t.amount_nis for t in result.transactions
                      if t.direction == "credit")
        assert len(result.transactions) == truth.row_count, (
            f"{p.name}: row count drift parser={len(result.transactions)} "
            f"oracle={truth.row_count}"
        )
        assert abs(debits - truth.sum_debits_nis) < 1.00, (
            f"{p.name}: debit sum drift parser={debits} oracle={truth.sum_debits_nis}"
        )
        assert abs(credits - truth.sum_credits_nis) < 1.00, (
            f"{p.name}: credit sum drift parser={credits} oracle={truth.sum_credits_nis}"
        )
```

- [ ] **Step 7: Run conservation test**

```powershell
$env:ARGOSY_EXPENSE_SAMPLES_ROOT = "D:\Google Drive\Family\Finances\Portfolio\Resources"
pytest tests/test_expense_parsers_ground_truth.py::test_leumi_parser_conservation -v
```
Expected: PASS for every Leumi sample.

If it fails: read the printed file name + drift, then debug the parser. Common causes: a row with non-standard date format (e.g., `*` prefix indicating not-final transactions), a merged-cell oddity, an extra blank row inside the data block.

- [ ] **Step 8: Commit**

```powershell
git add argosy/services/expense_ingest/parsers/__init__.py `
        argosy/services/expense_ingest/parsers/leumi_osh.py `
        tests/fixtures/expenses/leumi_osh_minimal.xls `
        tests/test_expense_parsers_unit.py `
        tests/test_expense_parsers_ground_truth.py
git commit -m "feat(expenses): Leumi current-account parser (HTML-as-xls)"
```

---

### Task 8: Isracard parser (TDD against oracle)

**Files:**
- Create: `argosy/services/expense_ingest/parsers/isracard.py`
- Create: `tests/fixtures/expenses/isracard_minimal.xlsx` (synthesized via openpyxl)
- Modify: `tests/test_expense_parsers_unit.py` (append Isracard cases)
- Modify: `tests/test_expense_parsers_ground_truth.py` (append Isracard conservation case)

- [ ] **Step 1: Generate the synthetic Isracard fixture**

Write a tiny script `tests/fixtures/expenses/_make_isracard_fixture.py`:

```python
"""Generates tests/fixtures/expenses/isracard_minimal.xlsx — 5 rows
covering the format quirks: NIS-only, USD-only, refund, standing-order."""
from __future__ import annotations

from pathlib import Path

import openpyxl

OUT = Path(__file__).parent / "isracard_minimal.xlsx"


def main():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "פירוט עסקאות"
    # Layout (rows 1-indexed in openpyxl):
    # row 5  col 1: card header line
    # row 5  col 8: total NIS  ('₪ 250.00')
    # row 6  col 1: 'על שם אריאל יעקב'
    # row 7  col 8: 'לחיוב ב-15.04'
    # row 13 col 1..8: header row
    # rows 14+: transactions
    ws.cell(row=5, column=1, value="פלטינה מסטרקארד - 1266")
    ws.cell(row=5, column=8, value="₪ 250.00")
    ws.cell(row=6, column=1, value="על שם אריאל יעקב")
    ws.cell(row=7, column=8, value="לחיוב ב-15.04")
    headers = ["תאריך רכישה", "שם בית עסק", "סכום עסקה", "מטבע עסקה",
               "סכום חיוב", "מטבע חיוב", "מס' שובר", "פירוט נוסף"]
    for i, h in enumerate(headers, start=1):
        ws.cell(row=13, column=i, value=h)
    rows = [
        # date,        merchant,     tx_amt, tx_ccy, charge_amt, charge_ccy, voucher, extras
        ("08.04.26", "NETFLIX.COM",   69.9,  "₪",    69.9,       "₪",        "143142516", "אתר חו\"ל\nהוראת קבע"),
        ("05.04.26", "הזמנה משלוח אוכל", 131,   "₪",    127.07,     "₪",        "136908473", "הנחה ₪3.93"),
        ("24.03.26", "NAME-CHEAP.COM", 12.18, "$",    12.18,      "$",        "072314356", "אתר חו\"ל\nהוראת קבע"),
        ("22.03.26", "ZARA",          -50.0, "₪",    -50.0,      "₪",        "075759881", ""),  # refund
        ("18.03.26", "PAYPAL *VENDOR", 16.11, "₪",    16.11,      "₪",        "091130802", "אתר חו\"ל"),
    ]
    for i, row in enumerate(rows, start=14):
        for j, v in enumerate(row, start=1):
            ws.cell(row=i, column=j, value=v)
    wb.save(OUT)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
```

Run it once to produce the fixture:
```powershell
& "D:/Projects/financial-advisor/.venv/Scripts/python.exe" tests/fixtures/expenses/_make_isracard_fixture.py
```

Commit both the script and the resulting `.xlsx` so future agents don't need to regenerate.

- [ ] **Step 2: Append failing Isracard tests**

Append to `tests/test_expense_parsers_unit.py`:

```python
def test_isracard_parser_returns_5_rows():
    from argosy.services.expense_ingest.parsers.isracard import parse
    result = parse(FIXTURES / "isracard_minimal.xlsx")
    assert len(result.transactions) == 5


def test_isracard_parser_extracts_card_last4():
    from argosy.services.expense_ingest.parsers.isracard import parse
    result = parse(FIXTURES / "isracard_minimal.xlsx")
    assert result.source_hint is not None
    assert result.source_hint.kind == "card"
    assert result.source_hint.issuer == "isracard"
    assert result.source_hint.external_id == "1266"
    assert "אריאל" in result.source_hint.cardholder_name


def test_isracard_parser_charge_date():
    from argosy.services.expense_ingest.parsers.isracard import parse
    from datetime import date
    result = parse(FIXTURES / "isracard_minimal.xlsx")
    # The fixture says לחיוב ב-15.04 — year inferred from latest tx year
    assert result.statement.charge_date == date(2026, 4, 15)


def test_isracard_parser_handles_usd_row():
    from argosy.services.expense_ingest.parsers.isracard import parse
    result = parse(FIXTURES / "isracard_minimal.xlsx")
    usd = next(t for t in result.transactions
               if "NAME-CHEAP" in t.merchant_raw)
    assert usd.currency_orig == "USD"
    assert usd.amount_orig == 12.18
    # NIS-approximation must be set (we use a fallback constant in tests)
    assert usd.amount_nis > 0


def test_isracard_parser_detects_refund():
    from argosy.services.expense_ingest.parsers.isracard import parse
    result = parse(FIXTURES / "isracard_minimal.xlsx")
    refund = next(t for t in result.transactions
                  if "ZARA" in t.merchant_raw)
    assert refund.tx_type == "refund"
    assert refund.direction == "credit"
    assert refund.amount_nis == 50.0  # always positive on storage


def test_isracard_parser_detects_standing_order():
    from argosy.services.expense_ingest.parsers.isracard import parse
    result = parse(FIXTURES / "isracard_minimal.xlsx")
    netflix = next(t for t in result.transactions
                   if "NETFLIX" in t.merchant_raw)
    assert netflix.tx_type == "standing_order"
```

- [ ] **Step 3: Run to verify they fail**

Run: `pytest tests/test_expense_parsers_unit.py -v -k isracard`
Expected: ImportError.

- [ ] **Step 4: Implement the Isracard parser**

Create `argosy/services/expense_ingest/parsers/isracard.py`:

```python
"""Parser for Isracard (and Mastercard via Isracard) Excel exports.

Layout:
  sheet name: 'פירוט עסקאות'
  row 5 col 8: card-level total NIS ('₪ 3,319.44')
  row 5 col 1: '<card type> - <last-4>'
  row 6 col 1: 'על שם <cardholder>'
  row 7 col 8: 'לחיוב ב-DD.MM' (charge date with implicit year)
  row 13   : header
  rows 14+ : transactions until first blank row 0

Multi-currency: when מטבע עסקה is '$' (or any non-₪), the NIS-billed
amount is in סכום חיוב col 5 (Isracard sometimes mirrors that as USD too).
We approximate USD→NIS via a simple ~3.7 fallback when the cache is empty;
the real spot lookup is wired by the orchestrator.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from argosy.services.expense_ingest.normalize import normalize
from argosy.services.expense_ingest.types import (
    NormalizedTransaction, ParseResult, ParserName, SourceHint, StatementMeta,
)

PARSER_VERSION = "0.1.0"

_LAST4_RE = re.compile(r"-\s*(\d{3,4})\s*$")
_CARDHOLDER_RE = re.compile(r"על שם\s+(.+)")
_CHARGE_DATE_RE = re.compile(r"לחיוב ב-?\s*(\d{1,2})[./](\d{1,2})")
_NIS_NUM_RE = re.compile(r"[-+]?[\d,]+\.?\d*")

_USD_NIS_FALLBACK = 3.70  # used when no cached spot rate available


def _to_float(x) -> float:
    if x is None:
        return 0.0
    try:
        return float(x)
    except (TypeError, ValueError):
        s = str(x).replace(",", "").replace("₪", "").replace("$", "").strip()
        try:
            return float(s)
        except ValueError:
            return 0.0


def _parse_short_date(s: str) -> date:
    """Isracard uses '08.04.26' format → 2026-04-08."""
    s = str(s).strip()
    return datetime.strptime(s, "%d.%m.%y").date()


def _normalize_currency(c) -> str:
    if pd.isna(c):
        return "NIS"
    s = str(c).strip()
    return {"₪": "NIS", "$": "USD", "€": "EUR"}.get(s, s.upper() or "NIS")


def parse(path: Path) -> ParseResult:
    df = pd.read_excel(path, sheet_name="פירוט עסקאות", header=None)

    # Card metadata
    card_label = str(df.iat[4, 0])
    m_last4 = _LAST4_RE.search(card_label)
    if not m_last4:
        raise ValueError(f"Isracard parser: card last-4 not found in '{card_label}'")
    last4 = m_last4.group(1)

    cardholder = None
    holder_cell = str(df.iat[5, 0])
    m_holder = _CARDHOLDER_RE.search(holder_cell)
    if m_holder:
        cardholder = m_holder.group(1).strip()

    # Declared total NIS (₪)
    declared_str = str(df.iat[4, 7])
    m_total = _NIS_NUM_RE.search(declared_str.replace(",", ""))
    declared_nis = float(m_total.group()) if m_total else None

    # Charge date — has DD.MM, we infer year from the most recent transaction
    charge_str = str(df.iat[6, 7])
    m_charge = _CHARGE_DATE_RE.search(charge_str)

    # Header validation
    headers = [str(df.iat[12, j]).strip() for j in range(8)]
    expected = ["תאריך רכישה", "שם בית עסק", "סכום עסקה", "מטבע עסקה",
                "סכום חיוב", "מטבע חיוב", "מס' שובר", "פירוט נוסף"]
    if headers != expected:
        raise ValueError(f"Isracard parser: unexpected header row {headers}")

    txs: list[NormalizedTransaction] = []
    for i in range(13, len(df)):
        row = df.iloc[i]
        if pd.isna(row[0]):
            break
        d = _parse_short_date(row[0])
        merchant_raw = str(row[1]).strip()
        tx_amount = _to_float(row[2])
        tx_ccy = _normalize_currency(row[3])
        charge_amount = _to_float(row[4])
        charge_ccy = _normalize_currency(row[5])
        voucher = None if pd.isna(row[6]) else str(row[6]).strip()
        extras = "" if pd.isna(row[7]) else str(row[7])

        if charge_ccy == "NIS":
            amount_nis = abs(charge_amount)
        else:
            amount_nis = abs(charge_amount) * _USD_NIS_FALLBACK if charge_ccy == "USD" else abs(charge_amount)

        # Direction + tx_type derivation
        is_refund = tx_amount < 0
        if is_refund:
            tx_type = "refund"
            direction = "credit"
        elif "הוראת קבע" in extras:
            tx_type = "standing_order"
            direction = "debit"
        elif "תשלום" in extras and re.search(r"\d+\s*(?:/|מ-|מתוך)\s*\d+", extras):
            tx_type = "installment"
            direction = "debit"
        else:
            tx_type = "regular"
            direction = "debit"

        txs.append(NormalizedTransaction(
            occurred_on=d,
            merchant_raw=merchant_raw,
            merchant_normalized=normalize(merchant_raw),
            amount_nis=amount_nis,
            amount_orig=abs(tx_amount) if tx_ccy != "NIS" else None,
            currency_orig=tx_ccy if tx_ccy != "NIS" else None,
            direction=direction,
            tx_type=tx_type,
            reference=voucher,
            issuer_category=None,    # Isracard does not categorize
            raw_row={
                "date": str(row[0]),
                "merchant": merchant_raw,
                "tx_amount": tx_amount, "tx_ccy": tx_ccy,
                "charge_amount": charge_amount, "charge_ccy": charge_ccy,
                "voucher": voucher, "extras": extras,
            },
        ))

    if not txs:
        raise ValueError(f"Isracard parser: 0 rows in {path}")

    # Charge date with year inferred from latest tx
    charge_date: date | None = None
    if m_charge:
        latest_year = max(t.occurred_on.year for t in txs)
        cd_day = int(m_charge.group(1))
        cd_month = int(m_charge.group(2))
        charge_date = date(latest_year, cd_month, cd_day)

    parsed_total = sum(
        t.amount_nis * (-1 if t.direction == "credit" else 1) for t in txs
    )
    return ParseResult(
        statement=StatementMeta(
            period_start=min(t.occurred_on for t in txs),
            period_end=max(t.occurred_on for t in txs),
            charge_date=charge_date,
            declared_total_nis=declared_nis,
            parsed_total_nis=parsed_total,
        ),
        transactions=txs,
        source_hint=SourceHint(
            kind="card",
            issuer="isracard",
            external_id=last4,
            cardholder_name=cardholder,
        ),
    )
```

- [ ] **Step 5: Run unit tests**

Run: `pytest tests/test_expense_parsers_unit.py -v -k isracard`
Expected: All six Isracard cases PASS.

- [ ] **Step 6: Append the Isracard conservation case**

Append to `tests/test_expense_parsers_ground_truth.py`:

```python
@pytest.fixture(scope="module")
def isracard_samples():
    paths = _all_existing("**/1266/1266_*.xlsx")
    if not paths:
        pytest.skip("no Isracard samples present")
    return paths


def test_isracard_parser_conservation(isracard_samples):
    from argosy.services.expense_ingest.parsers.isracard import parse
    for p in isracard_samples:
        truth = isracard_oracle(p)
        result = parse(p)
        debits = sum(t.amount_nis for t in result.transactions
                     if t.direction == "debit")
        credits = sum(t.amount_nis for t in result.transactions
                      if t.direction == "credit")
        assert len(result.transactions) == truth.row_count, (
            f"{p.name}: row count drift {len(result.transactions)} vs {truth.row_count}"
        )
        assert abs(debits - truth.sum_debits_nis) < 1.00, (
            f"{p.name}: debit drift {debits} vs {truth.sum_debits_nis}"
        )
        assert abs(credits - truth.sum_credits_nis) < 1.00, (
            f"{p.name}: credit drift {credits} vs {truth.sum_credits_nis}"
        )
        # Issuer footer reconciliation (within ₪50, looser per spec)
        if truth.declared_total_nis is not None:
            assert abs(result.statement.parsed_total_nis - truth.declared_total_nis) < 50.00, (
                f"{p.name}: parsed total {result.statement.parsed_total_nis} "
                f"vs declared {truth.declared_total_nis}"
            )
```

- [ ] **Step 7: Run conservation test on real samples**

```powershell
$env:ARGOSY_EXPENSE_SAMPLES_ROOT = "D:\Google Drive\Family\Finances\Portfolio\Resources"
pytest tests/test_expense_parsers_ground_truth.py::test_isracard_parser_conservation -v
```
Expected: PASS for every Isracard sample (12 in 2025, 4 in 2026).

- [ ] **Step 8: Commit**

```powershell
git add argosy/services/expense_ingest/parsers/isracard.py `
        tests/fixtures/expenses/isracard_minimal.xlsx `
        tests/fixtures/expenses/_make_isracard_fixture.py `
        tests/test_expense_parsers_unit.py `
        tests/test_expense_parsers_ground_truth.py
git commit -m "feat(expenses): Isracard parser (multi-currency, refund/standing-order detection)"
```

---

### Task 9: Max parser (TDD against oracle, with issuer-category extraction)

**Files:**
- Create: `argosy/services/expense_ingest/parsers/max.py`
- Create: `tests/fixtures/expenses/_make_max_fixture.py`
- Create: `tests/fixtures/expenses/max_minimal.xlsx`
- Modify: `tests/test_expense_parsers_unit.py` (append Max cases)
- Modify: `tests/test_expense_parsers_ground_truth.py` (append Max conservation case)

- [ ] **Step 1: Generate the synthetic Max fixture**

Create `tests/fixtures/expenses/_make_max_fixture.py`:

```python
"""Generates max_minimal.xlsx with 5 rows including a refund and ענף values."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import openpyxl

OUT = Path(__file__).parent / "max_minimal.xlsx"


def main():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "לאומי לישראל 882-44745280"
    ws.cell(row=1, column=1,
            value="פירוט עסקאות לחשבון לאומי לישראל 882-44745280 02/03/2026 — 31/03/2026")
    ws.cell(row=3, column=1, value="עסקאות לחיוב ב-15/04/2026: 654.88 ₪")
    headers = ["תאריך\nעסקה", "שם בית עסק", "סכום\nעסקה", "סכום\nחיוב",
               "סוג\nעסקה", "ענף", "הערות"]
    for i, h in enumerate(headers, start=1):
        ws.cell(row=4, column=i, value=h)

    rows = [
        # date,                merchant,           tx,    chg,    type,        anaf,           notes
        (datetime(2026, 3, 30), "ספייס אינביידרז",   355,   355,    "רגילה",      "מסעדות",        None),
        (datetime(2026, 3, 25), "ביטוח ישיר-חיים",   142,   142,    "הוראת קבע",  "ביטוח ופיננסים", None),
        (datetime(2026, 3, 21), "WIZZ AIRGR73FH",  -2097.83, -2097.83, "זיכוי",   "תיירות",        None),
        (datetime(2026, 3, 23), "אלקטרה פאוור חשמל", 293.12, 293.12,  "הוראת קבע","ריהוט ובית",   None),
        (datetime(2026, 3, 2),  "קרן מכבי-חיוב",   133.19, 133.19, "הוראת קבע",  "רפואה ובריאות", None),
    ]
    for i, row in enumerate(rows, start=5):
        for j, v in enumerate(row, start=1):
            ws.cell(row=i, column=j, value=v)
    wb.save(OUT)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
```

Run:
```powershell
& "D:/Projects/financial-advisor/.venv/Scripts/python.exe" tests/fixtures/expenses/_make_max_fixture.py
```

- [ ] **Step 2: Append failing Max tests**

Append to `tests/test_expense_parsers_unit.py`:

```python
def test_max_parser_returns_5_rows():
    from argosy.services.expense_ingest.parsers.max import parse
    result = parse(FIXTURES / "max_minimal.xlsx")
    assert len(result.transactions) == 5


def test_max_parser_extracts_account_last4():
    from argosy.services.expense_ingest.parsers.max import parse
    result = parse(FIXTURES / "max_minimal.xlsx")
    assert result.source_hint is not None
    assert result.source_hint.issuer == "max"
    # Account is 882-44745280 → last 4 of the post-dash chunk = '5280'
    assert result.source_hint.external_id == "5280"


def test_max_parser_keeps_anaf_as_issuer_category():
    from argosy.services.expense_ingest.parsers.max import parse
    result = parse(FIXTURES / "max_minimal.xlsx")
    rest = next(t for t in result.transactions
                if "ספייס" in t.merchant_raw)
    assert rest.issuer_category == "מסעדות"


def test_max_parser_detects_refund():
    from argosy.services.expense_ingest.parsers.max import parse
    result = parse(FIXTURES / "max_minimal.xlsx")
    refund = next(t for t in result.transactions
                  if "WIZZ" in t.merchant_raw)
    assert refund.tx_type == "refund"
    assert refund.direction == "credit"
    assert refund.amount_nis == 2097.83  # always positive


def test_max_parser_charge_date_extracted():
    from argosy.services.expense_ingest.parsers.max import parse
    from datetime import date
    result = parse(FIXTURES / "max_minimal.xlsx")
    assert result.statement.charge_date == date(2026, 4, 15)
    assert abs(result.statement.declared_total_nis - 654.88) < 0.01
```

- [ ] **Step 3: Run to verify they fail**

Run: `pytest tests/test_expense_parsers_unit.py -v -k max`
Expected: ImportError.

- [ ] **Step 4: Implement the Max parser**

Create `argosy/services/expense_ingest/parsers/max.py`:

```python
"""Parser for Max card Excel exports.

Layout:
  sheet name: 'לאומי לישראל <account-number>'
  row 1 col 1: title with account # and date range
  row 3 col 1: 'עסקאות לחיוב ב-DD/MM/YYYY: NNN.NN ₪'
  row 4   : header — תאריך עסקה|שם בית עסק|סכום עסקה|סכום חיוב|סוג עסקה|ענף|הערות
  rows 5+ : transactions until trailing notes row

Distinguishing feature: column 6 (ענף) is a pre-categorized issuer hint
that flows through to NormalizedTransaction.issuer_category.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from argosy.services.expense_ingest.normalize import normalize
from argosy.services.expense_ingest.types import (
    NormalizedTransaction, ParseResult, ParserName, SourceHint, StatementMeta,
)

PARSER_VERSION = "0.1.0"

_ACCOUNT_RE = re.compile(r"לאומי לישראל\s+([\d-]+)")
_CHARGE_RE = re.compile(r"לחיוב ב-?\s*(\d{1,2})/(\d{1,2})/(\d{4}):\s*([\d,.]+)")

_TX_TYPE_MAP = {
    "רגילה": "regular",
    "הוראת קבע": "standing_order",
    "תשלומים": "installment",
    "זיכוי": "refund",
}


def _to_float(x) -> float:
    if x is None:
        return 0.0
    try:
        return float(x)
    except (TypeError, ValueError):
        s = str(x).replace(",", "").replace("₪", "").strip()
        try:
            return float(s)
        except ValueError:
            return 0.0


def parse(path: Path) -> ParseResult:
    xl = pd.ExcelFile(path)
    sheet = next((s for s in xl.sheet_names
                  if s.startswith("לאומי לישראל")), None)
    if sheet is None:
        raise ValueError(f"Max parser: no 'לאומי לישראל' sheet in {path}, "
                         f"got {xl.sheet_names}")

    # Account number → last-4 of post-dash chunk
    m_acc = _ACCOUNT_RE.search(sheet)
    if not m_acc:
        raise ValueError(f"Max parser: account # not found in sheet name '{sheet}'")
    account_full = m_acc.group(1)               # e.g. '882-44745280'
    last4 = account_full.split("-")[-1][-4:]

    df = pd.read_excel(path, sheet_name=sheet, header=None)
    title = str(df.iat[0, 0])
    charge_str = str(df.iat[2, 0])
    m_charge = _CHARGE_RE.search(charge_str)
    declared = float(m_charge.group(4).replace(",", "")) if m_charge else None
    charge_date = (
        date(int(m_charge.group(3)), int(m_charge.group(2)), int(m_charge.group(1)))
        if m_charge else None
    )

    # Header validation
    expected_headers = ["תאריך\nעסקה", "שם בית עסק", "סכום\nעסקה",
                        "סכום\nחיוב", "סוג\nעסקה", "ענף", "הערות"]
    actual_headers = [str(df.iat[3, j]).strip() if not pd.isna(df.iat[3, j]) else ""
                      for j in range(7)]
    if [h.replace("\n", "") for h in actual_headers] != [h.replace("\n", "") for h in expected_headers]:
        raise ValueError(f"Max parser: unexpected header row {actual_headers}")

    txs: list[NormalizedTransaction] = []
    for i in range(4, len(df)):
        row = df.iloc[i]
        date_cell = row[0]
        if pd.isna(date_cell):
            continue
        # Skip the trailer note row (non-date col 0)
        if not isinstance(date_cell, (datetime, pd.Timestamp)):
            try:
                d = pd.to_datetime(date_cell).date()
            except Exception:
                continue
        else:
            d = date_cell.date() if isinstance(date_cell, datetime) else date_cell

        merchant_raw = str(row[1]).strip()
        tx_amount = _to_float(row[2])
        charge_amount = _to_float(row[3])
        tx_type_he = str(row[4]).strip() if not pd.isna(row[4]) else ""
        anaf = None if pd.isna(row[5]) else str(row[5]).strip()

        tx_type = _TX_TYPE_MAP.get(tx_type_he, "regular")
        is_refund = tx_type == "refund" or charge_amount < 0
        if is_refund:
            tx_type = "refund"
            direction = "credit"
        else:
            direction = "debit"

        txs.append(NormalizedTransaction(
            occurred_on=d,
            merchant_raw=merchant_raw,
            merchant_normalized=normalize(merchant_raw),
            amount_nis=abs(charge_amount),
            direction=direction,
            tx_type=tx_type,
            reference=None,
            issuer_category=anaf,
            raw_row={
                "date": str(date_cell),
                "merchant": merchant_raw,
                "tx_amount": tx_amount,
                "charge_amount": charge_amount,
                "tx_type_he": tx_type_he,
                "anaf": anaf,
            },
        ))

    if not txs:
        raise ValueError(f"Max parser: 0 rows in {path}")

    parsed_total = sum(
        t.amount_nis * (-1 if t.direction == "credit" else 1) for t in txs
    )
    return ParseResult(
        statement=StatementMeta(
            period_start=min(t.occurred_on for t in txs),
            period_end=max(t.occurred_on for t in txs),
            charge_date=charge_date,
            declared_total_nis=declared,
            parsed_total_nis=parsed_total,
        ),
        transactions=txs,
        source_hint=SourceHint(
            kind="card", issuer="max", external_id=last4,
            cardholder_name=None,        # Max sheet doesn't carry cardholder
        ),
    )
```

- [ ] **Step 5: Run unit tests**

Run: `pytest tests/test_expense_parsers_unit.py -v -k max`
Expected: All five Max cases PASS.

- [ ] **Step 6: Append Max conservation case**

Append to `tests/test_expense_parsers_ground_truth.py`:

```python
@pytest.fixture(scope="module")
def max_samples():
    paths = _all_existing("**/6225/*.xlsx")
    if not paths:
        pytest.skip("no Max samples present")
    return paths


def test_max_parser_conservation(max_samples):
    from argosy.services.expense_ingest.parsers.max import parse
    for p in max_samples:
        truth = max_oracle(p)
        result = parse(p)
        debits = sum(t.amount_nis for t in result.transactions
                     if t.direction == "debit")
        credits = sum(t.amount_nis for t in result.transactions
                      if t.direction == "credit")
        assert len(result.transactions) == truth.row_count, (
            f"{p.name}: row count {len(result.transactions)} vs {truth.row_count}"
        )
        assert abs(debits - truth.sum_debits_nis) < 1.00
        assert abs(credits - truth.sum_credits_nis) < 1.00
        if truth.declared_total_nis is not None:
            assert abs(result.statement.parsed_total_nis
                       - truth.declared_total_nis) < 50.00
```

- [ ] **Step 7: Run conservation test on real samples**

```powershell
$env:ARGOSY_EXPENSE_SAMPLES_ROOT = "D:\Google Drive\Family\Finances\Portfolio\Resources"
pytest tests/test_expense_parsers_ground_truth.py::test_max_parser_conservation -v
```
Expected: PASS for every Max sample.

- [ ] **Step 8: Commit**

```powershell
git add argosy/services/expense_ingest/parsers/max.py `
        tests/fixtures/expenses/max_minimal.xlsx `
        tests/fixtures/expenses/_make_max_fixture.py `
        tests/test_expense_parsers_unit.py `
        tests/test_expense_parsers_ground_truth.py
git commit -m "feat(expenses): Max parser (preserves ענף issuer category)"
```

---

### Task 10: Format sniff + stub parsers (Cal/Amex/Diners)

**Files:**
- Create: `argosy/services/expense_ingest/sniff.py`
- Create: `argosy/services/expense_ingest/parsers/cal.py` (raises NotImplementedError)
- Create: `argosy/services/expense_ingest/parsers/amex.py` (raises NotImplementedError)
- Create: `argosy/services/expense_ingest/parsers/diners.py` (raises NotImplementedError)
- Test: `tests/test_expense_sniff.py`

- [ ] **Step 1: Write failing sniff tests**

Create `tests/test_expense_sniff.py`:

```python
"""Format sniffing — content-based, filename is hint only."""

from pathlib import Path

import pytest

from argosy.services.expense_ingest.sniff import detect_format, UnknownFormatError
from argosy.services.expense_ingest.types import ParserName

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"


def test_sniff_leumi_html_xls():
    assert detect_format(FIXTURES / "leumi_osh_minimal.xls") == ParserName.LEUMI_OSH


def test_sniff_isracard_xlsx():
    assert detect_format(FIXTURES / "isracard_minimal.xlsx") == ParserName.ISRACARD


def test_sniff_max_xlsx():
    assert detect_format(FIXTURES / "max_minimal.xlsx") == ParserName.MAX


def test_sniff_unknown_xlsx_raises(tmp_path):
    import openpyxl
    p = tmp_path / "unknown.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "Bogus Sheet Name"
    wb.save(p)
    with pytest.raises(UnknownFormatError):
        detect_format(p)


def test_sniff_unknown_binary_raises(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"\x00\x01\x02\x03")
    with pytest.raises(UnknownFormatError):
        detect_format(p)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_expense_sniff.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement sniff + stubs**

Create `argosy/services/expense_ingest/sniff.py`:

```python
"""Format detection. Content sniff is canonical; filename is a hint only."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from argosy.services.expense_ingest.types import ParserName


class UnknownFormatError(Exception):
    """Raised when a file matches no known issuer's signature."""

    def __init__(self, msg: str, sheets: list[str] | None = None,
                 head: bytes | None = None):
        super().__init__(msg)
        self.sheets = sheets
        self.head = head


def detect_format(path: Path) -> ParserName:
    """Return the parser to use for this file.

    Sniff order:
      1. Read first 512 bytes.
      2. If starts with '<HTML' / '<html' → assume Leumi HTML-as-xls.
      3. If starts with PK zip header → it's an .xlsx; look at sheet names.
         - 'פירוט עסקאות' → Isracard
         - sheet starting with 'לאומי לישראל' → Max
         - other recognized sheets → Cal/Amex/Diners (stubs for now)
      4. Otherwise raise UnknownFormatError.
    """
    with open(path, "rb") as f:
        head = f.read(512)

    stripped = head.lstrip()
    if stripped.startswith(b"<HTML") or stripped.startswith(b"<html"):
        return ParserName.LEUMI_OSH

    if head[:4] == b"PK\x03\x04":          # ZIP magic = .xlsx
        try:
            xl = pd.ExcelFile(path)
        except Exception as e:
            raise UnknownFormatError(f"could not open xlsx: {e}", head=head[:64])
        sheets = xl.sheet_names
        if "פירוט עסקאות" in sheets:
            return ParserName.ISRACARD
        if any(s.startswith("לאומי לישראל") for s in sheets):
            return ParserName.MAX
        # TODO when samples arrive: Cal / Amex / Diners sheet patterns
        raise UnknownFormatError(
            f"xlsx with no recognized sheet: {sheets}", sheets=sheets,
        )

    raise UnknownFormatError(
        f"unrecognized file header: {head[:64]!r}", head=head[:64],
    )
```

Create the three stubs. `argosy/services/expense_ingest/parsers/cal.py`:

```python
"""Cal credit-card parser — TODO when sample arrives."""
from pathlib import Path

from argosy.services.expense_ingest.types import ParseResult


def parse(path: Path) -> ParseResult:
    raise NotImplementedError(
        "Cal parser not yet implemented. Provide a sample file and "
        "extend tests/fixtures/expenses/_make_cal_fixture.py."
    )
```

Same shape for `amex.py` and `diners.py`, with their respective issuer names.

- [ ] **Step 4: Run sniff tests**

Run: `pytest tests/test_expense_sniff.py -v`
Expected: All five PASS.

- [ ] **Step 5: Commit**

```powershell
git add argosy/services/expense_ingest/sniff.py `
        argosy/services/expense_ingest/parsers/cal.py `
        argosy/services/expense_ingest/parsers/amex.py `
        argosy/services/expense_ingest/parsers/diners.py `
        tests/test_expense_sniff.py
git commit -m "feat(expenses): format sniff + Cal/Amex/Diners parser stubs"
```

---

## Phase C — Stateful glue

### Task 11: Source registry (auto-register on first sight)

**Files:**
- Create: `argosy/services/expense_ingest/registry.py`
- Test: `tests/test_expense_registry.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_expense_registry.py`:

```python
"""Tests for ExpenseSource registry / auto-registration."""

from sqlalchemy.orm import Session

from argosy.services.expense_ingest.registry import (
    register_or_get_source, list_active_sources,
)
from argosy.services.expense_ingest.types import SourceHint
from argosy.state.models import ExpenseSource, User


def _seed_user(s: Session) -> None:
    s.add(User(id="ariel", plan="free"))
    s.flush()


def test_register_creates_new_source(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        _seed_user(s)
        hint = SourceHint(kind="card", issuer="isracard", external_id="1266",
                          cardholder_name="Ariel")
        src = register_or_get_source(s, "ariel", hint)
        s.commit()
        assert src.id is not None
        assert src.display_name  # auto-derived
        assert src.cardholder_name == "Ariel"


def test_register_reuses_existing_source(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        _seed_user(s)
        hint = SourceHint(kind="card", issuer="isracard", external_id="1266")
        src1 = register_or_get_source(s, "ariel", hint)
        s.commit()
        src2 = register_or_get_source(s, "ariel", hint)
        s.commit()
        assert src1.id == src2.id
        assert s.query(ExpenseSource).count() == 1


def test_register_does_not_overwrite_cardholder(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        _seed_user(s)
        hint1 = SourceHint(kind="card", issuer="isracard", external_id="1266",
                           cardholder_name="Ariel")
        src1 = register_or_get_source(s, "ariel", hint1)
        s.commit()
        # Second call without cardholder shouldn't blank it out
        hint2 = SourceHint(kind="card", issuer="isracard", external_id="1266")
        src2 = register_or_get_source(s, "ariel", hint2)
        s.commit()
        assert src2.cardholder_name == "Ariel"


def test_list_active_sources_filters_inactive(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        _seed_user(s)
        hint = SourceHint(kind="card", issuer="max", external_id="6225")
        src = register_or_get_source(s, "ariel", hint)
        src.active = False
        s.commit()
        assert len(list_active_sources(s, "ariel")) == 0
        src.active = True
        s.commit()
        assert len(list_active_sources(s, "ariel")) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_expense_registry.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the registry**

Create `argosy/services/expense_ingest/registry.py`:

```python
"""Source registration: idempotent insert/get for ExpenseSource rows."""

from __future__ import annotations

from sqlalchemy.orm import Session

from argosy.services.expense_ingest.types import SourceHint
from argosy.state.models import ExpenseSource


def _default_display_name(hint: SourceHint) -> str:
    parts = [hint.issuer.title(), hint.external_id]
    return " ".join(parts)


def register_or_get_source(
    session: Session, user_id: str, hint: SourceHint
) -> ExpenseSource:
    """Find an existing source by (user_id, kind, external_id) or create one.

    On a re-register, never blank out non-empty cardholder_name with None.
    """
    src = session.query(ExpenseSource).filter_by(
        user_id=user_id, kind=hint.kind, external_id=hint.external_id,
    ).one_or_none()
    if src is not None:
        if hint.cardholder_name and not src.cardholder_name:
            src.cardholder_name = hint.cardholder_name
        if hint.display_name and not src.display_name:
            src.display_name = hint.display_name
        return src

    src = ExpenseSource(
        user_id=user_id,
        kind=hint.kind,
        issuer=hint.issuer,
        external_id=hint.external_id,
        display_name=hint.display_name or _default_display_name(hint),
        cardholder_name=hint.cardholder_name,
        active=True,
    )
    session.add(src)
    session.flush()                # so callers get src.id
    return src


def list_active_sources(session: Session, user_id: str) -> list[ExpenseSource]:
    return list(session.query(ExpenseSource).filter_by(
        user_id=user_id, active=True
    ).order_by(ExpenseSource.created_at).all())
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_expense_registry.py -v`
Expected: All four PASS.

- [ ] **Step 5: Commit**

```powershell
git add argosy/services/expense_ingest/registry.py tests/test_expense_registry.py
git commit -m "feat(expenses): source registry with idempotent register-or-get"
```

---

### Task 12: Statement + transaction persistence (idempotent)

**Files:**
- Create: `argosy/services/expense_ingest/persistence.py`
- Test: `tests/test_expense_persistence.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_expense_persistence.py`:

```python
"""Tests for statement + transaction persistence with content-hash dedup."""

from datetime import date

from sqlalchemy.orm import Session

from argosy.services.expense_ingest.persistence import (
    persist_statement, persist_transactions,
)
from argosy.services.expense_ingest.types import (
    NormalizedTransaction, ParseResult, ParserName, SourceHint, StatementMeta,
)
from argosy.services.expense_ingest.registry import register_or_get_source
from argosy.state.models import (
    ExpenseStatement, ExpenseTransaction, User, UserFile,
)


def _seed(s: Session) -> int:
    s.add(User(id="ariel", plan="free"))
    s.flush()
    f = UserFile(
        user_id="ariel", sha256="a" * 64, original_name="x.xlsx",
        sanitized_name="x.xlsx", mime_type="application/vnd...sheet",
        kind="other", size_bytes=1, storage_path="/tmp/x", source="chat_attachment",
    )
    s.add(f)
    s.flush()
    return f.id


def _result() -> ParseResult:
    txs = [NormalizedTransaction(
        occurred_on=date(2026, 4, 8), merchant_raw="A",
        merchant_normalized="a", amount_nis=10, direction="debit",
        tx_type="regular",
    )]
    return ParseResult(
        statement=StatementMeta(
            period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
            charge_date=date(2026, 4, 15),
            declared_total_nis=10, parsed_total_nis=10,
        ),
        transactions=txs,
    )


def test_persist_statement_creates_row(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        file_id = _seed(s)
        src = register_or_get_source(s, "ariel", SourceHint(
            kind="card", issuer="isracard", external_id="1266"))
        s.commit()
        stmt = persist_statement(s, "ariel", src.id, file_id, _result(),
                                 ParserName.ISRACARD, "0.1.0")
        s.commit()
        assert stmt.id is not None
        assert stmt.status == "parsed"


def test_persist_statement_is_idempotent(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        file_id = _seed(s)
        src = register_or_get_source(s, "ariel", SourceHint(
            kind="card", issuer="isracard", external_id="1266"))
        s.commit()
        stmt1 = persist_statement(s, "ariel", src.id, file_id, _result(),
                                  ParserName.ISRACARD, "0.1.0")
        s.commit()
        stmt2 = persist_statement(s, "ariel", src.id, file_id, _result(),
                                  ParserName.ISRACARD, "0.1.0")
        s.commit()
        assert stmt1.id == stmt2.id
        assert s.query(ExpenseStatement).count() == 1


def test_persist_transactions_dedupes_by_content_hash(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        file_id = _seed(s)
        src = register_or_get_source(s, "ariel", SourceHint(
            kind="card", issuer="isracard", external_id="1266"))
        s.commit()
        result = _result()
        stmt = persist_statement(s, "ariel", src.id, file_id, result,
                                 ParserName.ISRACARD, "0.1.0")
        s.commit()
        n1 = persist_transactions(s, stmt, src.id, "ariel", result.transactions)
        s.commit()
        n2 = persist_transactions(s, stmt, src.id, "ariel", result.transactions)
        s.commit()
        assert n1 == 1
        assert n2 == 0
        assert s.query(ExpenseTransaction).count() == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_expense_persistence.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement persistence**

Create `argosy/services/expense_ingest/persistence.py`:

```python
"""Persistence helpers — idempotent inserts for statements + transactions.

Statement uniqueness: (user_id, source_id, period_start, period_end).
Transaction content-hash key: (statement_id, occurred_on, merchant_raw,
amount_nis, reference). Re-running on the same parsed file produces zero
new transaction rows.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.services.expense_ingest.types import (
    NormalizedTransaction, ParseResult, ParserName,
)
from argosy.state.models import ExpenseStatement, ExpenseTransaction


def _content_key(statement_id: int, tx: NormalizedTransaction) -> str:
    parts = [
        str(statement_id),
        tx.occurred_on.isoformat(),
        tx.merchant_raw,
        f"{tx.amount_nis:.2f}",
        tx.reference or "",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def persist_statement(
    session: Session,
    user_id: str,
    source_id: int,
    file_id: int,
    result: ParseResult,
    parser: ParserName,
    parser_version: str,
) -> ExpenseStatement:
    """Find or insert the ExpenseStatement row for this parse result."""
    existing = session.query(ExpenseStatement).filter_by(
        user_id=user_id, source_id=source_id,
        period_start=result.statement.period_start,
        period_end=result.statement.period_end,
    ).one_or_none()
    if existing is not None:
        return existing

    stmt = ExpenseStatement(
        user_id=user_id, source_id=source_id, file_id=file_id,
        period_start=result.statement.period_start,
        period_end=result.statement.period_end,
        charge_date=result.statement.charge_date,
        declared_total_nis=Decimal(str(result.statement.declared_total_nis))
            if result.statement.declared_total_nis is not None else None,
        parsed_total_nis=Decimal(str(result.statement.parsed_total_nis)),
        parser_name=parser.value,
        parser_version=parser_version,
        status="parsed",
    )
    session.add(stmt)
    session.flush()
    return stmt


def persist_transactions(
    session: Session,
    stmt: ExpenseStatement,
    source_id: int,
    user_id: str,
    txs: list[NormalizedTransaction],
) -> int:
    """Insert transactions for a statement; skip rows whose content hash
    already exists. Returns the count of newly-inserted rows.
    """
    inserted = 0
    seen_keys = set()
    for tx in txs:
        key = _content_key(stmt.id, tx)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        # Cheap dedup: same statement, same merchant, same date, same amount, same ref.
        existing = session.query(ExpenseTransaction).filter_by(
            statement_id=stmt.id, occurred_on=tx.occurred_on,
            merchant_raw=tx.merchant_raw,
        ).filter(
            ExpenseTransaction.amount_nis == Decimal(str(tx.amount_nis)),
            ExpenseTransaction.reference == tx.reference,
        ).first()
        if existing is not None:
            continue
        row = ExpenseTransaction(
            user_id=user_id, statement_id=stmt.id, source_id=source_id,
            occurred_on=tx.occurred_on, posted_on=tx.posted_on,
            merchant_raw=tx.merchant_raw,
            merchant_normalized=tx.merchant_normalized,
            amount_nis=Decimal(str(tx.amount_nis)),
            amount_orig=Decimal(str(tx.amount_orig))
                if tx.amount_orig is not None else None,
            currency_orig=tx.currency_orig,
            direction=tx.direction,
            tx_type=tx.tx_type,
            reference=tx.reference,
            category_id=None,                       # set by category_resolver later
            category_source=None,
            category_confidence=None,
            is_card_payment=False,                  # set by correlator later
            matched_statement_id=None,
            refund_of_id=None,
            raw_row_json=json.dumps(tx.raw_row, ensure_ascii=False, default=str),
        )
        session.add(row)
        inserted += 1
    session.flush()
    return inserted
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_expense_persistence.py -v`
Expected: All three PASS.

- [ ] **Step 5: Commit**

```powershell
git add argosy/services/expense_ingest/persistence.py tests/test_expense_persistence.py
git commit -m "feat(expenses): idempotent statement + transaction persistence"
```

---

### Task 13: Correlator (bank ↔ card statement linkage)

**Files:**
- Create: `argosy/services/expense_ingest/correlator.py`
- Test: `tests/test_expense_correlator.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_expense_correlator.py`:

```python
"""Tests for the bank ↔ card-statement correlator."""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from argosy.services.expense_ingest.correlator import correlate_for_user
from argosy.state.models import (
    ExpenseSource, ExpenseStatement, ExpenseTransaction, User, UserFile,
)


def _seed_minimal(s: Session) -> dict:
    s.add(User(id="ariel", plan="free"))
    s.flush()
    f = UserFile(
        user_id="ariel", sha256="a" * 64, original_name="x",
        sanitized_name="x", mime_type="application/octet-stream",
        kind="other", size_bytes=1, storage_path="/tmp/x",
        source="chat_attachment",
    )
    s.add(f); s.flush()
    bank = ExpenseSource(user_id="ariel", kind="bank", issuer="leumi",
                         external_id="44745280", display_name="Leumi 44745280")
    card = ExpenseSource(user_id="ariel", kind="card", issuer="isracard",
                         external_id="1266", display_name="Isracard 1266")
    s.add_all([bank, card]); s.flush()
    bank_stmt = ExpenseStatement(
        user_id="ariel", source_id=bank.id, file_id=f.id,
        period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
        parsed_total_nis=Decimal("0"), parser_name="leumi_osh",
        parser_version="0.1.0", status="parsed",
    )
    card_stmt = ExpenseStatement(
        user_id="ariel", source_id=card.id, file_id=f.id,
        period_start=date(2026, 3, 16), period_end=date(2026, 4, 15),
        charge_date=date(2026, 4, 15),
        declared_total_nis=Decimal("3319.44"),
        parsed_total_nis=Decimal("3319.44"),
        parser_name="isracard", parser_version="0.1.0", status="parsed",
    )
    s.add_all([bank_stmt, card_stmt]); s.flush()
    bank_tx = ExpenseTransaction(
        user_id="ariel", statement_id=bank_stmt.id, source_id=bank.id,
        occurred_on=date(2026, 4, 15), merchant_raw="ל.מאסטרקרד(יש)",
        merchant_normalized="ל.מאסטרקרד(יש)",
        amount_nis=Decimal("3319.44"), direction="debit", tx_type="regular",
        reference="1266", raw_row_json="{}",
    )
    s.add(bank_tx); s.flush()
    return {"bank": bank, "card": card, "bank_stmt": bank_stmt,
            "card_stmt": card_stmt, "bank_tx": bank_tx}


def test_correlator_links_via_reference_and_amount(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        ctx = _seed_minimal(s)
        s.commit()
        n = correlate_for_user(s, "ariel")
        s.commit()
        assert n == 1
        s.refresh(ctx["bank_tx"])
        assert ctx["bank_tx"].is_card_payment is True
        assert ctx["bank_tx"].matched_statement_id == ctx["card_stmt"].id


def test_correlator_skips_unknown_reference(alembic_engine_at_head):
    """Numeric ref that doesn't match an expense_sources.external_id stays uncorrelated."""
    with Session(alembic_engine_at_head) as s:
        ctx = _seed_minimal(s)
        # Mutate the bank tx to have a non-matching ref
        ctx["bank_tx"].reference = "99999"
        s.commit()
        n = correlate_for_user(s, "ariel")
        s.commit()
        s.refresh(ctx["bank_tx"])
        assert n == 0
        assert ctx["bank_tx"].is_card_payment is False


def test_correlator_amount_fallback(alembic_engine_at_head):
    """When ref is empty but amount + date match a card statement, link."""
    with Session(alembic_engine_at_head) as s:
        ctx = _seed_minimal(s)
        ctx["bank_tx"].reference = None
        s.commit()
        n = correlate_for_user(s, "ariel")
        s.commit()
        s.refresh(ctx["bank_tx"])
        assert n == 1
        assert ctx["bank_tx"].matched_statement_id == ctx["card_stmt"].id


def test_correlator_is_idempotent(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        _seed_minimal(s); s.commit()
        n1 = correlate_for_user(s, "ariel"); s.commit()
        n2 = correlate_for_user(s, "ariel"); s.commit()
        assert n1 == 1
        assert n2 == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_expense_correlator.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the correlator**

Create `argosy/services/expense_ingest/correlator.py`:

```python
"""Bank ↔ card-statement correlator. Marks bank rows that pay a card
statement total so they don't double-count itemized card spend.

Tier 1: bank_tx.reference matches an existing ExpenseSource.external_id
(card kind), amount within tolerance, date within window.

Tier 2: bank_tx.reference is None or unknown — fall back to amount + date
exact match against a single card statement.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from argosy.state.models import (
    ExpenseSource, ExpenseStatement, ExpenseTransaction,
)

# Tunables — exposed via agent_settings later; defaults here for tests.
AMOUNT_TOLERANCE_NIS = Decimal("50")
DATE_WINDOW_DAYS = 2


def _smells_like_card_payment(merchant: str) -> bool:
    keywords = ("ל.מאסטרקרד", "כרטיסי אשראי", "ויזה", "דיינרס",
                "אמריקן אקספרס", "ישראכרט", "מאסטרקרד")
    return any(k in merchant for k in keywords)


def correlate_for_user(session: Session, user_id: str) -> int:
    """Run correlation across all unmatched bank-side rows for this user.

    Returns the number of new matches made.
    """
    # Candidate bank rows: not yet marked, look like a card payment, on a
    # bank-kind source.
    candidates = (
        session.query(ExpenseTransaction)
        .join(ExpenseSource, ExpenseSource.id == ExpenseTransaction.source_id)
        .filter(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.is_card_payment.is_(False),
            ExpenseSource.kind == "bank",
        )
        .all()
    )

    card_sources = session.query(ExpenseSource).filter_by(
        user_id=user_id, kind="card",
    ).all()
    by_external = {src.external_id: src for src in card_sources}

    matches = 0
    for tx in candidates:
        if not _smells_like_card_payment(tx.merchant_raw):
            continue

        stmt: ExpenseStatement | None = None
        # Tier 1: ref matches a known card external_id
        if tx.reference and tx.reference in by_external:
            src = by_external[tx.reference]
            stmt = _find_card_statement(session, src.id, tx.occurred_on,
                                        tx.amount_nis)
        # Tier 2: amount + date fallback
        if stmt is None and (tx.reference is None
                              or tx.reference not in by_external):
            stmt = _find_by_amount_date(session, user_id, tx.amount_nis,
                                        tx.occurred_on)

        if stmt is not None:
            tx.is_card_payment = True
            tx.matched_statement_id = stmt.id
            matches += 1

    session.flush()
    return matches


def _find_card_statement(
    session: Session, source_id: int, target_date, amount: Decimal,
) -> ExpenseStatement | None:
    candidates = session.query(ExpenseStatement).filter(
        ExpenseStatement.source_id == source_id,
        ExpenseStatement.charge_date.isnot(None),
    ).all()
    for stmt in candidates:
        if abs((stmt.charge_date - target_date).days) > DATE_WINDOW_DAYS:
            continue
        if stmt.declared_total_nis is None:
            continue
        if abs(stmt.declared_total_nis - amount) <= AMOUNT_TOLERANCE_NIS:
            return stmt
    return None


def _find_by_amount_date(
    session: Session, user_id: str, amount: Decimal, target_date,
) -> ExpenseStatement | None:
    candidates = session.query(ExpenseStatement).join(
        ExpenseSource, ExpenseSource.id == ExpenseStatement.source_id,
    ).filter(
        ExpenseStatement.user_id == user_id,
        ExpenseSource.kind == "card",
        ExpenseStatement.charge_date == target_date,
    ).all()
    matching = [
        s for s in candidates
        if s.declared_total_nis is not None
        and abs(s.declared_total_nis - amount) < Decimal("0.50")
    ]
    if len(matching) == 1:
        return matching[0]
    return None
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_expense_correlator.py -v`
Expected: All four PASS.

- [ ] **Step 5: Commit**

```powershell
git add argosy/services/expense_ingest/correlator.py tests/test_expense_correlator.py
git commit -m "feat(expenses): bank↔card correlator (אסמכתא reference + amount/date fallback)"
```

---

### Task 14: Refund matcher (post-categorize inheritance)

**Files:**
- Create: `argosy/services/expense_ingest/refund_matcher.py`
- Test: `tests/test_expense_refund_matcher.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_expense_refund_matcher.py`:

```python
"""Refund matcher: links credit rows to prior debits and inherits category."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from argosy.services.expense_ingest.refund_matcher import match_refunds_for_user
from argosy.state.models import (
    ExpenseCategory, ExpenseSource, ExpenseStatement, ExpenseTransaction,
    User, UserFile,
)


def _seed(s: Session, with_prior: bool = True, prior_categorized: bool = True):
    s.add(User(id="ariel", plan="free")); s.flush()
    f = UserFile(user_id="ariel", sha256="a"*64, original_name="x",
                 sanitized_name="x", mime_type="x", kind="other",
                 size_bytes=1, storage_path="/tmp/x",
                 source="chat_attachment")
    s.add(f); s.flush()
    cat = ExpenseCategory(slug="travel.flights", label_en="Flights",
                          label_he="טיסות")
    s.add(cat); s.flush()
    src = ExpenseSource(user_id="ariel", kind="card", issuer="max",
                        external_id="6225", display_name="Max 6225")
    s.add(src); s.flush()
    stmt = ExpenseStatement(
        user_id="ariel", source_id=src.id, file_id=f.id,
        period_start=date(2026, 3, 1), period_end=date(2026, 3, 31),
        charge_date=date(2026, 4, 15), parsed_total_nis=Decimal("0"),
        parser_name="max", parser_version="0.1.0", status="parsed",
    )
    s.add(stmt); s.flush()
    refund = ExpenseTransaction(
        user_id="ariel", statement_id=stmt.id, source_id=src.id,
        occurred_on=date(2026, 3, 21), merchant_raw="WIZZ AIRGR73FH",
        merchant_normalized="wizz airgr73fh",
        amount_nis=Decimal("2097.83"), direction="credit",
        tx_type="refund", raw_row_json="{}",
    )
    s.add(refund); s.flush()
    prior = None
    if with_prior:
        prior = ExpenseTransaction(
            user_id="ariel", statement_id=stmt.id, source_id=src.id,
            occurred_on=date(2026, 2, 12), merchant_raw="WIZZ AIR123",
            merchant_normalized="wizz airgr73fh",
            amount_nis=Decimal("2097.83"), direction="debit",
            tx_type="regular",
            category_id=(cat.id if prior_categorized else None),
            category_source=("user" if prior_categorized else None),
            category_confidence=(Decimal("1.0") if prior_categorized else None),
            raw_row_json="{}",
        )
        s.add(prior); s.flush()
    return {"refund": refund, "prior": prior, "cat": cat}


def test_refund_matcher_inherits_category(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        ctx = _seed(s); s.commit()
        n = match_refunds_for_user(s, "ariel")
        s.commit()
        s.refresh(ctx["refund"])
        assert n == 1
        assert ctx["refund"].refund_of_id == ctx["prior"].id
        assert ctx["refund"].category_id == ctx["cat"].id
        assert ctx["refund"].category_source == "inherited_from_refund"


def test_refund_matcher_skips_when_no_prior(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        ctx = _seed(s, with_prior=False); s.commit()
        n = match_refunds_for_user(s, "ariel")
        s.commit()
        s.refresh(ctx["refund"])
        assert n == 0
        assert ctx["refund"].refund_of_id is None
        assert ctx["refund"].category_id is None


def test_refund_matcher_skips_when_prior_uncategorized(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        ctx = _seed(s, prior_categorized=False); s.commit()
        n = match_refunds_for_user(s, "ariel")
        s.commit()
        s.refresh(ctx["refund"])
        assert n == 0  # nothing to inherit
        assert ctx["refund"].category_id is None


def test_refund_matcher_is_idempotent(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        _seed(s); s.commit()
        n1 = match_refunds_for_user(s, "ariel"); s.commit()
        n2 = match_refunds_for_user(s, "ariel"); s.commit()
        assert n1 == 1
        assert n2 == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_expense_refund_matcher.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the matcher**

Create `argosy/services/expense_ingest/refund_matcher.py`:

```python
"""Refund matcher: links direction='credit' tx_type='refund' rows to a
matching prior debit (same merchant_normalized, similar amount, within 90
days prior) and inherits the prior's category. Runs AFTER categorization.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from argosy.state.models import ExpenseTransaction

LOOKBACK_DAYS = 90
AMOUNT_TOLERANCE_PCT = Decimal("0.05")


def match_refunds_for_user(session: Session, user_id: str) -> int:
    """Inherit category for unmatched refunds. Returns the count newly matched."""
    refunds = session.query(ExpenseTransaction).filter(
        ExpenseTransaction.user_id == user_id,
        ExpenseTransaction.direction == "credit",
        ExpenseTransaction.tx_type == "refund",
        ExpenseTransaction.refund_of_id.is_(None),
    ).all()

    matched = 0
    for refund in refunds:
        cutoff = refund.occurred_on - timedelta(days=LOOKBACK_DAYS)
        tolerance = refund.amount_nis * AMOUNT_TOLERANCE_PCT
        candidates = session.query(ExpenseTransaction).filter(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.merchant_normalized == refund.merchant_normalized,
            ExpenseTransaction.direction == "debit",
            ExpenseTransaction.occurred_on >= cutoff,
            ExpenseTransaction.occurred_on < refund.occurred_on,
            ExpenseTransaction.amount_nis >= refund.amount_nis - tolerance,
            ExpenseTransaction.amount_nis <= refund.amount_nis + tolerance,
            ExpenseTransaction.category_id.isnot(None),
        ).order_by(ExpenseTransaction.occurred_on.desc()).all()
        if not candidates:
            continue
        prior = candidates[0]
        refund.refund_of_id = prior.id
        refund.category_id = prior.category_id
        refund.category_source = "inherited_from_refund"
        refund.category_confidence = prior.category_confidence
        matched += 1
    session.flush()
    return matched
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_expense_refund_matcher.py -v`
Expected: All four PASS.

- [ ] **Step 5: Commit**

```powershell
git add argosy/services/expense_ingest/refund_matcher.py tests/test_expense_refund_matcher.py
git commit -m "feat(expenses): refund matcher inherits category from prior debit"
```

---

## Phase D — Categorization

### Task 15: Issuer-seeded category map (Hebrew → slug)

**Files:**
- Create: `argosy/services/expense_ingest/issuer_seed.py`
- Test: `tests/test_expense_issuer_seed.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_expense_issuer_seed.py`:

```python
"""Tests for the Hebrew ענף → slug mapping (Max card pre-categorization)."""

import pytest

from argosy.services.expense_ingest.issuer_seed import (
    map_issuer_category, IssuerSeedResult,
)


@pytest.mark.parametrize("anaf,slug,confidence", [
    ("מסעדות",            "dining_out.restaurants",        0.90),
    ("תיירות",            "travel.vacation_other",          0.85),
    ("רפואה ובריאות",     "healthcare.medical_other",       0.85),
    ("ריהוט ובית",        "housing.home_maintenance",       0.80),
    ("דלק ותחנות דלק",    "transportation.fuel",            0.95),
    ("לבוש והנעלה",       "discretionary.shopping_clothing", 0.90),
])
def test_unambiguous_anaf_maps_directly(anaf, slug, confidence):
    result = map_issuer_category(anaf)
    assert result.slug == slug
    assert result.confidence == confidence
    assert result.defer_to_llm is False


@pytest.mark.parametrize("anaf", [
    "ביטוח ופיננסים",
    "תקשורת ומחשבים",
    "מקצועות חופשיים",
])
def test_ambiguous_anaf_defers_to_llm(anaf):
    result = map_issuer_category(anaf)
    assert result.slug is None
    assert result.defer_to_llm is True
    assert result.hint == anaf


def test_unknown_anaf_defers_with_hint():
    result = map_issuer_category("בלה בלה ענף לא ידוע")
    assert result.defer_to_llm is True
    assert result.hint == "בלה בלה ענף לא ידוע"
    assert result.slug is None


def test_none_input_returns_no_seed():
    result = map_issuer_category(None)
    assert result.slug is None
    assert result.defer_to_llm is False
    assert result.hint is None
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_expense_issuer_seed.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the mapping**

Create `argosy/services/expense_ingest/issuer_seed.py`:

```python
"""Issuer-seeded category mapping for cards that pre-categorize (Max).

Two outcomes:
  * UNAMBIGUOUS: map directly to one slug with calibrated confidence.
  * AMBIGUOUS: defer to the LLM, passing the original Hebrew label as a hint.

When sample data shows new ענף values, extend the unambiguous map.
The CLI ``argosy admin expenses-issuer-coverage`` (Task 27) reports any
unmapped values seen in production.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IssuerSeedResult:
    slug: str | None             # None when defer_to_llm or no input
    confidence: float            # 0.0–1.0
    defer_to_llm: bool           # True when ambiguous
    hint: str | None             # original Hebrew passed to LLM as hint


# Unambiguous mappings (slug, confidence). Extend as new sample data arrives.
_UNAMBIGUOUS: dict[str, tuple[str, float]] = {
    "מסעדות":             ("dining_out.restaurants",         0.90),
    "תיירות":             ("travel.vacation_other",          0.85),
    "רפואה ובריאות":      ("healthcare.medical_other",       0.85),
    "ריהוט ובית":         ("housing.home_maintenance",       0.80),
    "סופרמרקטים":         ("food.groceries",                 0.90),
    "חנויות מזון":        ("food.groceries",                 0.90),
    "דלק ותחנות דלק":     ("transportation.fuel",            0.95),
    "לבוש והנעלה":        ("discretionary.shopping_clothing", 0.90),
    "בידור ותרבות":       ("discretionary.entertainment",    0.85),
}

# Ambiguous — known to span multiple slugs; route to LLM with hint.
_AMBIGUOUS: set[str] = {
    "ביטוח ופיננסים",     # could be insurance_other.* or financial.* or income.*
    "תקשורת ומחשבים",     # could be subscriptions.software / housing.internet_phone / discretionary.entertainment
    "מקצועות חופשיים",    # accountant / lawyer / contractor / consultant
}


def map_issuer_category(anaf: str | None) -> IssuerSeedResult:
    if anaf is None:
        return IssuerSeedResult(slug=None, confidence=0.0,
                                defer_to_llm=False, hint=None)
    anaf = anaf.strip()
    if anaf in _UNAMBIGUOUS:
        slug, conf = _UNAMBIGUOUS[anaf]
        return IssuerSeedResult(slug=slug, confidence=conf,
                                defer_to_llm=False, hint=None)
    if anaf in _AMBIGUOUS:
        return IssuerSeedResult(slug=None, confidence=0.50,
                                defer_to_llm=True, hint=anaf)
    # Unknown — defer with hint
    return IssuerSeedResult(slug=None, confidence=0.40,
                            defer_to_llm=True, hint=anaf)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_expense_issuer_seed.py -v`
Expected: All 11 PASS.

- [ ] **Step 5: Commit**

```powershell
git add argosy/services/expense_ingest/issuer_seed.py tests/test_expense_issuer_seed.py
git commit -m "feat(expenses): Hebrew ענף → slug map (Max card seed; ambiguous values defer to LLM)"
```

---

### Task 16: HouseholdCategorizerAgent (LLM, batched)

**Files:**
- Create: `argosy/agents/household_categorizer_types.py`
- Create: `argosy/agents/household_categorizer.py`
- Modify: `argosy/agents/base.py` (`DEFAULT_MODEL_BY_ROLE['household_categorizer'] = 'sonnet'`)
- Test: `tests/test_household_categorizer_unit.py` (mocked LLM)

- [ ] **Step 1: Survey the existing BaseAgent surface**

Read `argosy/agents/base.py` to confirm the `BaseAgent` API: kwarg-only `user_id`, `_call_via_api_key` / `_call_via_claude_code_inner`, structured-output validation. Match the existing agent style (see `argosy/agents/plan_synthesizer.py` for a typed-output exemplar).

- [ ] **Step 2: Write failing types tests**

Create `tests/test_household_categorizer_unit.py`:

```python
"""Unit tests for HouseholdCategorizerAgent — uses a mock LLM, no live calls."""

from datetime import date
from unittest.mock import patch

import pytest

from argosy.agents.household_categorizer_types import (
    CategorizeRow, CategorizeRequest, CategorizeResult, CategorizeResponse,
)


def _row(tx_id: int, merchant: str, amount: float = 100.0,
         direction: str = "debit", issuer: str = "isracard",
         hint: str | None = None) -> CategorizeRow:
    return CategorizeRow(
        tx_id=tx_id, merchant_normalized=merchant.lower(),
        merchant_raw=merchant, amount_nis=amount, direction=direction,
        occurred_on=date(2026, 4, 8),
        issuer_kind="card", issuer_name=issuer, issuer_category_he=hint,
    )


def test_categorize_row_construction():
    r = _row(1, "NETFLIX.COM")
    assert r.tx_id == 1
    assert r.amount_nis == 100.0


def test_categorize_request_round_trip():
    req = CategorizeRequest(
        transactions=[_row(1, "NETFLIX.COM"), _row(2, "WOLT")],
        taxonomy=["dining_out.takeout", "subscriptions.streaming"],
    )
    assert len(req.transactions) == 2


def test_categorize_response_parses_results():
    resp = CategorizeResponse(
        results=[
            CategorizeResult(tx_id=1, category_slug="subscriptions.streaming",
                             confidence=0.95, rationale="Netflix is streaming"),
            CategorizeResult(tx_id=2, category_slug="uncategorized",
                             confidence=0.40, rationale="ambiguous"),
        ],
        model="sonnet", tokens_in=100, tokens_out=50, cost_usd=0.001,
    )
    assert resp.results[0].confidence == 0.95


def test_categorize_result_validation_on_confidence_range():
    with pytest.raises(Exception):
        CategorizeResult(tx_id=1, category_slug="x", confidence=1.5,
                         rationale="x")
    with pytest.raises(Exception):
        CategorizeResult(tx_id=1, category_slug="x", confidence=-0.1,
                         rationale="x")


@patch("argosy.agents.household_categorizer.HouseholdCategorizerAgent._invoke_llm")
def test_agent_returns_uncategorized_below_threshold(mock_llm):
    """Even when the LLM picks a slug, confidence < 0.85 → uncategorized."""
    from argosy.agents.household_categorizer import HouseholdCategorizerAgent
    mock_llm.return_value = CategorizeResponse(
        results=[CategorizeResult(tx_id=1, category_slug="dining_out.restaurants",
                                  confidence=0.50, rationale="weak signal")],
        model="sonnet", tokens_in=10, tokens_out=5, cost_usd=0.0001,
    )
    agent = HouseholdCategorizerAgent(user_id="ariel")
    out = agent.categorize_batch([_row(1, "Vendor X")], taxonomy=["dining_out.restaurants"])
    assert out[0].category_slug == "uncategorized"
    assert out[0].confidence == 0.50


@patch("argosy.agents.household_categorizer.HouseholdCategorizerAgent._invoke_llm")
def test_agent_passes_through_high_confidence(mock_llm):
    from argosy.agents.household_categorizer import HouseholdCategorizerAgent
    mock_llm.return_value = CategorizeResponse(
        results=[CategorizeResult(tx_id=1, category_slug="subscriptions.streaming",
                                  confidence=0.95, rationale="Netflix")],
        model="sonnet", tokens_in=10, tokens_out=5, cost_usd=0.0001,
    )
    agent = HouseholdCategorizerAgent(user_id="ariel")
    out = agent.categorize_batch([_row(1, "NETFLIX.COM")],
                                  taxonomy=["subscriptions.streaming"])
    assert out[0].category_slug == "subscriptions.streaming"
    assert out[0].confidence == 0.95
```

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/test_household_categorizer_unit.py -v`
Expected: ImportError.

- [ ] **Step 4: Implement types**

Create `argosy/agents/household_categorizer_types.py`:

```python
"""Pydantic types for HouseholdCategorizerAgent's I/O."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class CategorizeRow(BaseModel):
    tx_id: int
    merchant_normalized: str
    merchant_raw: str
    amount_nis: float
    direction: Literal["debit", "credit"]
    occurred_on: date
    issuer_kind: Literal["bank", "card"]
    issuer_name: str
    issuer_category_he: str | None = None


class CategorizeRequest(BaseModel):
    transactions: list[CategorizeRow]
    taxonomy: list[str]


class CategorizeResult(BaseModel):
    tx_id: int
    category_slug: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class CategorizeResponse(BaseModel):
    results: list[CategorizeResult]
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
```

- [ ] **Step 5: Implement the agent**

Create `argosy/agents/household_categorizer.py`:

```python
"""HouseholdCategorizerAgent: batched LLM categorization with confidence
threshold ≥ 0.85. Below threshold → 'uncategorized' (preserved confidence
so callers can sort/route).

Refunds (direction='credit' AND tx_type='refund') are filtered out by the
orchestrator before the batch — the matcher inherits their category later.
If a refund somehow reaches this agent, it returns uncategorized with
rationale='refund — should be matched to prior purchase'.
"""

from __future__ import annotations

import json
from typing import Any

from argosy.agents.base import BaseAgent
from argosy.agents.household_categorizer_types import (
    CategorizeRequest, CategorizeResponse, CategorizeResult, CategorizeRow,
)

CONFIDENCE_THRESHOLD = 0.85


SYSTEM_PROMPT = """You are the household-budget categorizer on the Argosy fleet.
The user runs a household in Israel. You categorize each transaction into ONE
slug from the taxonomy provided, or return 'uncategorized'.

Rules:
- If you are not at least 0.85 confident, return 'uncategorized'. Do NOT guess.
  The user prefers reviewing uncategorized rows over accepting wrong categories.
- Refunds (direction='credit' AND tx_type='refund') should not normally appear
  in this batch — the orchestrator splits them out before the LLM call. If one
  does appear, return 'uncategorized' with rationale='refund — should be
  matched to prior purchase'.
- issuer_category_he is a hint, not gospel. Override when wrong.
- Hebrew merchant names: judge by the most recognizable substring, not exact
  prefix matching.
- Foreign merchants: use the post-prefix substring (PAYPAL *X → X).

Output: a JSON array of one result per input transaction, matching tx_id order.
Each result has fields: tx_id, category_slug (one of the taxonomy slugs OR
'uncategorized'), confidence (float 0..1), rationale (one sentence).
"""


class HouseholdCategorizerAgent(BaseAgent):
    """Sonnet, batched ~50 tx/call."""

    agent_role = "household_categorizer"

    def categorize_batch(
        self, rows: list[CategorizeRow], taxonomy: list[str],
    ) -> list[CategorizeResult]:
        request = CategorizeRequest(transactions=rows, taxonomy=taxonomy)
        response = self._invoke_llm(request)
        # Map results back, applying confidence threshold
        thresholded: list[CategorizeResult] = []
        for r in response.results:
            if r.confidence < CONFIDENCE_THRESHOLD:
                thresholded.append(CategorizeResult(
                    tx_id=r.tx_id, category_slug="uncategorized",
                    confidence=r.confidence,
                    rationale=f"below-threshold ({r.rationale})",
                ))
            else:
                thresholded.append(r)
        return thresholded

    def _invoke_llm(self, request: CategorizeRequest) -> CategorizeResponse:
        """Build the user prompt and call the model. Override target for
        unit tests via @patch — keeps the agent testable without a backend.
        """
        user_prompt = self._build_user_prompt(request)
        # Use BaseAgent's dispatch — typed for structured output
        result = self._run_structured(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_type=CategorizeResponse,
        )
        return result

    def _build_user_prompt(self, request: CategorizeRequest) -> str:
        tx_lines = []
        for r in request.transactions:
            tx_lines.append(json.dumps({
                "tx_id": r.tx_id,
                "merchant_normalized": r.merchant_normalized,
                "merchant_raw": r.merchant_raw,
                "amount_nis": r.amount_nis,
                "direction": r.direction,
                "occurred_on": r.occurred_on.isoformat(),
                "issuer_kind": r.issuer_kind,
                "issuer_name": r.issuer_name,
                "issuer_category_he": r.issuer_category_he,
            }, ensure_ascii=False))
        return (
            "<taxonomy>\n" + "\n".join(request.taxonomy) + "\n</taxonomy>\n\n"
            "<transactions>\n[\n" + ",\n".join(tx_lines) + "\n]\n</transactions>"
        )
```

- [ ] **Step 6: Register the role + default model**

Edit `argosy/agents/base.py`. Find `DEFAULT_MODEL_BY_ROLE` and add:

```python
"household_categorizer": "sonnet",
```

- [ ] **Step 7: Run tests**

Run: `pytest tests/test_household_categorizer_unit.py -v`
Expected: All six PASS.

If the existing `BaseAgent._run_structured` API differs from what I assumed (`response_type=...`), conform to the real signature — read `argosy/agents/plan_synthesizer.py` for the canonical pattern. The `_invoke_llm` indirection is specifically there so unit tests can patch it without exercising the SDK.

- [ ] **Step 8: Commit**

```powershell
git add argosy/agents/household_categorizer_types.py `
        argosy/agents/household_categorizer.py `
        argosy/agents/base.py `
        tests/test_household_categorizer_unit.py
git commit -m "feat(agents): HouseholdCategorizerAgent with confidence threshold + batched I/O"
```

---

### Task 17: Category resolver (the cascade)

**Files:**
- Create: `argosy/services/expense_ingest/category_resolver.py`
- Test: `tests/test_expense_category_resolver.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_expense_category_resolver.py`:

```python
"""Tests for the category cascade: user → issuer → cache → LLM → uncategorized."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy.orm import Session

from argosy.agents.household_categorizer_types import CategorizeResult
from argosy.services.expense_ingest.category_resolver import (
    resolve_categories_for_user,
)
from argosy.services.expense_ingest.taxonomy_seed import (
    seed_system_defaults, seed_user_categories,
)
from argosy.state.models import (
    ExpenseCategory, ExpenseSource, ExpenseStatement, ExpenseTransaction,
    MerchantCategoryCache, User, UserFile,
)


def _seed_world(s: Session) -> dict:
    s.add(User(id="ariel", plan="free")); s.flush()
    seed_system_defaults(s)
    seed_user_categories(s, "ariel")
    s.flush()
    f = UserFile(user_id="ariel", sha256="a"*64, original_name="x",
                 sanitized_name="x", mime_type="x", kind="other",
                 size_bytes=1, storage_path="/tmp/x", source="chat_attachment")
    s.add(f); s.flush()
    src = ExpenseSource(user_id="ariel", kind="card", issuer="max",
                        external_id="6225", display_name="Max 6225")
    s.add(src); s.flush()
    stmt = ExpenseStatement(
        user_id="ariel", source_id=src.id, file_id=f.id,
        period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
        parsed_total_nis=Decimal("0"), parser_name="max",
        parser_version="0.1.0", status="parsed",
    )
    s.add(stmt); s.flush()
    return {"src": src, "stmt": stmt}


def _add_tx(s: Session, ctx: dict, *, merchant: str, anaf: str | None = None,
            direction: str = "debit", tx_type: str = "regular") -> ExpenseTransaction:
    tx = ExpenseTransaction(
        user_id="ariel", statement_id=ctx["stmt"].id, source_id=ctx["src"].id,
        occurred_on=date(2026, 4, 10), merchant_raw=merchant,
        merchant_normalized=merchant.lower(),
        amount_nis=Decimal("100"), direction=direction, tx_type=tx_type,
        raw_row_json="{}",
    )
    if anaf:
        tx.raw_row_json = f'{{"anaf": "{anaf}"}}'
    s.add(tx); s.flush()
    return tx


def test_user_override_wins(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        ctx = _seed_world(s)
        cat = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="food.groceries").one()
        s.add(MerchantCategoryCache(
            user_id="ariel", merchant_pattern="netflix.com",
            category_id=cat.id, source="user", confidence=Decimal("1.00"),
        ))
        s.commit()
        tx = _add_tx(s, ctx, merchant="NETFLIX.COM")
        s.commit()
        with patch("argosy.services.expense_ingest.category_resolver"
                   "._categorize_via_llm") as mock_llm:
            n = resolve_categories_for_user(s, "ariel")
            s.commit()
            mock_llm.assert_not_called()  # cache hit short-circuits LLM
        s.refresh(tx)
        assert tx.category_id == cat.id
        assert tx.category_source == "cache"  # via cache, not 'user' direct


def test_issuer_seed_wins_when_unambiguous(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        ctx = _seed_world(s); s.commit()
        tx = _add_tx(s, ctx, merchant="ספייס אינביידרז", anaf="מסעדות")
        s.commit()
        with patch("argosy.services.expense_ingest.category_resolver"
                   "._categorize_via_llm") as mock_llm:
            n = resolve_categories_for_user(s, "ariel")
            s.commit()
            mock_llm.assert_not_called()
        s.refresh(tx)
        cat = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="dining_out.restaurants").one()
        assert tx.category_id == cat.id
        assert tx.category_source == "issuer"


def test_llm_called_for_unknown(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        ctx = _seed_world(s); s.commit()
        tx = _add_tx(s, ctx, merchant="UNKNOWN VENDOR XYZ")
        s.commit()
        with patch("argosy.services.expense_ingest.category_resolver"
                   "._categorize_via_llm") as mock_llm:
            mock_llm.return_value = [CategorizeResult(
                tx_id=tx.id, category_slug="dining_out.restaurants",
                confidence=0.95, rationale="Looks like a restaurant",
            )]
            resolve_categories_for_user(s, "ariel")
            s.commit()
            assert mock_llm.call_count == 1
        s.refresh(tx)
        cat = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="dining_out.restaurants").one()
        assert tx.category_id == cat.id
        assert tx.category_source == "llm"
        # Cache row must have been written
        cache = s.query(MerchantCategoryCache).filter_by(
            merchant_pattern="unknown vendor xyz").one()
        assert cache.source == "llm"


def test_uncategorized_below_threshold(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        ctx = _seed_world(s); s.commit()
        tx = _add_tx(s, ctx, merchant="WEIRD MERCHANT")
        s.commit()
        with patch("argosy.services.expense_ingest.category_resolver"
                   "._categorize_via_llm") as mock_llm:
            mock_llm.return_value = [CategorizeResult(
                tx_id=tx.id, category_slug="uncategorized",
                confidence=0.30, rationale="ambiguous",
            )]
            resolve_categories_for_user(s, "ariel")
            s.commit()
        s.refresh(tx)
        unc = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="uncategorized").one()
        assert tx.category_id == unc.id
        assert tx.category_source == "llm"


def test_refunds_not_sent_to_resolver(alembic_engine_at_head):
    """Refunds must be filtered out before the resolver runs."""
    with Session(alembic_engine_at_head) as s:
        ctx = _seed_world(s); s.commit()
        refund = _add_tx(s, ctx, merchant="WIZZ AIR",
                          direction="credit", tx_type="refund")
        s.commit()
        with patch("argosy.services.expense_ingest.category_resolver"
                   "._categorize_via_llm") as mock_llm:
            resolve_categories_for_user(s, "ariel")
            s.commit()
            mock_llm.assert_not_called()
        s.refresh(refund)
        assert refund.category_id is None
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_expense_category_resolver.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the resolver**

Create `argosy/services/expense_ingest/category_resolver.py`:

```python
"""Category resolver — the cascade.

Order, per spec §7.1 (non-refund rows only):
  1. user-override cache hit  → category, source='cache' (cached value),
                                  confidence=1.00
  2. issuer-seeded category    → unambiguous → use slug, source='issuer'
                                  ambiguous   → drop hint and fall through
  3. LLM cache hit              → reuse cached LLM verdict
  4. LLM batch call             → write cache, ≥0.85 → category;
                                  <0.85 → 'uncategorized', source='llm'

Refunds are filtered out BEFORE this stage; their category is set later by
``refund_matcher`` from the matched prior debit.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from argosy.agents.household_categorizer import HouseholdCategorizerAgent
from argosy.agents.household_categorizer_types import (
    CategorizeResult, CategorizeRow,
)
from argosy.services.expense_ingest.issuer_seed import map_issuer_category
from argosy.state.models import (
    ExpenseCategory, ExpenseSource, ExpenseTransaction, MerchantCategoryCache,
)


def resolve_categories_for_user(session: Session, user_id: str) -> int:
    """Resolve categories for all uncategorized non-refund transactions.

    Returns the count of rows newly categorized.
    """
    candidates = session.query(ExpenseTransaction).filter(
        ExpenseTransaction.user_id == user_id,
        ExpenseTransaction.category_id.is_(None),
        ~((ExpenseTransaction.direction == "credit") &
          (ExpenseTransaction.tx_type == "refund")),
    ).all()
    if not candidates:
        return 0

    cats_by_slug = {
        c.slug: c for c in session.query(ExpenseCategory).filter_by(
            user_id=user_id
        ).all()
    }
    uncat_id = cats_by_slug["uncategorized"].id
    cache_by_pattern = {
        r.merchant_pattern: r for r in session.query(MerchantCategoryCache)
        .filter_by(user_id=user_id, is_regex=False).all()
    }

    llm_batch: list[tuple[ExpenseTransaction, str | None]] = []
    resolved = 0

    for tx in candidates:
        # 1. Cache hit
        cached = cache_by_pattern.get(tx.merchant_normalized)
        if cached is not None:
            tx.category_id = cached.category_id
            tx.category_source = "cache"
            tx.category_confidence = cached.confidence
            cached.hit_count += 1
            cached.last_hit_at = datetime.utcnow()
            resolved += 1
            continue

        # 2. Issuer seed
        anaf = _extract_anaf_from_raw_row(tx.raw_row_json)
        seed = map_issuer_category(anaf)
        if seed.slug is not None and not seed.defer_to_llm:
            cat = cats_by_slug.get(seed.slug)
            if cat is not None:
                tx.category_id = cat.id
                tx.category_source = "issuer"
                tx.category_confidence = Decimal(str(seed.confidence))
                resolved += 1
                continue

        # 3+4. Defer to LLM (with hint if ambiguous)
        llm_batch.append((tx, seed.hint))

    if llm_batch:
        rows = [
            CategorizeRow(
                tx_id=tx.id,
                merchant_normalized=tx.merchant_normalized,
                merchant_raw=tx.merchant_raw,
                amount_nis=float(tx.amount_nis),
                direction=tx.direction,
                occurred_on=tx.occurred_on,
                issuer_kind=session.get(ExpenseSource, tx.source_id).kind,
                issuer_name=session.get(ExpenseSource, tx.source_id).issuer,
                issuer_category_he=hint,
            )
            for tx, hint in llm_batch
        ]
        results = _categorize_via_llm(user_id, rows)
        results_by_id = {r.tx_id: r for r in results}
        for tx, _ in llm_batch:
            r = results_by_id.get(tx.id)
            if r is None:
                continue
            slug = r.category_slug if r.category_slug != "uncategorized" else "uncategorized"
            cat = cats_by_slug.get(slug, cats_by_slug["uncategorized"])
            tx.category_id = cat.id
            tx.category_source = "llm"
            tx.category_confidence = Decimal(str(r.confidence))
            resolved += 1
            # Cache only confident, slug-bearing results
            if r.category_slug != "uncategorized":
                session.add(MerchantCategoryCache(
                    user_id=user_id,
                    merchant_pattern=tx.merchant_normalized,
                    category_id=cat.id,
                    source="llm",
                    confidence=Decimal(str(r.confidence)),
                    hit_count=1,
                    last_hit_at=datetime.utcnow(),
                ))

    session.flush()
    return resolved


def _extract_anaf_from_raw_row(raw_row_json: str) -> str | None:
    """Pull the Max 'ענף' field out of raw_row_json if present."""
    try:
        data = json.loads(raw_row_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        anaf = data.get("anaf")
        if isinstance(anaf, str) and anaf.strip():
            return anaf.strip()
    return None


def _categorize_via_llm(
    user_id: str, rows: list[CategorizeRow],
) -> list[CategorizeResult]:
    """Indirection seam — patched in unit tests."""
    agent = HouseholdCategorizerAgent(user_id=user_id)
    # Build the taxonomy slug list for the prompt
    from argosy.services.expense_ingest.taxonomy_seed import DEFAULT_TAXONOMY
    taxonomy = [e.slug for e in DEFAULT_TAXONOMY]
    BATCH_SIZE = 50
    out: list[CategorizeResult] = []
    for i in range(0, len(rows), BATCH_SIZE):
        chunk = rows[i : i + BATCH_SIZE]
        out.extend(agent.categorize_batch(chunk, taxonomy))
    return out
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_expense_category_resolver.py -v`
Expected: All five PASS.

- [ ] **Step 5: Commit**

```powershell
git add argosy/services/expense_ingest/category_resolver.py `
        tests/test_expense_category_resolver.py
git commit -m "feat(expenses): category resolver cascade (user → issuer → cache → LLM)"
```

---

### Task 18: Pipeline orchestrator (assembled flow)

**Files:**
- Create: `argosy/services/expense_ingest/orchestrator.py`
- Test: `tests/test_expense_orchestrator.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_expense_orchestrator.py`:

```python
"""End-to-end orchestrator tests using synthetic fixture files."""

from pathlib import Path
from unittest.mock import patch

from sqlalchemy.orm import Session

from argosy.agents.household_categorizer_types import CategorizeResult
from argosy.services.expense_ingest.orchestrator import ingest_user_file
from argosy.state.models import (
    ExpenseSource, ExpenseStatement, ExpenseTransaction, User, UserFile,
)

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"


def _file(s: Session, *, path: Path, mime: str) -> int:
    s.add(User(id="ariel", plan="free")); s.flush()
    f = UserFile(
        user_id="ariel", sha256="a"*64, original_name=path.name,
        sanitized_name=path.name, mime_type=mime, kind="other",
        size_bytes=path.stat().st_size, storage_path=str(path),
        source="chat_attachment",
    )
    s.add(f); s.flush()
    return f.id


def test_orchestrator_ingests_max_fixture(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s, \
         patch("argosy.services.expense_ingest.category_resolver"
               "._categorize_via_llm") as mock_llm:
        mock_llm.return_value = []   # nothing routes to LLM (all unambiguous ענף)
        file_id = _file(s, path=FIXTURES / "max_minimal.xlsx",
                         mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        s.commit()
        result = ingest_user_file(s, "ariel", file_id)
        s.commit()
        assert result.statement_id is not None
        assert s.query(ExpenseTransaction).count() == 5
        assert s.query(ExpenseSource).filter_by(issuer="max").count() == 1


def test_orchestrator_idempotent(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s, \
         patch("argosy.services.expense_ingest.category_resolver"
               "._categorize_via_llm") as mock_llm:
        mock_llm.return_value = []
        file_id = _file(s, path=FIXTURES / "max_minimal.xlsx",
                         mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        s.commit()
        r1 = ingest_user_file(s, "ariel", file_id); s.commit()
        r2 = ingest_user_file(s, "ariel", file_id); s.commit()
        # Same statement_id, no new tx rows the second time
        assert r1.statement_id == r2.statement_id
        assert r2.transactions_inserted == 0
        assert s.query(ExpenseTransaction).count() == 5


def test_orchestrator_correlates_after_both_ingested(alembic_engine_at_head):
    """Ingest a Leumi statement first, then an Isracard — correlation should
    fire and mark the bank's card-payment row as is_card_payment."""
    with Session(alembic_engine_at_head) as s, \
         patch("argosy.services.expense_ingest.category_resolver"
               "._categorize_via_llm") as mock_llm:
        # The mock returns dining_out for everything to keep the resolver happy
        def fake(_user_id, rows):
            return [
                CategorizeResult(tx_id=r.tx_id,
                                 category_slug="discretionary.shopping_other",
                                 confidence=0.90, rationale="x")
                for r in rows
            ]
        mock_llm.side_effect = fake

        leumi_file_id = _file(s, path=FIXTURES / "leumi_osh_minimal.xls",
                               mime="application/vnd.ms-excel")
        s.commit()
        ingest_user_file(s, "ariel", leumi_file_id); s.commit()

        # Add an Isracard statement matching the Leumi card-payment row of 3319.44
        isracard_file_id = _file(s, path=FIXTURES / "isracard_minimal.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        # The minimal fixture sums to a different total — for this test we
        # patch declared total post-ingest so the correlator finds a match.
        s.commit()
        ingest_user_file(s, "ariel", isracard_file_id); s.commit()

        # Force the Isracard statement's declared_total + charge_date to align
        # with the Leumi 3319.44 / 15.04 row, then run correlation:
        from argosy.services.expense_ingest.correlator import correlate_for_user
        from datetime import date
        from decimal import Decimal
        ica_stmt = (s.query(ExpenseStatement)
                     .filter_by(parser_name="isracard").one())
        ica_stmt.declared_total_nis = Decimal("3319.44")
        ica_stmt.charge_date = date(2026, 4, 15)
        s.commit()
        correlate_for_user(s, "ariel"); s.commit()

        bank_tx = s.query(ExpenseTransaction).filter(
            ExpenseTransaction.merchant_raw.contains("מאסטרקרד")
        ).one()
        assert bank_tx.is_card_payment is True
        assert bank_tx.matched_statement_id == ica_stmt.id
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_expense_orchestrator.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the orchestrator**

Create `argosy/services/expense_ingest/orchestrator.py`:

```python
"""End-to-end ingest pipeline. Idempotent on user_files.id.

Order:
  1. Sniff format → dispatch to the right parser.
  2. Register/get source from parser hint (or infer from Leumi headers).
  3. Persist statement (idempotent on user, source, period).
  4. Persist transactions (content-hash dedup).
  5. Correlate bank ↔ card statements (mark is_card_payment, link).
  6. Resolve categories (cascade — refunds skipped).
  7. Match refunds to prior debits (inherit category).
  8. Seed user categories on first run for this user.

Returns IngestResult with statement_id and counts so callers (REST, CLI)
can render a useful response.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from argosy.services.expense_ingest.category_resolver import (
    resolve_categories_for_user,
)
from argosy.services.expense_ingest.correlator import correlate_for_user
from argosy.services.expense_ingest.parsers import (
    leumi_osh as p_leumi, isracard as p_isra, max as p_max,
    cal as p_cal, amex as p_amex, diners as p_diners,
)
from argosy.services.expense_ingest.persistence import (
    persist_statement, persist_transactions,
)
from argosy.services.expense_ingest.refund_matcher import match_refunds_for_user
from argosy.services.expense_ingest.registry import register_or_get_source
from argosy.services.expense_ingest.sniff import detect_format
from argosy.services.expense_ingest.taxonomy_seed import (
    seed_system_defaults, seed_user_categories,
)
from argosy.services.expense_ingest.types import (
    ParseResult, ParserName, SourceHint,
)
from argosy.state.models import ExpenseCategory, UserFile

PARSER_VERSIONS = {
    ParserName.LEUMI_OSH: p_leumi.PARSER_VERSION,
    ParserName.ISRACARD:  p_isra.PARSER_VERSION,
    ParserName.MAX:       p_max.PARSER_VERSION,
}

PARSER_DISPATCH = {
    ParserName.LEUMI_OSH: p_leumi.parse,
    ParserName.ISRACARD:  p_isra.parse,
    ParserName.MAX:       p_max.parse,
    ParserName.CAL:       p_cal.parse,
    ParserName.AMEX:      p_amex.parse,
    ParserName.DINERS:    p_diners.parse,
}


@dataclass
class IngestResult:
    statement_id: int
    transactions_inserted: int
    correlations_made: int
    categories_resolved: int
    refunds_matched: int
    parser_name: str


def _ensure_categories_seeded(session: Session, user_id: str) -> None:
    """First time this user ingests → ensure system defaults + per-user copy."""
    has_user_rows = session.query(ExpenseCategory).filter_by(
        user_id=user_id
    ).first() is not None
    if has_user_rows:
        return
    has_sys_rows = session.query(ExpenseCategory).filter(
        ExpenseCategory.user_id.is_(None)
    ).first() is not None
    if not has_sys_rows:
        seed_system_defaults(session)
        session.flush()
    seed_user_categories(session, user_id)
    session.flush()


def _leumi_source_hint(result: ParseResult) -> SourceHint:
    """Leumi parser doesn't fill source_hint; derive from the header table.
    For now we extract from the bank merchant strings — refine with a header
    parser when the format ever changes.
    """
    return SourceHint(
        kind="bank", issuer="leumi",
        external_id="44745280",          # TODO: extract account # from HTML header
        display_name="Leumi current account",
    )


def ingest_user_file(
    session: Session, user_id: str, file_id: int,
) -> IngestResult:
    """Run the full ingest pipeline for one already-cataloged user file."""
    file = session.get(UserFile, file_id)
    if file is None or file.user_id != user_id:
        raise ValueError(f"UserFile {file_id} not found for user {user_id}")

    _ensure_categories_seeded(session, user_id)

    parser_name = detect_format(Path(file.storage_path))
    parser_fn = PARSER_DISPATCH[parser_name]
    result = parser_fn(Path(file.storage_path))

    hint = result.source_hint or _leumi_source_hint(result)
    src = register_or_get_source(session, user_id, hint)
    session.flush()

    stmt = persist_statement(
        session, user_id, src.id, file.id, result,
        parser_name, PARSER_VERSIONS.get(parser_name, "0.0.0"),
    )
    inserted = persist_transactions(
        session, stmt, src.id, user_id, result.transactions
    )

    correlations = correlate_for_user(session, user_id)
    resolved = resolve_categories_for_user(session, user_id)
    refunds = match_refunds_for_user(session, user_id)

    return IngestResult(
        statement_id=stmt.id,
        transactions_inserted=inserted,
        correlations_made=correlations,
        categories_resolved=resolved,
        refunds_matched=refunds,
        parser_name=parser_name.value,
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_expense_orchestrator.py -v`
Expected: All three PASS.

- [ ] **Step 5: Commit**

```powershell
git add argosy/services/expense_ingest/orchestrator.py tests/test_expense_orchestrator.py
git commit -m "feat(expenses): pipeline orchestrator (idempotent ingest_user_file)"
```

---

## Phase E — Surfaces (REST, WS, CLI, config)

### Task 19: REST — POST /api/expenses/upload

**Files:**
- Create: `argosy/api/routes/expenses.py` (upload endpoint only, more added in later tasks)
- Modify: `argosy/api/main.py` (register router)
- Test: `tests/test_expense_routes.py`

- [ ] **Step 1: Write failing route test**

Create `tests/test_expense_routes.py`:

```python
"""HTTP route tests for /api/expenses/*."""

from io import BytesIO
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"


def test_upload_max_xlsx_returns_parse_summary(client_with_db):
    with open(FIXTURES / "max_minimal.xlsx", "rb") as f:
        files = {"files": ("max_minimal.xlsx", f.read(),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        resp = client_with_db.post("/api/expenses/upload",
                                    files=files,
                                    data={"user_id": "ariel"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["results"]) == 1
    r = body["results"][0]
    assert r["filename"] == "max_minimal.xlsx"
    assert r["status"] == "parsed"
    assert r["transactions_inserted"] == 5


def test_upload_unknown_format_returns_415(client_with_db):
    files = {"files": ("garbage.bin", b"\x00\x01\x02\x03", "application/octet-stream")}
    resp = client_with_db.post("/api/expenses/upload",
                                files=files, data={"user_id": "ariel"})
    assert resp.status_code == 200          # we always 200; per-file status differs
    body = resp.json()
    assert body["results"][0]["status"] == "failed"
    assert "unrecognized" in body["results"][0]["error"].lower() or \
           "unknown" in body["results"][0]["error"].lower()


def test_upload_multi_file(client_with_db):
    files = [
        ("files", ("max_minimal.xlsx",
                   open(FIXTURES / "max_minimal.xlsx", "rb").read(),
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ("files", ("isracard_minimal.xlsx",
                   open(FIXTURES / "isracard_minimal.xlsx", "rb").read(),
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
    ]
    resp = client_with_db.post("/api/expenses/upload",
                                files=files, data={"user_id": "ariel"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 2
    assert all(r["status"] == "parsed" for r in body["results"])
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_expense_routes.py::test_upload_max_xlsx_returns_parse_summary -v`
Expected: 404 (route not registered).

- [ ] **Step 3: Implement the upload route**

Create `argosy/api/routes/expenses.py`:

```python
"""REST surface for the expenses subsystem (Wave EX1)."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from argosy.services.expense_ingest.orchestrator import ingest_user_file
from argosy.services.file_catalog import catalog_upload
from argosy.api.routes.plan import get_db    # reuse the existing get_db dep

router = APIRouter(prefix="/api/expenses", tags=["expenses"])


class UploadFileResult(BaseModel):
    filename: str
    status: str                                # 'parsed' | 'failed'
    statement_id: int | None = None
    transactions_inserted: int = 0
    correlations_made: int = 0
    categories_resolved: int = 0
    refunds_matched: int = 0
    parser_name: str | None = None
    error: str | None = None


class UploadResponse(BaseModel):
    results: list[UploadFileResult]


@router.post("/upload", response_model=UploadResponse)
async def upload_statements(
    files: list[UploadFile] = File(...),
    user_id: str = Form(...),
    db: Annotated[Session, Depends(get_db)] = ...,
) -> UploadResponse:
    """Multi-file ingestion. Each file flows through ``catalog_upload`` then
    ``ingest_user_file``; per-file outcome is reported back."""
    results: list[UploadFileResult] = []
    for upload in files:
        contents = await upload.read()
        try:
            user_file = catalog_upload(
                db, user_id=user_id, original_name=upload.filename,
                contents=contents, mime_type=upload.content_type or "application/octet-stream",
                kind="other", source="chat_attachment",
            )
            db.commit()
        except Exception as e:
            results.append(UploadFileResult(
                filename=upload.filename, status="failed",
                error=f"catalog failure: {e}",
            ))
            continue

        try:
            ing = ingest_user_file(db, user_id, user_file.id)
            db.commit()
        except Exception as e:
            db.rollback()
            results.append(UploadFileResult(
                filename=upload.filename, status="failed",
                error=str(e),
            ))
            continue

        results.append(UploadFileResult(
            filename=upload.filename, status="parsed",
            statement_id=ing.statement_id,
            transactions_inserted=ing.transactions_inserted,
            correlations_made=ing.correlations_made,
            categories_resolved=ing.categories_resolved,
            refunds_matched=ing.refunds_matched,
            parser_name=ing.parser_name,
        ))

    return UploadResponse(results=results)
```

- [ ] **Step 4: Register the router**

Edit `argosy/api/main.py`. Find where existing routers are imported and registered. Add:

```python
from argosy.api.routes import expenses as expenses_routes
# … later, where other routers are included:
app.include_router(expenses_routes.router)
```

- [ ] **Step 5: Run upload tests**

Run: `pytest tests/test_expense_routes.py -v`
Expected: All three upload-related tests PASS. (If `catalog_upload` signature differs from this snippet, conform to the real signature in `argosy/services/file_catalog.py`.)

- [ ] **Step 6: Commit**

```powershell
git add argosy/api/routes/expenses.py argosy/api/main.py tests/test_expense_routes.py
git commit -m "feat(expenses-api): POST /api/expenses/upload (multi-file ingest)"
```

---

### Task 20: REST — sources + transactions list/PATCH

**Files:**
- Modify: `argosy/api/routes/expenses.py` (add endpoints)
- Modify: `tests/test_expense_routes.py` (append cases)

- [ ] **Step 1: Append failing tests**

Append to `tests/test_expense_routes.py`:

```python
def test_list_sources_returns_active_only(client_with_db):
    """Upload an Isracard file, then list sources — should return one row."""
    with open(FIXTURES / "isracard_minimal.xlsx", "rb") as f:
        client_with_db.post("/api/expenses/upload",
                             files={"files": ("isracard_minimal.xlsx",
                                              f.read(),
                                              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                             data={"user_id": "ariel"})
    resp = client_with_db.get("/api/expenses/sources?user_id=ariel")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["sources"]) == 1
    assert body["sources"][0]["issuer"] == "isracard"
    assert body["sources"][0]["external_id"] == "1266"


def test_list_transactions_filters_by_category(client_with_db):
    """Ingest a Max file (issuer-categorized), filter by dining_out."""
    with open(FIXTURES / "max_minimal.xlsx", "rb") as f:
        client_with_db.post("/api/expenses/upload",
                             files={"files": ("max_minimal.xlsx", f.read(),
                                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                             data={"user_id": "ariel"})
    resp = client_with_db.get("/api/expenses/transactions",
                               params={"user_id": "ariel",
                                       "category": "dining_out.restaurants"})
    assert resp.status_code == 200
    body = resp.json()
    # The Max fixture has one row in 'מסעדות' → dining_out.restaurants
    assert any("ספייס" in t["merchant_raw"] for t in body["transactions"])


def test_patch_transaction_category_updates_cache(client_with_db):
    with open(FIXTURES / "max_minimal.xlsx", "rb") as f:
        client_with_db.post("/api/expenses/upload",
                             files={"files": ("max_minimal.xlsx", f.read(),
                                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                             data={"user_id": "ariel"})
    resp = client_with_db.get("/api/expenses/transactions",
                               params={"user_id": "ariel"})
    tx_id = resp.json()["transactions"][0]["id"]
    # Override category
    upd = client_with_db.patch(f"/api/expenses/transactions/{tx_id}",
                                json={"user_id": "ariel",
                                      "category_slug": "discretionary.entertainment"})
    assert upd.status_code == 200
    body = upd.json()
    assert body["category_source"] == "user"
    assert body["affected_count"] >= 1   # at least the row we patched
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_expense_routes.py -v -k "sources or transactions"`
Expected: 404s.

- [ ] **Step 3: Add endpoints**

Append to `argosy/api/routes/expenses.py`:

```python
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from argosy.state.models import (
    ExpenseCategory, ExpenseSource, ExpenseTransaction, MerchantCategoryCache,
)


class SourceOut(BaseModel):
    id: int
    kind: str
    issuer: str
    external_id: str
    display_name: str
    cardholder_name: str | None
    active: bool


class SourcesResponse(BaseModel):
    sources: list[SourceOut]


@router.get("/sources", response_model=SourcesResponse)
def list_sources(user_id: str,
                 db: Annotated[Session, Depends(get_db)]) -> SourcesResponse:
    rows = db.query(ExpenseSource).filter_by(
        user_id=user_id, active=True
    ).order_by(ExpenseSource.created_at).all()
    return SourcesResponse(sources=[
        SourceOut(id=r.id, kind=r.kind, issuer=r.issuer,
                  external_id=r.external_id, display_name=r.display_name,
                  cardholder_name=r.cardholder_name, active=r.active)
        for r in rows
    ])


class TransactionOut(BaseModel):
    id: int
    occurred_on: date
    merchant_raw: str
    amount_nis: float
    direction: str
    tx_type: str
    category_slug: str | None
    category_source: str | None
    is_card_payment: bool
    source_id: int


class TransactionsResponse(BaseModel):
    transactions: list[TransactionOut]
    total: int


@router.get("/transactions", response_model=TransactionsResponse)
def list_transactions(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    from_date: date | None = None,
    to_date: date | None = None,
    category: str | None = None,
    source_id: int | None = None,
    direction: str | None = None,
    include_card_payments: bool = False,
    search: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> TransactionsResponse:
    q = db.query(ExpenseTransaction).filter_by(user_id=user_id)
    if not include_card_payments:
        q = q.filter(ExpenseTransaction.is_card_payment.is_(False))
    if from_date:
        q = q.filter(ExpenseTransaction.occurred_on >= from_date)
    if to_date:
        q = q.filter(ExpenseTransaction.occurred_on <= to_date)
    if category:
        cat = db.query(ExpenseCategory).filter_by(
            user_id=user_id, slug=category).one_or_none()
        if cat is None:
            return TransactionsResponse(transactions=[], total=0)
        q = q.filter(ExpenseTransaction.category_id == cat.id)
    if source_id:
        q = q.filter(ExpenseTransaction.source_id == source_id)
    if direction:
        q = q.filter(ExpenseTransaction.direction == direction)
    if search:
        like = f"%{search}%"
        q = q.filter(ExpenseTransaction.merchant_raw.ilike(like))

    total = q.count()
    rows = q.order_by(ExpenseTransaction.occurred_on.desc()) \
            .offset(offset).limit(limit).all()
    cat_by_id = {
        c.id: c.slug for c in db.query(ExpenseCategory).filter_by(
            user_id=user_id
        ).all()
    }
    return TransactionsResponse(
        transactions=[
            TransactionOut(
                id=r.id, occurred_on=r.occurred_on, merchant_raw=r.merchant_raw,
                amount_nis=float(r.amount_nis), direction=r.direction,
                tx_type=r.tx_type,
                category_slug=cat_by_id.get(r.category_id),
                category_source=r.category_source,
                is_card_payment=r.is_card_payment,
                source_id=r.source_id,
            )
            for r in rows
        ],
        total=total,
    )


class PatchCategoryRequest(BaseModel):
    user_id: str
    category_slug: str


class PatchCategoryResponse(BaseModel):
    transaction_id: int
    category_slug: str
    category_source: str
    affected_count: int


@router.patch("/transactions/{transaction_id}",
               response_model=PatchCategoryResponse)
def patch_transaction_category(
    transaction_id: int,
    body: PatchCategoryRequest,
    db: Annotated[Session, Depends(get_db)],
) -> PatchCategoryResponse:
    tx = db.query(ExpenseTransaction).filter_by(
        id=transaction_id, user_id=body.user_id
    ).one_or_none()
    if tx is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    cat = db.query(ExpenseCategory).filter_by(
        user_id=body.user_id, slug=body.category_slug
    ).one_or_none()
    if cat is None:
        raise HTTPException(status_code=400,
                             detail=f"unknown category {body.category_slug}")
    tx.category_id = cat.id
    tx.category_source = "user"
    tx.category_confidence = Decimal("1.00")

    # Update cache: upsert pattern → category
    pattern = tx.merchant_normalized
    cache = db.query(MerchantCategoryCache).filter_by(
        user_id=body.user_id, merchant_pattern=pattern, is_regex=False,
    ).one_or_none()
    if cache is None:
        db.add(MerchantCategoryCache(
            user_id=body.user_id, merchant_pattern=pattern,
            category_id=cat.id, source="user", confidence=Decimal("1.00"),
            hit_count=1, last_hit_at=datetime.utcnow(),
        ))
    else:
        cache.category_id = cat.id
        cache.source = "user"
        cache.confidence = Decimal("1.00")
        cache.hit_count += 1
        cache.last_hit_at = datetime.utcnow()

    # Bulk re-bucket every other matching row
    siblings = db.query(ExpenseTransaction).filter(
        ExpenseTransaction.user_id == body.user_id,
        ExpenseTransaction.merchant_normalized == pattern,
        ExpenseTransaction.id != tx.id,
    ).all()
    for sib in siblings:
        sib.category_id = cat.id
        sib.category_source = "user"
        sib.category_confidence = Decimal("1.00")

    db.commit()
    return PatchCategoryResponse(
        transaction_id=tx.id, category_slug=body.category_slug,
        category_source="user", affected_count=1 + len(siblings),
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_expense_routes.py -v -k "sources or transactions"`
Expected: All three new cases PASS.

- [ ] **Step 5: Commit**

```powershell
git add argosy/api/routes/expenses.py tests/test_expense_routes.py
git commit -m "feat(expenses-api): GET /sources, GET /transactions, PATCH /transactions/{id}"
```

---

### Task 21: REST — categories + monthly summary

**Files:**
- Modify: `argosy/api/routes/expenses.py`
- Modify: `tests/test_expense_routes.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_expense_routes.py`:

```python
def test_list_categories_returns_taxonomy(client_with_db):
    # Trigger user-category seeding by ingesting one file
    with open(FIXTURES / "max_minimal.xlsx", "rb") as f:
        client_with_db.post("/api/expenses/upload",
                             files={"files": ("max_minimal.xlsx", f.read(),
                                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                             data={"user_id": "ariel"})
    resp = client_with_db.get("/api/expenses/categories?user_id=ariel")
    assert resp.status_code == 200
    body = resp.json()
    slugs = {c["slug"] for c in body["categories"]}
    assert "food.groceries" in slugs
    assert "dining_out.restaurants" in slugs
    assert "uncategorized" in slugs


def test_monthly_summary_excludes_card_payments(client_with_db):
    """Card-payment rows must NOT contribute to category totals."""
    with open(FIXTURES / "max_minimal.xlsx", "rb") as f:
        client_with_db.post("/api/expenses/upload",
                             files={"files": ("max_minimal.xlsx", f.read(),
                                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                             data={"user_id": "ariel"})
    resp = client_with_db.get("/api/expenses/monthly-summary",
                               params={"user_id": "ariel", "months": 12})
    assert resp.status_code == 200
    body = resp.json()
    assert "by_month" in body
    # Each by_month entry: {month: 'YYYY-MM', categories: {slug: amount}, total: ...}
    assert len(body["by_month"]) >= 1
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_expense_routes.py -v -k "categories or monthly"`
Expected: 404s.

- [ ] **Step 3: Add endpoints**

Append to `argosy/api/routes/expenses.py`:

```python
class CategoryOut(BaseModel):
    id: int
    slug: str
    label_en: str
    label_he: str
    parent_slug: str | None
    is_excluded_from_spend: bool
    is_inflow: bool


class CategoriesResponse(BaseModel):
    categories: list[CategoryOut]


@router.get("/categories", response_model=CategoriesResponse)
def list_categories(user_id: str,
                    db: Annotated[Session, Depends(get_db)]) -> CategoriesResponse:
    rows = db.query(ExpenseCategory).filter_by(user_id=user_id) \
             .order_by(ExpenseCategory.display_order).all()
    by_id = {r.id: r.slug for r in rows}
    return CategoriesResponse(categories=[
        CategoryOut(
            id=r.id, slug=r.slug, label_en=r.label_en, label_he=r.label_he,
            parent_slug=by_id.get(r.parent_id),
            is_excluded_from_spend=r.is_excluded_from_spend,
            is_inflow=r.is_inflow,
        )
        for r in rows
    ])


class MonthlySummaryRow(BaseModel):
    month: str                     # 'YYYY-MM'
    total_real_spend_nis: float
    total_real_income_nis: float
    by_category: dict[str, float]


class MonthlySummaryResponse(BaseModel):
    by_month: list[MonthlySummaryRow]


@router.get("/monthly-summary", response_model=MonthlySummaryResponse)
def monthly_summary(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    months: int = 12,
) -> MonthlySummaryResponse:
    """Aggregate by (month, category). Excludes is_card_payment rows.
    Categories with is_excluded_from_spend or is_inflow are not in
    real_spend; is_inflow rows go into real_income.
    """
    from sqlalchemy import func, and_

    cats = {c.id: c for c in db.query(ExpenseCategory).filter_by(
        user_id=user_id
    ).all()}
    rows = db.query(
        func.strftime("%Y-%m", ExpenseTransaction.occurred_on).label("ym"),
        ExpenseTransaction.category_id,
        ExpenseTransaction.direction,
        func.sum(ExpenseTransaction.amount_nis).label("total"),
    ).filter(
        ExpenseTransaction.user_id == user_id,
        ExpenseTransaction.is_card_payment.is_(False),
    ).group_by("ym", ExpenseTransaction.category_id,
                ExpenseTransaction.direction).all()

    by_month: dict[str, MonthlySummaryRow] = {}
    for ym, cat_id, direction, total in rows:
        if ym not in by_month:
            by_month[ym] = MonthlySummaryRow(
                month=ym, total_real_spend_nis=0.0,
                total_real_income_nis=0.0, by_category={},
            )
        cat = cats.get(cat_id)
        slug = cat.slug if cat else "uncategorized"
        by_month[ym].by_category[slug] = (
            by_month[ym].by_category.get(slug, 0.0) + float(total)
        )
        if cat is None:
            continue
        if cat.is_inflow and direction == "credit":
            by_month[ym].total_real_income_nis += float(total)
        elif (not cat.is_inflow and not cat.is_excluded_from_spend
              and direction == "debit"):
            by_month[ym].total_real_spend_nis += float(total)

    sorted_months = sorted(by_month.keys(), reverse=True)[:months]
    return MonthlySummaryResponse(
        by_month=[by_month[m] for m in sorted(sorted_months)]
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_expense_routes.py -v -k "categories or monthly"`
Expected: Both PASS.

- [ ] **Step 5: Commit**

```powershell
git add argosy/api/routes/expenses.py tests/test_expense_routes.py
git commit -m "feat(expenses-api): GET /categories, GET /monthly-summary"
```

---

### Task 22: WebSocket events

**Files:**
- Modify: `argosy/api/events.py` (add new event names to docstring + (if needed) emitter helpers)
- Modify: `argosy/services/expense_ingest/orchestrator.py` (emit on success/failure)
- Test: `tests/test_expense_ws_events.py`

- [ ] **Step 1: Write failing event test**

Create `tests/test_expense_ws_events.py`:

```python
"""Smoke test that orchestrator emits expense.statement.parsed."""

from pathlib import Path
from unittest.mock import patch

from sqlalchemy.orm import Session

from argosy.services.expense_ingest.orchestrator import ingest_user_file
from argosy.state.models import User, UserFile

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"


def test_orchestrator_emits_statement_parsed_event(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s, \
         patch("argosy.services.expense_ingest.category_resolver"
               "._categorize_via_llm") as mock_llm, \
         patch("argosy.api.events.publish_event_threadsafe") as mock_pub:
        mock_llm.return_value = []
        s.add(User(id="ariel", plan="free")); s.flush()
        path = FIXTURES / "max_minimal.xlsx"
        f = UserFile(user_id="ariel", sha256="x"*64, original_name="m.xlsx",
                     sanitized_name="m.xlsx", mime_type="x", kind="other",
                     size_bytes=1, storage_path=str(path),
                     source="chat_attachment")
        s.add(f); s.commit()
        ingest_user_file(s, "ariel", f.id); s.commit()
        # The orchestrator should fire 'expense.statement.parsed' once
        names = [c.args[0] for c in mock_pub.call_args_list]
        assert "expense.statement.parsed" in names
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_expense_ws_events.py -v`
Expected: FAIL — no event emitted.

- [ ] **Step 3: Emit from orchestrator**

Edit `argosy/services/expense_ingest/orchestrator.py`. At the bottom of `ingest_user_file`, before `return`, add:

```python
    from argosy.api.events import publish_event_threadsafe
    try:
        publish_event_threadsafe(
            "expense.statement.parsed",
            {
                "user_id": user_id,
                "statement_id": stmt.id,
                "source_id": src.id,
                "parsed_total_nis": float(stmt.parsed_total_nis),
                "status": stmt.status,
            },
        )
    except Exception:
        pass     # Best-effort — never fail the ingest because of telemetry
```

Also wrap the parser dispatch with a `try/except` so a parse failure emits `expense.statement.failed` and re-raises (so the caller sees it):

```python
    try:
        result = parser_fn(Path(file.storage_path))
    except Exception as e:
        try:
            publish_event_threadsafe(
                "expense.statement.failed",
                {"user_id": user_id, "file_id": file.id, "parse_error": str(e)},
            )
        except Exception:
            pass
        raise
```

- [ ] **Step 4: Document the events**

Edit `argosy/api/events.py`. Find the docstring listing reserved events and append:

```
expense.statement.parsed       — orchestrator success; payload: user_id, statement_id, source_id, parsed_total_nis, status
expense.statement.failed       — orchestrator parse error; payload: user_id, file_id, parse_error
expense.source.registered      — first-sight new source; payload: user_id, source_id, kind, issuer, external_id, suggested_cardholder
expense.recategorized          — user override applied; payload: user_id, merchant_pattern, affected_tx_ids
expense.budget_report.refreshed — EX3 hook; payload: user_id, report_id, refreshed_at
```

- [ ] **Step 5: Run test**

Run: `pytest tests/test_expense_ws_events.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add argosy/services/expense_ingest/orchestrator.py argosy/api/events.py tests/test_expense_ws_events.py
git commit -m "feat(expenses): WebSocket events on parse success/failure"
```

---

### Task 23: CLI — `argosy admin expenses-verify-file`

**Files:**
- Create: `argosy/cli/expenses_admin.py`
- Modify: `argosy/cli/__init__.py` (or wherever CLI subcommands are wired)
- Test: `tests/test_expense_cli_verify.py`

- [ ] **Step 1: Survey the existing CLI structure**

Read `argosy/cli/__init__.py` (or whichever file declares the top-level Typer/argparse app) to confirm the registration pattern. Match it.

- [ ] **Step 2: Write the failing test**

Create `tests/test_expense_cli_verify.py`:

```python
"""Smoke test for the expenses-verify-file CLI subcommand."""

from pathlib import Path

from typer.testing import CliRunner

from argosy.cli.expenses_admin import app as expenses_app

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"


def test_verify_file_isracard_minimal_passes():
    runner = CliRunner()
    result = runner.invoke(expenses_app, [
        "verify-file", str(FIXTURES / "isracard_minimal.xlsx"),
    ])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "Format:" in out
    assert "isracard" in out
    assert "Status: PASS" in out


def test_verify_file_unknown_format_exits_nonzero(tmp_path):
    bad = tmp_path / "garbage.bin"
    bad.write_bytes(b"\x00\x01\x02\x03")
    runner = CliRunner()
    result = runner.invoke(expenses_app, ["verify-file", str(bad)])
    assert result.exit_code != 0
    assert "unrecognized" in result.stdout.lower() or \
           "unknown" in result.stdout.lower()
```

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/test_expense_cli_verify.py -v`
Expected: ImportError.

- [ ] **Step 4: Implement the CLI**

Create `argosy/cli/expenses_admin.py`:

```python
"""Admin CLI for the expenses subsystem.

Subcommands:
  verify-file <path>     — print oracle vs parser side-by-side
  backfill <dir>         — bulk-ingest a directory tree (Task 24)
  issuer-coverage        — list unmapped Max ענף values seen in DB (Task 24)
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

app = typer.Typer(help="Argosy expenses admin utilities.")


@app.command("verify-file")
def verify_file(path: Path) -> None:
    """Print oracle vs parser side-by-side for one statement file."""
    from argosy.services.expense_ingest.sniff import detect_format, UnknownFormatError
    from argosy.services.expense_ingest.types import ParserName
    from argosy.services.expense_ingest.parsers import (
        leumi_osh as p_leumi, isracard as p_isra, max as p_max,
    )
    from tests.expense_ground_truth import (
        leumi_oracle, isracard_oracle, max_oracle,
    )

    try:
        fmt = detect_format(path)
    except UnknownFormatError as e:
        typer.echo(f"File: {path}")
        typer.echo(f"unrecognized format: {e}")
        sys.exit(2)

    parser = {
        ParserName.LEUMI_OSH: p_leumi.parse,
        ParserName.ISRACARD:  p_isra.parse,
        ParserName.MAX:       p_max.parse,
    }.get(fmt)
    if parser is None:
        typer.echo(f"no implementation for parser {fmt.value}")
        sys.exit(2)

    oracle = {
        ParserName.LEUMI_OSH: leumi_oracle,
        ParserName.ISRACARD:  isracard_oracle,
        ParserName.MAX:       max_oracle,
    }[fmt]

    truth = oracle(path)
    result = parser(path)
    debits = sum(t.amount_nis for t in result.transactions
                 if t.direction == "debit")
    credits = sum(t.amount_nis for t in result.transactions
                  if t.direction == "credit")

    typer.echo(f"File:    {path}")
    typer.echo(f"Format:  {fmt.value}")
    typer.echo("Oracle:")
    typer.echo(f"  rows           {truth.row_count}")
    typer.echo(f"  sum_debits     {truth.sum_debits_nis}")
    typer.echo(f"  sum_credits    {truth.sum_credits_nis}")
    typer.echo(f"  declared_total {truth.declared_total_nis}")
    typer.echo("Parser:")

    def mark(actual, expected, tol=1.0) -> str:
        return "✓" if abs(actual - expected) <= tol else "✗"

    typer.echo(f"  rows           {len(result.transactions)} "
               f"{'✓' if len(result.transactions) == truth.row_count else '✗'}")
    typer.echo(f"  sum_debits     {round(debits, 2)} "
               f"{mark(debits, truth.sum_debits_nis)}")
    typer.echo(f"  sum_credits    {round(credits, 2)} "
               f"{mark(credits, truth.sum_credits_nis)}")
    if truth.declared_total_nis is not None:
        typer.echo(f"  parsed_total   {round(float(result.statement.parsed_total_nis), 2)} "
                   f"{mark(float(result.statement.parsed_total_nis), truth.declared_total_nis, 50.0)}")

    rows_ok = len(result.transactions) == truth.row_count
    debit_ok = abs(debits - truth.sum_debits_nis) <= 1.0
    credit_ok = abs(credits - truth.sum_credits_nis) <= 1.0
    declared_ok = (truth.declared_total_nis is None) or (
        abs(float(result.statement.parsed_total_nis) - truth.declared_total_nis) <= 50.0
    )

    if rows_ok and debit_ok and credit_ok and declared_ok:
        typer.echo("Status: PASS")
        sys.exit(0)
    else:
        typer.echo("Status: FAIL")
        sys.exit(1)
```

- [ ] **Step 5: Wire the subcommand into the main CLI**

Edit `argosy/cli/__init__.py` to register `expenses_admin.app` as a Typer subcommand. The existing pattern is something like:

```python
from argosy.cli.expenses_admin import app as _expenses_app
main_app.add_typer(_expenses_app, name="expenses")
```

(Match whatever the codebase does; e.g., for `argosy admin gemelnet refresh-user` see how that hangs off of `argosy/cli/__init__.py`.)

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_expense_cli_verify.py -v`
Expected: Both PASS.

- [ ] **Step 7: Commit**

```powershell
git add argosy/cli/expenses_admin.py argosy/cli/__init__.py tests/test_expense_cli_verify.py
git commit -m "feat(expenses-cli): argosy expenses verify-file (oracle vs parser side-by-side)"
```

---

### Task 24: CLI — `expenses-backfill` + `issuer-coverage`

**Files:**
- Modify: `argosy/cli/expenses_admin.py`
- Modify: `tests/test_expense_cli_verify.py` (rename or split: `test_expense_cli.py`)

- [ ] **Step 1: Append failing test**

Append to `tests/test_expense_cli_verify.py`:

```python
def test_backfill_dry_run_prints_summary(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from argosy.cli.expenses_admin import app as expenses_app
    # Build a minimal sample tree
    src = tmp_path / "samples" / "2026" / "6225"
    src.mkdir(parents=True)
    (src / "Apr.xlsx").write_bytes(
        (FIXTURES / "max_minimal.xlsx").read_bytes()
    )
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(expenses_app, [
        "backfill", "--user-id", "ariel", "--dir",
        str(tmp_path / "samples"), "--dry-run",
    ])
    assert result.exit_code == 0
    assert "files: 1" in result.stdout.lower() or "1 file" in result.stdout.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_expense_cli_verify.py::test_backfill_dry_run_prints_summary -v`
Expected: FAIL (subcommand not registered).

- [ ] **Step 3: Add backfill + issuer-coverage subcommands**

Append to `argosy/cli/expenses_admin.py`:

```python
@app.command("backfill")
def backfill(
    user_id: str = typer.Option(..., "--user-id"),
    dir: Path = typer.Option(..., "--dir", exists=True),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Bulk-ingest every recognized statement file under <dir> for <user_id>.

    Idempotent — re-running on the same tree produces zero new rows.
    """
    files = [p for p in dir.rglob("*") if p.is_file() and p.suffix.lower() in {".xls", ".xlsx"}]
    typer.echo(f"Found {len(files)} files (.xls/.xlsx) under {dir}")
    if dry_run:
        for p in files:
            typer.echo(f"  would ingest: {p}")
        return

    from argosy.config import reload_settings
    reload_settings()
    from argosy.state.db import init_engine
    from argosy.services.file_catalog import catalog_upload
    from argosy.services.expense_ingest.orchestrator import ingest_user_file
    from sqlalchemy.orm import Session
    from argosy.state import db as db_module
    init_engine()  # uses ARGOSY_HOME
    # Use the sync engine for the bulk ingest

    successes = 0
    failures = 0
    with Session(db_module.sync_engine_for_cli()) as s:
        for p in files:
            try:
                contents = p.read_bytes()
                user_file = catalog_upload(
                    s, user_id=user_id, original_name=p.name,
                    contents=contents, mime_type="application/octet-stream",
                    kind="other", source="chat_attachment",
                )
                s.commit()
                ingest_user_file(s, user_id, user_file.id)
                s.commit()
                successes += 1
                typer.echo(f"  ✓ {p.name}")
            except Exception as e:
                s.rollback()
                failures += 1
                typer.echo(f"  ✗ {p.name}: {e}")

    typer.echo(f"\nDone. successes={successes} failures={failures}")


@app.command("issuer-coverage")
def issuer_coverage() -> None:
    """List Max-card ענף values seen in DB but not in the unambiguous map."""
    from argosy.services.expense_ingest.issuer_seed import (
        _UNAMBIGUOUS, _AMBIGUOUS,
    )
    import json as _json
    from argosy.state import db as db_module
    from sqlalchemy.orm import Session
    from argosy.state.models import ExpenseTransaction

    seen: dict[str, int] = {}
    with Session(db_module.sync_engine_for_cli()) as s:
        for tx in s.query(ExpenseTransaction).all():
            try:
                data = _json.loads(tx.raw_row_json)
            except Exception:
                continue
            anaf = data.get("anaf") if isinstance(data, dict) else None
            if not anaf:
                continue
            seen[anaf] = seen.get(anaf, 0) + 1

    unmapped = {a: n for a, n in seen.items()
                if a not in _UNAMBIGUOUS and a not in _AMBIGUOUS}
    if not unmapped:
        typer.echo("All ענף values are mapped.")
        return
    typer.echo("Unmapped ענף values (extend issuer_seed._UNAMBIGUOUS / _AMBIGUOUS):")
    for anaf, n in sorted(unmapped.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  {anaf:30s}  {n} txs")
```

The `db_module.sync_engine_for_cli()` helper: if `argosy/state/db.py` doesn't have one, add a small wrapper that returns a sync engine pointed at the configured DB path. Pattern:

```python
def sync_engine_for_cli():
    from argosy.config import get_settings
    import sqlalchemy as sa
    return sa.create_engine(f"sqlite:///{get_settings().db_file}")
```

- [ ] **Step 4: Run test**

Run: `pytest tests/test_expense_cli_verify.py::test_backfill_dry_run_prints_summary -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add argosy/cli/expenses_admin.py argosy/state/db.py tests/test_expense_cli_verify.py
git commit -m "feat(expenses-cli): backfill + issuer-coverage subcommands"
```

---

### Task 25: Configuration block in agent_settings.yaml

**Files:**
- Modify: `configs/<user_id>/agent_settings.yaml` (sample/template — see existing user's file)
- Modify: `argosy/config.py` (add a typed loader for the `expenses` block, mirroring `SpeculationCap`)
- Test: `tests/test_expense_config.py`

- [ ] **Step 1: Survey current config loaders**

Read `argosy/config.py` for the `SpeculationCap` pattern. Mirror it.

- [ ] **Step 2: Write failing test**

Create `tests/test_expense_config.py`:

```python
"""Tests for ExpensesConfig loader."""

from pathlib import Path

import pytest
import yaml

from argosy.config import load_expenses_config, ExpensesConfig


def test_load_expenses_config_returns_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    cfg = load_expenses_config(user_id="ariel")
    assert isinstance(cfg, ExpensesConfig)
    assert cfg.categorization.confidence_threshold == 0.85
    assert cfg.correlation.amount_tolerance_nis == 50
    assert cfg.refund_matcher.lookback_days == 90
    assert cfg.anomaly.mom_category_factor == 1.5


def test_load_expenses_config_respects_user_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    cfg_path = tmp_path / "configs" / "ariel" / "agent_settings.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(yaml.safe_dump({
        "expenses": {
            "categorization": {"confidence_threshold": 0.90},
            "anomaly": {"mom_category_factor": 2.0},
        }
    }))
    cfg = load_expenses_config(user_id="ariel")
    assert cfg.categorization.confidence_threshold == 0.90
    assert cfg.anomaly.mom_category_factor == 2.0
    # Unspecified fields remain at default
    assert cfg.correlation.amount_tolerance_nis == 50
```

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/test_expense_config.py -v`
Expected: ImportError.

- [ ] **Step 4: Add the loader**

Append to `argosy/config.py`:

```python
class ExpensesCategorizationConfig(BaseModel):
    confidence_threshold: float = 0.85
    llm_batch_size: int = 50
    llm_model_override: str | None = None


class ExpensesCorrelationConfig(BaseModel):
    amount_tolerance_nis: float = 50.0
    date_window_days: int = 2
    bank_row_keywords_he: list[str] = Field(default_factory=lambda: [
        "ל.מאסטרקרד", "כרטיסי אשראי", "ויזה", "דיינרס", "אמריקן אקספרס",
    ])


class ExpensesRefundMatcherConfig(BaseModel):
    amount_tolerance_pct: float = 0.05
    lookback_days: int = 90


class ExpensesAnomalyConfig(BaseModel):
    mom_category_factor: float = 1.5
    mom_category_min_baseline_nis: float = 500.0
    recurring_price_jump_pct: float = 15.0
    recurring_missed_after_days: int = 7
    new_recurring_after_n_months: int = 3
    big_one_off_nis: float = 3000.0
    coverage_gap_days: int = 35
    suppress_acknowledged_for_months: int = 3


class ExpensesParsersConfig(BaseModel):
    leumi_osh: bool = True
    isracard: bool = True
    max: bool = True
    cal: bool = False
    amex: bool = False
    diners: bool = False


class ExpensesConfig(BaseModel):
    enabled: bool = True
    parsers: ExpensesParsersConfig = Field(default_factory=ExpensesParsersConfig)
    categorization: ExpensesCategorizationConfig = Field(
        default_factory=ExpensesCategorizationConfig
    )
    correlation: ExpensesCorrelationConfig = Field(
        default_factory=ExpensesCorrelationConfig
    )
    refund_matcher: ExpensesRefundMatcherConfig = Field(
        default_factory=ExpensesRefundMatcherConfig
    )
    anomaly: ExpensesAnomalyConfig = Field(default_factory=ExpensesAnomalyConfig)


def load_expenses_config(user_id: str) -> ExpensesConfig:
    """Load expenses config from configs/<user_id>/agent_settings.yaml.
    Missing file or missing 'expenses' block → all defaults.
    """
    settings = get_settings()
    cfg_path = settings.configs_dir / user_id / "agent_settings.yaml"
    if not cfg_path.exists():
        return ExpensesConfig()
    import yaml
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    block = raw.get("expenses") or {}
    return ExpensesConfig.model_validate(block)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_expense_config.py -v`
Expected: Both PASS.

- [ ] **Step 6: Commit**

```powershell
git add argosy/config.py tests/test_expense_config.py
git commit -m "feat(expenses-config): typed ExpensesConfig + loader (mirrors SpeculationCap)"
```

---

## Phase F — Verification

### Task 26: Pipeline-invariant tests (LLM-independent)

**Files:**
- Create: `tests/test_expense_pipeline_invariants.py`

These hold *regardless of what the LLM picks for categories*. They verify plumbing.

- [ ] **Step 1: Write the invariant tests**

Create `tests/test_expense_pipeline_invariants.py`:

```python
"""Pipeline conservation invariants — pass even when LLM is mocked.

These verify the plumbing, not model judgment. They MUST pass on every
build; if one fails, a parser/correlator/refund-matcher is silently
dropping or duplicating rows.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.orm import Session
from sqlalchemy import text

from argosy.agents.household_categorizer_types import CategorizeResult
from argosy.services.expense_ingest.orchestrator import ingest_user_file
from argosy.state.models import (
    ExpenseCategory, ExpenseTransaction, User, UserFile,
)

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"


def _ingest(s: Session, fname: str) -> None:
    s.add(User(id="ariel", plan="free")); s.flush()
    p = FIXTURES / fname
    f = UserFile(user_id="ariel", sha256=fname, original_name=fname,
                 sanitized_name=fname, mime_type="x", kind="other",
                 size_bytes=p.stat().st_size, storage_path=str(p),
                 source="chat_attachment")
    s.add(f); s.commit()
    ingest_user_file(s, "ariel", f.id); s.commit()


def test_total_spend_equals_raw_sum(alembic_engine_at_head):
    """SUM of amounts is preserved through categorization."""
    with Session(alembic_engine_at_head) as s, \
         patch("argosy.services.expense_ingest.category_resolver"
               "._categorize_via_llm") as mock_llm:
        # Stub LLM: send everything to discretionary (a non-excluded slug)
        def fake(uid, rows):
            return [CategorizeResult(tx_id=r.tx_id,
                                      category_slug="discretionary.shopping_other",
                                      confidence=0.95, rationale="x")
                    for r in rows]
        mock_llm.side_effect = fake
        _ingest(s, "max_minimal.xlsx")
        raw_total = s.query(
            text("SUM(amount_nis)")
        ).select_from(ExpenseTransaction).filter(
            ExpenseTransaction.direction == "debit",
            ExpenseTransaction.is_card_payment.is_(False),
        ).scalar()
        cat_totals = s.query(
            text("SUM(amount_nis)")
        ).select_from(ExpenseTransaction).filter(
            ExpenseTransaction.category_id.isnot(None),
            ExpenseTransaction.direction == "debit",
            ExpenseTransaction.is_card_payment.is_(False),
        ).scalar()
        assert abs(float(raw_total or 0) - float(cat_totals or 0)) < 0.01


def test_card_payment_dedup_holds(alembic_engine_at_head):
    """If correlation has matched a bank row, the bank row's amount_nis must
    equal the matched card statement's parsed_total within tolerance.
    """
    with Session(alembic_engine_at_head) as s, \
         patch("argosy.services.expense_ingest.category_resolver"
               "._categorize_via_llm") as mock_llm:
        mock_llm.return_value = []
        _ingest(s, "leumi_osh_minimal.xls")
        # No card statements in this minimal scenario; just assert no rows
        # got marked is_card_payment without a matched_statement_id.
        rows = s.query(ExpenseTransaction).filter(
            ExpenseTransaction.is_card_payment.is_(True),
        ).all()
        for r in rows:
            assert r.matched_statement_id is not None


def test_refund_inheritance_consistent(alembic_engine_at_head):
    """Any refund with refund_of_id set must have category_id == prior.category_id."""
    with Session(alembic_engine_at_head) as s, \
         patch("argosy.services.expense_ingest.category_resolver"
               "._categorize_via_llm") as mock_llm:
        # Make the LLM categorize WIZZ AIR as travel.flights so the refund
        # has something to inherit
        def fake(uid, rows):
            return [
                CategorizeResult(
                    tx_id=r.tx_id,
                    category_slug=("travel.flights" if "wizz" in r.merchant_normalized
                                    else "discretionary.shopping_other"),
                    confidence=0.95, rationale="x",
                )
                for r in rows
            ]
        mock_llm.side_effect = fake
        # Hand-build a corpus with a debit + refund pair
        s.add(User(id="ariel", plan="free")); s.flush()
        from argosy.services.expense_ingest.taxonomy_seed import (
            seed_system_defaults, seed_user_categories,
        )
        seed_system_defaults(s); seed_user_categories(s, "ariel"); s.flush()
        cat = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="travel.flights").one()
        from argosy.state.models import ExpenseSource, ExpenseStatement
        f = UserFile(user_id="ariel", sha256="x"*64, original_name="x",
                     sanitized_name="x", mime_type="x", kind="other",
                     size_bytes=1, storage_path="/tmp/x", source="chat_attachment")
        s.add(f); s.flush()
        src = ExpenseSource(user_id="ariel", kind="card", issuer="max",
                            external_id="6225", display_name="Max 6225")
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id="ariel", source_id=src.id, file_id=f.id,
            period_start=date(2026, 2, 1), period_end=date(2026, 4, 30),
            parsed_total_nis=Decimal("0"), parser_name="max",
            parser_version="0.1.0", status="parsed",
        )
        s.add(stmt); s.flush()
        debit = ExpenseTransaction(
            user_id="ariel", statement_id=stmt.id, source_id=src.id,
            occurred_on=date(2026, 2, 12), merchant_raw="WIZZ AIR",
            merchant_normalized="wizz air", amount_nis=Decimal("2097.83"),
            direction="debit", tx_type="regular",
            category_id=cat.id, category_source="user",
            category_confidence=Decimal("1.00"),
            raw_row_json="{}",
        )
        refund = ExpenseTransaction(
            user_id="ariel", statement_id=stmt.id, source_id=src.id,
            occurred_on=date(2026, 3, 21), merchant_raw="WIZZ AIR",
            merchant_normalized="wizz air", amount_nis=Decimal("2097.83"),
            direction="credit", tx_type="refund", raw_row_json="{}",
        )
        s.add_all([debit, refund]); s.commit()

        from argosy.services.expense_ingest.refund_matcher import (
            match_refunds_for_user,
        )
        match_refunds_for_user(s, "ariel"); s.commit()

        s.refresh(refund)
        assert refund.refund_of_id == debit.id
        assert refund.category_id == debit.category_id
```

- [ ] **Step 2: Run**

Run: `pytest tests/test_expense_pipeline_invariants.py -v`
Expected: All three PASS.

- [ ] **Step 3: Commit**

```powershell
git add tests/test_expense_pipeline_invariants.py
git commit -m "test(expenses): pipeline invariants (LLM-independent conservation)"
```

---

### Task 27: Run conservation tests against the FULL real-sample corpus

This task validates §17.1.2 on the user's actual files. Not a code change — a verification gate.

- [ ] **Step 1: Set up the env var**

```powershell
$env:ARGOSY_EXPENSE_SAMPLES_ROOT = "D:\Google Drive\Family\Finances\Portfolio\Resources"
```

- [ ] **Step 2: Run the full conservation suite**

```powershell
pytest tests/test_expense_parsers_ground_truth.py -v
```

Expected: every parametrized case PASSES — Leumi (2 files), Isracard (16 files: 12 in 2025, 4 in 2026), Max (17 files: 12 in 2025, 5 in 2026). Each must satisfy:
- Row count exact match
- Debit sum within ₪1
- Credit sum within ₪1
- Parsed total within ₪50 of issuer-declared total (where issuer prints one)

- [ ] **Step 3: If anything fails — debug and re-run**

Common causes (per spec §17.1.2):
- A row with non-standard date format (asterisked "not-yet-final" tx in Leumi)
- A merged-cell quirk in older Excel files
- A trailing notes block we mis-detected as a tx row
- An `ENC` annotation inside merchant strings we forgot to handle

Per the user's "any doubt → uncategorized → ask me" preference, **do not lower tolerances to make tests pass**. Find and fix the parser. Re-run.

- [ ] **Step 4: Run the verify-file CLI on each issuer once for ad-hoc inspection**

```powershell
& "D:/Projects/financial-advisor/.venv/Scripts/python.exe" -m argosy expenses verify-file `
   "D:\Google Drive\Family\Finances\Portfolio\Resources\2026\Leumi\leumi_2026_May_Osh.xls"
& "D:/Projects/financial-advisor/.venv/Scripts/python.exe" -m argosy expenses verify-file `
   "D:\Google Drive\Family\Finances\Portfolio\Resources\2026\1266\1266_04_2026.xlsx"
& "D:/Projects/financial-advisor/.venv/Scripts/python.exe" -m argosy expenses verify-file `
   "D:\Google Drive\Family\Finances\Portfolio\Resources\2026\6225\Apr.xlsx"
```

All three should print `Status: PASS`.

- [ ] **Step 5: No commit unless a parser fix was made**

If you had to debug-and-fix a parser, commit that fix as `fix(expenses): <brief>` and re-run the suite.

---

### Task 28: Live LLM eval (`@pytest.mark.llm_eval`)

**Files:**
- Create: `tests/test_household_categorizer_e2e.py`

- [ ] **Step 1: Write the eval test**

Create `tests/test_household_categorizer_e2e.py`:

```python
"""Live LLM eval — opt-in via `@pytest.mark.llm_eval`.

Run with: pytest -m llm_eval tests/test_household_categorizer_e2e.py
Default suite (`pytest -m "not llm_eval"`) skips this entire module.

Tests structural properties of categorization on a hand-picked transaction
set covering recognizable merchants. Tolerates same-top-level drift on
ambiguous categories; demands ≥0.85 confidence for clearly-recognizable
rows.
"""

from __future__ import annotations

from datetime import date

import pytest

from argosy.agents.base import _llm_backend_available
from argosy.agents.household_categorizer import HouseholdCategorizerAgent
from argosy.agents.household_categorizer_types import CategorizeRow
from argosy.services.expense_ingest.taxonomy_seed import DEFAULT_TAXONOMY

pytestmark = [
    pytest.mark.llm_eval,
    pytest.mark.skipif(not _llm_backend_available(),
                        reason="no Claude backend configured"),
]


CASES: list[tuple[CategorizeRow, str]] = [
    # (row, expected top-level slug — sub-category drift OK)
    (CategorizeRow(tx_id=1, merchant_normalized="netflix.com",
                    merchant_raw="NETFLIX.COM", amount_nis=69.90,
                    direction="debit", occurred_on=date(2026, 4, 8),
                    issuer_kind="card", issuer_name="isracard"),
     "subscriptions"),
    (CategorizeRow(tx_id=2, merchant_normalized="שופרסל",
                    merchant_raw="שופרסל בע\"מ", amount_nis=440.20,
                    direction="debit", occurred_on=date(2026, 4, 5),
                    issuer_kind="card", issuer_name="isracard"),
     "food"),
    (CategorizeRow(tx_id=3, merchant_normalized="wolt", merchant_raw="WOLT",
                    amount_nis=85.0, direction="debit",
                    occurred_on=date(2026, 4, 1),
                    issuer_kind="card", issuer_name="isracard"),
     "dining_out"),
    (CategorizeRow(tx_id=4, merchant_normalized="פז דלק",
                    merchant_raw="פז חברת נפט", amount_nis=320.0,
                    direction="debit", occurred_on=date(2026, 4, 2),
                    issuer_kind="card", issuer_name="max",
                    issuer_category_he="דלק ותחנות דלק"),
     "transportation"),
    (CategorizeRow(tx_id=5, merchant_normalized="ביטוח ישיר",
                    merchant_raw="ביטוח ישיר-חיים", amount_nis=142.0,
                    direction="debit", occurred_on=date(2026, 3, 25),
                    issuer_kind="card", issuer_name="max",
                    issuer_category_he="ביטוח ופיננסים"),
     "insurance_other"),
    (CategorizeRow(tx_id=6, merchant_normalized="עיריית חיפה",
                    merchant_raw="עיריית חיפה-י", amount_nis=11834.98,
                    direction="debit", occurred_on=date(2026, 5, 5),
                    issuer_kind="bank", issuer_name="leumi"),
     "housing"),
]


def test_household_categorizer_recognizes_well_known_merchants():
    agent = HouseholdCategorizerAgent(user_id="ariel")
    taxonomy = [e.slug for e in DEFAULT_TAXONOMY]
    rows = [c[0] for c in CASES]
    results = agent.categorize_batch(rows, taxonomy)
    by_id = {r.tx_id: r for r in results}
    misses: list[str] = []
    for row, expected_top in CASES:
        r = by_id[row.tx_id]
        actual_top = r.category_slug.split(".", 1)[0]
        if actual_top != expected_top:
            misses.append(
                f"  tx={row.tx_id} merchant={row.merchant_raw!r} "
                f"got={r.category_slug} (conf={r.confidence:.2f}) "
                f"expected_top={expected_top!r} rationale={r.rationale!r}"
            )
    if misses:
        pytest.fail("Categorizer drift:\n" + "\n".join(misses))


def test_household_categorizer_returns_uncategorized_when_unsure():
    agent = HouseholdCategorizerAgent(user_id="ariel")
    taxonomy = [e.slug for e in DEFAULT_TAXONOMY]
    weird = CategorizeRow(
        tx_id=99, merchant_normalized="zzz garbage merchant 999",
        merchant_raw="ZZZ GARBAGE MERCHANT 999",
        amount_nis=42.0, direction="debit",
        occurred_on=date(2026, 4, 1),
        issuer_kind="card", issuer_name="isracard",
    )
    results = agent.categorize_batch([weird], taxonomy)
    assert results[0].category_slug == "uncategorized" or \
           results[0].confidence < 0.85, (
        f"expected uncategorized or low-confidence; got "
        f"{results[0].category_slug} @ {results[0].confidence}"
    )
```

- [ ] **Step 2: Run with explicit marker**

Either of:

```powershell
pytest -m llm_eval tests/test_household_categorizer_e2e.py -v
# or
pytest tests/test_household_categorizer_e2e.py -v --no-header `
       --override-ini="addopts="
```

Expected: both PASS. Cost: ~$0.02 (two batched Sonnet calls of 6 + 1 transactions). If failures: read the printed `Categorizer drift` block to see what the LLM picked. The tolerance is "same top-level"; you may need to expand `CASES` to drop a row that's genuinely ambiguous.

- [ ] **Step 3: Commit**

```powershell
git add tests/test_household_categorizer_e2e.py
git commit -m "test(expenses): live LLM eval for HouseholdCategorizerAgent (top-level drift tolerance)"
```

---

### Task 29: End-to-end backfill smoke (real samples)

This task runs the full backfill against the real samples and inspects the result. Not a unit test — a manual but repeatable verification step.

- [ ] **Step 1: Run the backfill in dry-run first**

```powershell
& "D:/Projects/financial-advisor/.venv/Scripts/python.exe" -m argosy expenses backfill `
   --user-id ariel --dir "D:\Google Drive\Family\Finances\Portfolio\Resources" --dry-run
```

Expected output: ~35 files listed (Leumi 2 + Isracard 16 + Max 17). No DB changes.

- [ ] **Step 2: Real backfill**

```powershell
& "D:/Projects/financial-advisor/.venv/Scripts/python.exe" -m argosy expenses backfill `
   --user-id ariel --dir "D:\Google Drive\Family\Finances\Portfolio\Resources"
```

Expected: per-file `✓` or `✗`, ending with `successes=N failures=0`. Live LLM cost: ~$2-5 over the year.

- [ ] **Step 3: Sanity-check via REST**

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/expenses/sources?user_id=ariel" | ConvertTo-Json -Depth 5
Invoke-RestMethod "http://127.0.0.1:8000/api/expenses/monthly-summary?user_id=ariel&months=12" | ConvertTo-Json -Depth 5
```

Expected:
- `sources` returns 1 bank + 2 cards (more after spouse's cards arrive).
- `monthly-summary` shows 12 months of `by_month` rows with category breakdowns and a non-zero `total_real_spend_nis` per month.

- [ ] **Step 4: Run `expenses-issuer-coverage`**

```powershell
& "D:/Projects/financial-advisor/.venv/Scripts/python.exe" -m argosy expenses issuer-coverage
```

This prints any unmapped Max ענף values. Extend `argosy/services/expense_ingest/issuer_seed.py::_UNAMBIGUOUS` for any high-frequency unmapped values; commit that map extension.

- [ ] **Step 5: No code commit unless a config or seed-map change was made.**

---

### Task 30: Final wave gate + agent_settings sample + handover

**Files:**
- Modify: `configs/<user_id>/agent_settings.yaml` (add the `expenses:` block as a sample)
- Modify: `pyproject.toml` (add `lxml` to dependencies if not already there — needed by `pd.read_html`)
- Modify: `docs/design/SDD.md` (add §18 stub pointing to the spec — leave the full §18 content as a follow-up after EX2-EX4 land)

- [ ] **Step 1: Add `expenses` block to the user's `agent_settings.yaml`**

Edit `configs/ariel/agent_settings.yaml` (or create from `configs/template/agent_settings.yaml` if that's the convention). Append:

```yaml
expenses:
  enabled: true
  parsers:
    leumi_osh: true
    isracard: true
    max: true
    cal: false
    amex: false
    diners: false
  categorization:
    confidence_threshold: 0.85
    llm_batch_size: 50
  correlation:
    amount_tolerance_nis: 50
    date_window_days: 2
  refund_matcher:
    amount_tolerance_pct: 0.05
    lookback_days: 90
  anomaly:
    mom_category_factor: 1.5
    mom_category_min_baseline_nis: 500
    big_one_off_nis: 3000
    coverage_gap_days: 35
```

- [ ] **Step 2: Verify `lxml` is on the dependency list**

Run `pip show lxml` inside the venv:
```powershell
& "D:/Projects/financial-advisor/.venv/Scripts/python.exe" -c "import lxml; print(lxml.__version__)"
```

If it raises ImportError, add to `pyproject.toml` `[project.dependencies]`:
```toml
lxml = ">=5.0"
```
And `uv sync` (or whatever the project's install command is).

- [ ] **Step 3: Add §18 stub to SDD**

Edit `docs/design/SDD.md`. Append a new top-level section before the appendices:

```markdown
## 18. Household Budget & Cash-Flow Analysis

Lands across Waves EX1–EX4. EX1 (this wave) ingests bank + card statements
through ``catalog_upload``, correlates bank credit-card-payment lines to
itemized card statements via the ``אסמכתא`` reference column, categorizes
transactions via a hybrid (issuer-seeded + cache + LLM at confidence ≥
0.85) pipeline, and exposes the data via ``/api/expenses/*``.

Full design: ``docs/superpowers/specs/2026-05-09-household-expenses-design.md``.
EX2 (anomaly detection + advisor surfacing), EX3 (HouseholdBudgetAnalystAgent
feeding plan synthesis), and EX4 (UI) are scheduled but not yet implemented.

### 18.1 EX1 surface (ingest core)

Six new tables (migration 0021): ``expense_sources``, ``expense_statements``,
``expense_transactions``, ``expense_categories``, ``merchant_category_cache``,
``expense_review_queue``. New REST routes under ``/api/expenses/*``
(upload, sources, transactions, categories, monthly-summary, transactions
PATCH for user override). New WebSocket events
``expense.statement.{parsed,failed}`` etc. CLI:
``argosy expenses verify-file`` and ``argosy expenses backfill``.

The deterministic ground-truth tests (``tests/test_expense_parsers_ground_truth.py``)
must pass on every real sample before EX1 is considered done. They check
row-count exact, debit/credit sums within ₪1, parsed totals within ₪50 of
issuer-declared totals.
```

- [ ] **Step 4: Final test sweep**

Run the default suite (excluding live LLM tests):

```powershell
pytest -m "not llm_eval" -v
```

Expected: all green. The ground-truth conservation tests will skip when
`ARGOSY_EXPENSE_SAMPLES_ROOT` is unset; set it to verify locally:

```powershell
$env:ARGOSY_EXPENSE_SAMPLES_ROOT = "D:\Google Drive\Family\Finances\Portfolio\Resources"
pytest tests/test_expense_parsers_ground_truth.py -v
```

Expected: all parametrized cases PASS.

- [ ] **Step 5: Commit**

```powershell
git add configs/ariel/agent_settings.yaml pyproject.toml docs/design/SDD.md
git commit -m "feat(expenses): EX1 wave gate — agent_settings sample, lxml dep, SDD §18 stub"
```

- [ ] **Step 6: Cap the wave with a tag (optional)**

```powershell
git tag ex1-ingest-core
```

EX1 is complete when:
- All `pytest -m "not llm_eval"` tests pass.
- Ground-truth conservation passes on every available real sample.
- `argosy expenses verify-file` returns PASS for at least one real file per issuer.
- `argosy expenses backfill --user-id ariel --dir <real samples>` reports zero failures.
- One live LLM eval run (`pytest -m llm_eval tests/test_household_categorizer_e2e.py`) has passed.

---

## Outline — EX2 (Anomaly + Advisor surfacing)

EX2 builds on EX1's `expense_review_queue` table to surface anomalies and uncategorized rows through the existing advisor `gap_driven` mode. Estimated 12-18 tasks; will get its own detailed plan after EX1 is verified live.

**Surface area:**
- New module `argosy/services/expense_ingest/anomaly_detector.py` with the six anomaly kinds from spec §8.2 (`mom_category_spike`, `new_recurring`, `recurring_price_jump`, `recurring_missed`, `big_one_off`, `coverage_gap`).
- New module `argosy/services/expense_ingest/recurring_set.py` for the confirmed-recurring tracking.
- New REST endpoints: `GET /api/expenses/review-queue`, `POST /api/expenses/review-queue/{id}/<action>` (acknowledge / recategorize / mark_recurring / investigate / dismiss), `POST /api/expenses/refresh-anomalies`.
- New WebSocket events: `expense.anomaly.flagged`.
- Advisor right-rail integration: a new "Spend review" group on the gap tracker reading from `expense_review_queue`.
- Gap tracker fields per source — `expense.statements.<source_kind>.<external_id>.last_uploaded` with `monthly` freshness — wired into `argosy.agents.gap_tracker.STAGE_FIELDS`.
- Daily/monthly cadence integration: anomaly detector fires after each ingest AND on `monthly_cycle`.

**Wave gate:**
- Anomaly detector deterministic on identical input (no LLM).
- A handful of real backfill anomalies surface; user resolves at least one of each kind end-to-end.

---

## Outline — EX3 (Plan integration)

EX3 is the loop-closure: expense data feeds plan synthesis. Estimated 14-20 tasks; own plan.

**Surface area:**
- New `argosy/agents/household_budget.py` — `HouseholdBudgetAnalystAgent` (Opus default), structured `HouseholdBudgetReport` per spec §9.1.
- Slot into `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` Phase 1 as the 10th analyst.
- Inject the `HOUSEHOLD BUDGET CONTEXT` block into `PlanSynthesizerAgent`'s prompt (per spec §9.2).
- Extend `PlanCritiqueAgent` with new RED/YELLOW criteria from spec §9.3.
- Advisor integration: confirm-derived-predictables flow (spec §9.4 Flow A) writing into `user_context.goals.near_term_spending` with `source: derived`.
- New `derived_household` block on `user_context.identity_yaml` (spec §9.5).
- New `monthly_cycle` stage `expense_monthly_run` (spec §10).
- New REST: `GET /api/expenses/budget-report`.
- New WebSocket: `expense.budget_report.refreshed`.

**Wave gate:**
- One full monthly_cycle run produces a synthesized plan whose horizons quote derived figures with citation.
- The advisor surfaces one derived predictable to the user; user accepts; entry lands in `goals.near_term_spending`.
- A live LLM eval covers `HouseholdBudgetAnalystAgent` end-to-end.

---

## Outline — EX4 (UI)

EX4 surfaces everything in the dashboard. Estimated 10-15 tasks; own plan.

**Surface area:**
- New top-level page `ui/src/app/expenses/page.tsx` with KPI strip, 24-month stacked bar (Recharts), interactive anomaly review queue, transactions table with editable category cells.
- Nav update `ui/src/components/nav.tsx` — slot 5 between Plan and Proposals.
- Home page tile `<CashFlowTile>` (`ui/src/components/cash-flow-tile.tsx`) with monthly net-savings %, 30-day spend bar, anomaly count.
- `<AdvisorBriefCard>`'s `_signal_bullet` chain extended to include `unresolved_anomalies` between `investor_events` and `pension_snapshots`.
- Advisor right-rail "Spend review" group component (`ui/src/components/advisor-spend-review.tsx`).
- Plan page "Cash-flow basis" panel (`ui/src/components/plan-cashflow-basis.tsx`).
- Shared `<ExpenseUploadWidget>` for both `/expenses` and `/advisor` chat input.
- API client extensions in `ui/src/lib/api.ts`.
- WebSocket subscriber updates in pages reading expense events.

**Wave gate:**
- Manual UI smoke (per user policy that backend tests are the verification surface) — at minimum: upload → see parse, review-queue interactive, plan page shows basis.
- Per `tsc --noEmit` clean.

---

## Done

EX1 produces a queryable expense subsystem with a strong test floor. EX2 makes the data interactive. EX3 closes the loop into plan synthesis. EX4 makes it visible. Each wave's gate is independent — EX1 alone is shippable as "Argosy now sees your spend"; the rest is incremental value on top.

---

*End of household expenses implementation plan.*
