"""Compiled regex patterns for history_leak and jargon_leak checks.

Each pattern is a `re.Pattern` with appropriate flags. Patterns are
designed against the false-positive corpus in
`tests/test_plan_output_gate.py::FINANCIAL_ADVICE_CORPUS` — adding a
pattern requires running that test to confirm legit financial-advice
prose still passes.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Check 1 — history_leak: revision-narration / prior-version references
# ---------------------------------------------------------------------------

HISTORY_LEAK_PATTERNS: list[re.Pattern[str]] = [
    # "prior draft / plan / synth / cycle / version / revision / round / item / target / theme / action"
    re.compile(
        r"\bprior\s+(draft|plan|synth|cycle|version|revision|round|item|target|theme|action)\b",
        re.IGNORECASE,
    ),
    # "previous draft / version / synth / cycle / revision / iteration"
    re.compile(
        r"\bprevious\s+(draft|plan|synth|cycle|version|revision|round|iteration)\b",
        re.IGNORECASE,
    ),
    # "earlier draft / plan / synth / version / revision"
    re.compile(
        r"\bearlier\s+(draft|plan|synth|version|revision)\b",
        re.IGNORECASE,
    ),
    # "former framing / approach / stance / position / recommendation"
    re.compile(
        r"\bformer\s+(framing|approach|stance|position|recommendation)\b",
        re.IGNORECASE,
    ),
    # explicit revision verbs (bare — high signal)
    re.compile(
        r"\b(retracted|retracts|retracting|supersedes|superseded|deprecated|rescinded|reversed)\b",
        re.IGNORECASE,
    ),
    # "updated/revised/amended from"
    re.compile(
        r"\b(updated|revised|amended)\s+from\b",
        re.IGNORECASE,
    ),
    # "was/has been updated/revised/amended/superseded/retracted"
    re.compile(
        r"\b(was|were|has\s+been|have\s+been)\s+"
        r"(updated|revised|amended|deprecated|superseded|retracted)\b",
        re.IGNORECASE,
    ),
    # "changed from X to Y" (revision narration; "changed from" alone
    # is the smoking gun)
    re.compile(r"\bchanged\s+from\b", re.IGNORECASE),
    # "no longer applies / relevant / recommended / valid / true"
    re.compile(
        r"\bno\s+longer\s+(applies|relevant|recommended|valid|true)\b",
        re.IGNORECASE,
    ),
    # "instead of the previous / prior / earlier / former / original"
    re.compile(
        r"\binstead\s+of\s+the\s+(previous|prior|earlier|former|original)\b",
        re.IGNORECASE,
    ),
    # "originally proposed / recommended / targeted / planned / stated / claimed"
    re.compile(
        r"\boriginally\s+(proposed|recommended|targeted|planned|stated|claimed)\b",
        re.IGNORECASE,
    ),
    # synth / draft / wave numbering (these are unambiguous)
    re.compile(r"\bsynth\s+#?\d+\b", re.IGNORECASE),
    re.compile(r"\bdraft\s+#?\d+\b", re.IGNORECASE),
    re.compile(r"\bwave\s+\d+\b", re.IGNORECASE),
    re.compile(r"\bpiece\s+[A-Za-z]\b", re.IGNORECASE),  # "Piece A" / "piece b"
    # version markers in cycle context: v2.4, v2.4.3 (require at
    # least one dot-segment so plain "v2" doesn't match; still permits
    # v2.0 / v2.4 / v2.4.3 — all are cycle markers in plan prose)
    re.compile(r"\bv\d+\.\d+(\.\d+)*\b"),
    # "lineage to prior / previous / earlier / draft"
    re.compile(
        r"\blineage\s+to\s+(prior|previous|earlier|draft)\b",
        re.IGNORECASE,
    ),
    # "preserved from prior / previous / earlier / the"
    re.compile(
        r"\bpreserved\s+from\s+(prior|previous|earlier|the)\b",
        re.IGNORECASE,
    ),
    # "prior-round delta / change / edit / amendment"
    re.compile(
        r"\bprior[-\s]round\s+(delta|change|edit|amendment)\b",
        re.IGNORECASE,
    ),
    # "accepted prior-round"
    re.compile(r"\baccepted\s+prior[-\s]round\b", re.IGNORECASE),
    # parenthetical metadata "(stated 2026-06-02; revisit 2026-07-01)"
    re.compile(
        r"\(stated\s+\d{4}-\d{2}-\d{2}\s*;\s*revisit\s+\d{4}-\d{2}-\d{2}\)"
    ),
    # render-layer markers
    re.compile(
        r"^\s*##\s*Deltas\s+vs\.?\s+prior",
        re.MULTILINE | re.IGNORECASE,
    ),
    re.compile(
        r"^\s*#.*—\s*status:\s*"
        r"(no_change|minor_revision|major_revision|new|in_review|draft)",
        re.MULTILINE | re.IGNORECASE,
    ),
]


# ---------------------------------------------------------------------------
# Check 2 — jargon_leak: internal agent / class / status names
# ---------------------------------------------------------------------------

JARGON_LEAK_PATTERNS: list[re.Pattern[str]] = [
    # Agent class names (unambiguous — case-sensitive Python CamelCase)
    re.compile(
        r"\b("
        r"TaxAnalyst|FXAnalyst|MacroAnalyst|NewsAnalyst|"
        r"TechnicalAnalyst|SentimentAnalyst|FundamentalsAnalyst|"
        r"ConcentrationAnalyst|HouseholdBudgetAnalyst|"
        r"PlanCritique|PlanCritiqueAgent|"
        r"PlanNarrator|PlanNarratorAgent|"
        r"PlanNarrative|PlanNarrativeAgent|"
        r"PlanSynthesizer|"
        r"PlanCoverageAnalyst|WithdrawalSequencerAgent|"
        r"PlanLanguageRewriter"
        r")\b"
    ),
    # System-internal terminology (case-insensitive — these are jargon
    # regardless of capitalization)
    re.compile(
        r"\b(substrate-gated|substrate\s+repair|self-flagged)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(distillate|topic\s+owner|publication\s+gate|gate\s+check)\b",
        re.IGNORECASE,
    ),
    # bare jargon words: "substrate", "fleet" (analyst fleet),
    # "orchestrator" (workflow orchestrator), "synthesizer" (the synth agent)
    re.compile(r"\bsubstrate\b", re.IGNORECASE),
    re.compile(r"\b(fleet|orchestrator|synthesizer)\b", re.IGNORECASE),
    # RED/YELLOW/GREEN grading (only when used as system labels —
    # "RED on", "GREEN on", "PlanCritique RED" etc.). Bare "red flag"
    # in plain text is allowed.
    re.compile(r"\b(RED|YELLOW|GREEN)\s+(on|flag|status|verdict)\b"),
    re.compile(r"\bPlanCritique\s+(RED|YELLOW|GREEN)\b"),
    # Raw analyst-report frames
    re.compile(r"={3,}\s+\w+(Agent)?\s+(\(FAILED\))?\s+={3,}"),
]
