# Insurance Coverage Analysis — Design

| Field | Value |
|---|---|
| **Wave name** | Insurance coverage analysis (working code name: `INS1`) |
| **Date** | 2026-05-24 |
| **Authors** | Ariel + Claude (collaborative brainstorm) |
| **Status** | Draft for review |
| **Scope estimate** | ~3-4 weeks (single big-wave, EX1-style) |
| **Migrations** | 0030_user_context_insurance_yaml |
| **Predecessor brainstorm** | `docs/design/SDD.md` "Proposed next wave" paragraph in handover (commit `c17a007`) |

---

## 1. Motivation

Argosy currently has three coverage pillars: **portfolio** (decision flow + synthesizer), **plan** (intake → plan_synthesizer → distillate), **expenses** (EX1-EX8 dashboard + categorization). Insurance is the fourth — and the only major financial-planning area the household reviews **annually** rather than continuously, which makes it a natural fit for a cadence-driven analyst fleet instead of an always-on cascade.

The brainstorm paragraph from the SDD handover specified three review axes:

- **"good"** — does the policy do what it claims; suspicious clauses, hidden exclusions.
- **"value"** — premium vs. expected benefit; carrier-side benchmarks.
- **"missing"** — coverage gaps relative to this household's age, family, income, goals.

This spec defines the agent fleet, KB, schema, persistence, UI, and testing strategy for a single-wave implementation.

### Non-goals

- **Pension fund fees / returns analysis** is out of scope (deferred to a future retirement-review wave). The fleet may **cite** existing `domain_knowledge/tax/israel/retirement/*.md` when reasoning about pension-fund-bundled survivor or disability rights, but does not assess pension-fund quality directly.
- **Claim-filing workflow** is out of scope. The schema captures `claims_history_notes` as free text; structured claim management is a future wave if desired.
- **Carrier shopping / quote requests** is out of scope. The wave produces "consider switching to {carrier_X, carrier_Y}" findings citing KB; actually obtaining new quotes is a manual user step.

---

## 2. Architecture

The wave adds a fourth pillar (Insurance) alongside the existing three. Policy PDFs flow through `file_catalog` (new `kind="insurance_policy"`). The extractor agent populates a new `user_context.insurance_yaml` section on every upload. A 4-analyst + 1-synthesizer cascade fires on the **annual loop** (Jan 2 — already publishes an `insurance_renewal` prompt) or a **manual UI button**. Cascade output is one durable `decision_run` (`kind='insurance_review'`) per run; the synthesizer's structured `InsuranceReview` payload is the user-facing artifact, accessible via `/api/decisions/{id}/replay`.

```
UI "Upload policy" ─► POST /api/insurance/policies/upload
                       ─► file_catalog (kind="insurance_policy", source="insurance_policy_upload")
                       ─► InsuranceExtractorAgent (background task)
                                                              │
                                                              ▼
                                                    user_context.insurance_yaml
                                                              │
   annual loop (Jan 2) ────► insurance_review_flow ◄──── manual button (POST /api/insurance/review)
                                       │
                                       ├─► CoverageCheckAgent  (per policy, parallel) ─┐
                                       ├─► ValueAnalystAgent   (per policy, parallel) ─┤
                                       └─► GapAnalystAgent     (household, after fan-in) ┤
                                                                                         ▼
                                                            InsuranceSynthesizerAgent
                                                                                         │
                                                                                         ▼
                                                    decision_runs row + agent_reports[]
                                                                                         │
                                                                                         ▼
                                                                              /insurance UI route
```

### 2.1 Why not a debate pattern?

The existing portfolio decision flow uses bull/bear debate (multi-perspective adversarial). Insurance review doesn't have a natural adversarial frame — coverage_check, value_analyst, and gap_analyst are **complementary axes** rather than opposing positions. A single InsuranceSynthesizerAgent rolling up three orthogonal axes is cleaner than forcing a debate shape.

### 2.2 Why annual cadence rather than monthly?

- Real-world household decisions about insurance happen at renewal time (mostly annual).
- Per-policy review with Opus-class models is expensive; monthly would burn ~12× the cost for marginal new signal.
- The annual loop already exists and already publishes an `insurance_renewal` prompt — wiring the cascade in is a one-line change.
- On-upload extractor keeps the **data** live (so the inventory tab reflects reality whenever the user drops a new PDF) without spending analyst tokens.

---

## 3. Knowledge base — `domain_knowledge/insurance/`

10 files in two subtrees, all following the `kupat_pensia.md` template (frontmatter with `topic / jurisdiction / last_verified / next_refresh_due / sources`):

```
domain_knowledge/insurance/
├── health/
│   ├── bituach_leumi_base.md       # מערכת הבריאות הציבורית (everyone has this layer)
│   ├── kupat_holim_sal.md          # סל הבריאות / 4 קופות (Clalit, Maccabi, Meuhedet, Leumit)
│   ├── shaban.md                   # שב"ן — שירותי בריאות נוספים (supplementary, per-קופה)
│   └── shlishi.md                  # ביטוח פרטי / ביטוח בריאות פרטי (third / private layer)
├── life.md                          # ביטוח חיים — risk-only vs. ביטוח חיים משולב חיסכון
├── disability.md                    # אובדן כושר עבודה (own-occupation vs. any-occupation)
├── long_term_care.md                # ביטוח סיעודי (regulated; הסדר סיעוד הקבוצתי)
├── property_casualty.md             # ביטוח דירה / ביטוח רכב / ביטוח חבות
├── life_stage_fit_rules.md          # opinionated rules: term-life face vs. mortgage balance; LTC age trigger; etc.
└── carriers/
    ├── clal.md
    ├── migdal.md
    ├── harel.md
    ├── menorah_mivtachim.md
    ├── phoenix.md
    └── ayalon.md
```

### 3.1 File template

Every KB file follows the `kupat_pensia.md` shape (see `domain_knowledge/tax/israel/retirement/kupat_pensia.md` for a worked example):

```yaml
---
topic: israel_insurance_<category>
jurisdiction: israel
last_verified: 2026-05-24
next_refresh_due: 2027-05-24
sources:
  - url: https://...
    retrieved: 2026-05-24
    tier: 1     # 1=government/regulator, 2=carrier/industry, 3=consumer-press
---
```

Required sections per file: **How it works** / **Why it matters** / **The user's situation** / **How agents should use this file** / **Refresh cadence** / **Open issues**.

### 3.2 Carrier files

`carriers/<carrier>.md` files carry **tariff bands per coverage type as ranges with vintage dates**, e.g.:

```markdown
## Term-life tariffs (vintage: 2025-Q3)

| Age band | Face amount (NIS) | Monthly premium range (NIS) |
|---|---|---|
| 30-39, non-smoker | 1,000,000 | 80-180 |
| 40-49, non-smoker | 1,000,000 | 140-320 |
| 30-39, smoker | 1,000,000 | 160-380 |
```

The vintage date is essential — `ValueAnalystAgent` reports the `benchmark_vintage` field on every value finding, and the synthesizer flags vintages > 18 months as a confidence reducer.

### 3.3 Refresh

KB files are automatically picked up by `argosy/orchestrator/loops/annual.py::_default_files_provider()`, which rglobs every `.md` under `domain_knowledge/`. No new wiring needed. `DomainRefreshAgent` will re-verify each insurance KB file on the next annual loop run after the wave ships.

---

## 4. Data model

### 4.1 `Policy` (Pydantic, in `argosy/agents/insurance_types.py`)

```python
from datetime import date, datetime
from enum import Enum
from typing import Literal
from pydantic import BaseModel, Field

from argosy.agents.base import ConfidenceBand


class PolicyType(str, Enum):
    HEALTH_SHABAN = "health_shaban"        # שב"ן supplementary
    HEALTH_SHLISHI = "health_shlishi"      # private / third-layer
    LIFE = "life"                           # ביטוח חיים
    DISABILITY = "disability"               # אובדן כושר עבודה
    LONG_TERM_CARE = "long_term_care"       # ביטוח סיעודי
    HOMEOWNER = "homeowner"                 # ביטוח דירה
    AUTO = "auto"                           # ביטוח רכב
    LIABILITY_OTHER = "liability_other"     # umbrella / professional / other liability


class Policy(BaseModel):
    policy_id: str                       # stable: sha8(carrier + policy_number + holders_joined)
    type: PolicyType
    carrier: str                          # e.g. "Clal"
    policy_number: str                    # carrier's identifier
    holders: list[str]                    # ["ariel"], ["noga"], ["ariel","noga"], ["household"], ["child:geva"]
    premium_amount: float | None          # in the period below
    premium_period: Literal["month", "quarter", "year"]
    premium_currency: Literal["ILS", "USD"] = "ILS"
    coverage_amount_nis: float | None     # face amount / sum insured / per-claim cap (FX-converted at extraction time if needed)
    deductible_nis: float | None
    term: Literal["whole_life", "term", "annual_renewable", "other"] | None
    term_expires_on: date | None          # only for fixed-term policies
    renewal_on: date | None               # next renewal for annual-renewable
    beneficiaries: list[str] = Field(default_factory=list)
    exclusions_summary: str = ""
    riders: list[str] = Field(default_factory=list)
    waiting_period_days: int | None = None
    coinsurance_pct: float | None = None  # 0.0-100.0; e.g. 30 = user pays 30%
    claims_history_notes: str = ""
    source_file_id: int                   # → user_files.id
    extracted_on: datetime
    confidence: ConfidenceBand
    notes: str = ""
    superseded_by: str | None = None      # when a newer policy replaces this one, point at its policy_id


class InsuranceContext(BaseModel):
    """The value of user_context.insurance_yaml after deserialization."""
    policies: list[Policy] = Field(default_factory=list)
    last_extracted_on: datetime | None = None
```

### 4.2 Relationship to existing `gap_tracker.py` stage_8

The existing `STAGE_FIELDS["stage_8"]` declares 6 flat-string `identity.*` slots: `life_insurance`, `disability_insurance`, `health_insurance`, `long_term_care_insurance`, `property_casualty_insurance`, `umbrella_liability_insurance`. These stay as **summary markers** — they answer "has the household addressed this category at all?" and drive the gap-tracker sidebar.

The new derivation: when `InsuranceExtractorAgent` writes a Policy to `insurance_yaml`, a service helper (`argosy/services/insurance_ingest.py::sync_gap_marker`) sets the corresponding `identity.<category>_insurance` slot to a one-line summary (e.g. `"Clal life term, 1M NIS face, expires 2030"`) if it was empty. **Existing string values are not overwritten** — the user's manual intake answers always win. This preserves the gap-tracker UX without forcing a coordinated rewrite.

### 4.3 `user_context.insurance_yaml`

Stored as a fourth top-level `TEXT` column on `user_context`, peer to `identity_yaml` / `goals_yaml` / `constraints_yaml`. Migration `0030_user_context_insurance_yaml` adds the column with `NOT NULL DEFAULT ''`. The empty default deserializes to `InsuranceContext(policies=[], last_extracted_on=None)`. The migration depends on `0029_agent_reports_prompts` (current head as of 2026-05-24).

### 4.4 `file_catalog` change — dedicated upload path

`argosy/services/file_catalog.py` is the canonical ingest boundary (CLAUDE.md binding constraint: do NOT bypass `catalog_upload` for any new user-byte-blob ingest path). Two changes:

- Add `"insurance_policy"` to `_ALLOWED_KINDS`.
- Add `"insurance_policy_upload"` to `_ALLOWED_SOURCES`.

**Why a new source rather than reusing `chat_attachment`:** `argosy/services/turn_attachments.py::save_attachment` runs incoming chat files through `_classify(mime_type, original_name)`, which only emits `text|image|pdf` — there is no MIME-based path from "user drops a PDF in chat" to `kind="insurance_policy"`. Without an explicit upload intent, the extractor hook is unreachable.

The fix is an explicit upload path: a new endpoint `POST /api/insurance/policies/upload` accepts a multipart file + holder hint, calls `catalog_upload(user_id=..., kind="insurance_policy", source="insurance_policy_upload", ...)` directly (no `_classify` call, no `turn_uuid`), and on success schedules the extractor task. The Inventory tab gets a prominent "Upload a policy" button that hits this endpoint. The chat-attachment flow continues to work for general PDFs but does NOT auto-trigger the extractor — policies enter the system only via the explicit endpoint.

Encryption gate: the new endpoint reuses the existing encryption check + per-user keyfile decryption logic from `save_attachment` (factor out to a helper `argosy/services/turn_attachments.py::decrypt_if_encrypted_pdf(contents: bytes, user_id: str, original_name: str) -> bytes` so both upload paths share it without duplication). The `original_name` arg is required because the existing `AttachmentEncryptedError` (HTTP 422) embeds the filename in its detail message — see `turn_attachments.py:244` — and we must preserve the same user-facing error to keep the Print-to-PDF workaround copy intact.

---

## 5. Agent fleet

### 5.1 Roles

| Role | Default model | Citations? | Input | Output | When |
|---|---|---|---|---|---|
| `InsuranceExtractorAgent` | Opus | No (source IS the PDF; per-field source_excerpt instead) | Policy PDF (document block) + current `insurance_yaml` | `Policy` (single) + merge directive | Per upload |
| `CoverageCheckAgent` | Opus | Required | One `Policy` + the policy PDF + `insurance/<type>.md` | `CoveragePolicyReport` | Review cascade, per policy |
| `ValueAnalystAgent` | Opus | Required | One `Policy` + `insurance/carriers/<carrier>.md` + `insurance/<type>.md` | `ValuePolicyReport` | Review cascade, per policy |
| `GapAnalystAgent` | Opus | Required | All `policies[]` + `identity_yaml.family` + `goals_yaml` + `life_stage_fit_rules.md` + per-type KB | `GapReport` | Review cascade, once after per-policy fan-in |
| `InsuranceSynthesizerAgent` | Opus | Required | All per-policy reports + `GapReport` + prior year's `InsuranceReview` (if any) + `user_context` summary | `InsuranceReview` | Review cascade, last |

All agents subclass `BaseAgent` and use the existing `build_prompt → (system, user, sources)` contract added in Wave A. Per the **accuracy-over-cost** binding preference, all default to Opus; no Haiku defaults.

### 5.2 Structured outputs

```python
class Severity(str, Enum):
    INFO = "info"
    YELLOW = "yellow"
    RED = "red"


# ---- per-policy outputs ------------------------------------------------------

class ClauseFinding(BaseModel):
    clause_excerpt: str                # 1-2 sentences quoted from the PDF
    finding: str                       # what the agent thinks of it
    severity: Severity
    kb_citation: str                   # required for non-INFO: "domain_kb:insurance/..."


class CoveragePolicyReport(BaseModel):
    policy_id: str
    overall_assessment: str
    findings: list[ClauseFinding] = Field(default_factory=list)
    confidence: ConfidenceBand


class ValuePolicyReport(BaseModel):
    policy_id: str
    premium_fair: Literal["under", "fair", "over", "unknown"]
    benchmark_low_nis: float | None
    benchmark_high_nis: float | None
    benchmark_vintage: str             # e.g. "2025-Q3"; flagged stale > 18 months
    alternative_carriers: list[str] = Field(default_factory=list)
    rationale: str
    citations: list[str] = Field(default_factory=list)  # domain_kb:insurance/carriers/...
    confidence: ConfidenceBand


# ---- household-level output --------------------------------------------------

class GapFinding(BaseModel):
    person: str                        # "ariel" | "noga" | "household" | "child:<name>"
    coverage_type: str                 # one of PolicyType values OR "none" (= absence of any policy in type)
    finding: str
    recommended_action: str
    severity: Severity
    kb_citation: str                   # required: life_stage_fit_rules.md or category KB


class GapReport(BaseModel):
    findings: list[GapFinding] = Field(default_factory=list)
    summary: str
    confidence: ConfidenceBand


# ---- synthesizer output ------------------------------------------------------

class PerPolicyBlock(BaseModel):
    policy_id: str
    carrier: str
    type: PolicyType
    holders: list[str]
    coverage_summary_md: str           # rollup of CoveragePolicyReport findings
    value_summary_md: str              # rollup of ValuePolicyReport
    combined_severity: Severity        # max of the two reports


class InsuranceReview(BaseModel):
    review_year: int
    executive_summary_md: str          # 2-3 paragraphs
    by_axis: dict[Literal["good", "value", "missing"], str]  # markdown per axis
    by_policy: list[PerPolicyBlock]
    household_gaps: GapReport
    deltas_vs_prior_year: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    confidence: ConfidenceBand
```

### 5.3 Extractor prompt notes

`InsuranceExtractorAgent` is the only role with `require_citations=False` (matching `intake_extractor.py`'s rationale: the source IS the user's PDF, not an external authority). It carries per-field `source_excerpt` strings on each Policy field — Citations API spans remain the machine-checkable provenance. The system prompt enforces:

1. **No fabrication.** If the PDF doesn't state a field, set it to `None`; do not infer.
2. **Holder detection.** The extractor reads `identity_yaml.spouse` + `family.children` to know which names map to which holder tags (`ariel` / `noga` / `child:<name>` / `household`). When the policy lists multiple insured persons, emit a multi-holder list.
3. **Currency handling.** If the policy is denominated in USD (rare but possible for expat-targeting carriers), `premium_currency="USD"` and `coverage_amount_nis` carries the FX-converted value using `argosy.services.fx.convert(amount, "USD", "ILS", date.today())`.
4. **Confidence band.** HIGH if every required field has a direct quote; MEDIUM if some fields are inferred from context; LOW if the policy structure was hard to parse. LOW-confidence policies are still written to `insurance_yaml` but get a `notes` warning.

### 5.4 Gap analyst KB-discipline

`GapAnalystAgent` MUST cite a KB section for every non-INFO finding. If the KB for a category hasn't been written yet (e.g. someone adds a new `PolicyType` before the corresponding `.md` exists), the agent returns `confidence=low` for findings in that category and notes `"KB missing: insurance/<category>.md not present"`. The synthesizer rolls these missing-KB notes up into the `by_axis["missing"]` block so the user sees the limitation.

---

## 6. Orchestration & triggers

### 6.1 On-upload extractor

New module: `argosy/services/insurance_ingest.py`.

```python
async def extract_uploaded_policy(*, user_file_id: int, user_id: str) -> Policy | None:
    """Fire InsuranceExtractorAgent on a single uploaded policy PDF.

    Returns the extracted Policy (also persisted into user_context.insurance_yaml),
    or None on extraction failure (logged; not re-raised — the upload itself is
    already persisted by the POST /api/insurance/policies/upload endpoint's
    catalog_upload call, so the user_file row remains recoverable for re-extract).
    """
```

Hooked into the dedicated `POST /api/insurance/policies/upload` endpoint (see §4.4): after `catalog_upload(...kind="insurance_policy", source="insurance_policy_upload"...)` commits, the endpoint schedules `extract_uploaded_policy(user_file_id=..., user_id=...)` as a fire-and-forget background task (FastAPI `BackgroundTasks`). The extraction does not block the HTTP response — the upload returns `{"user_file_id": <int>, "status": "queued"}` immediately, and the Inventory tab refreshes via the existing `agent.run.finished` WebSocket event once the extractor's `BaseAgent.run` settles.

The chat-attachment path (`turn_attachments.save_attachment`) is **unchanged** — dropping a PDF into chat does not trigger the extractor.

Merge logic: when a Policy with the same `policy_id` already exists in `insurance_yaml`, the existing row gets `superseded_by=<new_policy_id>` and the new row is appended. This preserves history (the Reviews tab can show "policy XYZ was replaced in 2026-05 with a new term") without aggregation logic on every read.

### 6.2 Review cascade orchestrator

New module: `argosy/orchestrator/flows/insurance_review/flow.py`. Mirrors `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` shape.

```python
async def run_insurance_review_flow(
    *,
    user_id: str,
    trigger: Literal["annual", "manual"],
) -> int:
    """Run the full 4-analyst + 1-synthesizer cascade. Returns the decision_run_id."""
```

Sequence:

1. **Pre-flight**: cost-guard check (`if await get_cost_guard(user_id=user_id).should_pause_non_routine(loop_name="insurance_review"): return None` — note the `await`; `should_pause_non_routine` is async per `argosy/orchestrator/cost_guard.py`). Load `insurance_yaml.policies`; short-circuit if empty (log + return None).
2. **Open decision_run**: `decision_runs` row with `decision_kind="insurance_review"`, `ticker="(insurance)"`, `notes_json={"trigger": ..., "review_year": ..., "policy_count": ...}`, `started_at=now()`, `status="running"`.
3. **Per-policy fan-out (parallel)**: For each policy, fire `CoverageCheckAgent` and `ValueAnalystAgent` concurrently via `asyncio.gather`. Each agent's report persists with `decision_run_id` set.
4. **Fan-in → gap analysis**: After all per-policy runs settle, fire `GapAnalystAgent` once with the full household context. (Sequential after fan-in because the gap analyst's findings depend on the per-policy reports' confidence values to weight its own conclusions — e.g. a LOW-confidence coverage report on a life policy means the gap analyst should flag uncertain coverage rather than ruling the gap closed.)
5. **Synthesizer**: Fire `InsuranceSynthesizerAgent` with concatenated reports + the prior year's `InsuranceReview` (if `get_latest_insurance_review(user_id)` returns a row from a prior year) + a `user_context` summary excerpt.
6. **Close decision_run**: set `finished_at`, write final synthesizer report.
7. **Publish events**: emit `insurance.review.completed` for the UI to refresh.

### 6.3 Annual loop wiring

In `argosy/orchestrator/loops/annual.py::AnnualLoop.tick`, after the existing prompt-publish loop and the existing pension snapshot block, add a third opportunistic block:

```python
# Phase 4: opportunistic insurance review cascade.
review_run_id: int | None = None
try:
    if self._insurance_review is not None:
        review_run_id = await self._insurance_review(self.user_id)
except Exception:  # pragma: no cover - defensive
    _log.exception("annual.insurance_review_failed")
```

Same non-fatal pattern as the existing pension snapshot — if the cascade fails, the rest of the annual loop completes.

### 6.4 Manual trigger

New endpoint `POST /api/insurance/review` returns `{"decision_run_id": <int>}` immediately; the cascade runs as a background task. The UI subscribes to `agent.run.started` / `agent.run.finished` WebSocket events (already emitted by `BaseAgent.run` per Wave B-UI) and groups by the API-field `decision_id`.

---

## 7. Persistence & replay

### 7.1 `decision_runs` row

`decision_runs.decision_kind = "insurance_review"` is a new string value joining the existing `"trade_proposal"`, `"plan_revision"`, `"plan_amendment_chat"`. The column is free TEXT — no enum migration. `argosy/state/models.py::DecisionRun` requires no schema change.

Fields populated:

- `user_id`
- `ticker="(insurance)"` — sentinel matching the existing `ticker="(plan)"` convention used by `plan_synthesis` and `plan_amendment_chat` runs (the column is NOT NULL).
- `tier=None` — not meaningful for insurance reviews; column is nullable post-migration 0018.
- `decision_kind="insurance_review"`
- `started_at`, `finished_at`, `status` ("running" → "completed" / "failed")
- `notes_json` — JSON payload `{"trigger": "annual" | "manual", "review_year": <int>, "policy_count": <int>}` for replay. Same usage pattern as Wave 4's amendment-chat notes_json.

Cascade grouping is by the `decision_runs.id` foreign key, not by a shared `run_correlation_id`. The existing `BaseAgent.run()` mints a fresh `uuid4()` per agent invocation as that report's `run_correlation_id` (Wave B-UI follow-up #2), so each of the 2N+2 reports carries its own per-invocation correlation_id. The orchestrator threads the same `decision_run_id` through every `BaseAgent.run(decision_run_id=...)` call instead — `agent_reports.decision_run_id` is already the cascade-grouping key.

**Naming note.** The DB column is `agent_reports.decision_run_id` (see `argosy/state/models.py`). The API serializes it as the JSON field `decision_id` (see `argosy/api/routes/agent_activity.py` and the existing payload shape), and the UI groups by that same JSON field (`ui/src/lib/useDecisionStream.ts::groupKey()` keys on `decision_id`). These are the same value at different layers — no rename or migration needed. When this spec refers to "cascade grouping," it means the DB-column `decision_run_id` / API-field `decision_id` pair.

### 7.2 `agent_reports` rows

One row per agent invocation:

- `coverage_check` × N policies
- `value_analyst` × N policies
- `gap_analyst` × 1
- `insurance_synthesizer` × 1

Total per cascade: `2N + 2` rows for N policies. The synthesizer's `output_json` IS the `InsuranceReview` payload — accessible directly via `/api/agent-activity?role=insurance_synthesizer&user_id=ariel`.

Each row carries `sources_json` (the document blocks used) per the Wave B-UI follow-up — so the UI Cascade Panel can show citations resolving back to KB files and policy PDFs.

### 7.3 Query helpers

In `argosy/state/queries.py`:

```python
def get_latest_insurance_review(session: Session, user_id: str) -> InsuranceReview | None:
    """Return the most recent completed insurance_review's synthesizer output."""

def get_insurance_review(session: Session, decision_run_id: int) -> InsuranceReview | None:
    """Return one specific insurance_review by decision_run_id."""

def list_insurance_reviews(session: Session, user_id: str, *, limit: int = 20) -> list[InsuranceReviewSummary]:
    """Lightweight rows for the Reviews tab list: year, decision_run_id, executive_summary first ~200 chars."""
```

### 7.4 Replay

`/api/decisions/{id}/replay` works for free via Wave A-F provenance. The replay payload returns all 4+2 agent_reports for the run, each with their `sources_json` and `output_json` intact — letting the user trace any finding in the synthesizer's report back to (a) the analyst that produced it, (b) the sources that analyst consumed, (c) the KB citations on each finding.

---

## 8. UI surface

### 8.1 Navigation

NavBar gets a new top-level tab "Insurance" between Plan and Expenses:

```
Portfolio | Plan | Insurance | Expenses
```

Route: `/insurance`. Tabs within the route: `Inventory` (default) and `Reviews`.

### 8.2 Inventory tab — `/insurance` (default tab)

A card grid grouped by `PolicyType`, then by holder. Each card shows:

- Carrier badge + policy number (truncated to last 4)
- Holder badges (`ariel`, `noga`, `household`, `child:geva`) reusing the EX8 tag chip styling
- Premium (annualized to NIS regardless of source period/currency) + coverage amount
- Renewal date with countdown ("renews in 42 days") if `renewal_on` is set
- Expand: full Policy fields + "Open source PDF" button (POSTs `/transactions/{id}/open-source-file` equivalent — TBD whether to reuse the expense one or add `/user-files/{id}/open`)

If `insurance_yaml.policies` is empty: empty-state with an "Upload a policy PDF" button that opens a file picker and POSTs to `/api/insurance/policies/upload`, plus a 2-3 sentence explainer of the flow ("Drop a policy PDF — Argosy will read it, extract the carrier / coverage amount / renewal date, and add it to your insurance inventory.").

### 8.3 Reviews tab — `/insurance/reviews`

List view: chronological table of past reviews, one row per `decision_run`. Columns: Year, Executive summary excerpt, Severity rollup (max severity across findings), Status (completed / partial / failed).

Drilldown view: `/insurance/reviews/[run_id]` renders:

- **Header**: review year + executive summary
- **3 axis columns** (good / value / missing) — markdown sections from `InsuranceReview.by_axis`
- **Per-policy section**: collapsible `PerPolicyBlock`s with the combined coverage+value markdown and a severity badge
- **Household gaps section**: `GapReport` findings, grouped by person, with KB-citation chips that link to the rendered KB file
- **Deltas vs prior year**: bulleted markdown
- **Cascade panel**: reuse the existing `<AgentCascadePanel>` from Wave B-UI to show all 4+2 agent runs with their sources / outputs / citations

### 8.4 New API endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/insurance/policies/upload` | Multipart upload of a policy PDF. Calls `catalog_upload(kind="insurance_policy", source="insurance_policy_upload")`, schedules `extract_uploaded_policy` as a background task. Returns `{"user_file_id": int, "status": "queued"}`. |
| `GET /api/insurance/policies?user_id=` | Inventory tab data — deserialized `insurance_yaml.policies` |
| `GET /api/insurance/reviews?user_id=&limit=` | Reviews tab list view |
| `GET /api/insurance/reviews/{run_id}` | Reviews tab drilldown |
| `POST /api/insurance/review` | Manual trigger of the review cascade; returns `{"decision_run_id": int}` |
| `POST /api/insurance/policies/{policy_id}/edit` | Manual edit/correction of an extracted field (writes through to `insurance_yaml`, audit-logged) |
| `DELETE /api/insurance/policies/{policy_id}` | Soft-delete (sets `superseded_by="user_deleted"`) — for when the user has canceled a policy |

### 8.5 Manual-edit semantics

Extractor confidence varies. The Inventory card should let the user correct a field inline (carrier name typo, wrong holder, missing renewal date) without re-uploading. `POST /api/insurance/policies/{policy_id}/edit` accepts a partial Policy patch and audit-logs the change as `insurance.policy.manually_edited` so the next review's "deltas_vs_prior_year" can distinguish "policy actually changed" from "user fixed an extraction error."

---

## 9. Error handling & edge cases

### 9.1 Extractor failures on upload

If `InsuranceExtractorAgent` raises (encrypted PDF the gate missed, LLM timeout, malformed output): catch in `extract_uploaded_policy`, log `insurance.policy.extracted.failed` with the error and `user_file_id`, audit-trail the failure, and **do not re-raise**. The `user_file` row is already persisted by the upstream `POST /api/insurance/policies/upload` call to `catalog_upload(...)`, so the file is recoverable; the user can retry extraction via a UI "Re-extract" button on the failed-state card (calls a new `POST /api/insurance/policies/{user_file_id}/re-extract` endpoint that fires `extract_uploaded_policy` again).

### 9.2 Encrypted PDFs

The new `POST /api/insurance/policies/upload` endpoint runs incoming bytes through the factored-out `argosy/services/turn_attachments.py::decrypt_if_encrypted_pdf(contents, user_id, original_name)` helper (§4.4) before calling `catalog_upload`. That helper carries the same logic the chat-attachment path uses today: byte-scan + `pypdf.is_encrypted` confirmation, per-user `configs/<user_id>/pdf_passwords.json` keyfile lookup, transparent re-serialization on successful decrypt, and on failure raises the existing `AttachmentEncryptedError` (HTTP 422) with the Print-to-PDF workaround message that includes the filename. The upload route lets the exception propagate so FastAPI returns the same 422 the chat path returns today — no new error type, no behavioral divergence.

### 9.3 KB-missing for a category

If `PolicyType` includes a value whose corresponding `domain_knowledge/insurance/<category>.md` does not exist (e.g. someone adds a new enum value before writing the KB), `GapAnalystAgent` returns `confidence=low` for that category's findings and notes `"KB missing: insurance/<category>.md not present at run time"`. The synthesizer surfaces this in the `by_axis["missing"]` block.

### 9.4 Stale benchmarks

`ValueAnalystAgent` outputs `benchmark_vintage` (e.g. `"2025-Q3"`). The synthesizer checks `(now - vintage) > 18 months` and, if true, lowers its overall `confidence` and adds a note to `executive_summary_md`: "Carrier tariff KB has not been refreshed in N months; value findings should be treated as directional."

### 9.5 Annual-loop failure isolation

The new opportunistic insurance-review block in `annual.py` wraps the cascade in `try/except` and logs but does not re-raise (same pattern as the existing pension snapshot block). A broken insurance cascade does not block the rest of the annual loop (prompts publish, domain refresh, pension snapshot).

### 9.6 Cost guard

Before the cascade fires, `await get_cost_guard(user_id=user_id).should_pause_non_routine(loop_name="insurance_review")` is checked. If the household's monthly LLM spend has tripped a guard threshold, the cascade is skipped and an audit event `insurance.review.skipped_by_cost_guard` is recorded. The manual UI button surfaces this as a "Cost guard active — try again next month" error. (The call is async — `should_pause_non_routine` is defined as `async def` in `argosy/orchestrator/cost_guard.py`; matches the existing usage in `AnnualLoop.tick`.)

### 9.7 Cross-user WebSocket filter

The cascade emits `agent.run.started` / `agent.run.finished` events per agent invocation, carrying `user_id`, the API-field `decision_id` (= DB-column `decision_run_id`), and the agent's per-invocation `run_correlation_id` — matching the existing Wave B-UI event shape. The UI groups by `decision_id`, not by `run_correlation_id`. The existing Wave B-UI `useDecisionStream` cross-user filter applies here unchanged.

### 9.8 Policy supersession

When the user uploads a renewed policy (same carrier + policy_number + holders → same `policy_id`), the existing record is marked `superseded_by=<new_policy_id>` and the new record is appended. The synthesizer's `deltas_vs_prior_year` block reads supersession events to produce "Clal life face amount raised from 1M to 1.5M NIS this cycle" deltas.

### 9.9 Empty insurance_yaml

If the cascade fires with zero policies in `insurance_yaml`, the pre-flight short-circuits before opening a `decision_run`, logs `insurance.review.skipped_empty_yaml`, and the UI manual-trigger surfaces "no policies uploaded yet — use the **Upload a policy PDF** button on the Inventory tab to add one."

---

## 10. Testing strategy

Test discipline: per the SDD convention, do NOT run the full 1,173-test suite during TDD. Pick the relevant 2-6 files. Full suite only pre-merge.

### 10.1 Per-agent unit tests (mocked LLM)

One test file per role, fixture-based:

- `tests/test_insurance_extractor_agent.py` — mock the LLM, assert: (a) plain-PDF extraction populates required fields; (b) holder detection uses `identity_yaml.spouse`; (c) USD policies populate `premium_currency` + FX-convert `coverage_amount_nis`; (d) missing required fields → confidence=LOW + non-empty notes; (e) merge into existing `insurance_yaml` with `policy_id` collision sets `superseded_by`.
- `tests/test_insurance_coverage_check_agent.py` — assert ClauseFinding citations are required for non-INFO severities; KB document block is attached as a source.
- `tests/test_insurance_value_analyst_agent.py` — assert `benchmark_vintage` is populated; LLM output without a vintage is rejected/regenerated; carrier KB document block is attached.
- `tests/test_insurance_gap_analyst_agent.py` — household enumeration covers spouse + children; per-person findings tag the right holder; KB-missing → confidence=LOW + note.
- `tests/test_insurance_synthesizer_agent.py` — output structure matches `InsuranceReview` schema; deltas_vs_prior_year computed correctly when prior year is provided; stale-vintage detection lowers overall confidence.

### 10.2 Flow integration test

`tests/test_insurance_review_flow.py` — mocks all 5 agents, drives `run_insurance_review_flow` with a 3-policy fixture, asserts:

- decision_run row created with `decision_kind="insurance_review"`, `ticker="(insurance)"`, valid `notes_json`
- 2N+2 agent_report rows (where N=3, so 8 rows)
- All rows share the same `decision_run_id` DB FK (the API-field `decision_id` the UI groups on); each row has its own per-invocation `run_correlation_id`
- Cost-guard pause is honored
- Empty-yaml short-circuit works

### 10.3 KB structure tests

`tests/test_insurance_kb.py` — asserts every `domain_knowledge/insurance/**/*.md` file has the required frontmatter (`topic`, `jurisdiction`, `last_verified`, `next_refresh_due`, `sources` with non-empty list). Catches drift from the `kupat_pensia.md` template.

### 10.4 Schema migration test

`tests/test_migration_0030.py` — asserts:

- Forward migration adds `insurance_yaml` column with `DEFAULT ''`
- Existing `user_context` rows get `insurance_yaml=''` after migration
- Reverse migration drops the column

### 10.5 file_catalog allow-list test

Add to existing `tests/test_file_catalog.py` (or whichever test file covers kinds): `"insurance_policy"` is accepted; round-trip persists.

### 10.6 gap_tracker backward-compat tests

`tests/test_gap_tracker_insurance_sync.py` — when `InsuranceExtractorAgent` writes a Policy into `insurance_yaml`, the `sync_gap_marker` helper:

- Sets the corresponding `identity.<type>_insurance` flat slot to a 1-line summary IF the slot was empty
- Does NOT overwrite an existing non-empty slot (manual intake wins)
- Mapping table: `health_shaban|health_shlishi → health_insurance`, `life → life_insurance`, etc.

### 10.7 UI tests

Per project convention (manual UI smokes skipped, no frontend test framework yet), UI testing is **deferred** — see SDD Open quality gap #1 (no frontend test framework). Verification is type-check + lint + manual smoke at merge time:

```
cd ui ; npm run lint ; npm run typecheck
```

### 10.8 Live LLM e2e

`tests/test_insurance_review_e2e.py` — gated on `llm_eval` marker (skipped by default per `pytest -m "not llm_eval"`). One full cascade against a 3-policy fixture (one health, one life, one homeowner) with the real `claude_code` backend. Asserts:

- Cascade completes without exception
- `InsuranceReview` structured output is valid (Pydantic-parseable)
- At least one citation per analyst report
- gap_analyst flags at least one missing-coverage finding (the fixture is intentionally missing disability coverage so the gap is detectable)

### 10.9 Tests against the codex-tandem kit

Per binding preference (use codex-tandem for risky work: money math, parsers, decision flows): the orchestrator (`insurance_review_flow`), the extractor's currency/FX handling, and the supersession logic are dispatched through the kit as a reviewer pass before merge.

---

## 11. Migration sequence

The wave lands as a series of commits on a single branch `wave-ins1-insurance` (or similar). Sequence:

1. **Schema + types** (1 commit): migration `0030_user_context_insurance_yaml`, `argosy/agents/insurance_types.py` (Policy + InsuranceContext + all per-agent output models). `file_catalog._ALLOWED_KINDS` adds `"insurance_policy"`; `file_catalog._ALLOWED_SOURCES` adds `"insurance_policy_upload"`. Factor `decrypt_if_encrypted_pdf(contents: bytes, user_id: str, original_name: str) -> bytes` helper out of `turn_attachments.save_attachment` so the new upload endpoint reuses it (preserves the existing HTTP 422 `AttachmentEncryptedError` filename-in-message behavior).
2. **KB skeleton** (1-2 commits): `domain_knowledge/insurance/**/*.md` — all 10 files with verified frontmatter and at least the "How it works" + "Why it matters" sections populated. Carrier files include at least one tariff band per coverage type with a vintage date. Test: `test_insurance_kb.py`.
3. **Extractor agent + upload endpoint** (1-2 commits): `argosy/agents/insurance_extractor.py` + `argosy/services/insurance_ingest.py::extract_uploaded_policy` + `sync_gap_marker` + new `POST /api/insurance/policies/upload` endpoint in `argosy/api/routes/insurance.py` (calls `catalog_upload` with `kind="insurance_policy"`, `source="insurance_policy_upload"`; schedules `extract_uploaded_policy` via FastAPI `BackgroundTasks`). Tests.
4. **Per-policy agents** (2 commits): coverage_check + value_analyst, each with their tests.
5. **Gap analyst + synthesizer** (1-2 commits): `gap_analyst.py` + `insurance_synthesizer.py`. Tests.
6. **Orchestrator flow** (1 commit): `argosy/orchestrator/flows/insurance_review/flow.py` + queries + integration test. Codex-tandem review pass on the orchestrator.
7. **Annual loop wiring** (1 commit): non-fatal opportunistic block in `annual.py`.
8. **API endpoints** (1-2 commits): 6 new routes in `argosy/api/routes/insurance.py`.
9. **UI** (3-5 commits): NavBar tab, `/insurance` route + tabs, Inventory cards, Reviews list + drilldown, manual edit. `npm run lint` + `npm run typecheck` clean before each commit.
10. **Live-LLM e2e** (1 commit): `test_insurance_review_e2e.py` under `llm_eval` marker, run once locally with `claude_code` backend.
11. **SDD update + handover** (1 commit): document the wave in the SDD with an "Wave INS1 landed" section + close the "proposed next wave" paragraph.

Estimated total: ~15-20 commits, ~3-4 weeks wall-clock.

---

## 12. Out-of-scope / future waves

- **Pension fund review wave** — analyzing kupat_pensia / kupat_gemel / keren_hishtalmut for fees, returns, default-fund gap, consolidation. Cite-but-don't-analyze in INS1.
- **Claim-filing workflow** — structured claim management with carrier integration.
- **Carrier quote requests** — actually requesting new quotes from alternative carriers identified by `value_analyst.alternative_carriers`.
- **Multi-jurisdiction insurance** — currently scoped to Israeli policies. If Noga gets a US-side policy in the future, a US-specific KB subtree `domain_knowledge/insurance/us/` would be added; the agent schemas are jurisdiction-agnostic.
- **Group insurance through employer** — NVIDIA-Israel-bundled group life + disability are real for Ariel; the extractor handles them as standard policies but a future wave might add an "employer-bundled" badge and skip premium analysis (employer pays).

---

## 13. Open questions (deferred to implementation)

These don't block the spec but will need an answer during the build:

1. **Exact `policy_id` hash inputs** — current sketch is `sha8(carrier + policy_number + holders_joined)`, but a renewed policy keeps the same number (good — supersession works) while a policy reassignment (e.g. spouse added to a homeowner policy mid-term) would change holders and produce a new ID. Probably acceptable; verify against real Ariel policy data.
2. **FX-convert at extraction vs at display time** — current sketch FX-converts USD coverage amounts to NIS at extraction time. Alternative: store both, convert at display via the existing `fx-mode` UI toggle. Cleaner for the data model but heavier UI.
3. **"Open source PDF" endpoint** — reuse the existing expense `/transactions/{id}/open-source-file` (shells `os.startfile`) or add a generic `/user-files/{id}/open`. The latter is more reusable; the former requires no new code.
4. **Manual review UI mid-cascade** — should the manual trigger button show a streaming view of the cascade (like `useDecisionStream` on the home page) or just a "running…" spinner that resolves to the final report? Streaming gives better feel; spinner is simpler.
5. **Initial KB content** — who writes the 10 KB files? Either Claude drafts them (one commit per category, leaning on web/general knowledge with `last_verified: 1900-01-01` to mark as needs-verify), or the user supplies source material. The default plan above assumes Claude drafts, marked-as-stale; user verifies in passes.

---

## 14. Acceptance criteria

The wave is done when:

- [ ] Migration `0030_user_context_insurance_yaml` lands; `insurance_yaml` column exists with `DEFAULT ''`.
- [ ] `file_catalog._ALLOWED_KINDS` includes `"insurance_policy"`; `_ALLOWED_SOURCES` includes `"insurance_policy_upload"`.
- [ ] `POST /api/insurance/policies/upload` accepts a multipart PDF, calls `catalog_upload` with the new kind/source, schedules the extractor; encryption gate reused via the new `decrypt_if_encrypted_pdf` helper.
- [ ] All 10 KB files exist with valid frontmatter and at least 80% of sections populated.
- [ ] All 5 agent classes exist, subclass `BaseAgent`, default to Opus, use the `(system, user, sources)` build_prompt contract.
- [ ] Per-agent unit tests pass (~5 files, ~30-50 tests total).
- [ ] Flow integration test passes.
- [ ] KB structure test passes.
- [ ] Annual loop runs the cascade opportunistically; failure is logged but doesn't break the rest of the loop.
- [ ] `/insurance` UI route exists with Inventory + Reviews tabs; both render against real data.
- [ ] `npm run lint` + `npm run typecheck` clean.
- [ ] Live LLM e2e under `llm_eval` marker passes once locally with a 3-policy fixture.
- [ ] SDD updated with a "Wave INS1 landed" section; handover paragraph refreshed.
- [ ] Full test suite (`pytest -m "not llm_eval"`) passes pre-merge.
- [ ] Codex-tandem reviewer pass on the orchestrator + extractor's currency/FX handling is clean.

---
