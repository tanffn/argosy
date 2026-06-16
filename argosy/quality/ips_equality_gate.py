"""Run-106 finding [5] — IPS instrument-map equality gate.

The IPS instrument map claims to be a 100%-summing partition of the tradeable
book, but in run-106 the named weights as RENDERED IN PROSE totalled ~106 before
an unspecified "residual absorption". That leaves the executable target weights
incoherent: the document asserts a 100% map while its own visible numbers don't
add up, and (when a canonical doc exists) the prose weights silently diverge from
the engine-authored allocation every surface is supposed to project.

This is a complementary check to ``check_ips_allocation_sum`` (which sums ONLY
the structured ``synth.medium.targets`` with unit ``pct_of_portfolio``). Here we
validate the RENDERED PROSE: (1) the prose weights must self-sum to ~100%, and
(2) where a sleeve appears in BOTH the prose and the canonical
``TargetAllocationDoc``, the two weights must agree.

Pure function, no I/O. Named compiled regexes with WHY comments. The doc is
DUCK-TYPED (getattr), never import-bound, so the gate stays decoupled from the
allocation service and degrades gracefully on an unexpected shape. Per Argosy's
fail-loud doctrine this biases toward FALSE-POSITIVE.
"""
from __future__ import annotations

import re

from argosy.quality.gate_types import GateCheck, GateViolation

# Allow ±1pp for rounding across ~8-11 sleeves — the SAME tolerance idea as
# plan_output_gate.IPS_SUM_TOLERANCE_PCT. Not a financial constant: a rounding
# band. Kept local so this module owns no shared state.
IPS_SUM_TOLERANCE_PCT = 1.0
# Per-sleeve prose-vs-canonical divergence band (percentage points). A sleeve
# whose prose weight differs from the canonical doc by more than this is a real
# contradiction, not a rounding artifact.
_SLEEVE_EQUALITY_TOLERANCE_PP = 1.0

# Locate the IPS / instrument-map SECTION so we sum only its weights, not every
# "<label> NN%" anywhere in the plan (a horizon table, a risk paragraph, …).
# We accept the common headings: "Investment Policy Statement", "IPS", or an
# explicit "instrument map". Match from the heading to the next markdown heading
# ("## " / "# ") or end-of-text. DOTALL so the body spans newlines. If no such
# section is found we scan the whole text (fail-loud: better to over-include and
# flag than to silently skip the map).
_IPS_SECTION_RE = re.compile(
    r"(?:#+\s*)?"                                  # optional markdown heading hashes
    r"(?:investment\s+policy\s+statement|\bIPS\b|instrument\s+map)"  # the heading cue
    r".*?"                                         # the section body (lazy)
    r"(?=\n#+\s|\Z)",                              # up to the next heading or EOT
    re.IGNORECASE | re.DOTALL,
)

# A single rendered sleeve weight: a label (words, allowing &, /, -, parens,
# digits like "ex-US", "T-bills") immediately followed by a number + '%'. The
# label is the run of non-digit, non-bullet text before the number; we capture it
# so we can match it against the canonical doc's sleeve labels. The leading
# anchor tolerates a list bullet ("- ", "* ", "• ") or line start so we bind the
# label to its OWN line, not a trailing fragment of the previous sentence.
#   "NVDA 13%"  ->  label="NVDA", pct=13
#   "- Global equity 35%" -> label="Global equity", pct=35
#   "Cash & T-bills 7%"   -> label="Cash & T-bills", pct=7
_SLEEVE_WEIGHT_RE = re.compile(
    r"(?:^|[\n\r])\s*[-*•]?\s*"               # line start + optional bullet
    r"([A-Za-z][A-Za-z0-9 &/().‑-]*?[A-Za-z)])"  # the sleeve label (>=2 chars, ends alpha/paren)
    r"\s*"                                         # optional gap
    r"(\d+(?:\.\d+)?)\s*%",                        # the weight number + percent
)

# A label is too generic to be a sleeve (avoids summing prose like "the 100%
# partition" or "a 5% buffer note"). Pure-number-ish or stop-word labels are
# dropped. WHY: fail-loud prefers over-inclusion, but a label that is ONLY a
# stop word ("of", "to", "the", "is", "at") is noise, not a sleeve, and would
# corrupt the sum with sentence-fragment matches.
_STOPWORD_LABELS: frozenset[str] = frozenset(
    {"of", "to", "the", "is", "at", "a", "an", "and", "or", "by", "in", "on"}
)
# A real sleeve label is a short noun phrase. A "<long sentence fragment> NN%"
# match (e.g. "The above sleeves form a 100% partition") is PROSE, not a sleeve —
# reject labels above this word count so a narrative sentence ending in a percent
# can't masquerade as a sleeve and corrupt the self-sum. Sleeve names in the
# canonical doc top out around 5 words ("Cash & T-bills (incl. ILS tranche)").
_MAX_SLEEVE_LABEL_WORDS = 6
# A label whose FIRST word is a sentence opener ("the", "a", "above", "this",
# "these", …) is prose narration, not a sleeve name.
_SENTENCE_OPENERS: frozenset[str] = frozenset(
    {"the", "a", "an", "above", "this", "these", "those", "that", "which",
     "all", "each", "every", "remaining", "residual", "total", "sum"}
)


def _normalize_label(label: str) -> str:
    """Casefold + collapse whitespace so prose labels and doc labels compare."""
    return re.sub(r"\s+", " ", label.strip()).casefold()


def _extract_prose_sleeves(plan_text: str) -> list[tuple[str, float]]:
    """Pull (label, pct) sleeve weights from the IPS/instrument-map prose section."""
    text = plan_text or ""
    section_match = _IPS_SECTION_RE.search(text)
    section = section_match.group(0) if section_match else text
    out: list[tuple[str, float]] = []
    for m in _SLEEVE_WEIGHT_RE.finditer(section):
        label = m.group(1).strip()
        norm = _normalize_label(label)
        if norm in _STOPWORD_LABELS:
            continue
        words = norm.split()
        # Drop sentence-fragment "labels" (a narrative line ending in "… NN%").
        if len(words) > _MAX_SLEEVE_LABEL_WORDS:
            continue
        if words and words[0] in _SENTENCE_OPENERS:
            continue
        out.append((label, float(m.group(2))))
    return out


def _doc_sleeve_weights(target_allocation_doc) -> dict[str, float] | None:
    """Duck-type a TargetAllocationDoc into {normalized_label: weight_pct}, or None.

    The canonical doc (``argosy/services/target_allocation_doc.py``) exposes
    ``.classes``, a list of objects each carrying ``.label`` (the sleeve name) and
    ``.target_pct`` (its % of the full tradeable book). We discover those exact
    attribute names by getattr and fall back to ``None`` (disabling check 2)
    whenever the shape isn't found — never raising on an unexpected object.
    """
    classes = getattr(target_allocation_doc, "classes", None)
    if not classes:
        return None
    weights: dict[str, float] = {}
    for c in classes:
        label = getattr(c, "label", None)
        # Accept the canonical ``target_pct``; fall back to a couple of plausible
        # weight attribute names so a near-twin object still binds.
        pct = getattr(c, "target_pct", None)
        if pct is None:
            pct = getattr(c, "weight", None)
        if pct is None:
            pct = getattr(c, "pct", None)
        if label is None or not isinstance(pct, (int, float)) or isinstance(pct, bool):
            continue
        weights[_normalize_label(str(label))] = float(pct)
    return weights or None


def check_ips_equality(
    *, plan_text: str, target_allocation_doc=None
) -> list[GateViolation]:
    """Flag an incoherent IPS instrument map (run-106 finding [5]).

    Inputs:
      - ``plan_text``: the rendered plan prose; its IPS/instrument-map section is
        scanned for "<sleeve> NN%" weights.
      - ``target_allocation_doc``: the canonical ``TargetAllocationDoc`` (duck-typed,
        optional). When provided, each sleeve present in BOTH the prose and the doc
        must agree within tolerance.

    Two checks:
      (1) PROSE SELF-SUM — the prose sleeve weights must sum to ``100 ±
          IPS_SUM_TOLERANCE_PCT``. The run-106 ~106% case fails here.
      (2) PROSE-vs-CANONICAL EQUALITY (only when ``target_allocation_doc`` is given
          and its shape is recognized) — a prose weight diverging from the canonical
          doc weight for the same sleeve by more than
          ``_SLEEVE_EQUALITY_TOLERANCE_PP`` fails. If the doc shape isn't found,
          check (2) is skipped (returns no violations from it).
    """
    violations: list[GateViolation] = []
    sleeves = _extract_prose_sleeves(plan_text)

    # (1) PROSE SELF-SUM.
    if sleeves:
        total = round(sum(pct for _, pct in sleeves), 2)
        if abs(total - 100.0) > IPS_SUM_TOLERANCE_PCT:
            listing = "; ".join(f"{label}={pct}" for label, pct in sleeves)
            direction = (
                "OVER (named weights exceed 100 before an unspecified residual)"
                if total > 100.0
                else "UNDER (named weights fall short of 100)"
            )
            violations.append(
                GateViolation(
                    check=GateCheck.IPS_EQUALITY,
                    detail=(
                        f"IPS instrument-map prose weights sum to {total}% "
                        f"(must be 100±{IPS_SUM_TOLERANCE_PCT}) — {direction}. The map "
                        f"claims a 100% partition but its rendered weights are "
                        f"incoherent. Sleeves: {listing}"
                    ),
                    locator="ips_prose_self_sum",
                )
            )

    # (2) PROSE-vs-CANONICAL EQUALITY.
    if target_allocation_doc is not None:
        doc_weights = _doc_sleeve_weights(target_allocation_doc)
        if doc_weights:
            for label, prose_pct in sleeves:
                norm = _normalize_label(label)
                if norm not in doc_weights:
                    continue
                canonical = doc_weights[norm]
                if abs(prose_pct - canonical) > _SLEEVE_EQUALITY_TOLERANCE_PP:
                    violations.append(
                        GateViolation(
                            check=GateCheck.IPS_EQUALITY,
                            detail=(
                                f"IPS prose sleeve '{label}' = {prose_pct}% diverges from "
                                f"the canonical target-allocation doc ({canonical}%) by "
                                f"more than {_SLEEVE_EQUALITY_TOLERANCE_PP}pp. The prose "
                                f"must project the canonical allocation, not restate it."
                            ),
                            locator=f"ips_sleeve={label}",
                        )
                    )

    return violations
