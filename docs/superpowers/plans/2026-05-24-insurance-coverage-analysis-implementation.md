# Insurance Coverage Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a 5-role insurance analyst fleet (extractor → coverage_check + value_analyst → gap_analyst → synthesizer) that ingests policy PDFs into a new `user_context.insurance_yaml` section and produces an annual `InsuranceReview` artifact, with a `/insurance` UI route exposing the inventory and reviews.

**Architecture:** Dedicated `POST /api/insurance/policies/upload` endpoint funnels PDFs through `catalog_upload(kind="insurance_policy", source="insurance_policy_upload")` → extractor populates `insurance_yaml`. Annual loop + manual UI button fire `insurance_review_flow`, which fans out per-policy analyst pairs, fans in to gap_analyst, then synthesizes. Persists as `decision_runs.decision_kind="insurance_review"`. KB at `domain_knowledge/insurance/` is annually re-verified by the existing `DomainRefreshAgent`.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / Alembic / Pydantic v2 / pytest / Next.js 16 / TypeScript / Tailwind / Recharts. LLM backend: Claude (Opus 4.7 default for all 5 roles per accuracy-over-cost binding preference).

**Spec:** `docs/superpowers/specs/2026-05-24-insurance-coverage-analysis-design.md`

**Test discipline:** NEVER run the full 1,173-test suite during TDD. Pick the 2-6 files relevant to the task. Full suite only pre-merge (Task 24).

**Verification command pattern (use everywhere):**
```
.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/<file>.py -v
```

---

## Task 1: DB migration + UserContext.insurance_yaml field

**Files:**
- Create: `alembic/versions/0030_user_context_insurance_yaml.py`
- Modify: `argosy/state/models.py` (find `class UserContext` and add the field)
- Test: `tests/test_migration_0030_insurance_yaml.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migration_0030_insurance_yaml.py
"""Verify migration 0030 adds insurance_yaml to user_context with the right default."""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from argosy.state.models import UserContext


def test_user_context_has_insurance_yaml_column(test_engine):
    """The user_context table has insurance_yaml: TEXT NOT NULL DEFAULT ''."""
    insp = sa.inspect(test_engine)
    cols = {c["name"]: c for c in insp.get_columns("user_context")}
    assert "insurance_yaml" in cols, "missing insurance_yaml column"
    assert cols["insurance_yaml"]["nullable"] is False
    # SQLite stores DEFAULT '' as a string '' surrounded by quotes; tolerate both
    default = cols["insurance_yaml"].get("default")
    assert default in (None, "''", "'\\u0027\\u0027'"), f"unexpected default: {default!r}"


def test_existing_user_context_rows_get_empty_string(test_session: Session):
    """New rows default to '' so callers never see NULL."""
    test_session.add(UserContext(user_id="ariel_test"))
    test_session.commit()
    row = test_session.scalar(sa.select(UserContext).where(UserContext.user_id == "ariel_test"))
    assert row is not None
    assert row.insurance_yaml == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_migration_0030_insurance_yaml.py -v`
Expected: FAIL — `'insurance_yaml' not in cols` AND `AttributeError: 'UserContext' object has no attribute 'insurance_yaml'`.

- [ ] **Step 3: Write the migration**

```python
# alembic/versions/0030_user_context_insurance_yaml.py
"""user_context: add insurance_yaml column for the insurance wave (INS1).

Revision ID: 0030_user_context_insurance_yaml
Revises: 0029_agent_reports_prompts
Create Date: 2026-05-24

Adds a fourth top-level YAML section (peer to identity_yaml / goals_yaml /
constraints_yaml) for structured per-policy insurance records. NOT NULL
DEFAULT '' so existing rows backfill cleanly and consumers never see NULL.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_user_context_insurance_yaml"
down_revision: str | None = "0029_agent_reports_prompts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_context") as batch:
        batch.add_column(
            sa.Column(
                "insurance_yaml",
                sa.Text(),
                nullable=False,
                server_default="",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("user_context") as batch:
        batch.drop_column("insurance_yaml")
```

- [ ] **Step 4: Add the ORM field**

Locate `class UserContext` in `argosy/state/models.py` (it's the class with `__tablename__ = "user_context"`). After the existing `constraints_yaml` line, add:

```python
    insurance_yaml: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_migration_0030_insurance_yaml.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Apply migration to dev DB**

```
.venv/Scripts/python.exe -m alembic upgrade head
```

Expected: `Running upgrade 0029_agent_reports_prompts -> 0030_user_context_insurance_yaml`.

- [ ] **Step 7: Commit**

```
git add alembic/versions/0030_user_context_insurance_yaml.py argosy/state/models.py tests/test_migration_0030_insurance_yaml.py
git commit -m "migration(0030): add user_context.insurance_yaml for INS1 wave"
```

---

## Task 2: Pydantic schemas — `Policy`, `InsuranceContext`, `PolicyType`

**Files:**
- Create: `argosy/agents/insurance_types.py`
- Test: `tests/test_insurance_types.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_insurance_types.py
"""Schema sanity for argosy/agents/insurance_types.py."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from argosy.agents.insurance_types import (
    InsuranceContext,
    Policy,
    PolicyType,
)
from argosy.agents.base import ConfidenceBand


def _make_policy(**overrides) -> Policy:
    base = dict(
        policy_id="abc12345",
        type=PolicyType.LIFE,
        carrier="Clal",
        policy_number="LIFE-001",
        holders=["ariel"],
        premium_amount=180.0,
        premium_period="month",
        premium_currency="ILS",
        coverage_amount_nis=1_000_000.0,
        deductible_nis=None,
        term="term",
        term_expires_on=date(2030, 1, 1),
        renewal_on=None,
        beneficiaries=["spouse"],
        exclusions_summary="war, suicide within 1yr",
        riders=[],
        waiting_period_days=None,
        coinsurance_pct=None,
        claims_history_notes="",
        source_file_id=1,
        extracted_on=datetime.now(timezone.utc),
        confidence=ConfidenceBand.HIGH,
    )
    base.update(overrides)
    return Policy(**base)


def test_policy_round_trip_yaml_safe():
    """Policy serializes to JSON-mode safe types (dates → ISO strings)."""
    p = _make_policy()
    j = p.model_dump(mode="json")
    assert j["type"] == "life"
    assert j["term_expires_on"] == "2030-01-01"
    p2 = Policy.model_validate(j)
    assert p2.policy_id == p.policy_id


def test_policy_type_values():
    """Enum has all 8 declared values."""
    values = {t.value for t in PolicyType}
    assert values == {
        "health_shaban", "health_shlishi", "life", "disability",
        "long_term_care", "homeowner", "auto", "liability_other",
    }


def test_holders_supports_child_and_household():
    """Holder strings allow the tag conventions from EX8."""
    p1 = _make_policy(holders=["household"])
    p2 = _make_policy(holders=["ariel", "noga"])
    p3 = _make_policy(holders=["child:geva"])
    for p in (p1, p2, p3):
        Policy.model_validate(p.model_dump(mode="json"))


def test_insurance_context_empty_default():
    """InsuranceContext() with no args is valid (empty inventory)."""
    ic = InsuranceContext()
    assert ic.policies == []
    assert ic.last_extracted_on is None


def test_insurance_context_yaml_round_trip():
    """InsuranceContext serializes to YAML-safe dict and round-trips."""
    p = _make_policy()
    ic = InsuranceContext(policies=[p], last_extracted_on=datetime.now(timezone.utc))
    j = ic.model_dump(mode="json")
    ic2 = InsuranceContext.model_validate(j)
    assert len(ic2.policies) == 1
    assert ic2.policies[0].policy_id == p.policy_id


def test_policy_premium_period_validation():
    """premium_period only accepts month|quarter|year."""
    with pytest.raises(Exception):
        _make_policy(premium_period="day")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_types.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'argosy.agents.insurance_types'`.

- [ ] **Step 3: Write the schemas**

```python
# argosy/agents/insurance_types.py
"""Pydantic schemas for the insurance coverage wave (INS1).

`Policy` is the source of truth for a single insurance policy after the
extractor reads its PDF. `InsuranceContext.policies` is the list serialized
into `user_context.insurance_yaml`. The per-agent output schemas
(CoveragePolicyReport, ValuePolicyReport, GapReport, InsuranceReview) live in
this module too so consumers import everything from one place.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.base import ConfidenceBand


class PolicyType(str, Enum):
    HEALTH_SHABAN = "health_shaban"
    HEALTH_SHLISHI = "health_shlishi"
    LIFE = "life"
    DISABILITY = "disability"
    LONG_TERM_CARE = "long_term_care"
    HOMEOWNER = "homeowner"
    AUTO = "auto"
    LIABILITY_OTHER = "liability_other"


class Severity(str, Enum):
    INFO = "info"
    YELLOW = "yellow"
    RED = "red"


class Policy(BaseModel):
    policy_id: str
    type: PolicyType
    carrier: str
    policy_number: str
    holders: list[str]
    premium_amount: float | None = None
    premium_period: Literal["month", "quarter", "year"] = "year"
    premium_currency: Literal["ILS", "USD"] = "ILS"
    coverage_amount_nis: float | None = None
    deductible_nis: float | None = None
    term: Literal["whole_life", "term", "annual_renewable", "other"] | None = None
    term_expires_on: date | None = None
    renewal_on: date | None = None
    beneficiaries: list[str] = Field(default_factory=list)
    exclusions_summary: str = ""
    riders: list[str] = Field(default_factory=list)
    waiting_period_days: int | None = None
    coinsurance_pct: float | None = None
    claims_history_notes: str = ""
    source_file_id: int
    extracted_on: datetime
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    notes: str = ""
    superseded_by: str | None = None


class InsuranceContext(BaseModel):
    policies: list[Policy] = Field(default_factory=list)
    last_extracted_on: datetime | None = None


# ---- per-policy outputs ------------------------------------------------------

class ClauseFinding(BaseModel):
    clause_excerpt: str
    finding: str
    severity: Severity
    kb_citation: str  # required for non-INFO severities (enforced in agent prompt)


class CoveragePolicyReport(BaseModel):
    policy_id: str
    overall_assessment: str
    findings: list[ClauseFinding] = Field(default_factory=list)
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM


class ValuePolicyReport(BaseModel):
    policy_id: str
    premium_fair: Literal["under", "fair", "over", "unknown"]
    benchmark_low_nis: float | None = None
    benchmark_high_nis: float | None = None
    benchmark_vintage: str  # e.g. "2025-Q3"
    alternative_carriers: list[str] = Field(default_factory=list)
    rationale: str
    citations: list[str] = Field(default_factory=list)
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM


# ---- household-level output --------------------------------------------------

class GapFinding(BaseModel):
    person: str
    coverage_type: str  # one of PolicyType values OR "none"
    finding: str
    recommended_action: str
    severity: Severity
    kb_citation: str


class GapReport(BaseModel):
    findings: list[GapFinding] = Field(default_factory=list)
    summary: str = ""
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM


# ---- synthesizer output ------------------------------------------------------

class PerPolicyBlock(BaseModel):
    policy_id: str
    carrier: str
    type: PolicyType
    holders: list[str]
    coverage_summary_md: str
    value_summary_md: str
    combined_severity: Severity


class InsuranceReview(BaseModel):
    review_year: int
    executive_summary_md: str
    by_axis: dict[Literal["good", "value", "missing"], str] = Field(default_factory=dict)
    by_policy: list[PerPolicyBlock] = Field(default_factory=list)
    household_gaps: GapReport
    deltas_vs_prior_year: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM


__all__ = [
    "ClauseFinding",
    "CoveragePolicyReport",
    "GapFinding",
    "GapReport",
    "InsuranceContext",
    "InsuranceReview",
    "PerPolicyBlock",
    "Policy",
    "PolicyType",
    "Severity",
    "ValuePolicyReport",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_types.py -v`
Expected: PASS — all 6 tests.

- [ ] **Step 5: Commit**

```
git add argosy/agents/insurance_types.py tests/test_insurance_types.py
git commit -m "types(insurance): Policy + InsuranceContext + per-agent output schemas"
```

---

## Task 3: file_catalog allow-lists — kind + source

**Files:**
- Modify: `argosy/services/file_catalog.py:65-77` (`_ALLOWED_SOURCES`, `_ALLOWED_KINDS`)
- Test: `tests/test_file_catalog_insurance_kind.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_file_catalog_insurance_kind.py
"""file_catalog accepts kind=insurance_policy + source=insurance_policy_upload."""
from __future__ import annotations

import pytest

from argosy.services import file_catalog


def test_insurance_policy_kind_in_allowed():
    assert "insurance_policy" in file_catalog._ALLOWED_KINDS


def test_insurance_policy_upload_source_in_allowed():
    assert "insurance_policy_upload" in file_catalog._ALLOWED_SOURCES


@pytest.mark.asyncio
async def test_catalog_upload_round_trip_insurance_policy(test_user_id):
    """catalog_upload accepts the new kind+source pair and persists it."""
    dto = await file_catalog.catalog_upload(
        user_id=test_user_id,
        raw_bytes=b"%PDF-1.4\n%fake test policy\n",
        original_name="test_policy.pdf",
        mime_type="application/pdf",
        kind="insurance_policy",
        source="insurance_policy_upload",
    )
    assert dto.kind == "insurance_policy"
    assert dto.source == "insurance_policy_upload"
    assert dto.original_name == "test_policy.pdf"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_file_catalog_insurance_kind.py -v`
Expected: FAIL — `insurance_policy` not in `_ALLOWED_KINDS`.

- [ ] **Step 3: Extend the allow-lists**

In `argosy/services/file_catalog.py`, locate `_ALLOWED_SOURCES = frozenset({...})` (around line 65) and add `"insurance_policy_upload"` to the set. Then locate `_ALLOWED_KINDS = frozenset({...})` (around line 75) and add `"insurance_policy"` to the set.

After edits, the two frozensets should look like:

```python
_ALLOWED_SOURCES = frozenset({
    "chat_attachment",
    "intake_upload",
    "intake_file_to_text",
    "cost_basis_import",
    "expense_statement",
    "insurance_policy_upload",
})

_ALLOWED_KINDS = frozenset({
    "text", "image", "pdf", "plan_markdown", "broker_csv", "other",
    "insurance_policy",
})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_file_catalog_insurance_kind.py -v`
Expected: PASS — 3 tests.

- [ ] **Step 5: Commit**

```
git add argosy/services/file_catalog.py tests/test_file_catalog_insurance_kind.py
git commit -m "file_catalog: allow insurance_policy kind + insurance_policy_upload source"
```

---

## Task 4: Factor out `decrypt_if_encrypted_pdf` helper

**Files:**
- Modify: `argosy/services/turn_attachments.py` (extract inline encryption logic at lines ~220-258 into a helper)
- Test: `tests/test_decrypt_if_encrypted_pdf.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_decrypt_if_encrypted_pdf.py
"""Verify the factored decrypt_if_encrypted_pdf helper."""
from __future__ import annotations

import pytest

from argosy.services.turn_attachments import (
    AttachmentEncryptedError,
    decrypt_if_encrypted_pdf,
)


def test_passthrough_for_plain_pdf():
    """Non-encrypted PDF bytes return unchanged."""
    pdf_bytes = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n"
    out = decrypt_if_encrypted_pdf(pdf_bytes, user_id="ariel", original_name="test.pdf")
    assert out == pdf_bytes


def test_passthrough_for_non_pdf():
    """Non-PDF bytes pass through unchanged."""
    out = decrypt_if_encrypted_pdf(b"hello world", user_id="ariel", original_name="x.txt")
    assert out == b"hello world"


def test_raises_on_unrecoverable_encrypted_pdf(encrypted_pdf_fixture: bytes):
    """Encrypted PDF with no matching password → AttachmentEncryptedError.

    The error must be HTTP 422 and must include the filename in `detail`.
    """
    with pytest.raises(AttachmentEncryptedError) as exc:
        decrypt_if_encrypted_pdf(encrypted_pdf_fixture, user_id="nobody", original_name="secret.pdf")
    assert exc.value.status_code == 422
    assert "secret.pdf" in exc.value.detail


def test_decrypts_with_matching_user_password(
    encrypted_pdf_fixture: bytes, monkeypatch
):
    """When the keyfile has a matching password, the helper returns decrypted bytes.

    Patch `load_pdf_passwords` to return the fixture's password rather than
    creating a real keyfile.
    """
    import argosy.services.turn_attachments as ta

    monkeypatch.setattr(
        "argosy.services.pdf_passwords.load_pdf_passwords",
        lambda user_id: ["secret123"],
    )
    out = decrypt_if_encrypted_pdf(encrypted_pdf_fixture, user_id="ariel", original_name="locked.pdf")
    assert out != encrypted_pdf_fixture  # was re-serialized through PdfWriter
    assert out.startswith(b"%PDF")
```

You'll need a fixture for `encrypted_pdf_fixture`. Add this to `tests/conftest.py` (if it isn't there already):

```python
# tests/conftest.py — add near other PDF fixtures
import io

import pytest
from pypdf import PdfReader, PdfWriter


@pytest.fixture
def encrypted_pdf_fixture() -> bytes:
    """Create a tiny PDF encrypted with the user password 'secret123'."""
    # Build a minimal PDF via PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=72, height=72)
    plain = io.BytesIO()
    w.write(plain)
    plain.seek(0)
    # Re-open + encrypt
    r = PdfReader(plain)
    enc = PdfWriter()
    for page in r.pages:
        enc.add_page(page)
    enc.encrypt(user_password="secret123", owner_password="secret123")
    out = io.BytesIO()
    enc.write(out)
    return out.getvalue()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_decrypt_if_encrypted_pdf.py -v`
Expected: FAIL — `cannot import name 'decrypt_if_encrypted_pdf'`.

- [ ] **Step 3: Extract the helper**

In `argosy/services/turn_attachments.py`, the encryption logic currently lives inline inside `save_attachment` (around lines 220-258). Extract it. Add this new top-level function above `save_attachment`:

```python
def decrypt_if_encrypted_pdf(contents: bytes, user_id: str, original_name: str) -> bytes:
    """Pass-through unless the bytes are an encrypted PDF.

    For encrypted PDFs, try each password in the user's keyfile. On success,
    re-serialize through PdfWriter (strips /Encrypt). On failure, raise
    AttachmentEncryptedError (HTTP 422) with the existing user-facing
    Print-to-PDF workaround message, with the filename interpolated.

    Idempotent for non-PDFs and non-encrypted PDFs.
    """
    # Quick reject: not a PDF, or not encrypted — return unchanged.
    if not contents.startswith(b"%PDF"):
        return contents
    if not _is_pdf_encrypted(contents):
        return contents

    from argosy.services.pdf_passwords import (
        load_pdf_passwords,
        try_decrypt_pdf,
    )

    decrypted = try_decrypt_pdf(contents, load_pdf_passwords(user_id))
    if decrypted is not None:
        return decrypted

    raise AttachmentEncryptedError(
        f"{original_name!r} has PDF encryption that blocks the AI "
        "from reading it. Israeli payslips (תלוש שכר), Form 106s "
        "from some employers, and similar 'secured' documents open "
        "in Adobe / Chrome without prompting for a password, but "
        "the encryption dict in the file still blocks programmatic "
        "extraction (yours, Argosy's, and Anthropic's). Workaround "
        "options: (1) add the password to "
        "${ARGOSY_HOME}/configs/<user_id>/pdf_passwords.json so "
        "Argosy can decrypt server-side automatically next time, "
        "or (2) open the PDF, choose File → Print → 'Microsoft "
        "Print to PDF' (or 'Save as PDF' in macOS Preview), save "
        "the reprinted copy, and upload that instead."
    )
```

Then in `save_attachment`, **replace** the inline encryption block (the `if kind == "pdf" and _is_pdf_encrypted(contents): ...` block) with:

```python
    # Encryption gate — see decrypt_if_encrypted_pdf for the full rationale.
    if kind == "pdf":
        contents = decrypt_if_encrypted_pdf(contents, user_id, original_name)
        size = len(contents)
```

Make sure to export the helper. In the `__all__` near the bottom of the file, add `"decrypt_if_encrypted_pdf"`.

- [ ] **Step 4: Run new and existing tests**

```
.venv/Scripts/python.exe -m pytest tests/test_decrypt_if_encrypted_pdf.py tests/test_turn_attachments.py -v
```

Expected: All new tests PASS. All existing `test_turn_attachments` tests STILL PASS (behavior preserved).

- [ ] **Step 5: Commit**

```
git add argosy/services/turn_attachments.py tests/test_decrypt_if_encrypted_pdf.py tests/conftest.py
git commit -m "refactor(attachments): factor decrypt_if_encrypted_pdf helper for reuse by insurance upload"
```

---

## Task 5: KB skeleton — 10 files under `domain_knowledge/insurance/`

**Files:**
- Create: `domain_knowledge/insurance/health/bituach_leumi_base.md`
- Create: `domain_knowledge/insurance/health/kupat_holim_sal.md`
- Create: `domain_knowledge/insurance/health/shaban.md`
- Create: `domain_knowledge/insurance/health/shlishi.md`
- Create: `domain_knowledge/insurance/life.md`
- Create: `domain_knowledge/insurance/disability.md`
- Create: `domain_knowledge/insurance/long_term_care.md`
- Create: `domain_knowledge/insurance/property_casualty.md`
- Create: `domain_knowledge/insurance/life_stage_fit_rules.md`
- Create: `domain_knowledge/insurance/carriers/clal.md`
- Create: `domain_knowledge/insurance/carriers/migdal.md`
- Create: `domain_knowledge/insurance/carriers/harel.md`
- Create: `domain_knowledge/insurance/carriers/menorah_mivtachim.md`
- Create: `domain_knowledge/insurance/carriers/phoenix.md`
- Create: `domain_knowledge/insurance/carriers/ayalon.md`
- Test: `tests/test_insurance_kb_structure.py`

**Pattern:** Each file follows the `kupat_pensia.md` template. Frontmatter has `topic / jurisdiction / last_verified / next_refresh_due / sources` (at least one tier-1 source). Body has the sections `## How it works`, `## Why it matters`, `## The user's situation`, `## How agents should use this file`, `## Refresh cadence`, `## Open issues`. Mark `last_verified: 1900-01-01` for files Claude drafts from general knowledge — that's the deliberate signal that DomainRefreshAgent should re-verify on the next annual loop.

- [ ] **Step 1: Write the structure test**

```python
# tests/test_insurance_kb_structure.py
"""Every domain_knowledge/insurance/**/*.md follows the kupat_pensia.md template."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

KB_ROOT = Path(__file__).resolve().parent.parent / "domain_knowledge" / "insurance"

REQUIRED_FRONTMATTER_KEYS = {"topic", "jurisdiction", "last_verified", "next_refresh_due", "sources"}
REQUIRED_SECTIONS = {
    "## How it works",
    "## Why it matters",
    "## The user's situation",
    "## How agents should use this file",
    "## Refresh cadence",
}
EXPECTED_FILES = {
    "health/bituach_leumi_base.md",
    "health/kupat_holim_sal.md",
    "health/shaban.md",
    "health/shlishi.md",
    "life.md",
    "disability.md",
    "long_term_care.md",
    "property_casualty.md",
    "life_stage_fit_rules.md",
    "carriers/clal.md",
    "carriers/migdal.md",
    "carriers/harel.md",
    "carriers/menorah_mivtachim.md",
    "carriers/phoenix.md",
    "carriers/ayalon.md",
}


def _split_frontmatter(text: str) -> tuple[dict, str]:
    assert text.startswith("---\n"), "missing frontmatter opener"
    end = text.find("\n---\n", 4)
    assert end > 0, "missing frontmatter closer"
    fm = yaml.safe_load(text[4:end])
    body = text[end + 5 :]
    return fm, body


def test_expected_files_present():
    found = {str(p.relative_to(KB_ROOT)).replace("\\", "/") for p in KB_ROOT.rglob("*.md")}
    missing = EXPECTED_FILES - found
    assert not missing, f"missing KB files: {sorted(missing)}"


@pytest.mark.parametrize("rel_path", sorted(EXPECTED_FILES))
def test_frontmatter_complete(rel_path: str):
    path = KB_ROOT / rel_path
    text = path.read_text(encoding="utf-8")
    fm, _ = _split_frontmatter(text)
    missing = REQUIRED_FRONTMATTER_KEYS - set(fm.keys())
    assert not missing, f"{rel_path}: missing frontmatter keys {sorted(missing)}"
    assert isinstance(fm["sources"], list) and len(fm["sources"]) >= 1, \
        f"{rel_path}: sources must be a non-empty list"
    for src in fm["sources"]:
        assert "url" in src and "tier" in src, f"{rel_path}: each source needs url + tier"


@pytest.mark.parametrize("rel_path", sorted(EXPECTED_FILES))
def test_required_sections_present(rel_path: str):
    path = KB_ROOT / rel_path
    text = path.read_text(encoding="utf-8")
    _, body = _split_frontmatter(text)
    missing = {s for s in REQUIRED_SECTIONS if s not in body}
    assert not missing, f"{rel_path}: missing sections {sorted(missing)}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_kb_structure.py -v`
Expected: FAIL — every `test_expected_files_present` and `test_frontmatter_complete`/`test_required_sections_present` parametrized case fails because no files exist.

- [ ] **Step 3: Write the 15 KB files**

Use this template for each file. Substitute the fields per category. **Do NOT** copy real tariff numbers from carrier marketing material without verification — for the carrier files, use deliberately wide bands (low/high spread of 2-3×) with `last_verified: 1900-01-01` and a `## Open issues` note that the bands need verification against current carrier rate sheets.

Template:

```markdown
---
topic: israel_insurance_<category-slug>
jurisdiction: israel
last_verified: 1900-01-01
next_refresh_due: 2027-05-24
sources:
  - url: <tier-1 government / regulator URL>
    retrieved: 1900-01-01
    tier: 1
  - url: <tier-2 carrier or industry URL>
    retrieved: 1900-01-01
    tier: 2
---

# <Title — Hebrew name>

<1-paragraph plain-English summary.>

> **Verification status:** `last_verified: 1900-01-01`. DomainRefreshAgent must verify on the next annual run.

## How it works

<3-6 paragraphs on the legal/regulatory mechanics.>

## Why it matters

<Why a household would or wouldn't carry this — the policy decision frame.>

## The user's situation

<What's known about Ariel + Noga + Geva so far; what the gap_analyst should expect.>

## How agents should use this file

- **Cite this file** for any claim about <topic>.
- Pair with <adjacent KB files>.
- The extractor should record <specific fields>.
- The gap_analyst should flag <specific gaps>.

## Refresh cadence

- **Annual (January)** — verify <what specifically>.
- **On reform** — Israeli insurance regulation is periodically updated; any reform triggers a full refresh.

## Open issues

- The current numbers in this file have `last_verified: 1900-01-01`; the next DomainRefreshAgent run must replace them with sourced figures from <citation tier 1>.
```

**Specific content notes per file:**

- `health/bituach_leumi_base.md` — Mandatory state health coverage; National Health Insurance Law 1994; every Israeli resident has it via Bituach Leumi. Cite gov.il + btl.gov.il.
- `health/kupat_holim_sal.md` — The `sal habriut` covered services basket; 4 HMOs (Clalit, Maccabi, Meuhedet, Leumit); financed by Bituach Leumi. Cite gov.il/health.
- `health/shaban.md` — Supplementary HMO programs (`שירותי בריאות נוספים`); voluntary, per-HMO premium; covers things outside the basket. Cite each HMO's page.
- `health/shlishi.md` — Third-layer private insurance (Migdal, Clal, Harel etc.); surgery abroad, organ transplants, exotic drugs. Cite Ministry of Finance commissioner page.
- `life.md` — `ביטוח חיים`; pure risk vs `ביטוח חיים משולב חיסכון` (savings hybrid, often via pension fund); Capital Markets Authority regulation.
- `disability.md` — `אובדן כושר עבודה`; own-occupation vs any-occupation; usually a rider on life or via pension fund.
- `long_term_care.md` — `ביטוח סיעודי`; the group regime ended in 2017; today bought individually or as a HMO supplementary; Ministry of Finance regulates.
- `property_casualty.md` — Homeowner (`ביטוח דירה` — structure + contents + earthquake), auto (`ביטוח רכב חובה`+`מקיף`), liability.
- `life_stage_fit_rules.md` — opinionated rules: term-life face amount ≥ outstanding mortgage; disability for primary earner if no spouse income; LTC by age 60 (or earlier if family history); P&C: every household with assets > 0.
- `carriers/*.md` — One per major carrier. Frontmatter `topic: israel_insurance_carrier_clal` etc. Body: 2-paragraph carrier intro; per-coverage-type tariff bands as ranges with vintage. Use `## Term-life tariffs (vintage: 1900-Q1)` style headings until verified.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_kb_structure.py -v`
Expected: PASS — all parametrized cases pass.

- [ ] **Step 5: Commit**

```
git add domain_knowledge/insurance/ tests/test_insurance_kb_structure.py
git commit -m "kb(insurance): 15 markdown files scaffolded under domain_knowledge/insurance/

All files use last_verified: 1900-01-01 so DomainRefreshAgent picks them up
for verification on the next annual loop. Carrier tariff bands are deliberately
wide ranges with vintage 1900-Q1 until verified."
```

---

## Task 6: `InsuranceExtractorAgent`

**Files:**
- Create: `argosy/agents/insurance_extractor.py`
- Test: `tests/test_insurance_extractor_agent.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_insurance_extractor_agent.py
"""Unit tests for InsuranceExtractorAgent (mocked LLM)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from argosy.agents.base import ConfidenceBand
from argosy.agents.insurance_extractor import InsuranceExtractorAgent
from argosy.agents.insurance_types import Policy, PolicyType


@pytest.fixture
def fake_policy_dict() -> dict:
    return {
        "policy_id": "abc12345",
        "type": "life",
        "carrier": "Clal",
        "policy_number": "L-001",
        "holders": ["ariel"],
        "premium_amount": 180.0,
        "premium_period": "month",
        "premium_currency": "ILS",
        "coverage_amount_nis": 1_000_000.0,
        "deductible_nis": None,
        "term": "term",
        "term_expires_on": "2030-01-01",
        "renewal_on": None,
        "beneficiaries": ["spouse"],
        "exclusions_summary": "war, suicide within 1yr",
        "riders": [],
        "waiting_period_days": None,
        "coinsurance_pct": None,
        "claims_history_notes": "",
        "source_file_id": 1,
        "extracted_on": datetime.now(timezone.utc).isoformat(),
        "confidence": "high",
        "notes": "",
    }


def test_agent_role_and_config():
    agent = InsuranceExtractorAgent(user_id="ariel")
    assert agent.agent_role == "insurance_extractor"
    assert agent.output_model is Policy
    assert agent.require_citations is False  # source IS the PDF, no external authority


def test_build_prompt_returns_triplet():
    """build_prompt returns (system, user, sources) per Wave A contract."""
    agent = InsuranceExtractorAgent(user_id="ariel")
    out = agent.build_prompt(
        policy_pdf_bytes=b"%PDF-1.4 fake",
        pdf_filename="clal_life.pdf",
        source_file_id=1,
        identity_yaml="spouse: noga\nfamily:\n  children: [{name: geva}]\n",
        existing_insurance_yaml="",
    )
    assert isinstance(out, tuple) and len(out) == 3
    system, user, sources = out
    assert isinstance(system, str) and "extractor" in system.lower()
    assert isinstance(user, str) and "clal_life.pdf" in user
    assert sources == [("insurance/policy_pdf/clal_life.pdf", b"%PDF-1.4 fake")]


def test_system_prompt_carries_holder_hints():
    """The system prompt tells the LLM how to map names to holder tags."""
    agent = InsuranceExtractorAgent(user_id="ariel")
    system, _, _ = agent.build_prompt(
        policy_pdf_bytes=b"%PDF-1.4",
        pdf_filename="x.pdf",
        source_file_id=1,
        identity_yaml="primary_name: Ariel\nspouse:\n  name: Noga\nfamily:\n  children:\n    - name: Geva\n",
        existing_insurance_yaml="",
    )
    assert "ariel" in system.lower()
    assert "noga" in system.lower()
    assert "child:geva" in system.lower() or "child:<name>" in system.lower()


def test_system_prompt_forbids_fabrication():
    agent = InsuranceExtractorAgent(user_id="ariel")
    system, _, _ = agent.build_prompt(
        policy_pdf_bytes=b"%PDF-1.4",
        pdf_filename="x.pdf",
        source_file_id=1,
        identity_yaml="",
        existing_insurance_yaml="",
    )
    assert "do not fabricate" in system.lower() or "no fabrication" in system.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_extractor_agent.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement the agent**

```python
# argosy/agents/insurance_extractor.py
"""Insurance extractor agent — reads one policy PDF and emits a Policy record.

Pattern mirrors `argosy/agents/intake_extractor.py`: the user's PDF is the
authoritative source, so `require_citations = False`. The per-field
`source_excerpt` strings on each Policy field carry an audit trail without
needing the Citations API.

Holder detection: the system prompt tells the LLM how to map proper names
to the holder tag convention used elsewhere in Argosy (`holder:ariel`,
`holder:noga`, `child:<name>`, `household`). Inputs include the user's
`identity_yaml` so name→tag mapping is grounded.

No fabrication: if a field is not in the PDF, the LLM must omit it. Missing
required fields drop the policy to confidence=LOW with a `notes` warning.
"""
from __future__ import annotations

from typing import Any

from argosy.agents.base import BaseAgent
from argosy.agents.insurance_types import Policy


class InsuranceExtractorAgent(BaseAgent[Policy]):
    agent_role = "insurance_extractor"
    output_model = Policy
    require_citations = False
    max_tokens = 4096

    def build_prompt(
        self,
        *,
        policy_pdf_bytes: bytes,
        pdf_filename: str,
        source_file_id: int,
        identity_yaml: str,
        existing_insurance_yaml: str,
    ) -> tuple[str, str, list[tuple[str, bytes]]]:
        source_id = f"insurance/policy_pdf/{pdf_filename}"

        system = (
            "You are the insurance-extractor agent on the Argosy fleet. Your job: "
            "read ONE insurance policy PDF and produce a Policy record.\n\n"
            "ABSOLUTE RULES:\n"
            "1. No fabrication. If a field is not stated in the PDF, omit it (use null or empty). "
            "Do NOT infer from outside knowledge. The downstream gap_analyst will flag missing "
            "data — that is the SAFE outcome when in doubt.\n"
            "2. Holders. Map proper names in the policy to the household's tag convention. "
            "The user's identity_yaml below tells you which name → which tag. Possible tag values: "
            "  - 'ariel' or 'noga' (the two adults)\n"
            "  - 'child:<name>' for each minor (e.g. 'child:geva')\n"
            "  - 'household' when the policy covers the family as a unit (homeowner P&C, family Shaban)\n"
            "  - A list when multiple distinct persons are explicitly named.\n"
            "3. Currency. If the policy is denominated in USD (rare), set premium_currency='USD' "
            "and convert coverage_amount_nis using current spot FX. Otherwise stay in ILS.\n"
            "4. Confidence. HIGH if every required field has a direct quote. MEDIUM if some "
            "fields are inferred from context (e.g. 'face amount' inferred from a 'sum insured' line). "
            "LOW if the policy structure was hard to parse — set this and write a notes warning.\n"
            "5. Policy ID. Compute as sha256(carrier + policy_number + sorted(holders))[:8] hex. "
            "This makes renewals (same carrier+number+holders) collapse to one policy_id (later "
            "upload supersedes the earlier one).\n"
            "6. Output strictly conforms to the Policy JSON schema. No extra commentary outside.\n\n"
            "OUTPUT JSON SCHEMA:\n"
            f"{Policy.model_json_schema()}\n"
        )

        usr_lines: list[str] = []
        usr_lines.append("User identity context (YAML; may be empty):")
        usr_lines.append("```yaml")
        usr_lines.append(identity_yaml or "(empty)")
        usr_lines.append("```")
        usr_lines.append("")
        usr_lines.append("Existing insurance_yaml (avoid restating policies already on file):")
        usr_lines.append("```yaml")
        usr_lines.append(existing_insurance_yaml or "(empty)")
        usr_lines.append("```")
        usr_lines.append("")
        usr_lines.append(f"Policy PDF: see document `{source_id}` (filename: {pdf_filename}).")
        usr_lines.append(f"Use source_file_id={source_file_id} in the output (do not guess this).")
        usr_lines.append("")
        usr_lines.append("Produce the Policy JSON now. Set extracted_on to UTC now.")
        usr_lines.append("Remember: when in doubt, omit and let downstream agents flag it.")

        sources: list[tuple[str, bytes]] = [(source_id, policy_pdf_bytes)]
        return system, "\n".join(usr_lines), sources


__all__ = ["InsuranceExtractorAgent"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_extractor_agent.py -v`
Expected: PASS — 4 tests.

- [ ] **Step 5: Commit**

```
git add argosy/agents/insurance_extractor.py tests/test_insurance_extractor_agent.py
git commit -m "agent(insurance): InsuranceExtractorAgent — reads policy PDF, emits Policy"
```

---

## Task 7: `insurance_ingest` service — `extract_uploaded_policy` + `sync_gap_marker`

**Files:**
- Create: `argosy/services/insurance_ingest.py`
- Test: `tests/test_insurance_ingest.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_insurance_ingest.py
"""Tests for argosy/services/insurance_ingest.py.

Covers:
  - extract_uploaded_policy fires the agent, merges into insurance_yaml,
    handles agent failure non-fatally.
  - sync_gap_marker writes a 1-line summary into identity.<type>_insurance
    only when the slot is empty.
  - Merge logic: new policy with same policy_id supersedes the prior.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from argosy.agents.insurance_types import InsuranceContext, Policy, PolicyType
from argosy.agents.base import ConfidenceBand
from argosy.services.insurance_ingest import (
    extract_uploaded_policy,
    sync_gap_marker,
    merge_policy_into_yaml,
)


def _make_policy(policy_id: str = "abc12345", ptype: PolicyType = PolicyType.LIFE) -> Policy:
    return Policy(
        policy_id=policy_id,
        type=ptype,
        carrier="Clal",
        policy_number="L-001",
        holders=["ariel"],
        premium_amount=180.0,
        premium_period="month",
        premium_currency="ILS",
        coverage_amount_nis=1_000_000.0,
        source_file_id=1,
        extracted_on=datetime.now(timezone.utc),
        confidence=ConfidenceBand.HIGH,
    )


def test_merge_into_empty_yaml():
    new_yaml = merge_policy_into_yaml(existing_yaml="", policy=_make_policy())
    ic = InsuranceContext.model_validate(yaml.safe_load(new_yaml))
    assert len(ic.policies) == 1
    assert ic.policies[0].policy_id == "abc12345"


def test_merge_supersedes_existing_with_same_id():
    p1 = _make_policy(policy_id="same1234")
    p1.coverage_amount_nis = 500_000.0
    initial = yaml.safe_dump(InsuranceContext(policies=[p1]).model_dump(mode="json"))
    p2 = _make_policy(policy_id="same1234")
    p2.coverage_amount_nis = 1_500_000.0
    new_yaml = merge_policy_into_yaml(existing_yaml=initial, policy=p2)
    ic = InsuranceContext.model_validate(yaml.safe_load(new_yaml))
    # Two rows: old marked superseded_by=new, new is active
    assert len(ic.policies) == 2
    old = [p for p in ic.policies if p.coverage_amount_nis == 500_000.0][0]
    new = [p for p in ic.policies if p.coverage_amount_nis == 1_500_000.0][0]
    assert old.superseded_by == new.policy_id
    assert new.superseded_by is None


def test_merge_appends_new_distinct_policy():
    p1 = _make_policy(policy_id="aaa11111")
    initial = yaml.safe_dump(InsuranceContext(policies=[p1]).model_dump(mode="json"))
    p2 = _make_policy(policy_id="bbb22222")
    new_yaml = merge_policy_into_yaml(existing_yaml=initial, policy=p2)
    ic = InsuranceContext.model_validate(yaml.safe_load(new_yaml))
    assert {p.policy_id for p in ic.policies} == {"aaa11111", "bbb22222"}


def test_sync_gap_marker_writes_when_empty():
    policy = _make_policy(ptype=PolicyType.LIFE)
    identity_yaml = "spouse:\n  name: Noga\n"
    new_id = sync_gap_marker(identity_yaml=identity_yaml, policy=policy)
    parsed = yaml.safe_load(new_id)
    assert "life_insurance" in parsed
    assert "Clal" in parsed["life_insurance"]


def test_sync_gap_marker_does_not_overwrite_existing():
    policy = _make_policy(ptype=PolicyType.LIFE)
    identity_yaml = "life_insurance: 'Migdal term life, set by user during intake'\n"
    new_id = sync_gap_marker(identity_yaml=identity_yaml, policy=policy)
    parsed = yaml.safe_load(new_id)
    assert parsed["life_insurance"] == "Migdal term life, set by user during intake"


def test_sync_gap_marker_maps_health_shaban_to_health_insurance():
    """Both health_shaban and health_shlishi update identity.health_insurance."""
    policy = _make_policy(ptype=PolicyType.HEALTH_SHABAN)
    new_id = sync_gap_marker(identity_yaml="", policy=policy)
    parsed = yaml.safe_load(new_id)
    assert "health_insurance" in parsed


@pytest.mark.asyncio
async def test_extract_uploaded_policy_happy_path(test_session, test_user_id, monkeypatch):
    """extract_uploaded_policy: fires agent, writes policy, updates identity slot, audit-logs."""
    fake_policy = _make_policy()

    async def fake_run(self, **kwargs):
        report = MagicMock()
        report.output = fake_policy
        return report

    monkeypatch.setattr(
        "argosy.agents.insurance_extractor.InsuranceExtractorAgent.run", fake_run
    )

    # Pre-seed: a user_file row with a fake PDF
    from argosy.state.models import UserFile
    uf = UserFile(
        user_id=test_user_id,
        sha256="x" * 64,
        original_name="clal_life.pdf",
        sanitized_name="clal_life.pdf",
        mime_type="application/pdf",
        kind="insurance_policy",
        size_bytes=10,
        storage_path="/tmp/fake.pdf",
        source="insurance_policy_upload",
    )
    test_session.add(uf)
    test_session.commit()
    test_session.refresh(uf)

    result = await extract_uploaded_policy(user_file_id=uf.id, user_id=test_user_id)
    assert result is not None
    assert result.policy_id == fake_policy.policy_id

    # Verify insurance_yaml updated
    from argosy.state.models import UserContext
    uc = test_session.scalar(
        __import__("sqlalchemy").select(UserContext).where(UserContext.user_id == test_user_id)
    )
    ic = InsuranceContext.model_validate(yaml.safe_load(uc.insurance_yaml))
    assert len(ic.policies) == 1


@pytest.mark.asyncio
async def test_extract_uploaded_policy_agent_failure_returns_none(
    test_session, test_user_id, monkeypatch
):
    """Extractor raises → service logs, audit-trails, returns None (no re-raise)."""

    async def fake_run_raises(self, **kwargs):
        raise RuntimeError("simulated LLM error")

    monkeypatch.setattr(
        "argosy.agents.insurance_extractor.InsuranceExtractorAgent.run", fake_run_raises
    )

    from argosy.state.models import UserFile
    uf = UserFile(
        user_id=test_user_id,
        sha256="y" * 64,
        original_name="x.pdf",
        sanitized_name="x.pdf",
        mime_type="application/pdf",
        kind="insurance_policy",
        size_bytes=10,
        storage_path="/tmp/x.pdf",
        source="insurance_policy_upload",
    )
    test_session.add(uf)
    test_session.commit()
    test_session.refresh(uf)

    result = await extract_uploaded_policy(user_file_id=uf.id, user_id=test_user_id)
    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_ingest.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement the service**

```python
# argosy/services/insurance_ingest.py
"""Insurance ingest service — extract uploaded policies + sync gap markers.

Wires the InsuranceExtractorAgent to the user_context.insurance_yaml store and
keeps the legacy gap_tracker stage_8 identity.* slots in sync.

Functions:
  - extract_uploaded_policy: full pipeline. Reads the user_file's PDF bytes,
    fires the agent, merges the result into user_context.insurance_yaml,
    syncs the gap marker, audit-logs success/failure. Returns the Policy or
    None on failure (never re-raises).
  - merge_policy_into_yaml: pure function. Appends or supersedes by policy_id.
  - sync_gap_marker: pure function. Writes identity.<type>_insurance slot
    only when the slot is empty.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from argosy.agents.insurance_extractor import InsuranceExtractorAgent
from argosy.agents.insurance_types import InsuranceContext, Policy, PolicyType
from argosy.execution.audit import record_audit_event
from argosy.logging import get_logger
from argosy.state import db as db_mod
from argosy.state.models import UserContext, UserFile

_log = get_logger(__name__)


# Map PolicyType → identity slot name (stage_8 fields are flat strings).
_TYPE_TO_IDENTITY_SLOT = {
    PolicyType.HEALTH_SHABAN: "health_insurance",
    PolicyType.HEALTH_SHLISHI: "health_insurance",
    PolicyType.LIFE: "life_insurance",
    PolicyType.DISABILITY: "disability_insurance",
    PolicyType.LONG_TERM_CARE: "long_term_care_insurance",
    PolicyType.HOMEOWNER: "property_casualty_insurance",
    PolicyType.AUTO: "property_casualty_insurance",
    PolicyType.LIABILITY_OTHER: "umbrella_liability_insurance",
}


def merge_policy_into_yaml(*, existing_yaml: str, policy: Policy) -> str:
    """Merge `policy` into `existing_yaml` (a serialized InsuranceContext).

    Behavior:
      - If existing has no policy with the same policy_id, append.
      - If existing HAS a policy with the same policy_id, mark the existing
        row as superseded_by=<new_policy_id> and append the new row. Both
        rows persist (history).
    """
    if existing_yaml.strip():
        loaded = yaml.safe_load(existing_yaml)
        ic = InsuranceContext.model_validate(loaded) if loaded else InsuranceContext()
    else:
        ic = InsuranceContext()

    for existing in ic.policies:
        if existing.policy_id == policy.policy_id and existing.superseded_by is None:
            existing.superseded_by = policy.policy_id

    ic.policies.append(policy)
    ic.last_extracted_on = datetime.now(timezone.utc)
    return yaml.safe_dump(ic.model_dump(mode="json"), allow_unicode=True, sort_keys=False)


def sync_gap_marker(*, identity_yaml: str, policy: Policy) -> str:
    """Write identity.<slot>_insurance to a 1-line summary IFF currently empty.

    Returns the updated identity_yaml. Existing non-empty slots are preserved
    (manual intake answers always win).
    """
    slot = _TYPE_TO_IDENTITY_SLOT.get(policy.type)
    if slot is None:
        return identity_yaml

    parsed = yaml.safe_load(identity_yaml) if identity_yaml.strip() else {}
    if not isinstance(parsed, dict):
        parsed = {}

    if parsed.get(slot):  # already populated — don't overwrite
        return identity_yaml

    coverage_part = (
        f" {policy.coverage_amount_nis:,.0f} NIS face"
        if policy.coverage_amount_nis
        else ""
    )
    expires_part = f", expires {policy.term_expires_on.isoformat()}" if policy.term_expires_on else ""
    parsed[slot] = f"{policy.carrier} {policy.type.value}{coverage_part}{expires_part}"

    return yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False)


async def extract_uploaded_policy(*, user_file_id: int, user_id: str) -> Policy | None:
    """Fire InsuranceExtractorAgent on a single uploaded policy PDF.

    Returns the extracted Policy (also persisted into user_context.insurance_yaml),
    or None on extraction failure (logged; not re-raised — the upload itself
    is already persisted by the POST /api/insurance/policies/upload endpoint's
    catalog_upload call, so the user_file row remains recoverable for re-extract).
    """
    async with db_mod.session_scope() as session:
        user_file = session.scalar(select(UserFile).where(UserFile.id == user_file_id))
        if user_file is None:
            _log.error("insurance_ingest.user_file_not_found", extra={"user_file_id": user_file_id})
            return None

        try:
            pdf_bytes = Path(user_file.storage_path).read_bytes()
        except OSError as exc:
            _log.exception("insurance_ingest.read_storage_failed", extra={"user_file_id": user_file_id})
            await record_audit_event(
                user_id=user_id,
                event_type="insurance.policy.extracted.failed",
                entity_type="user_file",
                entity_id=str(user_file_id),
                payload={"error": str(exc), "phase": "read_storage"},
            )
            return None

        uc = session.scalar(select(UserContext).where(UserContext.user_id == user_id))
        if uc is None:
            uc = UserContext(user_id=user_id)
            session.add(uc)
            session.commit()
            session.refresh(uc)

        identity_yaml = uc.identity_yaml or ""
        existing_insurance_yaml = uc.insurance_yaml or ""

    # Run the agent OUTSIDE the DB session — LLM calls can take minutes.
    agent = InsuranceExtractorAgent(user_id=user_id)
    try:
        report = await agent.run(
            policy_pdf_bytes=pdf_bytes,
            pdf_filename=user_file.original_name,
            source_file_id=user_file_id,
            identity_yaml=identity_yaml,
            existing_insurance_yaml=existing_insurance_yaml,
        )
        policy: Policy = report.output
    except Exception as exc:  # noqa: BLE001
        _log.exception("insurance_ingest.extract_failed", extra={"user_file_id": user_file_id})
        await record_audit_event(
            user_id=user_id,
            event_type="insurance.policy.extracted.failed",
            entity_type="user_file",
            entity_id=str(user_file_id),
            payload={"error": str(exc), "phase": "agent_run"},
        )
        return None

    # Persist
    async with db_mod.session_scope() as session:
        uc = session.scalar(select(UserContext).where(UserContext.user_id == user_id))
        if uc is None:
            uc = UserContext(user_id=user_id)
            session.add(uc)
        uc.insurance_yaml = merge_policy_into_yaml(
            existing_yaml=uc.insurance_yaml or "", policy=policy
        )
        uc.identity_yaml = sync_gap_marker(
            identity_yaml=uc.identity_yaml or "", policy=policy
        )
        session.commit()

    await record_audit_event(
        user_id=user_id,
        event_type="insurance.policy.extracted",
        entity_type="user_file",
        entity_id=str(user_file_id),
        payload={"policy_id": policy.policy_id, "type": policy.type.value, "carrier": policy.carrier},
    )
    return policy


__all__ = [
    "extract_uploaded_policy",
    "merge_policy_into_yaml",
    "sync_gap_marker",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_ingest.py -v`
Expected: PASS — 8 tests.

- [ ] **Step 5: Commit**

```
git add argosy/services/insurance_ingest.py tests/test_insurance_ingest.py
git commit -m "service(insurance): extract_uploaded_policy + merge + gap-marker sync"
```

---

## Task 8: `POST /api/insurance/policies/upload` endpoint

**Files:**
- Create: `argosy/api/routes/insurance.py`
- Modify: `argosy/api/main.py` (register the router)
- Test: `tests/test_insurance_upload_route.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_insurance_upload_route.py
"""Tests for POST /api/insurance/policies/upload."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


def test_upload_accepts_pdf_and_schedules_extractor(api_client: TestClient, monkeypatch):
    """Happy path: multipart PDF upload → 200 + queued status + background task scheduled."""
    scheduled = []

    async def fake_extract(user_file_id: int, user_id: str):
        scheduled.append((user_file_id, user_id))
        return None

    monkeypatch.setattr(
        "argosy.services.insurance_ingest.extract_uploaded_policy", fake_extract
    )

    pdf_bytes = b"%PDF-1.4\nfake test\n"
    resp = api_client.post(
        "/api/insurance/policies/upload",
        files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
        params={"user_id": "ariel"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "user_file_id" in body
    assert body["status"] == "queued"


def test_upload_rejects_non_pdf(api_client: TestClient):
    """Non-PDF MIME → 415 Unsupported."""
    resp = api_client.post(
        "/api/insurance/policies/upload",
        files={"file": ("test.txt", b"hello", "text/plain")},
        params={"user_id": "ariel"},
    )
    assert resp.status_code == 415


def test_upload_encrypted_pdf_returns_422(
    api_client: TestClient, encrypted_pdf_fixture: bytes
):
    """Encrypted PDF without matching keyfile → 422 with the existing error message."""
    resp = api_client.post(
        "/api/insurance/policies/upload",
        files={"file": ("locked.pdf", encrypted_pdf_fixture, "application/pdf")},
        params={"user_id": "nobody"},
    )
    assert resp.status_code == 422
    assert "encryption" in resp.json()["detail"].lower()
    assert "locked.pdf" in resp.json()["detail"]


def test_upload_persists_user_file_with_correct_kind_and_source(
    api_client: TestClient, test_session, monkeypatch
):
    """The persisted user_file has kind=insurance_policy + source=insurance_policy_upload."""

    async def fake_extract(user_file_id: int, user_id: str):
        return None

    monkeypatch.setattr(
        "argosy.services.insurance_ingest.extract_uploaded_policy", fake_extract
    )

    pdf_bytes = b"%PDF-1.4\nfake\n"
    resp = api_client.post(
        "/api/insurance/policies/upload",
        files={"file": ("a.pdf", pdf_bytes, "application/pdf")},
        params={"user_id": "ariel"},
    )
    assert resp.status_code == 200
    uf_id = resp.json()["user_file_id"]

    from argosy.state.models import UserFile
    import sqlalchemy as sa
    uf = test_session.scalar(sa.select(UserFile).where(UserFile.id == uf_id))
    assert uf.kind == "insurance_policy"
    assert uf.source == "insurance_policy_upload"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_upload_route.py -v`
Expected: FAIL — `404 Not Found` (route doesn't exist).

- [ ] **Step 3: Implement the route + wire it**

Create `argosy/api/routes/insurance.py`:

```python
# argosy/api/routes/insurance.py
"""Insurance API surface (INS1).

Endpoints:
  POST /api/insurance/policies/upload   — multipart PDF upload, schedules extractor
  (more endpoints added in later tasks: list, edit, delete, review, re-extract)

The upload endpoint is the SINGLE ingress for insurance policies. The
chat-attachment flow (turn_attachments.save_attachment) is unchanged and
does NOT trigger the extractor — that decision lives in design §4.4.
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Query, UploadFile

from argosy.logging import get_logger
from argosy.services.file_catalog import catalog_upload
from argosy.services.insurance_ingest import extract_uploaded_policy
from argosy.services.turn_attachments import decrypt_if_encrypted_pdf

_log = get_logger(__name__)

router = APIRouter(prefix="/api/insurance", tags=["insurance"])


_ACCEPTED_MIMES = frozenset({"application/pdf"})


@router.post("/policies/upload")
async def upload_policy(
    background: BackgroundTasks,
    user_id: str = Query(...),
    file: UploadFile = File(...),
) -> dict[str, object]:
    """Accept a policy PDF, persist via catalog_upload, schedule extractor."""
    if (file.content_type or "").lower() not in _ACCEPTED_MIMES:
        raise HTTPException(
            status_code=415,
            detail=f"Insurance policy upload requires application/pdf; got {file.content_type!r}",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")

    # Encryption gate — uses the shared helper (same logic + same error
    # codes as turn_attachments.save_attachment).
    raw = decrypt_if_encrypted_pdf(raw, user_id=user_id, original_name=file.filename or "policy.pdf")

    dto = await catalog_upload(
        user_id=user_id,
        raw_bytes=raw,
        original_name=file.filename or "policy.pdf",
        mime_type=file.content_type or "application/pdf",
        kind="insurance_policy",
        source="insurance_policy_upload",
    )

    background.add_task(extract_uploaded_policy, user_file_id=dto.id, user_id=user_id)
    _log.info("insurance.upload.queued", extra={"user_file_id": dto.id, "user_id": user_id})
    return {"user_file_id": dto.id, "status": "queued"}


__all__ = ["router"]
```

In `argosy/api/main.py`, find where existing routers are registered (e.g. `app.include_router(advisor_router)`) and add:

```python
from argosy.api.routes.insurance import router as insurance_router  # near the other imports
...
app.include_router(insurance_router)  # near the other includes
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_upload_route.py -v`
Expected: PASS — 4 tests.

- [ ] **Step 5: Commit**

```
git add argosy/api/routes/insurance.py argosy/api/main.py tests/test_insurance_upload_route.py
git commit -m "route(insurance): POST /api/insurance/policies/upload + extractor BackgroundTask"
```

---

## Task 9: `CoverageCheckAgent`

**Files:**
- Create: `argosy/agents/insurance_coverage_check.py`
- Test: `tests/test_insurance_coverage_check_agent.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_insurance_coverage_check_agent.py
"""Unit tests for CoverageCheckAgent (mocked LLM, structural assertions)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from argosy.agents.base import ConfidenceBand
from argosy.agents.insurance_coverage_check import CoverageCheckAgent
from argosy.agents.insurance_types import (
    ClauseFinding,
    CoveragePolicyReport,
    Policy,
    PolicyType,
    Severity,
)


def _policy() -> Policy:
    return Policy(
        policy_id="abc12345",
        type=PolicyType.LIFE,
        carrier="Clal",
        policy_number="L-001",
        holders=["ariel"],
        source_file_id=1,
        extracted_on=datetime.now(timezone.utc),
        confidence=ConfidenceBand.HIGH,
    )


def test_agent_config():
    agent = CoverageCheckAgent(user_id="ariel")
    assert agent.agent_role == "coverage_check"
    assert agent.output_model is CoveragePolicyReport
    assert agent.require_citations is True


def test_build_prompt_attaches_policy_pdf_and_kb(tmp_path: Path):
    agent = CoverageCheckAgent(user_id="ariel")
    p = _policy()
    pdf_bytes = b"%PDF-1.4\nfake policy text"
    kb_text = "---\ntopic: x\n---\n# Life KB body\n"
    system, user, sources = agent.build_prompt(
        policy=p,
        policy_pdf_bytes=pdf_bytes,
        pdf_filename="clal_life.pdf",
        type_kb_text=kb_text,
        type_kb_path="domain_knowledge/insurance/life.md",
    )
    assert "coverage_check" in system.lower() or "coverage check" in system.lower()
    src_ids = [sid for sid, _ in sources]
    assert "insurance/policy_pdf/clal_life.pdf" in src_ids
    assert "insurance/life.md" in " ".join(src_ids)


def test_system_prompt_requires_citations_for_non_info_findings():
    agent = CoverageCheckAgent(user_id="ariel")
    system, _, _ = agent.build_prompt(
        policy=_policy(),
        policy_pdf_bytes=b"%PDF-1.4",
        pdf_filename="x.pdf",
        type_kb_text="",
        type_kb_path="domain_knowledge/insurance/life.md",
    )
    assert "kb_citation" in system.lower() or "citation" in system.lower()
    assert "non-info" in system.lower() or "yellow" in system.lower() or "red" in system.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_coverage_check_agent.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement the agent**

```python
# argosy/agents/insurance_coverage_check.py
"""Per-policy coverage check — the "good" axis of the insurance review.

Reads ONE policy + its source PDF + the category KB and produces a
CoveragePolicyReport. Every non-INFO finding MUST cite the KB section that
backs the judgment (enforced in the prompt; base-class citation gate also on).
"""
from __future__ import annotations

from argosy.agents.base import BaseAgent
from argosy.agents.insurance_types import CoveragePolicyReport, Policy


class CoverageCheckAgent(BaseAgent[CoveragePolicyReport]):
    agent_role = "coverage_check"
    output_model = CoveragePolicyReport
    require_citations = True
    max_tokens = 6144

    def build_prompt(
        self,
        *,
        policy: Policy,
        policy_pdf_bytes: bytes,
        pdf_filename: str,
        type_kb_text: str,
        type_kb_path: str,
    ) -> tuple[str, str, list[tuple[str, bytes | str]]]:
        pdf_source_id = f"insurance/policy_pdf/{pdf_filename}"
        kb_source_id = type_kb_path  # e.g. "domain_knowledge/insurance/life.md"

        system = (
            "You are the coverage-check agent on the Argosy insurance fleet — the \"good\" "
            "axis of the review.\n\n"
            "Your job: read ONE policy and its source PDF. Identify clauses that are "
            "suspicious, narrower than they appear, exclusionary in unusual ways, or that "
            "fail to deliver what the policy's marketing language claims. Compare against "
            "the category KB (attached as a document block) to ground your judgments.\n\n"
            "ABSOLUTE RULES:\n"
            "1. Severity. Three values: info (informational), yellow (worth flagging, not "
            "urgent), red (action recommended).\n"
            "2. KB citation. EVERY non-INFO finding MUST have kb_citation set to "
            "`domain_kb:<kb_source_id>#<section>` form. The base-class citation gate "
            "enforces this — outputs missing citations will be rejected.\n"
            "3. clause_excerpt. Each finding MUST quote 1-2 sentences from the PDF that "
            "back the finding. The Citations API spans on these excerpts let the user "
            "audit-trail back to the exact clause.\n"
            "4. No fabrication. If the PDF doesn't say something, don't infer it. \"The "
            "policy doesn't say what happens if X\" is itself a legitimate yellow finding.\n"
            "5. Output strictly conforms to the CoveragePolicyReport JSON schema.\n\n"
            "OUTPUT JSON SCHEMA:\n"
            f"{CoveragePolicyReport.model_json_schema()}\n"
        )

        user_lines = [
            "=== POLICY UNDER REVIEW ===",
            f"policy_id: {policy.policy_id}",
            f"type: {policy.type.value}",
            f"carrier: {policy.carrier}",
            f"policy_number: {policy.policy_number}",
            f"holders: {policy.holders}",
            f"premium_amount: {policy.premium_amount} {policy.premium_currency} / {policy.premium_period}",
            f"coverage_amount_nis: {policy.coverage_amount_nis}",
            f"deductible_nis: {policy.deductible_nis}",
            f"term: {policy.term} (expires: {policy.term_expires_on})",
            f"exclusions_summary: {policy.exclusions_summary}",
            f"riders: {policy.riders}",
            "",
            f"=== POLICY PDF ===  document `{pdf_source_id}`",
            f"=== CATEGORY KB ===  document `{kb_source_id}`",
            "",
            f"Produce a CoveragePolicyReport JSON for policy_id={policy.policy_id}.",
            "Set overall_assessment to a 2-3 sentence summary.",
            "List 0..N findings. INFO findings can omit kb_citation; YELLOW and RED must include it.",
        ]
        sources: list[tuple[str, bytes | str]] = [
            (pdf_source_id, policy_pdf_bytes),
            (kb_source_id, type_kb_text),
        ]
        return system, "\n".join(user_lines), sources


__all__ = ["CoverageCheckAgent"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_coverage_check_agent.py -v`
Expected: PASS — 3 tests.

- [ ] **Step 5: Commit**

```
git add argosy/agents/insurance_coverage_check.py tests/test_insurance_coverage_check_agent.py
git commit -m "agent(insurance): CoverageCheckAgent — per-policy 'good' axis"
```

---

## Task 10: `ValueAnalystAgent`

**Files:**
- Create: `argosy/agents/insurance_value_analyst.py`
- Test: `tests/test_insurance_value_analyst_agent.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_insurance_value_analyst_agent.py
"""Unit tests for ValueAnalystAgent."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from argosy.agents.base import ConfidenceBand
from argosy.agents.insurance_types import (
    Policy,
    PolicyType,
    ValuePolicyReport,
)
from argosy.agents.insurance_value_analyst import ValueAnalystAgent


def _policy() -> Policy:
    return Policy(
        policy_id="abc12345",
        type=PolicyType.LIFE,
        carrier="Clal",
        policy_number="L-001",
        holders=["ariel"],
        premium_amount=180.0,
        premium_period="month",
        premium_currency="ILS",
        coverage_amount_nis=1_000_000.0,
        source_file_id=1,
        extracted_on=datetime.now(timezone.utc),
        confidence=ConfidenceBand.HIGH,
    )


def test_agent_config():
    agent = ValueAnalystAgent(user_id="ariel")
    assert agent.agent_role == "value_analyst"
    assert agent.output_model is ValuePolicyReport
    assert agent.require_citations is True


def test_build_prompt_attaches_carrier_and_type_kb():
    agent = ValueAnalystAgent(user_id="ariel")
    system, user, sources = agent.build_prompt(
        policy=_policy(),
        carrier_kb_text="# Clal carrier KB\n",
        carrier_kb_path="domain_knowledge/insurance/carriers/clal.md",
        type_kb_text="# Life KB\n",
        type_kb_path="domain_knowledge/insurance/life.md",
    )
    src_ids = {sid for sid, _ in sources}
    assert "domain_knowledge/insurance/carriers/clal.md" in src_ids
    assert "domain_knowledge/insurance/life.md" in src_ids


def test_system_prompt_requires_benchmark_vintage():
    agent = ValueAnalystAgent(user_id="ariel")
    system, _, _ = agent.build_prompt(
        policy=_policy(),
        carrier_kb_text="",
        carrier_kb_path="domain_knowledge/insurance/carriers/clal.md",
        type_kb_text="",
        type_kb_path="domain_knowledge/insurance/life.md",
    )
    assert "benchmark_vintage" in system
    assert "stale" in system.lower() or "vintage" in system.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_value_analyst_agent.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement the agent**

```python
# argosy/agents/insurance_value_analyst.py
"""Per-policy value analyst — the "value" axis of the insurance review.

Reads ONE policy + the carrier's tariff KB + the type's category KB and
produces a ValuePolicyReport with a fairness rating + benchmark gap +
alternative-carrier suggestions, all KB-cited.
"""
from __future__ import annotations

from argosy.agents.base import BaseAgent
from argosy.agents.insurance_types import Policy, ValuePolicyReport


class ValueAnalystAgent(BaseAgent[ValuePolicyReport]):
    agent_role = "value_analyst"
    output_model = ValuePolicyReport
    require_citations = True
    max_tokens = 4096

    def build_prompt(
        self,
        *,
        policy: Policy,
        carrier_kb_text: str,
        carrier_kb_path: str,
        type_kb_text: str,
        type_kb_path: str,
    ) -> tuple[str, str, list[tuple[str, bytes | str]]]:
        system = (
            "You are the value-analyst agent on the Argosy insurance fleet — the \"value\" axis.\n\n"
            "Your job: judge whether this policy's premium is fair given its coverage, using the "
            "carrier's tariff KB (attached) and the category KB (attached).\n\n"
            "ABSOLUTE RULES:\n"
            "1. premium_fair: one of under / fair / over / unknown. Use 'unknown' if the carrier "
            "KB lacks a band for this coverage type — do NOT guess.\n"
            "2. benchmark_low_nis / benchmark_high_nis: the band you used from the carrier KB. "
            "Both annualized to NIS regardless of the policy's billing period or currency.\n"
            "3. benchmark_vintage: REQUIRED. Format: 'YYYY-Qn' or 'YYYY-MM'. Copy from the KB. "
            "If the KB's tariff section has `(vintage: 1900-Q1)`, this benchmark is unverified — "
            "set confidence=LOW and note the staleness in rationale.\n"
            "4. alternative_carriers: 0-3 carriers from the carrier KB whose tariff band is lower "
            "than this policy's actual premium for the same coverage type. Cite the carrier KB "
            "for each.\n"
            "5. citations: list of `domain_kb:<path>#<section>` references — at least one per "
            "non-trivial claim. The base-class citation gate enforces this.\n"
            "6. Output strictly conforms to the ValuePolicyReport JSON schema.\n\n"
            "OUTPUT JSON SCHEMA:\n"
            f"{ValuePolicyReport.model_json_schema()}\n"
        )

        annualized_premium_nis: float | None = None
        if policy.premium_amount is not None:
            factor = {"month": 12, "quarter": 4, "year": 1}[policy.premium_period]
            annualized_premium_nis = policy.premium_amount * factor
            if policy.premium_currency == "USD":
                annualized_premium_nis *= 3.7  # rough; agent should refine via FX KB if precise needed

        user_lines = [
            "=== POLICY UNDER REVIEW ===",
            f"policy_id: {policy.policy_id}",
            f"type: {policy.type.value}",
            f"carrier: {policy.carrier}",
            f"policy_number: {policy.policy_number}",
            f"holders: {policy.holders}",
            f"premium: {policy.premium_amount} {policy.premium_currency} / {policy.premium_period}",
            f"annualized_premium_nis (rough): {annualized_premium_nis}",
            f"coverage_amount_nis: {policy.coverage_amount_nis}",
            f"deductible_nis: {policy.deductible_nis}",
            f"term: {policy.term} (expires: {policy.term_expires_on})",
            "",
            f"=== CARRIER KB ===  document `{carrier_kb_path}`",
            f"=== TYPE KB ===  document `{type_kb_path}`",
            "",
            f"Produce a ValuePolicyReport JSON for policy_id={policy.policy_id}.",
        ]
        sources: list[tuple[str, bytes | str]] = [
            (carrier_kb_path, carrier_kb_text),
            (type_kb_path, type_kb_text),
        ]
        return system, "\n".join(user_lines), sources


__all__ = ["ValueAnalystAgent"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_value_analyst_agent.py -v`
Expected: PASS — 3 tests.

- [ ] **Step 5: Commit**

```
git add argosy/agents/insurance_value_analyst.py tests/test_insurance_value_analyst_agent.py
git commit -m "agent(insurance): ValueAnalystAgent — per-policy 'value' axis with carrier benchmarks"
```

---

## Task 11: `GapAnalystAgent`

**Files:**
- Create: `argosy/agents/insurance_gap_analyst.py`
- Test: `tests/test_insurance_gap_analyst_agent.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_insurance_gap_analyst_agent.py
"""Unit tests for GapAnalystAgent — household coverage-gap detection."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from argosy.agents.base import ConfidenceBand
from argosy.agents.insurance_gap_analyst import GapAnalystAgent
from argosy.agents.insurance_types import GapReport, Policy, PolicyType


def _policy(ptype: PolicyType = PolicyType.LIFE, holders: list[str] | None = None) -> Policy:
    return Policy(
        policy_id="x" * 8,
        type=ptype,
        carrier="Clal",
        policy_number="X-001",
        holders=holders or ["ariel"],
        source_file_id=1,
        extracted_on=datetime.now(timezone.utc),
        confidence=ConfidenceBand.HIGH,
    )


def test_agent_config():
    agent = GapAnalystAgent(user_id="ariel")
    assert agent.agent_role == "gap_analyst"
    assert agent.output_model is GapReport
    assert agent.require_citations is True


def test_build_prompt_attaches_all_kb_files_and_household_context():
    agent = GapAnalystAgent(user_id="ariel")
    system, user, sources = agent.build_prompt(
        policies=[_policy()],
        identity_yaml="primary_name: Ariel\nspouse:\n  name: Noga\nfamily:\n  children: [{name: Geva}]\n",
        goals_yaml="retirement_target_year: 2045\n",
        kb_documents=[
            ("domain_knowledge/insurance/life.md", "# Life KB\n"),
            ("domain_knowledge/insurance/disability.md", "# Disability KB\n"),
            ("domain_knowledge/insurance/life_stage_fit_rules.md", "# Life-stage rules\n"),
        ],
        per_policy_confidence_summary="life policy x covered with HIGH confidence",
    )
    src_ids = {sid for sid, _ in sources}
    assert "domain_knowledge/insurance/life_stage_fit_rules.md" in src_ids
    assert "domain_knowledge/insurance/disability.md" in src_ids


def test_system_prompt_requires_kb_citation_per_finding():
    agent = GapAnalystAgent(user_id="ariel")
    system, _, _ = agent.build_prompt(
        policies=[],
        identity_yaml="",
        goals_yaml="",
        kb_documents=[],
        per_policy_confidence_summary="",
    )
    assert "kb_citation" in system.lower()
    assert "every" in system.lower() or "all" in system.lower()


def test_system_prompt_describes_person_tag_values():
    agent = GapAnalystAgent(user_id="ariel")
    system, _, _ = agent.build_prompt(
        policies=[],
        identity_yaml="primary_name: Ariel\nspouse:\n  name: Noga\nfamily:\n  children: [{name: Geva}]\n",
        goals_yaml="",
        kb_documents=[],
        per_policy_confidence_summary="",
    )
    assert "ariel" in system.lower()
    assert "noga" in system.lower()
    assert "household" in system.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_gap_analyst_agent.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement the agent**

```python
# argosy/agents/insurance_gap_analyst.py
"""Household gap analyst — the "missing" axis of the insurance review.

Reads ALL policies for the household + identity + goals + every per-type KB
+ the life_stage_fit_rules KB. Produces GapReport listing per-person gaps
with KB citations.

Runs ONCE per review cascade (not per-policy). Sequential after the per-policy
fan-in because gap_analyst weights its findings by the per-policy coverage
reports' confidence values.
"""
from __future__ import annotations

import json

from argosy.agents.base import BaseAgent
from argosy.agents.insurance_types import GapReport, Policy


class GapAnalystAgent(BaseAgent[GapReport]):
    agent_role = "gap_analyst"
    output_model = GapReport
    require_citations = True
    max_tokens = 6144

    def build_prompt(
        self,
        *,
        policies: list[Policy],
        identity_yaml: str,
        goals_yaml: str,
        kb_documents: list[tuple[str, str]],
        per_policy_confidence_summary: str,
    ) -> tuple[str, str, list[tuple[str, bytes | str]]]:
        system = (
            "You are the gap-analyst agent on the Argosy insurance fleet — the \"missing\" axis.\n\n"
            "Your job: read the household's current policy set + identity + goals + the "
            "category KB + the life_stage_fit_rules KB. Identify coverage gaps per person.\n\n"
            "ABSOLUTE RULES:\n"
            "1. person values: 'ariel', 'noga', 'household', 'child:<name>'. Enumerate based on "
            "identity_yaml (primary_name + spouse.name + family.children[].name).\n"
            "2. coverage_type: one of the PolicyType values (health_shaban, health_shlishi, life, "
            "disability, long_term_care, homeowner, auto, liability_other) OR the literal 'none' "
            "when the person has no policy at all in a category that the life_stage_fit_rules KB "
            "says they should have.\n"
            "3. severity: info / yellow / red.\n"
            "4. kb_citation REQUIRED for every finding — cite the specific rule from "
            "life_stage_fit_rules.md or the category KB that makes the finding load-bearing. "
            "Format: `domain_kb:<path>#<section>`.\n"
            "5. If the KB for a category isn't present in your attached documents, set the "
            "finding's confidence-equivalent flag in your summary text and lower the report's "
            "overall confidence to LOW. Note 'KB missing: insurance/<category>.md not present'.\n"
            "6. Weight your findings by the per-policy coverage-report confidence values "
            "provided below — a LOW-confidence coverage report on Ariel's life policy means "
            "you should NOT confidently rule the gap closed; flag uncertainty instead.\n"
            "7. Output strictly conforms to the GapReport JSON schema.\n\n"
            "OUTPUT JSON SCHEMA:\n"
            f"{GapReport.model_json_schema()}\n"
        )

        policies_brief = json.dumps(
            [
                {
                    "policy_id": p.policy_id,
                    "type": p.type.value,
                    "carrier": p.carrier,
                    "holders": p.holders,
                    "coverage_amount_nis": p.coverage_amount_nis,
                    "superseded_by": p.superseded_by,
                }
                for p in policies
                if p.superseded_by is None  # active policies only
            ],
            indent=2,
        )

        kb_listing = "\n".join(f"- `{path}`" for path, _ in kb_documents)

        user = (
            "=== HOUSEHOLD IDENTITY ===\n"
            f"```yaml\n{identity_yaml or '(empty)'}\n```\n\n"
            "=== GOALS ===\n"
            f"```yaml\n{goals_yaml or '(empty)'}\n```\n\n"
            "=== ACTIVE POLICIES ===\n"
            f"```json\n{policies_brief}\n```\n\n"
            "=== PER-POLICY COVERAGE REPORT CONFIDENCE SUMMARY ===\n"
            f"{per_policy_confidence_summary or '(none — first review)'}\n\n"
            f"=== KB DOCUMENTS ATTACHED ===\n{kb_listing}\n\n"
            "Produce a GapReport JSON. Enumerate gaps per person. Cite KB for every finding."
        )

        sources: list[tuple[str, bytes | str]] = [(path, text) for path, text in kb_documents]
        return system, user, sources


__all__ = ["GapAnalystAgent"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_gap_analyst_agent.py -v`
Expected: PASS — 4 tests.

- [ ] **Step 5: Commit**

```
git add argosy/agents/insurance_gap_analyst.py tests/test_insurance_gap_analyst_agent.py
git commit -m "agent(insurance): GapAnalystAgent — household 'missing' axis with KB-cited gaps"
```

---

## Task 12: `InsuranceSynthesizerAgent`

**Files:**
- Create: `argosy/agents/insurance_synthesizer.py`
- Test: `tests/test_insurance_synthesizer_agent.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_insurance_synthesizer_agent.py
"""Unit tests for InsuranceSynthesizerAgent."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from argosy.agents.base import ConfidenceBand
from argosy.agents.insurance_synthesizer import InsuranceSynthesizerAgent
from argosy.agents.insurance_types import (
    CoveragePolicyReport,
    GapReport,
    InsuranceReview,
    Policy,
    PolicyType,
    Severity,
    ValuePolicyReport,
)


def _policy() -> Policy:
    return Policy(
        policy_id="abc12345",
        type=PolicyType.LIFE,
        carrier="Clal",
        policy_number="L-001",
        holders=["ariel"],
        source_file_id=1,
        extracted_on=datetime.now(timezone.utc),
        confidence=ConfidenceBand.HIGH,
    )


def _coverage_report() -> CoveragePolicyReport:
    return CoveragePolicyReport(
        policy_id="abc12345",
        overall_assessment="standard term life, no red flags",
        findings=[],
        confidence=ConfidenceBand.HIGH,
    )


def _value_report() -> ValuePolicyReport:
    return ValuePolicyReport(
        policy_id="abc12345",
        premium_fair="fair",
        benchmark_low_nis=2000.0,
        benchmark_high_nis=2400.0,
        benchmark_vintage="2025-Q3",
        rationale="within Clal's published band for 35-45M, NIS 1M face",
        citations=["domain_kb:insurance/carriers/clal.md#term-life-tariffs"],
        confidence=ConfidenceBand.HIGH,
    )


def _gap_report() -> GapReport:
    return GapReport(findings=[], summary="no critical gaps", confidence=ConfidenceBand.HIGH)


def test_agent_config():
    agent = InsuranceSynthesizerAgent(user_id="ariel")
    assert agent.agent_role == "insurance_synthesizer"
    assert agent.output_model is InsuranceReview
    assert agent.require_citations is True


def test_build_prompt_assembles_inputs():
    agent = InsuranceSynthesizerAgent(user_id="ariel")
    system, user, sources = agent.build_prompt(
        review_year=2026,
        policies=[_policy()],
        coverage_reports=[_coverage_report()],
        value_reports=[_value_report()],
        gap_report=_gap_report(),
        prior_review=None,
        user_context_summary="single-earner household, retirement target 2045",
    )
    assert "2026" in user
    assert "abc12345" in user


def test_system_prompt_describes_three_axes():
    agent = InsuranceSynthesizerAgent(user_id="ariel")
    system, _, _ = agent.build_prompt(
        review_year=2026,
        policies=[],
        coverage_reports=[],
        value_reports=[],
        gap_report=_gap_report(),
        prior_review=None,
        user_context_summary="",
    )
    assert "good" in system.lower()
    assert "value" in system.lower()
    assert "missing" in system.lower()


def test_system_prompt_flags_stale_vintage():
    agent = InsuranceSynthesizerAgent(user_id="ariel")
    system, _, _ = agent.build_prompt(
        review_year=2026,
        policies=[],
        coverage_reports=[],
        value_reports=[],
        gap_report=_gap_report(),
        prior_review=None,
        user_context_summary="",
    )
    assert "vintage" in system.lower()
    assert "18 months" in system.lower() or "stale" in system.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_synthesizer_agent.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement the agent**

```python
# argosy/agents/insurance_synthesizer.py
"""Insurance synthesizer — rolls up the 4 analyst reports into one InsuranceReview.

Per the accuracy-over-cost binding preference, defaults to Opus and requires
citations. Output structure: executive summary + 3-axis sections + per-policy
blocks + household gaps + deltas-vs-prior-year.
"""
from __future__ import annotations

import json
from typing import Any

from argosy.agents.base import BaseAgent
from argosy.agents.insurance_types import (
    CoveragePolicyReport,
    GapReport,
    InsuranceReview,
    Policy,
    ValuePolicyReport,
)


class InsuranceSynthesizerAgent(BaseAgent[InsuranceReview]):
    agent_role = "insurance_synthesizer"
    output_model = InsuranceReview
    require_citations = True
    max_tokens = 16384

    def build_prompt(
        self,
        *,
        review_year: int,
        policies: list[Policy],
        coverage_reports: list[CoveragePolicyReport],
        value_reports: list[ValuePolicyReport],
        gap_report: GapReport,
        prior_review: InsuranceReview | None,
        user_context_summary: str,
    ) -> tuple[str, str, list[tuple[str, bytes | str]]]:
        system = (
            "You are the insurance synthesizer on the Argosy fleet — the rollup peer to "
            "the plan synthesizer.\n\n"
            "Your job: produce ONE InsuranceReview document for the household, summarizing the "
            "per-policy coverage + value reports and the household gap report. Organize the "
            "narrative around the three axes:\n"
            "  - good   = what the existing policies do right\n"
            "  - value  = which premiums are fair, which are over/under market\n"
            "  - missing = household gaps that the current policy set does not address\n\n"
            "ABSOLUTE RULES:\n"
            "1. by_axis. dict with keys 'good', 'value', 'missing' — each is a markdown string "
            "summarizing findings across all policies and the gap report under that axis.\n"
            "2. by_policy. One PerPolicyBlock per active policy. coverage_summary_md and "
            "value_summary_md are markdown rollups of the analyst findings. combined_severity is "
            "the max severity across the two reports (red > yellow > info).\n"
            "3. deltas_vs_prior_year. If a prior_review is provided, list changes vs that "
            "review: new policies added, policies superseded, premium changes, gap closures, new "
            "gaps. If first review, empty list + executive_summary_md notes it's the baseline.\n"
            "4. Stale benchmarks. Check every ValuePolicyReport.benchmark_vintage. If any vintage "
            "is more than 18 months before the review_year, lower this report's overall "
            "confidence by one band and add an executive_summary_md note: 'Carrier tariff KB "
            "has not been refreshed in N months; value findings should be treated as directional.'\n"
            "5. KB-missing notes. If any analyst reports note 'KB missing: insurance/<cat>.md', "
            "surface that in the by_axis['missing'] section as a meta-limitation.\n"
            "6. citations. List every domain_kb reference cited by the analysts. Required.\n"
            "7. Output strictly conforms to the InsuranceReview JSON schema.\n\n"
            "OUTPUT JSON SCHEMA:\n"
            f"{InsuranceReview.model_json_schema()}\n"
        )

        coverage_json = json.dumps([r.model_dump(mode="json") for r in coverage_reports], indent=2)
        value_json = json.dumps([r.model_dump(mode="json") for r in value_reports], indent=2)
        gap_json = gap_report.model_dump_json(indent=2)
        policies_json = json.dumps(
            [
                {
                    "policy_id": p.policy_id,
                    "type": p.type.value,
                    "carrier": p.carrier,
                    "holders": p.holders,
                    "coverage_amount_nis": p.coverage_amount_nis,
                    "superseded_by": p.superseded_by,
                }
                for p in policies
            ],
            indent=2,
        )
        prior_json = prior_review.model_dump_json(indent=2) if prior_review else "(none — baseline review)"

        user = (
            f"=== REVIEW YEAR ===\n{review_year}\n\n"
            f"=== USER CONTEXT SUMMARY ===\n{user_context_summary or '(empty)'}\n\n"
            f"=== POLICIES ===\n```json\n{policies_json}\n```\n\n"
            f"=== COVERAGE REPORTS ===\n```json\n{coverage_json}\n```\n\n"
            f"=== VALUE REPORTS ===\n```json\n{value_json}\n```\n\n"
            f"=== GAP REPORT ===\n```json\n{gap_json}\n```\n\n"
            f"=== PRIOR YEAR REVIEW ===\n```json\n{prior_json}\n```\n\n"
            "Produce the InsuranceReview JSON now."
        )

        return system, user, []


__all__ = ["InsuranceSynthesizerAgent"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_synthesizer_agent.py -v`
Expected: PASS — 4 tests.

- [ ] **Step 5: Commit**

```
git add argosy/agents/insurance_synthesizer.py tests/test_insurance_synthesizer_agent.py
git commit -m "agent(insurance): InsuranceSynthesizerAgent — 3-axis rollup peer to plan_synthesizer"
```

---

## Task 13: `insurance_review_flow` orchestrator

**Files:**
- Create: `argosy/orchestrator/flows/insurance_review/__init__.py`
- Create: `argosy/orchestrator/flows/insurance_review/flow.py`
- Create: `argosy/state/queries_insurance.py` — helper module for review-row lookups
- Test: `tests/test_insurance_review_flow.py`

**Codex-tandem reviewer pass**: this orchestrator is a decision-flow file per the binding preference; dispatch via the codex-tandem kit before merge. Pattern from `~/.claude/projects/D--Projects-financial-advisor/memory/reference_codex_tandem.md`:

```python
# After implementing the flow:
import sys
sys.path.insert(0, "D:/Projects/financial-advisor/tools/codex-tandem/scripts")
from engine_codex import run_codex
r = run_codex(
    node_dir=Path("tools/codex-tandem/sessions/insurance-flow-review"),
    prompt="Review flow.py at <abs path>. Flag any concurrency / DB-session / fan-out bugs.",
    agent_name="insurance_flow_review",
    role="reviewer",
)
# Fix any BLOCKERS before committing.
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_insurance_review_flow.py
"""Integration test for run_insurance_review_flow with mocked agents."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from argosy.agents.base import ConfidenceBand
from argosy.agents.insurance_types import (
    CoveragePolicyReport,
    GapReport,
    InsuranceContext,
    InsuranceReview,
    Policy,
    PolicyType,
    Severity,
    ValuePolicyReport,
    PerPolicyBlock,
)
from argosy.orchestrator.flows.insurance_review.flow import run_insurance_review_flow


def _policies(n: int = 3) -> list[Policy]:
    types = [PolicyType.LIFE, PolicyType.HEALTH_SHABAN, PolicyType.HOMEOWNER]
    return [
        Policy(
            policy_id=f"pol{i}aaaa",
            type=types[i],
            carrier="Clal",
            policy_number=f"P-{i}",
            holders=["ariel"],
            source_file_id=i + 1,
            extracted_on=datetime.now(timezone.utc),
            confidence=ConfidenceBand.HIGH,
        )
        for i in range(n)
    ]


@pytest.fixture
def primed_user_context(test_session, test_user_id):
    """Seed user_context with 3 policies."""
    from argosy.state.models import UserContext
    import sqlalchemy as sa
    import yaml

    ic = InsuranceContext(policies=_policies(), last_extracted_on=datetime.now(timezone.utc))
    uc = test_session.scalar(sa.select(UserContext).where(UserContext.user_id == test_user_id))
    if uc is None:
        uc = UserContext(user_id=test_user_id)
        test_session.add(uc)
    uc.insurance_yaml = yaml.safe_dump(ic.model_dump(mode="json"))
    test_session.commit()
    return uc


@pytest.fixture
def fake_synth_output() -> InsuranceReview:
    return InsuranceReview(
        review_year=2026,
        executive_summary_md="ok",
        by_axis={"good": "g", "value": "v", "missing": "m"},
        by_policy=[],
        household_gaps=GapReport(findings=[], summary="", confidence=ConfidenceBand.HIGH),
        confidence=ConfidenceBand.HIGH,
    )


@pytest.mark.asyncio
async def test_flow_persists_decision_run_and_agent_reports(
    primed_user_context, test_user_id, test_session, monkeypatch, fake_synth_output
):
    """Full happy path: 3 policies → decision_run + 2N+2 agent_reports."""
    # Mock all 4 agents' .run() to return appropriately-shaped output.
    def make_fake_run(output):
        async def fake(self, **kwargs):
            r = MagicMock()
            r.output = output
            return r
        return fake

    coverage = CoveragePolicyReport(
        policy_id="x", overall_assessment="ok", confidence=ConfidenceBand.HIGH
    )
    value = ValuePolicyReport(
        policy_id="x", premium_fair="fair", benchmark_vintage="2025-Q3",
        rationale="ok", confidence=ConfidenceBand.HIGH,
    )
    gap = GapReport(findings=[], summary="ok", confidence=ConfidenceBand.HIGH)

    monkeypatch.setattr(
        "argosy.agents.insurance_coverage_check.CoverageCheckAgent.run",
        make_fake_run(coverage),
    )
    monkeypatch.setattr(
        "argosy.agents.insurance_value_analyst.ValueAnalystAgent.run",
        make_fake_run(value),
    )
    monkeypatch.setattr(
        "argosy.agents.insurance_gap_analyst.GapAnalystAgent.run",
        make_fake_run(gap),
    )
    monkeypatch.setattr(
        "argosy.agents.insurance_synthesizer.InsuranceSynthesizerAgent.run",
        make_fake_run(fake_synth_output),
    )

    run_id = await run_insurance_review_flow(user_id=test_user_id, trigger="manual")
    assert run_id is not None

    # Verify decision_run row
    from argosy.state.models import DecisionRun, AgentReport
    import sqlalchemy as sa
    run = test_session.scalar(sa.select(DecisionRun).where(DecisionRun.id == run_id))
    assert run.decision_kind == "insurance_review"
    assert run.ticker == "(insurance)"
    assert run.status == "completed"

    # 3 policies → 2*3 + 1 gap + 1 synth = 8 agent_reports
    # Note: AgentReport.decision_id is a String(64), not an int FK.
    # Orchestrator stamps str(run_id) into agent_reports.decision_id.
    reports = test_session.scalars(
        sa.select(AgentReport).where(AgentReport.decision_id == str(run_id))
    ).all()
    assert len(reports) == 8
    # All share decision_id (= str(decision_run_id)); each has its own per-invocation run_correlation_id
    correlation_ids = {r.run_correlation_id for r in reports}
    assert len(correlation_ids) == 8, "each agent invocation must mint its own uuid"


@pytest.mark.asyncio
async def test_flow_short_circuits_on_empty_policies(test_session, test_user_id):
    """Zero policies → returns None, no decision_run row created."""
    from argosy.state.models import UserContext
    import sqlalchemy as sa

    uc = test_session.scalar(sa.select(UserContext).where(UserContext.user_id == test_user_id))
    if uc is None:
        uc = UserContext(user_id=test_user_id, insurance_yaml="")
        test_session.add(uc)
        test_session.commit()
    else:
        uc.insurance_yaml = ""
        test_session.commit()

    run_id = await run_insurance_review_flow(user_id=test_user_id, trigger="manual")
    assert run_id is None


@pytest.mark.asyncio
async def test_flow_respects_cost_guard(primed_user_context, test_user_id, monkeypatch):
    """Cost guard returning True → flow returns None, no decision_run."""

    class FakeGuard:
        async def should_pause_non_routine(self, *, loop_name: str) -> bool:
            return True

    monkeypatch.setattr(
        "argosy.orchestrator.cost_guard.get_cost_guard", lambda **k: FakeGuard()
    )
    run_id = await run_insurance_review_flow(user_id=test_user_id, trigger="annual")
    assert run_id is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_review_flow.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement the orchestrator**

Create `argosy/orchestrator/flows/insurance_review/__init__.py` (empty package marker):

```python
"""Insurance review cascade flow (INS1)."""
from argosy.orchestrator.flows.insurance_review.flow import run_insurance_review_flow

__all__ = ["run_insurance_review_flow"]
```

Create `argosy/orchestrator/flows/insurance_review/flow.py`:

```python
# argosy/orchestrator/flows/insurance_review/flow.py
"""Insurance review cascade orchestrator (INS1).

Sequence:
  1. Pre-flight: cost-guard check, load policies, short-circuit if empty.
  2. Open decision_run row with decision_kind='insurance_review',
     ticker='(insurance)', notes_json={trigger, review_year, policy_count}.
  3. Per-policy fan-out (parallel via asyncio.gather): CoverageCheckAgent +
     ValueAnalystAgent for each active policy.
  4. Fan-in → GapAnalystAgent (single household run, sequential after fan-in).
  5. InsuranceSynthesizerAgent — last, with all upstream reports + prior review.
  6. Close decision_run, publish event, return run_id.

All agent invocations carry `decision_id=str(decision_run_id)` so agent_reports
group correctly via the API-field name (which IS the same value as the orchestrator's
decision_run_id, just stringified — `AgentReport.decision_id` is a `String(64)` column,
not an int FK). Each invocation mints its own `run_correlation_id` inside `BaseAgent.run`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from sqlalchemy import select

from argosy.agents.insurance_coverage_check import CoverageCheckAgent
from argosy.agents.insurance_gap_analyst import GapAnalystAgent
from argosy.agents.insurance_synthesizer import InsuranceSynthesizerAgent
from argosy.agents.insurance_types import (
    CoveragePolicyReport,
    GapReport,
    InsuranceContext,
    InsuranceReview,
    Policy,
    PolicyType,
    ValuePolicyReport,
)
from argosy.agents.insurance_value_analyst import ValueAnalystAgent
from argosy.api.events import publish_event
from argosy.config import get_settings
from argosy.execution.audit import record_audit_event
from argosy.logging import get_logger
from argosy.orchestrator.cost_guard import get_cost_guard
from argosy.state import db as db_mod
from argosy.state.models import DecisionRun, UserContext, UserFile
from argosy.state.queries_insurance import get_latest_insurance_review

_log = get_logger(__name__)


_KB_ROOT = Path(get_settings().domain_knowledge_dir) / "insurance"


def _kb_path_for_type(ptype: PolicyType) -> Path:
    """Map a PolicyType to its category KB file path."""
    return {
        PolicyType.HEALTH_SHABAN: _KB_ROOT / "health" / "shaban.md",
        PolicyType.HEALTH_SHLISHI: _KB_ROOT / "health" / "shlishi.md",
        PolicyType.LIFE: _KB_ROOT / "life.md",
        PolicyType.DISABILITY: _KB_ROOT / "disability.md",
        PolicyType.LONG_TERM_CARE: _KB_ROOT / "long_term_care.md",
        PolicyType.HOMEOWNER: _KB_ROOT / "property_casualty.md",
        PolicyType.AUTO: _KB_ROOT / "property_casualty.md",
        PolicyType.LIABILITY_OTHER: _KB_ROOT / "property_casualty.md",
    }[ptype]


def _kb_path_for_carrier(carrier: str) -> Path:
    """Map carrier display name to its KB file. Falls back to a generic stub when unknown."""
    slug = carrier.lower().replace(" ", "_").replace("מנורה_מבטחים", "menorah_mivtachim")
    candidates = list(_KB_ROOT.rglob(f"carriers/{slug}.md"))
    return candidates[0] if candidates else _KB_ROOT / "carriers" / "unknown.md"


def _read_kb(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_pdf_bytes(user_file_id: int, session) -> bytes:
    uf = session.scalar(select(UserFile).where(UserFile.id == user_file_id))
    if uf is None:
        return b""
    try:
        return Path(uf.storage_path).read_bytes()
    except OSError:
        return b""


async def _run_pair(
    *, policy: Policy, decision_run_id: int, user_id: str
) -> tuple[CoveragePolicyReport, ValuePolicyReport]:
    """Run coverage_check + value_analyst in parallel for one policy."""
    type_kb_path = _kb_path_for_type(policy.type)
    carrier_kb_path = _kb_path_for_carrier(policy.carrier)
    type_kb_text = _read_kb(type_kb_path)
    carrier_kb_text = _read_kb(carrier_kb_path)

    # Read the policy PDF inside a short-lived session.
    async with db_mod.session_scope() as session:
        pdf_bytes = _read_pdf_bytes(policy.source_file_id, session)

    coverage_agent = CoverageCheckAgent(user_id=user_id)
    value_agent = ValueAnalystAgent(user_id=user_id)

    decision_id_str = str(decision_run_id)
    coverage_task = coverage_agent.run(
        decision_id=decision_id_str,
        policy=policy,
        policy_pdf_bytes=pdf_bytes,
        pdf_filename=f"{policy.policy_id}.pdf",
        type_kb_text=type_kb_text,
        type_kb_path=str(type_kb_path.relative_to(_KB_ROOT.parent.parent)),
    )
    value_task = value_agent.run(
        decision_id=decision_id_str,
        policy=policy,
        carrier_kb_text=carrier_kb_text,
        carrier_kb_path=str(carrier_kb_path.relative_to(_KB_ROOT.parent.parent)),
        type_kb_text=type_kb_text,
        type_kb_path=str(type_kb_path.relative_to(_KB_ROOT.parent.parent)),
    )
    coverage_report, value_report = await asyncio.gather(coverage_task, value_task)
    return coverage_report.output, value_report.output


def _all_kb_documents() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for p in sorted(_KB_ROOT.rglob("*.md")):
        rel = str(p.relative_to(_KB_ROOT.parent.parent))
        out.append((rel, _read_kb(p)))
    return out


async def run_insurance_review_flow(
    *,
    user_id: str,
    trigger: Literal["annual", "manual"],
) -> int | None:
    """Run the 4-analyst + 1-synthesizer cascade. Returns decision_run_id or None.

    Pre-flight short-circuits: empty inventory, cost-guard active. Both log
    + audit + return None without opening a decision_run row.
    """
    # Pre-flight: cost guard
    guard = get_cost_guard(user_id=user_id)
    if await guard.should_pause_non_routine(loop_name="insurance_review"):
        _log.info("insurance_review.cost_guard_paused")
        await record_audit_event(
            user_id=user_id,
            event_type="insurance.review.skipped_by_cost_guard",
            entity_type="cadence",
            entity_id="insurance_review",
            payload={"trigger": trigger},
        )
        return None

    # Load policies
    async with db_mod.session_scope() as session:
        uc = session.scalar(select(UserContext).where(UserContext.user_id == user_id))
        if uc is None or not (uc.insurance_yaml or "").strip():
            _log.info("insurance_review.empty_yaml_skip")
            await record_audit_event(
                user_id=user_id,
                event_type="insurance.review.skipped_empty_yaml",
                entity_type="cadence",
                entity_id="insurance_review",
                payload={"trigger": trigger},
            )
            return None
        ic = InsuranceContext.model_validate(yaml.safe_load(uc.insurance_yaml))
        identity_yaml = uc.identity_yaml or ""
        goals_yaml = uc.goals_yaml or ""

    active_policies = [p for p in ic.policies if p.superseded_by is None]
    if not active_policies:
        _log.info("insurance_review.no_active_policies")
        return None

    review_year = datetime.now(timezone.utc).year

    # Open decision_run
    import json as _json
    async with db_mod.session_scope() as session:
        run = DecisionRun(
            user_id=user_id,
            ticker="(insurance)",
            tier=None,
            decision_kind="insurance_review",
            status="running",
            notes_json=_json.dumps(
                {
                    "trigger": trigger,
                    "review_year": review_year,
                    "policy_count": len(active_policies),
                }
            ),
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        decision_run_id = run.id

    # Per-policy fan-out (parallel)
    pair_tasks = [
        _run_pair(policy=p, decision_run_id=decision_run_id, user_id=user_id)
        for p in active_policies
    ]
    pair_results = await asyncio.gather(*pair_tasks, return_exceptions=False)
    coverage_reports = [c for c, _ in pair_results]
    value_reports = [v for _, v in pair_results]

    # Fan-in → gap analyst
    confidence_summary_lines = [
        f"- {p.carrier} {p.type.value}: coverage conf={c.confidence.value}"
        for p, c in zip(active_policies, coverage_reports)
    ]
    kb_docs = _all_kb_documents()
    decision_id_str = str(decision_run_id)
    gap_agent = GapAnalystAgent(user_id=user_id)
    gap_report = (
        await gap_agent.run(
            decision_id=decision_id_str,
            policies=active_policies,
            identity_yaml=identity_yaml,
            goals_yaml=goals_yaml,
            kb_documents=kb_docs,
            per_policy_confidence_summary="\n".join(confidence_summary_lines),
        )
    ).output

    # Synthesizer
    prior_review = get_latest_insurance_review(user_id=user_id)
    synth_agent = InsuranceSynthesizerAgent(user_id=user_id)
    synth_report = (
        await synth_agent.run(
            decision_id=decision_id_str,
            review_year=review_year,
            policies=active_policies,
            coverage_reports=coverage_reports,
            value_reports=value_reports,
            gap_report=gap_report,
            prior_review=prior_review,
            user_context_summary="(from identity + goals YAML; orchestrator passes them raw)",
        )
    ).output

    # Close decision_run
    async with db_mod.session_scope() as session:
        run = session.scalar(select(DecisionRun).where(DecisionRun.id == decision_run_id))
        run.status = "completed"
        run.finished_at = datetime.now(timezone.utc)
        session.commit()

    await publish_event(
        "insurance.review.completed",
        {"user_id": user_id, "decision_run_id": decision_run_id, "review_year": review_year},
    )

    return decision_run_id


__all__ = ["run_insurance_review_flow"]
```

Create `argosy/state/queries_insurance.py`:

```python
# argosy/state/queries_insurance.py
"""Lookup helpers for completed insurance_review decision_runs.

Kept in a sibling module rather than queries.py to keep INS1 changes
self-contained in this wave.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import cast, select
from sqlalchemy.types import String

from argosy.agents.insurance_types import InsuranceReview
from argosy.state import db as db_mod
from argosy.state.models import AgentReport, DecisionRun


def sa_cast_str(col):
    """Cast a DecisionRun.id Integer column to String for joining with AgentReport.decision_id."""
    return cast(col, String)


class InsuranceReviewSummary(BaseModel):
    decision_run_id: int
    review_year: int
    started_at: datetime
    finished_at: datetime | None
    status: str
    executive_summary_excerpt: str  # first ~200 chars
    policy_count: int


def _parse_synth_output(rep: AgentReport) -> InsuranceReview | None:
    # AgentReport persists the model output in `response_text` (Text column).
    # BaseAgent.run serializes structured outputs as the JSON form of the
    # output_model — for InsuranceSynthesizerAgent that's the InsuranceReview JSON.
    if not rep.response_text:
        return None
    try:
        return InsuranceReview.model_validate_json(rep.response_text)
    except Exception:  # noqa: BLE001
        return None


def get_latest_insurance_review(*, user_id: str) -> InsuranceReview | None:
    """Most recent completed InsuranceReview synthesizer output for user, or None."""
    with db_mod.session_scope_sync() as session:
        # Join via AgentReport.decision_id (stringified DecisionRun.id) per the
        # AgentReport schema — there's no decision_run_id column on agent_reports.
        stmt = (
            select(AgentReport)
            .join(DecisionRun, AgentReport.decision_id == sa_cast_str(DecisionRun.id))
            .where(
                DecisionRun.user_id == user_id,
                DecisionRun.decision_kind == "insurance_review",
                DecisionRun.status == "completed",
                AgentReport.agent_role == "insurance_synthesizer",
            )
            .order_by(DecisionRun.finished_at.desc())
            .limit(1)
        )
        rep = session.scalar(stmt)
        return _parse_synth_output(rep) if rep else None


def get_insurance_review(*, decision_run_id: int) -> InsuranceReview | None:
    with db_mod.session_scope_sync() as session:
        stmt = select(AgentReport).where(
            AgentReport.decision_id == str(decision_run_id),
            AgentReport.agent_role == "insurance_synthesizer",
        )
        rep = session.scalar(stmt)
        return _parse_synth_output(rep) if rep else None


def list_insurance_reviews(*, user_id: str, limit: int = 20) -> list[InsuranceReviewSummary]:
    out: list[InsuranceReviewSummary] = []
    with db_mod.session_scope_sync() as session:
        stmt = (
            select(DecisionRun, AgentReport)
            .outerjoin(
                AgentReport,
                (AgentReport.decision_id == sa_cast_str(DecisionRun.id))
                & (AgentReport.agent_role == "insurance_synthesizer"),
            )
            .where(
                DecisionRun.user_id == user_id,
                DecisionRun.decision_kind == "insurance_review",
            )
            .order_by(DecisionRun.started_at.desc())
            .limit(limit)
        )
        for run, rep in session.execute(stmt).all():
            review = _parse_synth_output(rep) if rep else None
            notes = json.loads(run.notes_json or "{}")
            excerpt = (review.executive_summary_md if review else "")[:200]
            out.append(
                InsuranceReviewSummary(
                    decision_run_id=run.id,
                    review_year=notes.get("review_year", run.started_at.year),
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    status=run.status,
                    executive_summary_excerpt=excerpt,
                    policy_count=notes.get("policy_count", 0),
                )
            )
    return out


__all__ = [
    "InsuranceReviewSummary",
    "get_insurance_review",
    "get_latest_insurance_review",
    "list_insurance_reviews",
]
```

- [ ] **Step 4: Dispatch codex-tandem reviewer pass on flow.py**

```
.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0, 'D:/Projects/financial-advisor/tools/codex-tandem/scripts')
from pathlib import Path
from engine_codex import run_codex
node = Path('D:/Projects/financial-advisor/tools/codex-tandem/sessions/insurance-flow-review')
node.mkdir(parents=True, exist_ok=True)
r = run_codex(
    node_dir=node,
    prompt='Review argosy/orchestrator/flows/insurance_review/flow.py for concurrency / DB-session / fan-out bugs. The file is at D:/Projects/financial-advisor/argosy/orchestrator/flows/insurance_review/flow.py. Cross-check against the established pattern at argosy/orchestrator/flows/plan_synthesis/orchestrator.py. Return BLOCKERS or COMMIT AS-IS.',
    agent_name='insurance_flow_review',
    role='reviewer',
)
print(r.verdict_text)
"
```

Address any BLOCKERS returned before proceeding.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_review_flow.py -v`
Expected: PASS — 3 tests.

- [ ] **Step 6: Commit**

```
git add argosy/orchestrator/flows/insurance_review/ argosy/state/queries_insurance.py tests/test_insurance_review_flow.py
git commit -m "flow(insurance): run_insurance_review_flow + query helpers + codex-reviewed orchestrator"
```

---

## Task 14: Annual loop wiring

**Files:**
- Modify: `argosy/orchestrator/loops/annual.py` — add `insurance_review` injectable + opportunistic block
- Test: `tests/test_annual_loop_insurance.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_annual_loop_insurance.py
"""Annual loop fires insurance_review opportunistically (non-fatal)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from argosy.orchestrator.loops.annual import AnnualLoop
from argosy.orchestrator.loops.base import LoopSchedule


@pytest.mark.asyncio
async def test_annual_calls_insurance_review_when_provided(monkeypatch, test_user_id):
    schedule = LoopSchedule(cron="0 8 2 1 *")
    insurance_calls = []

    async def fake_insurance_review(user_id: str):
        insurance_calls.append(user_id)
        return 999  # fake decision_run_id

    loop = AnnualLoop(
        schedule=schedule,
        enabled=True,
        user_id=test_user_id,
        domain_files_provider=lambda: [],
        pension_refresh_callable=None,
        insurance_review_callable=fake_insurance_review,
    )
    await loop.tick()
    assert insurance_calls == [test_user_id]


@pytest.mark.asyncio
async def test_annual_swallows_insurance_review_exception(monkeypatch, test_user_id):
    schedule = LoopSchedule(cron="0 8 2 1 *")

    async def fake_insurance_review_fails(user_id: str):
        raise RuntimeError("simulated cascade failure")

    loop = AnnualLoop(
        schedule=schedule,
        enabled=True,
        user_id=test_user_id,
        domain_files_provider=lambda: [],
        pension_refresh_callable=None,
        insurance_review_callable=fake_insurance_review_fails,
    )
    # Must not raise — annual loop's contract is non-fatal isolation.
    await loop.tick()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_annual_loop_insurance.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'insurance_review_callable'`.

- [ ] **Step 3: Wire the loop**

In `argosy/orchestrator/loops/annual.py`:

(a) Extend the constructor signature. Find the `__init__` method and add the new injectable param after `pension_refresh_callable`:

```python
        # before:
        # pension_refresh_callable: Callable[[str], Any] | None = None,
        # after:
        pension_refresh_callable: Callable[[str], Any] | None = None,
        insurance_review_callable: Callable[[str], Any] | None = None,
```

And add the corresponding instance assignment:

```python
        self._insurance_review: Callable[[str], Any] | None = insurance_review_callable
```

(b) In `tick`, after the existing pension snapshot block, add:

```python
        # Phase 4: opportunistic insurance review cascade.
        # Same non-fatal pattern as the pension snapshot block.
        insurance_review_run_id: int | None = None
        try:
            if self._insurance_review is not None:
                outcome = self._insurance_review(self.user_id)
                if hasattr(outcome, "__await__"):
                    outcome = await outcome  # type: ignore[assignment]
                if isinstance(outcome, int):
                    insurance_review_run_id = outcome
        except Exception:  # pragma: no cover - defensive
            _log.exception("annual.insurance_review_failed")
```

(c) Add `insurance_review_run_id` to the existing `record_audit_event` payload at the bottom of `tick`:

```python
                "insurance_review_run_id": insurance_review_run_id,
```

- [ ] **Step 4: Wire the actual callable in app bootstrap**

Locate the place where `AnnualLoop` is instantiated for production (likely in `argosy/orchestrator/scheduler.py` or `argosy/api/main.py` — grep for `AnnualLoop(`). Where it's constructed, pass:

```python
from argosy.orchestrator.flows.insurance_review import run_insurance_review_flow

AnnualLoop(
    ...
    insurance_review_callable=lambda user_id: run_insurance_review_flow(
        user_id=user_id, trigger="annual"
    ),
)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_annual_loop_insurance.py -v`
Expected: PASS — 2 tests.

- [ ] **Step 6: Commit**

```
git add argosy/orchestrator/loops/annual.py argosy/orchestrator/scheduler.py tests/test_annual_loop_insurance.py
git commit -m "loop(annual): opportunistic insurance_review cascade after pension snapshot"
```

(Note: if the scheduler file path differs in this repo, adjust the `git add` to the correct file. Use `grep -rn 'AnnualLoop(' argosy/` to confirm.)

---

## Task 15: Remaining `/api/insurance/...` endpoints

**Files:**
- Modify: `argosy/api/routes/insurance.py` — add policies-list / review-trigger / reviews-list / reviews-drilldown / edit / delete / re-extract
- Test: `tests/test_insurance_routes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_insurance_routes.py
"""End-to-end tests for the insurance API surface (beyond /upload)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import yaml
from fastapi.testclient import TestClient

from argosy.agents.base import ConfidenceBand
from argosy.agents.insurance_types import (
    InsuranceContext,
    InsuranceReview,
    Policy,
    PolicyType,
    GapReport,
)


def _seed_policies(test_session, test_user_id, policies: list[Policy]) -> None:
    from argosy.state.models import UserContext
    import sqlalchemy as sa
    ic = InsuranceContext(policies=policies, last_extracted_on=datetime.now(timezone.utc))
    uc = test_session.scalar(sa.select(UserContext).where(UserContext.user_id == test_user_id))
    if uc is None:
        uc = UserContext(user_id=test_user_id)
        test_session.add(uc)
    uc.insurance_yaml = yaml.safe_dump(ic.model_dump(mode="json"))
    test_session.commit()


def _policy(policy_id: str = "abc12345") -> Policy:
    return Policy(
        policy_id=policy_id,
        type=PolicyType.LIFE,
        carrier="Clal",
        policy_number="L-001",
        holders=["ariel"],
        source_file_id=1,
        extracted_on=datetime.now(timezone.utc),
        confidence=ConfidenceBand.HIGH,
    )


def test_list_policies_empty(api_client: TestClient):
    resp = api_client.get("/api/insurance/policies", params={"user_id": "ariel"})
    assert resp.status_code == 200
    assert resp.json() == {"policies": []}


def test_list_policies_returns_active_only(api_client: TestClient, test_session, test_user_id):
    p1 = _policy("alive111")
    p2 = _policy("dead2222")
    p2.superseded_by = "alive111"
    _seed_policies(test_session, test_user_id, [p1, p2])
    resp = api_client.get("/api/insurance/policies", params={"user_id": test_user_id})
    assert resp.status_code == 200
    ids = {p["policy_id"] for p in resp.json()["policies"]}
    assert ids == {"alive111"}


def test_trigger_review_returns_decision_run_id(api_client: TestClient, monkeypatch, test_user_id):
    async def fake_flow(*, user_id: str, trigger: str):
        return 42

    monkeypatch.setattr(
        "argosy.orchestrator.flows.insurance_review.run_insurance_review_flow", fake_flow
    )
    resp = api_client.post("/api/insurance/review", params={"user_id": test_user_id})
    assert resp.status_code == 200
    assert resp.json() == {"decision_run_id": 42, "status": "queued"}


def test_edit_policy_writes_through(api_client: TestClient, test_session, test_user_id):
    _seed_policies(test_session, test_user_id, [_policy("edit1234")])
    resp = api_client.post(
        "/api/insurance/policies/edit1234/edit",
        json={"user_id": test_user_id, "patch": {"carrier": "Migdal", "notes": "user fix"}},
    )
    assert resp.status_code == 200

    # Verify written
    from argosy.state.models import UserContext
    import sqlalchemy as sa
    uc = test_session.scalar(sa.select(UserContext).where(UserContext.user_id == test_user_id))
    ic = InsuranceContext.model_validate(yaml.safe_load(uc.insurance_yaml))
    p = next(p for p in ic.policies if p.policy_id == "edit1234")
    assert p.carrier == "Migdal"
    assert p.notes == "user fix"


def test_delete_policy_marks_superseded(api_client: TestClient, test_session, test_user_id):
    _seed_policies(test_session, test_user_id, [_policy("del12345")])
    resp = api_client.delete(
        "/api/insurance/policies/del12345", params={"user_id": test_user_id}
    )
    assert resp.status_code == 200

    from argosy.state.models import UserContext
    import sqlalchemy as sa
    uc = test_session.scalar(sa.select(UserContext).where(UserContext.user_id == test_user_id))
    ic = InsuranceContext.model_validate(yaml.safe_load(uc.insurance_yaml))
    p = next(p for p in ic.policies if p.policy_id == "del12345")
    assert p.superseded_by == "user_deleted"


def test_re_extract_endpoint(api_client: TestClient, monkeypatch, test_session, test_user_id):
    """POST /policies/{user_file_id}/re-extract schedules extract_uploaded_policy."""
    from argosy.state.models import UserFile
    uf = UserFile(
        user_id=test_user_id,
        sha256="r" * 64,
        original_name="re.pdf",
        sanitized_name="re.pdf",
        mime_type="application/pdf",
        kind="insurance_policy",
        size_bytes=10,
        storage_path="/tmp/re.pdf",
        source="insurance_policy_upload",
    )
    test_session.add(uf)
    test_session.commit()
    test_session.refresh(uf)

    scheduled = []
    async def fake_extract(user_file_id: int, user_id: str):
        scheduled.append(user_file_id)
        return None

    monkeypatch.setattr(
        "argosy.services.insurance_ingest.extract_uploaded_policy", fake_extract
    )
    resp = api_client.post(
        f"/api/insurance/policies/{uf.id}/re-extract",
        params={"user_id": test_user_id},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    # FastAPI runs BackgroundTasks AFTER the response is returned; the
    # TestClient's `with` block flushes them. Assert that extract_uploaded_policy
    # was actually scheduled — without this assert, a missing add_task call
    # would silently pass the test.
    assert scheduled == [uf.id]


def test_reviews_list_and_drilldown(api_client: TestClient, monkeypatch, test_user_id):
    """List endpoint returns summaries; drilldown returns full payload."""
    fake_summary = {
        "decision_run_id": 7,
        "review_year": 2026,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": "completed",
        "executive_summary_excerpt": "ok",
        "policy_count": 3,
    }
    monkeypatch.setattr(
        "argosy.api.routes.insurance.list_insurance_reviews",
        lambda *, user_id, limit=20: [type("S", (), fake_summary)],
    )

    resp = api_client.get("/api/insurance/reviews", params={"user_id": test_user_id})
    assert resp.status_code == 200
    assert len(resp.json()["reviews"]) == 1

    fake_review = InsuranceReview(
        review_year=2026,
        executive_summary_md="hello",
        by_axis={"good": "g", "value": "v", "missing": "m"},
        household_gaps=GapReport(findings=[], summary="", confidence=ConfidenceBand.HIGH),
        confidence=ConfidenceBand.HIGH,
    )
    monkeypatch.setattr(
        "argosy.api.routes.insurance.get_insurance_review",
        lambda *, decision_run_id: fake_review,
    )
    resp = api_client.get("/api/insurance/reviews/7")
    assert resp.status_code == 200
    assert resp.json()["review_year"] == 2026
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_routes.py -v`
Expected: FAIL — most routes 404.

- [ ] **Step 3: Add the endpoints**

Append to `argosy/api/routes/insurance.py`:

```python
# --- below the existing upload endpoint --------------------------------

from datetime import datetime, timezone
from typing import Any

from fastapi import BackgroundTasks, HTTPException, Query
from pydantic import BaseModel
import yaml
import sqlalchemy as sa

from argosy.agents.insurance_types import InsuranceContext, InsuranceReview, Policy
from argosy.state import db as db_mod
from argosy.state.models import UserContext
from argosy.state.queries_insurance import (
    InsuranceReviewSummary,
    get_insurance_review,
    list_insurance_reviews,
)


def _load_insurance_context(user_id: str) -> InsuranceContext:
    with db_mod.session_scope_sync() as session:
        uc = session.scalar(sa.select(UserContext).where(UserContext.user_id == user_id))
        if uc is None or not (uc.insurance_yaml or "").strip():
            return InsuranceContext()
        return InsuranceContext.model_validate(yaml.safe_load(uc.insurance_yaml))


def _save_insurance_context(user_id: str, ic: InsuranceContext) -> None:
    with db_mod.session_scope_sync() as session:
        uc = session.scalar(sa.select(UserContext).where(UserContext.user_id == user_id))
        if uc is None:
            uc = UserContext(user_id=user_id)
            session.add(uc)
        uc.insurance_yaml = yaml.safe_dump(ic.model_dump(mode="json"), allow_unicode=True)
        session.commit()


@router.get("/policies")
async def list_policies(user_id: str = Query(...)) -> dict[str, list[dict[str, Any]]]:
    ic = _load_insurance_context(user_id)
    active = [p for p in ic.policies if p.superseded_by is None]
    return {"policies": [p.model_dump(mode="json") for p in active]}


class PolicyEditRequest(BaseModel):
    user_id: str
    patch: dict[str, Any]


@router.post("/policies/{policy_id}/edit")
async def edit_policy(policy_id: str, req: PolicyEditRequest) -> dict[str, str]:
    ic = _load_insurance_context(req.user_id)
    for p in ic.policies:
        if p.policy_id == policy_id:
            for k, v in req.patch.items():
                if hasattr(p, k):
                    setattr(p, k, v)
            _save_insurance_context(req.user_id, ic)
            return {"status": "edited"}
    raise HTTPException(status_code=404, detail=f"policy {policy_id} not found")


@router.delete("/policies/{policy_id}")
async def delete_policy(policy_id: str, user_id: str = Query(...)) -> dict[str, str]:
    ic = _load_insurance_context(user_id)
    for p in ic.policies:
        if p.policy_id == policy_id and p.superseded_by is None:
            p.superseded_by = "user_deleted"
            _save_insurance_context(user_id, ic)
            return {"status": "deleted"}
    raise HTTPException(status_code=404, detail=f"policy {policy_id} not found")


@router.post("/policies/{user_file_id}/re-extract")
async def re_extract(
    user_file_id: int,
    background: BackgroundTasks,
    user_id: str = Query(...),
) -> dict[str, object]:
    from argosy.services.insurance_ingest import extract_uploaded_policy
    background.add_task(extract_uploaded_policy, user_file_id=user_file_id, user_id=user_id)
    return {"user_file_id": user_file_id, "status": "queued"}


@router.post("/review")
async def trigger_review(
    background: BackgroundTasks,
    user_id: str = Query(...),
) -> dict[str, object]:
    """Fire the cascade as a background task; return the decision_run_id."""
    from argosy.orchestrator.flows.insurance_review import run_insurance_review_flow

    # We need the run_id BEFORE returning, so run flow inline up to the
    # decision_run row creation but the analysts run async. Simplest: await
    # the full flow; the UI can subscribe to insurance.review.completed for
    # streaming. If latency becomes a problem, swap for a "kick off + return
    # the just-created run_id" pattern.
    run_id = await run_insurance_review_flow(user_id=user_id, trigger="manual")
    if run_id is None:
        raise HTTPException(status_code=409, detail="cascade skipped (empty inventory or cost-guard active)")
    return {"decision_run_id": run_id, "status": "queued"}


@router.get("/reviews")
async def get_reviews(user_id: str = Query(...), limit: int = 20) -> dict[str, list[dict[str, Any]]]:
    rows = list_insurance_reviews(user_id=user_id, limit=limit)
    return {"reviews": [r.model_dump(mode="json") if isinstance(r, InsuranceReviewSummary) else dict(r.__dict__) for r in rows]}


@router.get("/reviews/{decision_run_id}")
async def get_review(decision_run_id: int) -> dict[str, Any]:
    rv = get_insurance_review(decision_run_id=decision_run_id)
    if rv is None:
        raise HTTPException(status_code=404, detail=f"review {decision_run_id} not found")
    return rv.model_dump(mode="json")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_insurance_routes.py -v`
Expected: PASS — 7 tests.

- [ ] **Step 5: Commit**

```
git add argosy/api/routes/insurance.py tests/test_insurance_routes.py
git commit -m "route(insurance): list/edit/delete/re-extract + review trigger + reviews list/drilldown"
```

---

## Task 16: UI — NavBar + `/insurance` route shell

**Files:**
- Modify: `ui/src/components/NavBar.tsx` — add Insurance tab
- Create: `ui/src/app/insurance/page.tsx`
- Create: `ui/src/app/insurance/layout.tsx` — shared tabs frame for Inventory/Reviews
- Create: `ui/src/lib/insurance/api.ts`

- [ ] **Step 1: Add the API client**

```typescript
// ui/src/lib/insurance/api.ts
import { getJSON, postJSON, deleteJSON } from '@/lib/api';

export type Severity = 'info' | 'yellow' | 'red';
export type PolicyType =
  | 'health_shaban' | 'health_shlishi' | 'life' | 'disability'
  | 'long_term_care' | 'homeowner' | 'auto' | 'liability_other';

export interface Policy {
  policy_id: string;
  type: PolicyType;
  carrier: string;
  policy_number: string;
  holders: string[];
  premium_amount: number | null;
  premium_period: 'month' | 'quarter' | 'year';
  premium_currency: 'ILS' | 'USD';
  coverage_amount_nis: number | null;
  deductible_nis: number | null;
  term: 'whole_life' | 'term' | 'annual_renewable' | 'other' | null;
  term_expires_on: string | null;
  renewal_on: string | null;
  beneficiaries: string[];
  exclusions_summary: string;
  riders: string[];
  waiting_period_days: number | null;
  coinsurance_pct: number | null;
  claims_history_notes: string;
  source_file_id: number;
  extracted_on: string;
  confidence: 'low' | 'medium' | 'high';
  notes: string;
  superseded_by: string | null;
}

export interface ReviewSummary {
  decision_run_id: number;
  review_year: number;
  started_at: string;
  finished_at: string | null;
  status: string;
  executive_summary_excerpt: string;
  policy_count: number;
}

export interface InsuranceReview {
  review_year: number;
  executive_summary_md: string;
  by_axis: { good?: string; value?: string; missing?: string };
  by_policy: Array<{
    policy_id: string;
    carrier: string;
    type: PolicyType;
    holders: string[];
    coverage_summary_md: string;
    value_summary_md: string;
    combined_severity: Severity;
  }>;
  household_gaps: {
    findings: Array<{
      person: string;
      coverage_type: string;
      finding: string;
      recommended_action: string;
      severity: Severity;
      kb_citation: string;
    }>;
    summary: string;
    confidence: 'low' | 'medium' | 'high';
  };
  deltas_vs_prior_year: string[];
  citations: string[];
  confidence: 'low' | 'medium' | 'high';
}

export async function listPolicies(userId: string): Promise<Policy[]> {
  const data = await getJSON<{ policies: Policy[] }>(
    `/api/insurance/policies?user_id=${encodeURIComponent(userId)}`,
  );
  return data.policies;
}

export async function uploadPolicy(userId: string, file: File): Promise<{ user_file_id: number; status: string }> {
  const fd = new FormData();
  fd.append('file', file);
  const resp = await fetch(
    `/api/insurance/policies/upload?user_id=${encodeURIComponent(userId)}`,
    { method: 'POST', body: fd, cache: 'no-store' },
  );
  if (!resp.ok) throw new Error(`upload failed: ${resp.status} ${await resp.text()}`);
  return resp.json();
}

export async function editPolicy(userId: string, policyId: string, patch: Partial<Policy>): Promise<void> {
  await postJSON(`/api/insurance/policies/${encodeURIComponent(policyId)}/edit`, {
    user_id: userId,
    patch,
  });
}

export async function deletePolicy(userId: string, policyId: string): Promise<void> {
  await deleteJSON(`/api/insurance/policies/${encodeURIComponent(policyId)}?user_id=${encodeURIComponent(userId)}`);
}

export async function triggerReview(userId: string): Promise<{ decision_run_id: number; status: string }> {
  return postJSON(`/api/insurance/review?user_id=${encodeURIComponent(userId)}`, {});
}

export async function listReviews(userId: string, limit = 20): Promise<ReviewSummary[]> {
  const data = await getJSON<{ reviews: ReviewSummary[] }>(
    `/api/insurance/reviews?user_id=${encodeURIComponent(userId)}&limit=${limit}`,
  );
  return data.reviews;
}

export async function getReview(decisionRunId: number): Promise<InsuranceReview> {
  return getJSON<InsuranceReview>(`/api/insurance/reviews/${decisionRunId}`);
}
```

- [ ] **Step 2: Add the NavBar entry**

In `ui/src/components/NavBar.tsx`, locate the existing nav items (Portfolio, Plan, Expenses) and add Insurance between Plan and Expenses:

```tsx
{ href: '/insurance', label: 'Insurance' },
```

- [ ] **Step 3: Add the layout**

```tsx
// ui/src/app/insurance/layout.tsx
'use client';
import Link from 'next/link';
import { usePathname } from 'next/navigation';

export default function InsuranceLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const isReviews = pathname.startsWith('/insurance/reviews');
  return (
    <div className="p-6">
      <h1 className="text-2xl font-semibold mb-4">Insurance</h1>
      <nav className="flex gap-3 border-b mb-6 text-sm">
        <Link
          href="/insurance"
          className={isReviews ? 'pb-2 px-2 text-zinc-500' : 'pb-2 px-2 border-b-2 border-blue-500 font-medium'}
        >
          Inventory
        </Link>
        <Link
          href="/insurance/reviews"
          className={isReviews ? 'pb-2 px-2 border-b-2 border-blue-500 font-medium' : 'pb-2 px-2 text-zinc-500'}
        >
          Reviews
        </Link>
      </nav>
      {children}
    </div>
  );
}
```

- [ ] **Step 4: Add the page shell (Inventory placeholder)**

```tsx
// ui/src/app/insurance/page.tsx
export default function InsuranceInventoryPage() {
  return <div className="text-sm text-zinc-500">Inventory tab — populated in Task 17.</div>;
}
```

- [ ] **Step 5: Verify**

```
cd ui ; npm run lint ; npm run typecheck
```

Expected: both clean (lint may warn about the placeholder page; if so, the warning is non-blocking for this commit and resolves in Task 17).

Visit `http://localhost:1337/insurance` — the NavBar tab and shared tabs frame render.

- [ ] **Step 6: Commit**

```
git add ui/src/components/NavBar.tsx ui/src/app/insurance/ ui/src/lib/insurance/api.ts
git commit -m "ui(insurance): NavBar tab + /insurance route shell + API client"
```

---

## Task 17: UI — Inventory tab (policy cards + Upload button)

**Files:**
- Modify: `ui/src/app/insurance/page.tsx` (replace placeholder with real Inventory)
- Create: `ui/src/components/insurance/policy-card.tsx`
- Create: `ui/src/components/insurance/upload-policy-button.tsx`
- Create: `ui/src/components/insurance/holder-badge.tsx`

- [ ] **Step 1: Holder badge component**

```tsx
// ui/src/components/insurance/holder-badge.tsx
export function HolderBadge({ holder }: { holder: string }) {
  const color =
    holder === 'household' ? 'bg-zinc-200 text-zinc-700'
    : holder.startsWith('child:') ? 'bg-amber-100 text-amber-800'
    : holder === 'ariel' ? 'bg-blue-100 text-blue-800'
    : holder === 'noga' ? 'bg-pink-100 text-pink-800'
    : 'bg-zinc-100 text-zinc-700';
  return (
    <span className={`text-xs px-2 py-0.5 rounded ${color}`}>
      {holder}
    </span>
  );
}
```

- [ ] **Step 2: Policy card component**

```tsx
// ui/src/components/insurance/policy-card.tsx
'use client';
import { useState } from 'react';
import { Policy, deletePolicy } from '@/lib/insurance/api';
import { HolderBadge } from './holder-badge';

function formatNIS(n: number | null): string {
  if (n === null) return '—';
  return `₪${n.toLocaleString('en-IL', { maximumFractionDigits: 0 })}`;
}

function annualizedNIS(p: Policy): number | null {
  if (p.premium_amount === null) return null;
  const f = { month: 12, quarter: 4, year: 1 }[p.premium_period];
  let nis = p.premium_amount * f;
  if (p.premium_currency === 'USD') nis *= 3.7;
  return nis;
}

function daysUntil(iso: string | null): number | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime() - Date.now();
  return Math.round(ms / (24 * 60 * 60 * 1000));
}

export function PolicyCard({ policy, userId, onChange }: { policy: Policy; userId: string; onChange: () => void }) {
  const [expanded, setExpanded] = useState(false);
  const renewalDays = daysUntil(policy.renewal_on);
  const annualPremium = annualizedNIS(policy);

  return (
    <div className="border rounded-lg p-4 bg-white shadow-sm">
      <div className="flex justify-between items-start gap-2">
        <div>
          <div className="font-medium">{policy.carrier} <span className="text-zinc-400 text-sm">·{policy.policy_number.slice(-4)}</span></div>
          <div className="text-xs text-zinc-500 uppercase tracking-wide">{policy.type.replace('_', ' ')}</div>
        </div>
        <div className="flex gap-1 flex-wrap justify-end">
          {policy.holders.map((h) => <HolderBadge key={h} holder={h} />)}
        </div>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2 text-sm">
        <div><span className="text-zinc-500">Premium:</span> {formatNIS(annualPremium)} /yr</div>
        <div><span className="text-zinc-500">Coverage:</span> {formatNIS(policy.coverage_amount_nis)}</div>
        {renewalDays !== null && (
          <div className="col-span-2 text-xs text-zinc-600">
            Renews in {renewalDays} days ({policy.renewal_on})
          </div>
        )}
      </div>
      <div className="mt-3 flex gap-2 text-xs">
        <button className="text-blue-600 hover:underline" onClick={() => setExpanded(!expanded)}>
          {expanded ? 'Hide details' : 'Show details'}
        </button>
        <button
          className="text-red-600 hover:underline ml-auto"
          onClick={async () => {
            if (!confirm(`Delete ${policy.carrier} ${policy.type}? (Mark as canceled.)`)) return;
            await deletePolicy(userId, policy.policy_id);
            onChange();
          }}
        >
          Cancel policy
        </button>
      </div>
      {expanded && (
        <dl className="mt-3 text-xs space-y-1 border-t pt-3">
          {policy.term && <div><dt className="inline text-zinc-500">Term: </dt><dd className="inline">{policy.term} {policy.term_expires_on ? `→ ${policy.term_expires_on}` : ''}</dd></div>}
          {policy.deductible_nis !== null && <div><dt className="inline text-zinc-500">Deductible: </dt><dd className="inline">{formatNIS(policy.deductible_nis)}</dd></div>}
          {policy.beneficiaries.length > 0 && <div><dt className="inline text-zinc-500">Beneficiaries: </dt><dd className="inline">{policy.beneficiaries.join(', ')}</dd></div>}
          {policy.exclusions_summary && <div><dt className="text-zinc-500">Exclusions:</dt><dd>{policy.exclusions_summary}</dd></div>}
          {policy.riders.length > 0 && <div><dt className="text-zinc-500">Riders:</dt><dd>{policy.riders.join(', ')}</dd></div>}
          {policy.notes && <div className="text-amber-700">Notes: {policy.notes}</div>}
          <div className="text-zinc-400 mt-2">
            Confidence: {policy.confidence} · Extracted {new Date(policy.extracted_on).toISOString().slice(0, 10)}
          </div>
        </dl>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Upload button**

```tsx
// ui/src/components/insurance/upload-policy-button.tsx
'use client';
import { useRef, useState } from 'react';
import { uploadPolicy } from '@/lib/insurance/api';

export function UploadPolicyButton({ userId, onUploaded }: { userId: string; onUploaded: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  return (
    <div>
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf"
        className="hidden"
        onChange={async (e) => {
          const f = e.target.files?.[0];
          if (!f) return;
          setBusy(true);
          setError(null);
          try {
            await uploadPolicy(userId, f);
            onUploaded();
          } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
          } finally {
            setBusy(false);
            if (inputRef.current) inputRef.current.value = '';
          }
        }}
      />
      <button
        className="bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700 disabled:opacity-50"
        disabled={busy}
        onClick={() => inputRef.current?.click()}
      >
        {busy ? 'Uploading…' : 'Upload a policy PDF'}
      </button>
      {error && <p className="text-red-600 text-sm mt-2">{error}</p>}
    </div>
  );
}
```

- [ ] **Step 4: Inventory page**

```tsx
// ui/src/app/insurance/page.tsx
'use client';
import { useEffect, useState } from 'react';
import { Policy, listPolicies } from '@/lib/insurance/api';
import { PolicyCard } from '@/components/insurance/policy-card';
import { UploadPolicyButton } from '@/components/insurance/upload-policy-button';

const USER_ID = 'ariel';  // single-user today; multi-tenant ready

export default function InsuranceInventoryPage() {
  const [policies, setPolicies] = useState<Policy[] | null>(null);
  const reload = () => listPolicies(USER_ID).then(setPolicies);
  useEffect(() => { reload(); }, []);

  if (policies === null) return <p className="text-sm text-zinc-500">Loading…</p>;

  if (policies.length === 0) {
    return (
      <div className="space-y-4">
        <UploadPolicyButton userId={USER_ID} onUploaded={reload} />
        <div className="border rounded-lg p-6 bg-zinc-50 text-sm text-zinc-700">
          <p>No policies on file yet. Drop a policy PDF using the button above — Argosy will
          read it, extract the carrier / coverage amount / renewal date, and add it to your
          insurance inventory.</p>
        </div>
      </div>
    );
  }

  // Group by type
  const byType = policies.reduce<Record<string, Policy[]>>((acc, p) => {
    (acc[p.type] ||= []).push(p);
    return acc;
  }, {});

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <p className="text-sm text-zinc-600">{policies.length} active polic{policies.length === 1 ? 'y' : 'ies'}</p>
        <UploadPolicyButton userId={USER_ID} onUploaded={reload} />
      </div>
      {Object.entries(byType).map(([type, list]) => (
        <section key={type}>
          <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-700 mb-2">
            {type.replace('_', ' ')}
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {list.map((p) => <PolicyCard key={p.policy_id} policy={p} userId={USER_ID} onChange={reload} />)}
          </div>
        </section>
      ))}
    </div>
  );
}
```

- [ ] **Step 5: Verify**

```
cd ui ; npm run lint ; npm run typecheck
```

Expected: both clean. Visit `/insurance` — empty-state renders; uploading a sample PDF surfaces a card within a few seconds (extractor BackgroundTask completes).

- [ ] **Step 6: Commit**

```
git add ui/src/app/insurance/page.tsx ui/src/components/insurance/
git commit -m "ui(insurance): Inventory tab — policy cards, holder badges, upload button"
```

---

## Task 18: UI — Reviews tab (list view)

**Files:**
- Create: `ui/src/app/insurance/reviews/page.tsx`
- Create: `ui/src/components/insurance/review-list.tsx`
- Create: `ui/src/components/insurance/trigger-review-button.tsx`

- [ ] **Step 1: Trigger button**

```tsx
// ui/src/components/insurance/trigger-review-button.tsx
'use client';
import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { triggerReview } from '@/lib/insurance/api';

export function TriggerReviewButton({ userId }: { userId: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <div>
      <button
        className="bg-emerald-600 text-white px-4 py-2 rounded hover:bg-emerald-700 disabled:opacity-50"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError(null);
          try {
            const { decision_run_id } = await triggerReview(userId);
            router.push(`/insurance/reviews/${decision_run_id}`);
          } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
            setBusy(false);
          }
        }}
      >
        {busy ? 'Running cascade…' : 'Run insurance review'}
      </button>
      {error && <p className="text-red-600 text-sm mt-2">{error}</p>}
    </div>
  );
}
```

- [ ] **Step 2: Review list component**

```tsx
// ui/src/components/insurance/review-list.tsx
'use client';
import Link from 'next/link';
import { ReviewSummary } from '@/lib/insurance/api';

export function ReviewList({ reviews }: { reviews: ReviewSummary[] }) {
  if (reviews.length === 0) {
    return (
      <div className="border rounded-lg p-6 bg-zinc-50 text-sm text-zinc-700">
        <p>No insurance reviews yet. Click <strong>Run insurance review</strong> above to fire
        the cascade — Argosy will read every policy on file, score coverage / value / gaps,
        and produce a written review.</p>
      </div>
    );
  }
  return (
    <table className="w-full text-sm">
      <thead className="text-left text-xs uppercase text-zinc-500 border-b">
        <tr>
          <th className="py-2">Year</th>
          <th>Status</th>
          <th>Policies</th>
          <th>Summary</th>
          <th>Date</th>
        </tr>
      </thead>
      <tbody>
        {reviews.map((r) => (
          <tr key={r.decision_run_id} className="border-b hover:bg-zinc-50">
            <td className="py-2">
              <Link href={`/insurance/reviews/${r.decision_run_id}`} className="text-blue-600 hover:underline">
                {r.review_year}
              </Link>
            </td>
            <td><StatusPill status={r.status} /></td>
            <td>{r.policy_count}</td>
            <td className="text-zinc-600 truncate max-w-md">{r.executive_summary_excerpt}…</td>
            <td className="text-zinc-500 text-xs">{r.started_at.slice(0, 10)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function StatusPill({ status }: { status: string }) {
  const cls = status === 'completed' ? 'bg-emerald-100 text-emerald-800'
    : status === 'failed' ? 'bg-red-100 text-red-800'
    : 'bg-amber-100 text-amber-800';
  return <span className={`text-xs px-2 py-0.5 rounded ${cls}`}>{status}</span>;
}
```

- [ ] **Step 3: Reviews list page**

```tsx
// ui/src/app/insurance/reviews/page.tsx
'use client';
import { useEffect, useState } from 'react';
import { ReviewSummary, listReviews } from '@/lib/insurance/api';
import { ReviewList } from '@/components/insurance/review-list';
import { TriggerReviewButton } from '@/components/insurance/trigger-review-button';

const USER_ID = 'ariel';

export default function ReviewsListPage() {
  const [reviews, setReviews] = useState<ReviewSummary[] | null>(null);
  useEffect(() => { listReviews(USER_ID).then(setReviews); }, []);

  if (reviews === null) return <p className="text-sm text-zinc-500">Loading…</p>;

  return (
    <div className="space-y-6">
      <div className="flex justify-end">
        <TriggerReviewButton userId={USER_ID} />
      </div>
      <ReviewList reviews={reviews} />
    </div>
  );
}
```

- [ ] **Step 4: Verify**

```
cd ui ; npm run lint ; npm run typecheck
```

Expected: clean. Visit `/insurance/reviews` — empty-state renders; running a review navigates to the (yet-empty) drilldown page.

- [ ] **Step 5: Commit**

```
git add ui/src/app/insurance/reviews/page.tsx ui/src/components/insurance/review-list.tsx ui/src/components/insurance/trigger-review-button.tsx
git commit -m "ui(insurance): Reviews tab list view + Run-review trigger button"
```

---

## Task 19: UI — Reviews drilldown

**Files:**
- Create: `ui/src/app/insurance/reviews/[runId]/page.tsx`
- Create: `ui/src/components/insurance/review-drilldown.tsx`
- Create: `ui/src/components/insurance/axis-section.tsx`

- [ ] **Step 1: Axis section component**

```tsx
// ui/src/components/insurance/axis-section.tsx
'use client';
import { Markdown } from '@/components/Markdown';  // existing component from Wave B-UI

export function AxisSection({
  title,
  body,
  accent,
}: {
  title: string;
  body: string;
  accent: 'good' | 'value' | 'missing';
}) {
  const color = {
    good: 'border-emerald-500 bg-emerald-50',
    value: 'border-blue-500 bg-blue-50',
    missing: 'border-amber-500 bg-amber-50',
  }[accent];
  return (
    <section className={`border-l-4 rounded p-4 ${color}`}>
      <h3 className="font-medium text-zinc-800 mb-2">{title}</h3>
      <div className="prose prose-sm max-w-none">
        <Markdown content={body || '_No findings under this axis._'} />
      </div>
    </section>
  );
}
```

- [ ] **Step 2: Drilldown component**

```tsx
// ui/src/components/insurance/review-drilldown.tsx
'use client';
import { InsuranceReview, Severity } from '@/lib/insurance/api';
import { Markdown } from '@/components/Markdown';
import { AxisSection } from './axis-section';
import { HolderBadge } from './holder-badge';

function severityCls(s: Severity): string {
  return s === 'red' ? 'bg-red-100 text-red-800'
    : s === 'yellow' ? 'bg-amber-100 text-amber-800'
    : 'bg-zinc-100 text-zinc-700';
}

export function ReviewDrilldown({ review }: { review: InsuranceReview }) {
  return (
    <article className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold">{review.review_year} Insurance Review</h1>
        <div className="text-xs text-zinc-500 mt-1">
          Confidence: {review.confidence}
          {review.deltas_vs_prior_year.length > 0 && <> · {review.deltas_vs_prior_year.length} change{review.deltas_vs_prior_year.length === 1 ? '' : 's'} vs prior year</>}
        </div>
      </header>

      <section className="prose max-w-none">
        <Markdown content={review.executive_summary_md} />
      </section>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <AxisSection title="Good" body={review.by_axis.good ?? ''} accent="good" />
        <AxisSection title="Value" body={review.by_axis.value ?? ''} accent="value" />
        <AxisSection title="Missing" body={review.by_axis.missing ?? ''} accent="missing" />
      </div>

      {review.by_policy.length > 0 && (
        <section>
          <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-700 mb-3">Per-policy findings</h2>
          <div className="space-y-3">
            {review.by_policy.map((b) => (
              <details key={b.policy_id} className="border rounded p-3 bg-white">
                <summary className="cursor-pointer flex justify-between items-center">
                  <div>
                    <span className="font-medium">{b.carrier}</span>
                    <span className="text-zinc-400 text-sm"> · {b.type.replace('_', ' ')}</span>
                    <span className="ml-2">
                      {b.holders.map((h) => <HolderBadge key={h} holder={h} />)}
                    </span>
                  </div>
                  <span className={`text-xs px-2 py-0.5 rounded ${severityCls(b.combined_severity)}`}>
                    {b.combined_severity}
                  </span>
                </summary>
                <div className="mt-3 space-y-3 text-sm">
                  <div>
                    <h4 className="font-medium text-zinc-700">Coverage</h4>
                    <div className="prose prose-sm max-w-none"><Markdown content={b.coverage_summary_md} /></div>
                  </div>
                  <div>
                    <h4 className="font-medium text-zinc-700">Value</h4>
                    <div className="prose prose-sm max-w-none"><Markdown content={b.value_summary_md} /></div>
                  </div>
                </div>
              </details>
            ))}
          </div>
        </section>
      )}

      {review.household_gaps.findings.length > 0 && (
        <section>
          <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-700 mb-3">Household gaps</h2>
          <ul className="space-y-2 text-sm">
            {review.household_gaps.findings.map((f, i) => (
              <li key={i} className="border-l-2 border-amber-400 bg-amber-50 px-3 py-2">
                <div className="flex justify-between items-start gap-2">
                  <div>
                    <strong>{f.person}</strong> <span className="text-zinc-500">/ {f.coverage_type}</span>
                  </div>
                  <span className={`text-xs px-2 py-0.5 rounded ${severityCls(f.severity)}`}>{f.severity}</span>
                </div>
                <p className="mt-1">{f.finding}</p>
                <p className="mt-1 text-zinc-700"><em>Action:</em> {f.recommended_action}</p>
                <p className="mt-1 text-xs text-zinc-500">Cite: <code>{f.kb_citation}</code></p>
              </li>
            ))}
          </ul>
        </section>
      )}

      {review.deltas_vs_prior_year.length > 0 && (
        <section>
          <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-700 mb-3">Deltas vs prior year</h2>
          <ul className="list-disc list-inside text-sm space-y-1">
            {review.deltas_vs_prior_year.map((d, i) => <li key={i}>{d}</li>)}
          </ul>
        </section>
      )}
    </article>
  );
}
```

- [ ] **Step 3: Drilldown page (with Wave B-UI cascade panel)**

Per spec §8.3, the drilldown reuses `<AgentCascadePanel>` from Wave B-UI to show all 4+2 agent runs with their sources / outputs / citations. The panel takes a `decisionId` prop and resolves the cascade via the existing `/api/agent-activity` route.

```tsx
// ui/src/app/insurance/reviews/[runId]/page.tsx
'use client';
import { use, useEffect, useState } from 'react';
import { InsuranceReview, getReview } from '@/lib/insurance/api';
import { ReviewDrilldown } from '@/components/insurance/review-drilldown';
import { AgentCascadePanel } from '@/components/AgentCascadePanel';  // existing from Wave B-UI

export default function ReviewDrilldownPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = use(params);
  const [review, setReview] = useState<InsuranceReview | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getReview(Number(runId))
      .then(setReview)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [runId]);

  if (error) return <p className="text-red-600 text-sm">Error loading review: {error}</p>;
  if (!review) return <p className="text-sm text-zinc-500">Loading review…</p>;

  return (
    <div className="space-y-8">
      <ReviewDrilldown review={review} />
      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-700 mb-3">
          Agent cascade
        </h2>
        {/* Wave B-UI panel — shows extractor/coverage/value/gap/synth runs +
            their sources_json / response_text / citations. groupKey = decision_id
            which equals str(decision_run_id) — see Task 13 orchestrator. */}
        <AgentCascadePanel decisionId={runId} />
      </section>
    </div>
  );
}
```

If the `AgentCascadePanel` import path or prop shape differs in this repo, grep `ui/src/components/` for the actual component before adopting the snippet — the symbol existed at Wave B-UI ship time but may have moved.

- [ ] **Step 4: Verify**

```
cd ui ; npm run lint ; npm run typecheck
```

Expected: clean. After running a review and navigating to its drilldown, the 3-axis layout + per-policy + gaps render.

- [ ] **Step 5: Commit**

```
git add ui/src/app/insurance/reviews/[runId]/ ui/src/components/insurance/review-drilldown.tsx ui/src/components/insurance/axis-section.tsx
git commit -m "ui(insurance): Reviews drilldown — 3 axes + per-policy + household gaps"
```

---

## Task 20: Live LLM end-to-end test

**Files:**
- Create: `tests/test_insurance_review_e2e.py`
- Create: `tests/fixtures/insurance/clal_life.pdf` (synthetic — see step 1)
- Create: `tests/fixtures/insurance/maccabi_shaban.pdf`
- Create: `tests/fixtures/insurance/migdal_homeowner.pdf`

- [ ] **Step 1: Synthesize 3 fixture PDFs**

For each fixture, generate a small PDF with plausible policy text. Use `pypdf` or `reportlab` (already in the venv). Example helper script — run once locally and commit the resulting PDFs:

```python
# scripts/build_insurance_fixtures.py — temporary; do not commit
from reportlab.pdfgen import canvas
from pathlib import Path

OUT = Path("tests/fixtures/insurance")
OUT.mkdir(parents=True, exist_ok=True)

def make(name: str, lines: list[str]) -> None:
    c = canvas.Canvas(str(OUT / name))
    y = 760
    for line in lines:
        c.drawString(40, y, line)
        y -= 18
    c.showPage()
    c.save()

make("clal_life.pdf", [
    "CLAL Life Insurance — Policy L-12345",
    "Insured: Ariel Jacobs",
    "Type: Term Life (10 years)",
    "Sum Insured: NIS 1,000,000",
    "Premium: NIS 180 / month",
    "Term expires: 2030-01-01",
    "Exclusions: war, suicide within 1yr",
    "Beneficiary: spouse (Noga Jacobs)",
])

make("maccabi_shaban.pdf", [
    "Maccabi Health Services — SHABAN supplementary",
    "Member: Ariel Jacobs + dependent (Geva)",
    "Annual premium: NIS 2,400 (family)",
    "Includes: private consultant access, abroad-care upgrade",
    "Does NOT include: cosmetic procedures, fertility beyond 2 cycles",
])

make("migdal_homeowner.pdf", [
    "MIGDAL Home Insurance — Policy H-77777",
    "Insured: Jacobs household",
    "Property: Tivon, structure + contents",
    "Earthquake coverage: included",
    "Annual premium: NIS 3,200",
    "Deductible: NIS 2,000 per claim",
])
```

Commit the resulting PDFs to `tests/fixtures/insurance/`. (Do not commit the helper script.)

- [ ] **Step 2: Write the e2e test**

```python
# tests/test_insurance_review_e2e.py
"""Live-LLM end-to-end test for the insurance review cascade.

GATED on the `llm_eval` pytest marker (skipped under `pytest -m "not llm_eval"`).
Runs the full cascade against 3 fixture PDFs with the real claude_code backend.
Asserts the basic shape — does NOT assert specific findings (LLM output varies).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from argosy.agents.base import ConfidenceBand
from argosy.agents.insurance_types import (
    InsuranceContext,
    InsuranceReview,
    Policy,
    PolicyType,
)
from argosy.services.insurance_ingest import extract_uploaded_policy

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "insurance"

pytestmark = pytest.mark.llm_eval


@pytest.mark.asyncio
async def test_full_cascade_three_policies(test_session, test_user_id, tmp_path):
    """Upload 3 fixtures via catalog_upload, fire extractor on each, fire cascade."""
    from argosy.services.file_catalog import catalog_upload

    fixtures = ["clal_life.pdf", "maccabi_shaban.pdf", "migdal_homeowner.pdf"]
    user_file_ids = []
    for name in fixtures:
        raw = (FIXTURE_DIR / name).read_bytes()
        dto = await catalog_upload(
            user_id=test_user_id,
            raw_bytes=raw,
            original_name=name,
            mime_type="application/pdf",
            kind="insurance_policy",
            source="insurance_policy_upload",
        )
        user_file_ids.append(dto.id)

    # Extract — fires the real LLM
    for uf_id in user_file_ids:
        policy = await extract_uploaded_policy(user_file_id=uf_id, user_id=test_user_id)
        assert policy is not None, f"extractor returned None for {uf_id}"
        assert policy.carrier  # filled
        assert policy.type  # filled

    # Run the cascade
    from argosy.orchestrator.flows.insurance_review import run_insurance_review_flow

    run_id = await run_insurance_review_flow(user_id=test_user_id, trigger="manual")
    assert run_id is not None

    # Verify outputs
    from argosy.state.queries_insurance import get_insurance_review

    review = get_insurance_review(decision_run_id=run_id)
    assert review is not None
    assert isinstance(review, InsuranceReview)
    assert review.executive_summary_md
    assert set(review.by_axis.keys()) <= {"good", "value", "missing"}

    # The fixtures intentionally exclude disability — gap_analyst should catch it.
    gap_types = {f.coverage_type for f in review.household_gaps.findings}
    assert "disability" in gap_types or "disability" in (review.by_axis.get("missing") or "").lower(), \
        "expected disability gap to be flagged given fixtures"

    # At least one citation came back from somewhere.
    assert len(review.citations) >= 1
```

- [ ] **Step 3: Run the e2e (manually, opt-in)**

```
.venv/Scripts/python.exe -m pytest tests/test_insurance_review_e2e.py -m llm_eval -v
```

Expected: PASS. This run uses real Opus tokens (~$1-5 worth). If it fails, examine the assertion that fails — the typical fix is a prompt refinement on the relevant agent.

- [ ] **Step 4: Commit**

```
git add tests/test_insurance_review_e2e.py tests/fixtures/insurance/
git commit -m "test(insurance): live-LLM e2e — 3 fixture policies through full cascade"
```

---

## Task 21: Full test suite + SDD handover refresh

**Files:**
- Modify: `docs/design/SDD.md` — update handover at the top + add a "Wave INS1 landed" section
- Verify: full backend test suite

- [ ] **Step 1: Run the full backend test suite**

```
.venv/Scripts/python.exe -m pytest -m "not llm_eval"
```

Expected: ALL PASS. Before the wave the suite was at 1,173 passing; this wave adds ~30-40 new tests + does not break any existing ones. Target: 1,200+ passing, 0 failures.

If failures appear: investigate root cause. Common suspects:
- The `decrypt_if_encrypted_pdf` refactor in Task 4 may have broken `tests/test_turn_attachments.py` if the inline replacement was wrong — re-read the existing tests.
- Adding `insurance_yaml` to UserContext may break tests that compare the full ORM row — those tests should be updated to include the new field with default `""`.
- The `gap_tracker.sync_gap_marker` integration in Task 7 may affect `tests/test_gap_tracker.py` if any existing test seeds the identity_yaml with the legacy slots — verify the "don't overwrite existing" branch holds.

- [ ] **Step 2: Update the SDD handover**

Locate the existing "Handover note" section at the top of `docs/design/SDD.md` (around line 16). Append a new paragraph after the most recent existing handover paragraph:

```markdown
**Wave INS1 — Insurance coverage analysis landed** on branch `wave-ins1-insurance`. 22 commits across 21 tasks. Key deliverables: (a) new `user_context.insurance_yaml` column (migration 0030) + `Policy` / `InsuranceContext` Pydantic schemas; (b) 5-role agent fleet — `InsuranceExtractorAgent` (per-upload, source-PDF document-block, no external citations), `CoverageCheckAgent` + `ValueAnalystAgent` (per-policy, KB-cited, parallel fan-out), `GapAnalystAgent` (per-household, after fan-in), `InsuranceSynthesizerAgent` (rollup, 3-axis InsuranceReview); (c) 15 KB files under `domain_knowledge/insurance/` (4 health categories + life + disability + LTC + P&C + life_stage_fit_rules + 6 carriers), all `last_verified: 1900-01-01` for DomainRefreshAgent to verify on the next annual run; (d) dedicated `POST /api/insurance/policies/upload` endpoint with `kind="insurance_policy" / source="insurance_policy_upload"` (bypasses chat-attachment auto-classify), reuses factored-out `decrypt_if_encrypted_pdf(contents, user_id, original_name)` helper; (e) `insurance_review_flow` orchestrator with per-policy parallel + fan-in + synthesizer sequence, codex-tandem-reviewed before merge; (f) annual loop wired with opportunistic insurance_review block (non-fatal on failure, same pattern as the pension snapshot); (g) `/insurance` UI with Inventory + Reviews tabs, holder badges, 3-axis drilldown, manual edit/delete/re-extract. Cascade persists as `decision_runs.decision_kind="insurance_review"` with `notes_json={trigger, review_year, policy_count}`; replay-able via `/api/decisions/{id}/replay` via Wave A-F provenance. Spec: `docs/superpowers/specs/2026-05-24-insurance-coverage-analysis-design.md`. Plan: `docs/superpowers/plans/2026-05-24-insurance-coverage-analysis-implementation.md`.
```

Also update the **handover header table** at the top of SDD.md:
- Bump `Last updated` date and brief summary to reflect INS1.

- [ ] **Step 3: Close the "Proposed next wave" paragraph**

Find the paragraph in the handover starting with `**Proposed next wave — Insurance & pension coverage analysis**`. Either delete it (the new "Wave INS1 landed" paragraph supersedes it) or replace it with:

```markdown
**Proposed next wave (closed by INS1) — Insurance & pension coverage analysis.** Shipped 2026-XX-XX as Wave INS1. See the "Wave INS1 landed" paragraph below for delivery details. Pension fund fees / returns / consolidation analysis (the deferred half from this brainstorm) is still queued as a future retirement-review wave.
```

- [ ] **Step 4: Commit**

```
git add docs/design/SDD.md
git commit -m "docs(sdd): Wave INS1 — Insurance coverage analysis landed"
```

- [ ] **Step 5: Open the PR**

```
git push -u origin wave-ins1-insurance
gh pr create --title "Wave INS1: Insurance coverage analysis" --body "$(cat <<'EOF'
## Summary
- 5-role agent fleet: extractor → coverage_check + value_analyst → gap_analyst → synthesizer
- New \`user_context.insurance_yaml\` YAML section (migration 0030)
- 15-file KB tree under \`domain_knowledge/insurance/\` with DomainRefreshAgent integration
- Dedicated upload endpoint, annual-loop wired cascade, /insurance UI route

## Test plan
- [ ] Full backend suite passes: \`.venv/Scripts/python.exe -m pytest -m "not llm_eval"\`
- [ ] UI lint + typecheck clean: \`cd ui ; npm run lint ; npm run typecheck\`
- [ ] Live LLM e2e once with \`-m llm_eval\` (real-cost; opt-in)
- [ ] Manual smoke: upload a real policy PDF; verify card appears; run review; verify drilldown

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review checklist

Before declaring the plan done, walk through this:

**Spec coverage:**
- [ ] Spec §2 architecture diagram → implemented by Tasks 8, 13
- [ ] Spec §3 KB structure → Task 5
- [ ] Spec §4.1 Policy schema → Task 2
- [ ] Spec §4.2 gap_tracker stage_8 sync → Task 7 (`sync_gap_marker`)
- [ ] Spec §4.3 insurance_yaml column → Task 1
- [ ] Spec §4.4 file_catalog changes → Tasks 3 + 8
- [ ] Spec §5 agent fleet → Tasks 6, 9, 10, 11, 12
- [ ] Spec §6.1 on-upload extractor → Tasks 7, 8
- [ ] Spec §6.2 review cascade orchestrator → Task 13
- [ ] Spec §6.3 annual loop wiring → Task 14
- [ ] Spec §6.4 manual trigger → Task 15
- [ ] Spec §7 persistence + queries → Tasks 13 (orchestrator), 13 (queries_insurance)
- [ ] Spec §8 UI surface → Tasks 16, 17, 18, 19
- [ ] Spec §9 error handling — each numbered subsection: 9.1 (Task 7), 9.2 (Task 4 helper + Task 8), 9.3 (Task 11 prompt), 9.4 (Task 12 prompt), 9.5 (Task 14), 9.6 (Task 13 pre-flight), 9.7 (Task 13 events), 9.8 (Task 7 merge), 9.9 (Task 13 short-circuit)
- [ ] Spec §10 testing strategy — per-agent unit tests, flow integration, KB structure, migration, file_catalog allow-list, gap_tracker backward-compat (covered in Task 7 tests), live e2e
- [ ] Spec §11 migration sequence → Tasks 1-21 follow it
- [ ] Spec §13 open questions left for impl — `policy_id` hash is in Task 6 prompt; FX-at-extraction is in Task 6; open-source-PDF endpoint reuses existing expense endpoint (no new code); manual-trigger streaming is the synchronous-await pattern in Task 15
- [ ] Spec §14 acceptance criteria — all items covered

**No placeholders:**
- All code blocks are full implementations.
- No "TBD", "implement appropriately," or unspecified edge cases.

**Type consistency:**
- `Policy.holders: list[str]` consistent across Task 2 schema, Task 6 extractor prompt, Task 11 gap_analyst, Task 17 UI.
- `decrypt_if_encrypted_pdf(contents: bytes, user_id: str, original_name: str) -> bytes` consistent in Tasks 4 and 8.
- `run_insurance_review_flow(*, user_id, trigger)` signature consistent in Tasks 13, 14, 15.
- Endpoint paths consistent: `/api/insurance/policies/upload`, `/api/insurance/policies/{id}/edit`, `/api/insurance/policies/{id}/re-extract`, `/api/insurance/review`, `/api/insurance/reviews`, `/api/insurance/reviews/{id}`.

If any item above fails the check, fix it inline and re-run that section's tests.

---




