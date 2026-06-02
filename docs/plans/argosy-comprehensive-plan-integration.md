# Argosy comprehensive-plan integration — plan v3.1

## §0 — Progression checklist (handover)

State markers: `[ ]` not started · `[wip]` in progress · `[done]` shipped

- `[done]` **Phase 0** — Failing CI gate (`argosy/quality/plan_output_gate.py`, 5 checks, v20 fixture). 103 tests; v20 produces 182 violations (75 history + 107 jargon). Codex review: COMMIT AS-IS after 2 rounds.
- `[wip]` **Phase 1** — Clean synth context + renderer split + audit migration. Design at `tmp_review/phase1_design/spec.md`. Target: `history_leak` check passes on re-synth.
- `[ ]` **Phase 2** — Publication gate + `PlanLanguageRewriter` + invariant validator. Design at `tmp_review/phase2_design/spec.md`. Target: `jargon_leak` check passes.
- `[ ]` **Phase 3** — `SectionEvidence` contract + validators + content gate. Design at `tmp_review/phase3_design/spec.md`. Target: `evidence_per_section` passes. **MVP ship.**
- `[ ]` **Phase 4** — Distillate schema expansion + section-binding gate. Target: `section_coverage` at 18/18.
- `[ ]` **Phase 5** — `PlanCoverageAnalyst` + `WithdrawalSequencerAgent`. Target: orphan content owned.
- `[ ]` **Phase 6** — Feature flag + override path. Target: rollout complete. **Full ship.**

**Currently shipping:** Phase 1. Last update: 2026-06-02.

---

**v3.1 patch deltas vs v3** (from codex round 3, three surgical fixes):
- §3.2 Check 4: non-numeric fact-extract support — extract must contain
  token-overlap with FactClaim.text (not just ≥8 chars).
- §5.2 rewriter validator: runs `check_jargon_leak` + `check_history_leak`
  on ALL rewritten prose fields (Section.title, Section.body_md,
  Theme.rationale_md, Theme.label, Action.description_md,
  Action.rationale_md, posture_md), not just body_md.
- §7 distillate-section binding: proves USE not just presence — every
  non-empty distillate field must produce ≥1 SectionEvidence citation
  with locator pattern `distillate.<field_name>` in the bound section.
  Map extended to all expanded fields with `_ungated_` marker for
  fields that intentionally have no binding.

Everything else from v3 stands.

---

# Argosy comprehensive-plan integration — plan v3

Final integration plan. Supersedes v1 and v2.

**v3 deltas vs v2** (from codex round 2 verdict ONE MORE ROUND, six
surgical fixes):
- §3.2 Check 1 regex tightened: word-boundary context disambiguation
  to stop false positives on "prior year", "former employer",
  "revised tax calculation", "v2.0".
- §3.2 Check 3 rewritten: structured `section_id` coverage replaces
  brittle H2 markdown parsing. v20's horizons only have
  `Targets / Themes / Actions / Deltas / Rationale` H2s, not section
  H2s — H2 parsing was non-executable.
- §6 evidence contract tightened: `FactClaim` typed + min-length;
  citations must SUPPORT facts (verbatim extract), not just resolve;
  assumptions required when source kind is `inference` or
  `agent_baseline`.
- §5.2 `PlanLanguageRewriter` invariants specified: rewritable vs
  preserved fields enumerated; post-rewrite diff check.
- §7 schema convergence: gate requires each canonical `section_id`
  to be present and bound to its distillate inputs — synth cannot
  silently omit by documenting in `assumptions`.
- §10 MVP timeline corrected from 2 weeks to 3 weeks (Phase 1's audit
  migration underpriced in v2).

Everything else from v2 stands; this doc lists the changed sections
explicitly and references v2 for unchanged ones.

---

## §1, §2 — unchanged

(See `integration_plan_v2.md` §1 "Why this plan is different" and §2
"Three defects, restated against the spec".)

---

## §3 — Phase 0: the failing-CI gate (v3 — corrected)

### 3.1 What the gate is — unchanged (see v2 §3.1)

### 3.2 Four checks — corrected regexes and parsers

**Check 1 — `history_leak`** — corrected Python regex with word-context
disambiguation so legitimate user-plan terms ("prior year", "former
employer") don't trigger:

```python
import re

# Block "prior X" only when X is plan-revision context.
_HISTORY_LEAK_PATTERNS: list[re.Pattern] = [
    # "prior draft", "prior plan", "prior synth", "prior cycle", "prior version", "prior round"
    re.compile(r"\bprior\s+(draft|plan|synth|cycle|version|revision|round|item|target|theme|action)\b", re.IGNORECASE),
    # "previous draft / version / synth", etc.
    re.compile(r"\bprevious\s+(draft|plan|synth|cycle|version|revision|round|iteration)\b", re.IGNORECASE),
    # "earlier draft / version", etc.
    re.compile(r"\bearlier\s+(draft|plan|synth|version|revision)\b", re.IGNORECASE),
    # "former framing / framing / approach" only when paired with revision verbs
    re.compile(r"\bformer\s+(framing|approach|stance|position|recommendation)\b", re.IGNORECASE),
    # explicit revision verbs
    re.compile(r"\b(retracted|retracts|retracting|supersedes|superseded|deprecated|rescinded|reversed)\b", re.IGNORECASE),
    # "updated/revised from X" or "X has been updated/revised"
    re.compile(r"\b(updated|revised|amended)\s+from\b", re.IGNORECASE),
    re.compile(r"\b(was|were|has\s+been|have\s+been)\s+(updated|revised|amended|deprecated|superseded|retracted)\b", re.IGNORECASE),
    # "changed from X to Y" (revision narration)
    re.compile(r"\bchanged\s+from\b", re.IGNORECASE),
    # "no longer X" / "instead of the X approach"
    re.compile(r"\bno\s+longer\s+(applies|relevant|recommended|valid|true)\b", re.IGNORECASE),
    re.compile(r"\binstead\s+of\s+the\s+(previous|prior|earlier|former|original)\b", re.IGNORECASE),
    # "originally proposed/recommended" (revision narration; "originally from X" is legit)
    re.compile(r"\boriginally\s+(proposed|recommended|targeted|planned|stated|claimed)\b", re.IGNORECASE),
    # version markers in narrative context — "synth #19", "draft #18", "wave 8", "piece B", "v2.4"
    re.compile(r"\bsynth\s+#?\d+\b", re.IGNORECASE),
    re.compile(r"\bdraft\s+#?\d+\b", re.IGNORECASE),
    re.compile(r"\bwave\s+\d+\b", re.IGNORECASE),
    re.compile(r"\bpiece\s+[A-Z]\b"),  # case-sensitive — "Piece A"
    re.compile(r"\bv\d+(\.\d+){1,3}\b"),  # v2.4, v2.4.3 — block "v2.0" only when used as cycle marker (precision tradeoff: this also blocks v2.0 IF written in horizon prose; acceptable since "v2.0" in financial advice prose is almost always a draft marker)
    # "lineage" / "preserved from prior" / "accepted prior-round delta"
    re.compile(r"\blineage\s+to\s+(prior|previous|earlier|draft)\b", re.IGNORECASE),
    re.compile(r"\bpreserved\s+from\s+(prior|previous|earlier|the)\b", re.IGNORECASE),
    re.compile(r"\bprior[-\s]round\s+(delta|change|edit|amendment)\b", re.IGNORECASE),
    re.compile(r"\baccepted\s+prior[-\s]round\b", re.IGNORECASE),
    # parenthetical metadata: "(stated 2026-06-02; revisit 2026-07-01)"
    re.compile(r"\(stated\s+\d{4}-\d{2}-\d{2}\s*;\s*revisit\s+\d{4}-\d{2}-\d{2}\)"),
    # render-layer markers
    re.compile(r"^\s*##\s*Deltas\s+vs\.?\s+prior", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*#.*—\s*status:\s*(no_change|minor_revision|major_revision|new|in_review|draft)", re.MULTILINE | re.IGNORECASE),
]

def check_history_leak(text: str) -> list[GateViolation]:
    return [
        GateViolation(check="history_leak", pattern=p.pattern, match=m.group(), pos=m.start())
        for p in _HISTORY_LEAK_PATTERNS
        for m in p.finditer(text)
    ]
```

Tolerance: **zero violations** in `horizon_*_md` for `current` role.

False-positive guard: a corpus of 20 legitimate financial-advice
sentences (containing "prior year", "former employer", "revised tax
calculation", "originally from") MUST produce zero matches. Asserted
in `test_history_leak_no_false_positives`.

**Check 2 — `jargon_leak`** — unchanged from v2 (the agent-name and
RED/YELLOW/GREEN patterns are unambiguous). One addition per codex's
note that the rewriter must specify what's allowed:

```python
_JARGON_LEAK_PATTERNS: list[re.Pattern] = [
    # Agent / class names — these are unambiguous (no false positives expected)
    re.compile(r"\b(TaxAnalyst|FXAnalyst|MacroAnalyst|NewsAnalyst|TechnicalAnalyst|SentimentAnalyst|FundamentalsAnalyst|ConcentrationAnalyst|HouseholdBudgetAnalyst|PlanCritique(Agent)?|PlanNarrat(or|ive)(Agent)?|PlanSynthesizer|PlanCoverageAnalyst|WithdrawalSequencerAgent|PlanLanguageRewriter)\b"),
    # System-internal terminology
    re.compile(r"\b(substrate|substrate-gated|self-flagged|fleet|orchestrator|distillate|topic\s+owner|synthesizer|gate\s+check|publication\s+gate)\b", re.IGNORECASE),
    # RED/YELLOW/GREEN grading language
    re.compile(r"\b(RED|YELLOW|GREEN)\s+(on|flag|status|verdict)\b"),
    re.compile(r"\bPlanCritique\s+(RED|YELLOW|GREEN)\b"),
    # Raw analyst-report frames
    re.compile(r"={3,}\s+\w+(Agent)?\s+(\(FAILED\))?\s+={3,}"),
]
```

Tolerance: **zero violations**.

**Check 3 — `section_coverage`** — REWRITTEN. v2's H2-parsing approach
was non-executable against v20's actual horizon shape (which has only
`Targets / Themes / Actions / Deltas / Rationale` H2s). v3 reads
structured fields directly from `PlanSynthesisOutput`.

Step 1: Define canonical section IDs (matches spec §1 numbering):

```python
CANONICAL_SECTION_IDS: dict[str, str] = {
    "cover_assumptions":          "Cover, Scope, Assumptions Register",
    "client_goals":               "Client Circumstances + Goals",
    "net_worth":                  "Net Worth Statement",
    "cashflow":                   "Cash Flow + Savings Rate",
    "capital_sufficiency":        "Capital Sufficiency / Goal Funding",
    "ips":                        "Investment Policy Statement",
    "concentration":              "Concentration & Single-Stock Risk",
    "withdrawal":                 "Retirement Income / Withdrawal Strategy",
    "monte_carlo":                "Monte Carlo / Sensitivity",
    "tax_plan":                   "Tax Plan",
    "insurance":                  "Insurance + Risk Management",
    "healthcare":                 "Healthcare Cost Plan",
    "estate":                     "Estate + Document Inventory",
    "cross_border":               "Cross-Border / Multi-Jurisdictional",
    "equity_comp":                "Equity Compensation Per-Grant",
    "fi_bridge":                  "FI Bridge (pre-statutory-age)",
    "life_events":                "Life-Event Phasing",
    "action_items":               "Action Items + Owner + Due Date",
}
```

Step 2: `PlanSynthesisOutput.sections: list[Section]` where each
`Section` carries:

```python
class Section(BaseModel):
    section_id: str                # MUST be one of CANONICAL_SECTION_IDS keys
    horizon: Literal["short", "medium", "long"]
    title: str                     # may differ from canonical title
    body_md: str                   # the prose
    evidence: SectionEvidence      # see Check 4
```

Step 3: Gate check 3 reads `PlanSynthesisOutput.sections[].section_id`
across all three horizons and verifies the union covers ≥ THRESHOLD
of the canonical 18:

```python
def check_section_coverage(synth_output: PlanSynthesisOutput, threshold: int) -> GateVerdict:
    section_ids_present = {s.section_id for s in synth_output.sections}
    unknown_ids = section_ids_present - CANONICAL_SECTION_IDS.keys()
    missing = CANONICAL_SECTION_IDS.keys() - section_ids_present
    if len(section_ids_present) < threshold:
        return GateVerdict.fail(f"coverage {len(section_ids_present)}/{len(CANONICAL_SECTION_IDS)} below threshold {threshold}", missing=sorted(missing))
    if unknown_ids:
        return GateVerdict.fail(f"unknown section_ids: {sorted(unknown_ids)}")
    return GateVerdict.pass_()
```

Thresholds: **MVP launch = 12/18** (the 8 already covered + 4 stretch).
**Full ship = 18/18.**

A section may exist in only one horizon (e.g. IPS is long-horizon only)
or in multiple (concentration shows in all three with different
content). Counted as present if `section_id` appears in any.

The renderer (`_horizon_md_user`) emits canonical H2 titles from
`CANONICAL_SECTION_IDS[section_id]` so the rendered MD has stable,
human-readable section headings.

**Check 4 — `evidence_per_section`** — STRENGTHENED per codex round 2.
Original v2 contract:

```python
class SectionEvidence:
    section_id: str
    facts: list[FactClaim]
    source_span: list[Citation]
    assumptions: list[Assumption]
    missing_data: list[str]
```

was insufficient — model could emit `facts=["NVDA"]` (single token,
no value), `source_span=[citation_that_resolves_but_doesnt_support]`,
empty `assumptions`. v3 contract:

```python
class FactClaim(BaseModel):
    text: str                    # NL claim — min 12 chars, max 300
    kind: Literal["numeric", "categorical", "policy", "qualitative"]
    value: Decimal | str | None  # the bound value (None ok for qualitative)
    unit: str | None             # NIS, USD, %, share, etc. (numeric kind)
    horizon: Literal["short", "medium", "long"] | None

    @validator("text")
    def min_text_length(cls, v):
        if len(v.strip()) < 12:
            raise ValueError("FactClaim.text must be ≥12 chars (no single-token facts)")
        return v

class Citation(BaseModel):
    source_kind: Literal["plan_doc", "portfolio_snapshot", "analyst_report",
                         "assumption_register", "inference", "agent_baseline"]
    source_locator: str          # e.g. "plan_doc:H2:Tax Optimization:L405"
    extract: str | None          # verbatim excerpt — required for non-inference kinds
    supports_fact_index: int     # index into Section.evidence.facts that this citation supports

class Assumption(BaseModel):
    text: str
    default_value: Decimal | str
    rationale: str
    can_be_overridden: bool = True

class SectionEvidence(BaseModel):
    section_id: str
    facts: list[FactClaim]
    source_span: list[Citation]
    assumptions: list[Assumption]
    missing_data: list[str]

    @root_validator
    def evidence_or_missing(cls, vals):
        facts = vals.get("facts", [])
        missing = vals.get("missing_data", [])
        if not facts and not missing:
            raise ValueError("Section must have either facts or missing_data — silent empty is forbidden")
        return vals

    @root_validator
    def every_fact_cited(cls, vals):
        facts = vals.get("facts", [])
        cites = vals.get("source_span", [])
        fact_indices_with_cite = {c.supports_fact_index for c in cites}
        for i, _ in enumerate(facts):
            if i not in fact_indices_with_cite:
                raise ValueError(f"FactClaim[{i}] has no Citation in source_span")
        return vals

    @root_validator
    def inference_requires_assumption(cls, vals):
        cites = vals.get("source_span", [])
        assumptions = vals.get("assumptions", [])
        has_inference_or_baseline = any(c.source_kind in {"inference", "agent_baseline"} for c in cites)
        if has_inference_or_baseline and not assumptions:
            raise ValueError("Section uses inference or agent_baseline citations but declares no assumptions")
        return vals

    @root_validator
    def extract_required_for_concrete_sources(cls, vals):
        for c in vals.get("source_span", []):
            if c.source_kind in {"plan_doc", "portfolio_snapshot", "analyst_report"}:
                if not c.extract or len(c.extract) < 8:
                    raise ValueError(f"Citation to {c.source_kind} must include verbatim extract ≥8 chars")
        return vals
```

Validators ensure:
1. Section has facts OR missing_data (never silently empty).
2. Every FactClaim has at least one Citation.
3. Citations with `source_kind` in {inference, agent_baseline}
   REQUIRE matching assumptions.
4. Citations to concrete sources (plan_doc, portfolio_snapshot,
   analyst_report) REQUIRE a verbatim `extract` ≥8 chars AND the
   extract must SUPPORT the fact: for numeric facts, the value must
   appear as substring (already handled by content gate); for
   non-numeric facts (categorical/policy/qualitative), the extract
   must share ≥3 lowercased content tokens with `FactClaim.text`
   (stopwords removed). This stops a vaguely-related extract from
   "supporting" a confabulated categorical claim.
5. FactClaim text ≥12 chars (no `["NVDA"]` single-token fluency).

Plus a content-level gate beyond Pydantic validators:

```python
_STOPWORDS = {"the", "a", "an", "is", "are", "was", "were", "be", "to", "of", "in", "on", "at", "for", "and", "or", "but", "with", "by", "as", "this", "that", "it", "from", "has", "have", "had"}

def _content_tokens(s: str) -> set[str]:
    return {w for w in s.lower().split() if w not in _STOPWORDS and len(w) > 2}

def check_evidence_supports_facts(section: Section) -> list[GateViolation]:
    violations = []
    for cite in section.evidence.source_span:
        if cite.source_kind in {"plan_doc", "portfolio_snapshot", "analyst_report"}:
            fact = section.evidence.facts[cite.supports_fact_index]
            # numeric facts: the extract must contain the value as substring
            if fact.kind == "numeric" and fact.value is not None:
                if str(fact.value) not in cite.extract:
                    violations.append(GateViolation(
                        check="evidence_per_section",
                        detail=f"Section {section.section_id}: FactClaim {cite.supports_fact_index} value {fact.value} not present in cite extract",
                    ))
            # categorical/policy/qualitative facts: extract must share ≥3 content tokens with fact.text
            elif fact.kind in {"categorical", "policy", "qualitative"}:
                overlap = _content_tokens(fact.text) & _content_tokens(cite.extract)
                if len(overlap) < 3:
                    violations.append(GateViolation(
                        check="evidence_per_section",
                        detail=f"Section {section.section_id}: FactClaim {cite.supports_fact_index} ({fact.kind}) extract shares only {len(overlap)} content tokens with fact (need ≥3)",
                    ))
    return violations
```

Tolerance: **zero validator failures and zero content-gate violations.**

### 3.3, 3.4 — unchanged from v2

---

## §4 — Phase 1: clean synthesizer context (v3 — timeline corrected)

§4.1, §4.2, §4.3 — unchanged from v2.

**§4.4 cost — corrected.** v2 said 2 days. Reality with DB migration:
~120 LoC + 1 DB migration (`0026_add_horizon_md_audit_columns.sql`) +
backfill script + 3 tests. **3-4 days** including migration review.

---

## §5 — Phase 2: plain-language rewrite + publication gate (v3 — rewriter invariants specified)

§5.1 — unchanged from v2.

### 5.2 PlanLanguageRewriter — specified (v3)

v2 was hand-wavy. v3 explicit contract:

**Input.** `PlanSynthesisOutput` (the structured output from
PlanSynthesizer).

**Output.** `PlanSynthesisOutput` with prose fields rewritten;
structured fields preserved bit-for-bit.

**Rewritable fields** (LLM allowed to rephrase the text):
- `Section.body_md`
- `Section.title` (allowed to clarify but NOT to drop canonical title)
- `Theme.rationale_md`
- `Theme.label`
- `Action.description_md`
- `Action.rationale_md`
- `posture_md`

**Preserved fields** (LLM MUST NOT modify; gate verifies bit-equality):
- `Section.section_id`
- `Section.horizon`
- `Section.evidence.*` (entire SectionEvidence subtree)
- `Theme.item_id`
- `Theme.kind` (`lean_into` / `lean_away_from` / `monitor`)
- `Action.item_id`
- `Action.target_date`
- `Action.condition_expr` (conditional triggers stay in the structured
  field but are NOT mentioned verbatim in `description_md`)
- All numeric `Target.value` and `Target.unit`
- Lengths: `len(sections)`, `len(themes per horizon)`,
  `len(actions per horizon)`, `len(facts per section)` must be
  preserved.

**Validation step** (`plan_language_rewriter_validator.py`):

```python
def validate_rewriter_invariants(before: PlanSynthesisOutput, after: PlanSynthesisOutput) -> list[GateViolation]:
    violations = []
    # Section count, IDs, horizons preserved
    if [s.section_id for s in before.sections] != [s.section_id for s in after.sections]:
        violations.append(...)
    # Per section: evidence preserved bit-for-bit (Pydantic dict equality)
    for b, a in zip(before.sections, after.sections):
        if b.evidence.model_dump() != a.evidence.model_dump():
            violations.append(...)
    # Numeric Target.value preserved
    if [(t.item_id, t.value, t.unit) for t in before.targets] != [(t.item_id, t.value, t.unit) for t in after.targets]:
        violations.append(...)
    # Theme / Action item_ids preserved
    ...
    # Rewriter MAY change prose fields but MUST NOT introduce gate-banned strings
    # in ANY of the rewritable prose fields enumerated in §5.2.
    REWRITABLE_PROSE_PATHS = [
        # (object_iter_lambda, field_name)
        (lambda o: o.sections, "title"),
        (lambda o: o.sections, "body_md"),
        (lambda o: o.themes, "rationale_md"),
        (lambda o: o.themes, "label"),
        (lambda o: o.actions, "description_md"),
        (lambda o: o.actions, "rationale_md"),
    ]
    for iter_fn, field in REWRITABLE_PROSE_PATHS:
        for item in iter_fn(after):
            value = getattr(item, field, None) or ""
            violations.extend(check_history_leak(value))
            violations.extend(check_jargon_leak(value))
    # posture_md is a top-level field
    violations.extend(check_history_leak(after.posture_md or ""))
    violations.extend(check_jargon_leak(after.posture_md or ""))
    return violations
```

The validator runs **automatically** between rewriter output and
persist. Any violation aborts the synth cycle.

**Rewriter system prompt rubric** (rendered into the prompt):

```
You translate financial-planning prose from internal-system phrasing
to household-readable English. Constraints:

1. Preserve structure exactly: do not add, remove, reorder, merge, or
   split sections / themes / actions / facts / citations / assumptions
   / numeric targets.
2. Translate jargon:
   - "TaxAnalyst" → "the tax analysis"
   - "ConcentrationAnalyst" → "the concentration analysis"
   - "PlanCritique" → "the plan review" / "internal review"
   - "RED / YELLOW / GREEN" → "critical / elevated / validated"
   - "substrate" → "underlying inputs" or "supporting data"
   - "fleet" → "the analysis suite"
   - agent-output frames "=== X ===" → drop entirely (the prose body
     stands alone)
3. Conditional triggers in prose:
   - "if(lot_grant_date <= 2024-06-02 AND ...)" →
     "for grants made before June 2024"
   - "if(USD/NIS spot < 2.95)" → "while the dollar is weak vs. shekel"
   - Keep the structured `condition_expr` field unchanged.
4. Revision narration: never write "prior", "previous", "earlier",
   "former", "revised from", "updated from", "lineage", "synth #N",
   "wave N", "v2.X". Write the current state directly.
5. Audience: a financially literate household member who has not
   read the internal Argosy documentation.

Do NOT modify:
- Numeric values, units, percentages, share counts, dates.
- section_id, item_id, horizon, kind, target_date, condition_expr.
- The `SectionEvidence.*` subtree (facts, citations, assumptions,
  missing_data) — preserve bit-for-bit.

Output the same JSON schema as input.
```

Cost: ~250 LoC rewriter agent + ~150 LoC validator + prompt + 4
tests. **4-5 days** (v2 said 3-4; codex's "hand-wavy" critique
required this expansion).

### 5.3, 5.4 — unchanged from v2

---

## §6 — Phase 3: evidence contract (v3 — see §3.2 Check 4 above)

The structural contract was already defined in §3.2 Check 4 above.
Phase 3 implementation:

1. Add `SectionEvidence` Pydantic model to
   `argosy/agents/plan_synthesizer_types.py` (~150 LoC schema).
2. Update synth prompt to require evidence per section.
3. Wire validators (Pydantic root_validators + content gate).
4. Tests for each validator + content gate.

Cost: ~300 LoC + 6 tests. **4-5 days** (v2 said 3-4; the validator
cardinality lifted estimate).

---

## §7 — Phase 4: distillate schema expansion (v3 — gate-bound convergence)

Codex's round-2 concern: "synth can omit non-empty fields by
documenting omission in `assumptions`; §6 may still pass. Synth can
ignore new fields unless the gate requires each canonical `section_id`
to be present and bound to its distillate inputs."

v3 fix — distillate-section binding:

```python
# argosy/quality/distillate_section_binding.py
DISTILLATE_FIELD_TO_SECTION_ID: dict[str, str | None] = {
    # Bound (must appear as section AND be cited)
    "plan_assumptions":         "cover_assumptions",
    "goals":                    "client_goals",
    "cashflow_phases":          "cashflow",
    "capital_sufficiency":      "capital_sufficiency",
    "ips":                      "ips",
    "withdrawal_schedule":      "withdrawal",
    "monte_carlo_grid":         "monte_carlo",
    "tax_schedule":             "tax_plan",
    "insurance_matrix":         "insurance",
    "healthcare_cost_plan":     "healthcare",
    "estate_documents":         "estate",
    "cross_border":             "cross_border",
    "equity_comp_grants":       "equity_comp",
    "fi_bridge":                "fi_bridge",
    "life_events":              "life_events",
    "priority_matrix":          "action_items",
    "real_estate_plan":         "net_worth",
    "fx_strategy":              "cashflow",
    "etf_reference":            "ips",            # ETF table is part of the IPS
    "securities_lending":       "ips",            # SL is an IPS execution detail
    "charitable_giving":        "tax_plan",       # charitable is a tax-plan lever
    # Explicitly ungated — meta fields, not content
    "unmapped_sections":        None,             # meta — surfaced as separate signal
    "stress_tolerance":         None,             # used by IPS section but not directly cited
    "risk_priorities":          None,             # used by IPS section but not directly cited
    "decision_rules":           None,             # synth-wide, not per-section
    "constraints":              None,             # synth-wide, not per-section
    "principles":               None,             # synth-wide, not per-section
    "targets":                  None,             # decomposed across sections
}

def check_distillate_section_binding(
    distillate: PlanDistillate,
    synth_output: PlanSynthesisOutput,
) -> list[GateViolation]:
    """For every non-empty distillate field:
       (a) the bound section_id must appear in synth output, AND
       (b) that section must carry ≥1 SectionEvidence citation with a
           source_locator matching `distillate.<field_name>` —
           proving the field was USED, not merely matched by name.
    """
    violations = []
    sections_by_id = {s.section_id: s for s in synth_output.sections}
    for field_name, section_id in DISTILLATE_FIELD_TO_SECTION_ID.items():
        if section_id is None:
            continue  # ungated meta field
        field_value = getattr(distillate, field_name)
        if not field_value:
            continue
        # (a) section presence
        if section_id not in sections_by_id:
            violations.append(GateViolation(
                check="distillate_section_binding",
                detail=f"distillate.{field_name} non-empty but section_id '{section_id}' absent",
            ))
            continue
        # (b) section USE — at least one citation in the bound section
        # must have source_locator starting with `distillate.<field_name>`
        section = sections_by_id[section_id]
        expected_locator_prefix = f"distillate.{field_name}"
        has_citation = any(
            c.source_locator.startswith(expected_locator_prefix)
            for c in section.evidence.source_span
        )
        if not has_citation:
            violations.append(GateViolation(
                check="distillate_section_binding",
                detail=f"distillate.{field_name} non-empty and section '{section_id}' present, but no citation with source_locator '{expected_locator_prefix}*' — field appears unused",
            ))
    return violations
```

This adds a fifth gate check (`distillate_section_binding`) that runs
when both distillate and synth output are available. Forces the synth
to **use** ingested content (not just emit a section with the matching
name).

Citation `source_locator` convention: when a fact derives from a
distillate field, the citation MUST format its locator as
`distillate.<field_name>[<row_id_or_index>]` so the gate can verify
USE, not merely PRESENCE. The synth prompt is updated to require this
locator format for distillate-sourced facts.

§7.1, §7.2, §7.3 — unchanged otherwise from v2.

§7.4 cost — unchanged (~1,800 LoC, 4-5 weeks).

---

## §8, §9 — unchanged

(See `integration_plan_v2.md` §8 PlanCoverageAnalyst +
WithdrawalSequencerAgent, §9 feature flag rollout.)

---

## §10 — Phasing — corrected timing (v3)

Codex's round-2 concern: "2-week MVP fragile — Phases 0-3 include
migration, renderer split, schema, prompt rewrite, rewriter, gate,
publication behavior, fixtures, tests."

Corrected:

| Phase | Deliverable | Wall time (v2) | Wall time (v3) |
|---|---|---|---|
| 0 | Failing CI gate + fixture | 2-3 days | 3-4 days (regex disambiguation + section_id model) |
| 1 | Pass history-leak (clean context + renderer split + audit migration) | 2 days | 3-4 days (migration underpriced) |
| 2 | Pass jargon-leak (publication gate + rewriter + invariant validator) | 3-4 days | 4-5 days (validator was missing) |
| 3 | Pass evidence-per-section (contract + validators + content gate) | 3-4 days | 4-5 days (typed FactClaim, content support gate) |
| 4 | Pass section-coverage at 18/18 (distillate schema + binding gate + synth wiring) | 4-5 weeks | 5-6 weeks (binding gate adds work) |
| 5 | Topic owners (PlanCoverage + WithdrawalSequencer) | 2 weeks | 2 weeks (unchanged) |
| 6 | Feature flag + override path | 2-3 days | 2-3 days (unchanged) |

**Two ship points (v3 corrected):**
- **MVP / first ship: end of Phase 3 (~3 weeks).** Defects gone, evidence
  required, jargon and history banished. Section coverage stays at 8/18
  with each section carrying real evidence.
- **Full ship: end of Phase 6 (~11 weeks).** All 18 sections, PFIC ×
  estate-tax per-holder, IPS one-shot, full coverage, flag rolled out.

---

## §11, §12, §13, §14 — unchanged from v2

(See `integration_plan_v2.md` §11 backlog mapping, §12 hidden deps,
§13 open questions, §14 risks. Codex round 2 noted §13 questions are
RESOLVED, §12 deps are RESOLVED.)

---

## §15 — Success criteria (v3 additions)

In addition to v2's tests, add:

| Phase | Test name | Asserts |
|---|---|---|
| 0 | `test_history_leak_no_false_positives` | 20 legitimate financial-advice sentences from a corpus produce zero matches |
| 0 | `test_jargon_leak_no_false_positives` | financial-advice sentences containing "fleet" in nautical sense, "RED in the chart" in market context, etc. produce zero matches |
| 1 | `test_audit_migration_idempotent` | DB migration 0026 can be applied to a populated DB without data loss |
| 2 | `test_rewriter_preserves_structure` | for a fixture input, post-rewrite output has identical section_ids, item_ids, target values, evidence subtree |
| 2 | `test_rewriter_translates_jargon` | known jargon strings in input prose are absent from output prose |
| 3 | `test_section_evidence_validators` | each of the 5 Pydantic validators rejects the matching bad input |
| 3 | `test_content_gate_numeric_support` | a FactClaim with numeric value not in citation extract is rejected |
| 3 | `test_inference_requires_assumption` | a section with inference-kind citation and empty assumptions is rejected |
| 4 | `test_distillate_section_binding_missing_section` | a distillate with non-empty `real_estate_plan` and no section_id "net_worth" in synth output fails the binding gate |
| 4 | `test_distillate_section_binding_unused_field` | a distillate with non-empty `charitable_giving` AND section_id "tax_plan" present, but no citation with locator `distillate.charitable_giving*` in tax_plan section, fails the binding gate |
| 3 | `test_evidence_extract_support_categorical` | a categorical FactClaim with extract sharing <3 content tokens fails the content gate |
| 3 | `test_evidence_extract_support_numeric` | a numeric FactClaim with value 277000 not in extract fails the content gate |
| 2 | `test_rewriter_validator_checks_all_prose_fields` | banned string in `Theme.rationale_md` (not body_md) is caught by the validator |
| 2 | `test_rewriter_validator_checks_action_fields` | banned string in `Action.description_md` is caught by the validator |

---

## §16 — Day-N visibility — corrected timing

- **Day 4.** Failing CI on every PR. v20 fails 4+1 checks.
- **Day 8.** Phase 1 lands; v21 horizon MD has clean prose, no history,
  no jargon (Phase 2 ships at end of day 8-10).
- **Day 13.** Phase 2 lands; rewriter pipeline live; jargon gone.
- **Day 21 (end of Phase 3, MVP ship).** Evidence-per-section enforced.
  No fluent confabulation possible. **Quality-milestone ship.**
- **Day 77 (end of Phase 6, full ship).** 18/18 coverage, PFIC×estate,
  IPS endpoint, flag rolled out.

---

## §17 — Codex tandem log

- Round 1 (`tmp_review/codex_critique.log`): critiqued v1; verdict
  ONE MORE ROUND with 8 specific defects.
- Round 2 (`tmp_review/codex_verify_v2c.log`): verified v2 addresses
  round 1; identified 6 new defects with surgical fixes.
- Round 3 (this doc's verify): pending.

---

## §18 — Decision needed from Ariel — unchanged from v2

Three answers unblock all of Phase 0:

**Q1.** Confirm the FIVE gate checks (v3 adds `distillate_section_binding`)
as launch policy:
- `history_leak: error` (regex with word-context disambiguation)
- `jargon_leak: error`
- `section_coverage: error (threshold=12 at MVP, 18 at full ship)`
- `evidence_per_section: error` (typed contract + content gate)
- `distillate_section_binding: error` (non-empty distillate field must
  surface as section)

**Q2.** Confirm doc location:
- `docs/plans/argosy-comprehensive-plan-integration.md` (new dir)
- `docs/integration_plan.md`
- Keep in `tmp_review/` until Phase 0 lands

**Q3.** Confirm zero-defect tolerance on all gate checks vs warning-only
at launch. v3 default is zero-defect on history + jargon; threshold-based
on coverage; zero-defect on evidence + binding.

If all three confirmed, Phase 0 starts the next session.
