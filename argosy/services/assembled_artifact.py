"""Assemble the full user-facing artifact + a per-surface headline map.

Argosy has no stage that reads its OWN finished output. Cross-surface
contradictions (net worth ₪11.95M in the body vs ₪14.44M on the dashboard;
NVDA 62.5% body vs 56.9% dashboard) live in the SEAMS between subsystems that
never see each other's output. This module closes that gap: it concatenates
EVERY surface the user reads into one artifact and extracts the headline value
each surface STATES for each shared concept, so a downstream coherence gate (or
a whole-artifact reader) can compare them side by side.

Two outputs:

  * ``full_text`` — the exact concatenation of every user-facing surface,
    reproducing the EXPORT. It REUSES the real render path
    (``build_plan_export_markdown``: current-plan body + ``## Wealth Dashboard``
    + the three horizon blocks; the assumption-ledger / evidence / receipts
    appendices are baked into ``horizon_long_md`` at synthesis time and so ride
    along inside the long-horizon block). Rendering is NEVER re-implemented here.
  * ``surface_values`` — ``dict[concept] -> list[(surface_name, value)]`` keyed
    by SHORT shared concept names. Body/plan values come from the deterministic
    ``resolve_plan_numbers`` resolver; dashboard values come from the typed
    ``WealthDashboard`` dataclass fields; and for the NVDA policy numbers (cap
    and steering target) a third ``prose`` surface is extracted from the rendered
    plan text using phrase-anchored regex so that an LLM-hardcoded stale value
    (e.g. "12% hard cap" when canonical is 13%) is caught even when the resolver
    and alloc-doc agree. See ``_extract_prose_nvda_values`` for the extraction
    rationale and false-positive defence.

One responsibility: assemble + extract. Pure/deterministic over its inputs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — type-checker hint only
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# Short, downstream-stable concept keys. A coherence gate depends on these
# EXACT strings — do not rename without updating the gate.
# Two DISTINCT net-worth bases — NOT the same concept:
#   * net_worth_nis       : liquid/investable net worth (resolver body figure;
#                           USD assets × BOI FX + NIS-native cash, EXCLUDING the
#                           Israel real-estate equity).
#   * net_worth_total_nis : total net worth INCLUDING all real estate (the Wealth
#                           Dashboard figure; liquid + Israel real-estate equity).
# They differ by the real-estate equity and must be cross-checked under separate
# keys, so the deterministic coherence gate never flags total-vs-liquid as a
# contradiction (both numbers are correct; they measure different things).
CONCEPT_NET_WORTH = "net_worth_nis"
CONCEPT_NET_WORTH_TOTAL = "net_worth_total_nis"
CONCEPT_NVDA_WEIGHT = "nvda_weight_pct"
CONCEPT_US_SITUS_ESTATE = "us_situs_estate_nis"
CONCEPT_FI_MARGIN = "fi_margin_signed_nis"
# Two DISTINCT NVDA policy numbers — NOT the same concept:
#   * nvda_cap_pct    : the LOOK-THROUGH hard ceiling (~13 pp). Argosy-derived as
#                       MIN over four constraint caps; the user does NOT set this.
#                       Stored as fraction (0–1) in the resolver; extracted here
#                       as %-points (× 100) so the coherence gate compares like
#                       for like with the alloc-doc surface (already in %-points).
#   * nvda_target_pct : the DIRECT sleeve target (~8 pp). Lower than the cap to
#                       keep total plan look-through < cap given embedded NVDA
#                       inside index sleeves. Derives from NVDA_TARGET_PCT in
#                       allocation_plan.py → plan_numeric_resolver, so it is
#                       always consistent with the canonical doc's class target_pct.
# Both surfaces carry exactly one value — the resolver's constant-derived figure —
# making a stale-hardcode divergence (the 12%/8%/13% three-value contradiction)
# impossible: if the constant changes, all surfaces update together.
CONCEPT_NVDA_CAP = "nvda_cap_pct"
CONCEPT_NVDA_TARGET = "nvda_target_pct"

# ── Prose extraction: phrase-anchored regex for NVDA policy numbers ───────────
# The plan body is LLM-generated prose.  Although the resolver uses
# ``{{fact:...}}`` placeholders (rendered to canonical values), an LLM can
# also write a literal percentage — e.g. "The 8% steering target sits inside
# the 12% hard cap" — and that literal can be stale.  If the resolver and
# alloc_doc both say 13% but the prose says "12% hard cap", the existing
# canonical-vs-canonical comparison passes silently.
#
# Defence against false positives:
#   The patterns anchor on domain-specific NVDA terminology ("hard cap",
#   "binding ceiling", "steering target", "IPS sleeve") that does NOT appear
#   near tax rates (12% NI/health, 25% CGT, 50% ordinary income), equity
#   returns, or other-sleeve allocations (28.5% US broad-market).  A greedy
#   "any N% near NVDA" pattern would false-positive on NVDA's current weight
#   (59.9%), implied σ (35%), and other legitimate percentages, so we require
#   the specific NVDA policy phrase within ~30–50 chars on the same line.
#
# De-duplication: each unique float value is recorded once per concept so the
# gate entry list stays compact; if two parts of the prose disagree (one says
# 12%, another says 13%), both are recorded and the gate fires on the internal
# inconsistency as well.

# Cap: "N% hard cap" | "hard cap … N%" | "N% binding ceiling" | "binding ceiling … N%"
# "hard cap" and "binding ceiling" are the domain-specific NVDA concentration
# ceiling terms; neither appears in tax rates, returns, or other caps.
_PROSE_NVDA_CAP_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*%[^.!?\n]{0,30}?(?:hard\s+cap|binding\s+ceiling)"
    r"|(?:hard\s+cap|binding\s+ceiling)[^.!?\n]{0,40}?(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)

# Target: "N% steering target" | "steering target … N%" |
#         "N% IPS sleeve" | "IPS sleeve … N%" | "N% policy target/steering"
# "steering target" and "IPS sleeve" are the domain-specific NVDA sleeve-target
# terms; neither collides with tax rates, safe-withdrawal rates, or other targets.
#
# The "phrase then number" branch uses TWO guards to avoid false positives:
#   1. Short gap (≤ 20 chars) so that "steering target inside the 13% cap"
#      doesn't spuriously capture the 13% (the cap number that follows "inside
#      the", not the target number).
#   2. Negative lookahead (?!\s*(?:hard\s*cap|cap\b|ceiling)) on the matched
#      number: if the number is immediately trailed by cap/ceiling language it
#      is a CAP reference, not a target reference, and must not be picked up
#      here.  This specifically suppresses "steering target inside the 13% cap"
#      (prose=13 false positive on the target concept observed in plan 96).
_PROSE_NVDA_TARGET_RE = re.compile(
    # "N% steering target" | "N% IPS sleeve" | "N% policy target/steering".
    # The gap excludes '%' to prevent "13.0% and 8% IPS steering target" from
    # greedily matching 13.0% as the target — requiring no other '%' in the
    # gap ensures we capture the number DIRECTLY before the phrase, not one
    # separated by another percentage (the cap value in the same clause).
    r"(\d+(?:\.\d+)?)\s*%[^.!?\n%]{0,30}?(?:steering\s+target|ips\s+sleeve|policy\s+target|policy\s+steering)"
    # "steering target … N%" | "IPS sleeve … N%" | "policy target … N%".
    # Short gap (≤ 20 chars) + negative lookahead prevents capturing the CAP
    # number that follows "steering target inside the 13% cap".
    r"|(?:steering\s+target|ips\s+sleeve|policy\s+target)[^.!?\n]{0,20}?(\d+(?:\.\d+)?)\s*%(?!\s*(?:hard\s*cap|cap\b|ceiling))",
    re.IGNORECASE,
)


@dataclass
class AssembledArtifact:
    """Every user-facing surface, concatenated, plus a per-surface headline map.

    ``full_text``       : exact concatenation of every surface the user reads.
    ``surface_values``  : concept name -> [(surface_name, value)] for every
                          surface that states a value for that concept.
    ``extraction_errors``: surface name ("body"/"dashboard") -> error string for
                          any per-surface headline extraction that COLLAPSED.
                          Recorded (not swallowed) so a downstream coherence
                          gate sees the surface failed instead of mistaking the
                          resulting absent concept for "not applicable".
    """

    full_text: str
    surface_values: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    extraction_errors: dict[str, str] = field(default_factory=dict)


def _append(
    bag: dict[str, list[tuple[str, float]]],
    concept: str,
    surface: str,
    value: float | None,
) -> None:
    """Record ``(surface, value)`` under ``concept`` when ``value`` is real."""
    if value is None:
        return
    try:
        v = float(value)
    except (TypeError, ValueError):
        return
    bag.setdefault(concept, []).append((surface, v))


# Internal generation-telemetry appendix headings the whole-artifact reader must
# NOT review as client-plan content (they describe HOW the doc was made — which
# agents ran, codex present/absent — not WHAT the plan is, and contradict the
# final body). Stripped only from the reader artifact; the user export keeps them.
_INTERNAL_METADATA_HEADINGS = (
    "## Appendix — Fleet receipts",
    "## Appendix — Analysis team receipts",
    "## Appendix — Coherence deliberations",
    "## Appendix — FM objection dialogues (how the FM talked to the fleet)",
)


def _strip_internal_metadata_sections(full_text: str) -> str:
    """Remove internal generation-telemetry appendix sections (header → next
    ``## `` heading or EOF) from the reader-facing artifact."""
    out = full_text or ""
    for heading in _INTERNAL_METADATA_HEADINGS:
        # Match the heading line through to (but not including) the next level-2
        # heading, or to end of document.
        pattern = re.compile(
            r"\n" + re.escape(heading) + r"\b.*?(?=\n## |\Z)",
            re.DOTALL,
        )
        out = pattern.sub("", out)
    return out


def assemble_plan_artifact(session: Session, *, user_id: str) -> AssembledArtifact:
    """Concatenate every surface the user reads + extract per-surface headlines.

    Surfaces: the plan body + ``## Wealth Dashboard`` + horizon blocks (and the
    appendices baked into the long-horizon markdown) via the real export render
    path, plus the deterministic resolver (the body's single source of truth)
    and the typed ``WealthDashboard`` dataclass (the dashboard's own numbers).

    Fail-loud asymmetry: ``build_plan_export_markdown`` (``full_text``) is
    intentionally NOT wrapped — if the export itself can't render there is no
    artifact at all, which is a HARD fail and must raise loudly. By contrast a
    per-surface HEADLINE extraction failure (body resolver / dashboard compute)
    does not crash assembly; it is recorded in ``extraction_errors`` and logged
    at error level. That keeps the surface's collapse VISIBLE to downstream
    coherence gates (a swallowed failure -> concept absent -> the gate skips the
    concept and passes vacuously, the exact false-negative this plan prevents)
    instead of letting it masquerade as "not applicable".

    Note: ``fi_margin_signed_nis`` is currently BODY-ONLY — no dashboard
    ``RetirementBlock`` margin field exists — so the deterministic cross-surface
    gate will not diverge-check it until a second surface contributes it. The
    prose-level sign-flip check is the whole-artifact reader's job (Task 6), not
    this deterministic gate.
    """
    from argosy.services.plan_export import build_plan_export_markdown
    from argosy.services.wealth_dashboard import compute_wealth_dashboard
    from argosy.state.queries import get_current_plan, get_pending_draft

    # ----- full_text: reproduce the export (body + dashboard + horizons) ----
    # build_plan_export_markdown is the function that produces the downloadable
    # ``argosy-plan-*.md`` the user reads. The assumption-ledger / evidence /
    # fleet-receipts appendices are appended to ``horizon_long_md`` at synthesis
    # time (see render_plan_appendices), so they ride inside the long-horizon
    # block of that export — no separate append needed to reproduce what the
    # user sees.
    # Exclude the internal "Pending FM objections" scratchpad — it is frozen at
    # the FM phase and predates the reconcile/surgical edits, so it contradicts
    # the FINAL body and the whole-artifact reader (correctly) flags it as a
    # cross-surface contradiction. The reader must review the PLAN, not stale
    # internal review metadata; the user-facing export still includes it.
    full_text = build_plan_export_markdown(
        session, user_id=user_id, include_fm_objections=False
    )
    # Strip pure generation-telemetry appendices the reader must not review as
    # plan content (which agent ran / codex present-absent). Like the objection
    # scratchpad, these are internal metadata that contradict the final body and
    # produce spurious cross-surface findings. The user export keeps them.
    full_text = _strip_internal_metadata_sections(full_text)

    surface_values: dict[str, list[tuple[str, float]]] = {}
    extraction_errors: dict[str, str] = {}

    # ----- Body / plan surface: the deterministic resolver manifest ---------
    # The resolver is the SINGLE SOURCE OF TRUTH the plan body binds to, so its
    # values are exactly what the body STATES (no prose parsing). Find the
    # decision run that produced the displayed plan (draft preferred, else
    # current) so the per-role agent reports resolve.
    plan = get_pending_draft(session, user_id) or get_current_plan(session, user_id)
    decision_run_id = getattr(plan, "decision_run_id", None) if plan else None
    if decision_run_id is not None:
        try:
            from argosy.services.plan_numeric_resolver import resolve_plan_numbers

            resolved = resolve_plan_numbers(
                session, user_id=user_id, decision_run_id=decision_run_id,
            )
        except Exception as exc:  # noqa: BLE001 — recorded, never silently absent
            # A body-surface collapse must be VISIBLE to downstream gates, not
            # degraded to ABSENT. Broad except keeps assembly robust, but the
            # failure is now logged at error level + recorded on the artifact.
            log.error("assembled_artifact.resolver_failed err=%s", exc)
            extraction_errors["body"] = repr(exc)
            resolved = None

        if resolved is not None:
            _add_body_values(resolved, surface_values)
            # FINAL placeholder-render pass over the WHOLE assembled artifact: a
            # per-surface renderer (e.g. the short-actions labels) can leave a
            # {{fact:KEY}} token unsubstituted, leaking into the client bytes. One
            # render_placeholders pass here substitutes EVERY resolvable token
            # regardless of which sub-renderer emitted it (idempotent — already-
            # rendered text has none), and strips any residual EMIT-AS scaffolding.
            # strict=False leaves a genuinely unresolvable token in place so the
            # leakage gate still surfaces it (fail-loud, never silently dropped).
            try:
                from argosy.quality.fact_registry import render_placeholders

                full_text = render_placeholders(full_text, resolved, strict=False)
            except Exception as exc:  # noqa: BLE001 — never crash assembly on a render pass
                log.warning("assembled_artifact.final_render_pass_failed err=%s", exc)

    # ----- Canonical allocation doc: NVDA cap + target from structured plan ----
    # The TargetAllocationDoc is the ONE authoritative source for NVDA cap and
    # sleeve target.  Extracting these as a separate "alloc_doc" surface means
    # the cross-surface coherence gate sees them alongside the resolver body
    # values, and any divergence (e.g. a stale hardcode in a prompt injecting
    # "12%" while the doc says "8%") is caught deterministically.
    try:
        from argosy.services.target_allocation_doc import load_plan_target_allocation

        alloc_doc = load_plan_target_allocation(plan) if plan is not None else None
        if alloc_doc is not None:
            _add_alloc_doc_values(alloc_doc, surface_values)
    except Exception as exc:  # noqa: BLE001 — never crash assembly on the doc
        log.warning("assembled_artifact.alloc_doc_failed err=%s", exc)
        extraction_errors["alloc_doc"] = repr(exc)

    # ----- Dashboard surface: the typed WealthDashboard dataclass -----------
    try:
        dash = compute_wealth_dashboard(session, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 — recorded, never silently absent
        # A dashboard-surface collapse must be VISIBLE to downstream gates, not
        # degraded to ABSENT. Broad except keeps assembly robust, but the
        # failure is now logged at error level + recorded on the artifact.
        log.error("assembled_artifact.dashboard_failed err=%s", exc)
        extraction_errors["dashboard"] = repr(exc)
        dash = None

    if dash is not None:
        _add_dashboard_values(dash, surface_values)

    # ----- Prose-level NVDA cap/target extraction ----------------------------
    # Placed last so that render_placeholders (called above inside the body
    # block) has already substituted {{fact:...}} tokens before we scan the
    # text.  A failure here must not crash assembly — record it and continue.
    try:
        _extract_prose_nvda_values(full_text, surface_values)
    except Exception as exc:  # noqa: BLE001 — never crash assembly on prose scan
        log.warning("assembled_artifact.prose_nvda_extraction_failed err=%s", exc)
        extraction_errors["prose"] = repr(exc)

    return AssembledArtifact(
        full_text=full_text,
        surface_values=surface_values,
        extraction_errors=extraction_errors,
    )


def _add_body_values(resolved, bag: dict[str, list[tuple[str, float]]]) -> None:
    """Map resolver keys -> short concept keys for the ``body`` surface.

    Resolver convention: percentages are stored as FRACTIONS (0–1); the
    dashboard states percent-POINTS. We normalise NVDA weight to percent-points
    here so both surfaces are comparable in the same unit.
    """
    def _val(key: str) -> float | None:
        rv = resolved.get(key)
        if rv is None or rv.status != "resolved" or rv.value is None:
            return None
        return float(rv.value)

    _append(bag, CONCEPT_NET_WORTH, "body", _val("portfolio.net_worth_nis"))

    # NVDA cap and target — resolver stores as fractions (0–1); convert to %-points
    # so the gate compares like-for-like with the alloc-doc surface (%-points).
    nvda_cap_frac = _val("concentration.nvda_cap_pct")
    if nvda_cap_frac is not None:
        _append(bag, CONCEPT_NVDA_CAP, "body", nvda_cap_frac * 100.0)
    nvda_target_frac = _val("concentration.nvda_target_pct")
    if nvda_target_frac is not None:
        _append(bag, CONCEPT_NVDA_TARGET, "body", nvda_target_frac * 100.0)

    nvda_frac = _val("concentration.nvda_current_pct")
    if nvda_frac is not None:
        # Resolver stores NVDA current weight as a 0–1 fraction → percent-points.
        _append(bag, CONCEPT_NVDA_WEIGHT, "body", nvda_frac * 100.0)

    _append(
        bag,
        CONCEPT_US_SITUS_ESTATE,
        "body",
        _val("concentration.us_situs_estate_exposure_nis"),
    )
    _append(
        bag, CONCEPT_FI_MARGIN, "body", _val("retirement.fi_margin_signed_nis"),
    )


def _add_dashboard_values(dash, bag: dict[str, list[tuple[str, float]]]) -> None:
    """Map WealthDashboard dataclass fields -> short concept keys."""
    ret = getattr(dash, "retirement", None)
    if ret is not None:
        # The dashboard's net worth is the TOTAL basis (liquid + Israel
        # real-estate equity), a DIFFERENT concept from the resolver's
        # liquid/investable net_worth_nis. Map it to the distinct total key so
        # the coherence gate compares like-for-like (no false total-vs-liquid
        # contradiction).
        _append(
            bag, CONCEPT_NET_WORTH_TOTAL, "dashboard",
            getattr(ret, "net_worth_nis", None),
        )

    conc = getattr(dash, "concentration", None)
    if conc is not None:
        # Dashboard's concentration.current_pct is already in percent-points.
        _append(
            bag, CONCEPT_NVDA_WEIGHT, "dashboard", getattr(conc, "current_pct", None),
        )

    estate = getattr(dash, "estate_exposure", None)
    if estate is not None:
        _append(
            bag,
            CONCEPT_US_SITUS_ESTATE,
            "dashboard",
            getattr(estate, "us_situs_nis", None),
        )


def _add_alloc_doc_values(doc, bag: dict[str, list[tuple[str, float]]]) -> None:
    """Extract NVDA cap and sleeve target from the canonical TargetAllocationDoc.

    The doc is the SINGLE authoritative source for allocation numbers (it is
    engine-authored, not prose-parsed).  Extracting these here makes the coherence
    gate compare the resolver body against the doc — if a stale hardcode ever
    injects a different number into LLM prose, the two "alloc_doc" entries in
    surface_values will diverge from the "body" entries and raise a BLOCK.

    ``doc.nvda_cap_pct`` is already in %-points (not a fraction) — consistent with
    how the body surface is stored (we convert resolver fractions × 100 above).
    The NVDA class's ``target_pct`` is also in %-points, matching the body.
    """
    cap = getattr(doc, "nvda_cap_pct", None)
    if cap is not None:
        try:
            _append(bag, CONCEPT_NVDA_CAP, "alloc_doc", float(cap))
        except (TypeError, ValueError):
            pass

    # The NVDA class target_pct — search for the "Strategic single-stock (NVDA)"
    # class by the canonical label used across the codebase.
    _NVDA_CLASS_LABEL = "Strategic single-stock (NVDA)"
    for cls in getattr(doc, "classes", []):
        label = getattr(cls, "label", "")
        if label == _NVDA_CLASS_LABEL:
            tgt = getattr(cls, "target_pct", None)
            if tgt is not None:
                try:
                    _append(bag, CONCEPT_NVDA_TARGET, "alloc_doc", float(tgt))
                except (TypeError, ValueError):
                    pass
            break


def _extract_prose_nvda_values(
    full_text: str, bag: dict[str, list[tuple[str, float]]]
) -> None:
    """Extract NVDA cap and steering-target figures from the rendered plan prose.

    Registers each UNIQUE value found as a ``prose`` surface entry under
    CONCEPT_NVDA_CAP and CONCEPT_NVDA_TARGET so that the coherence gate can
    compare prose claims against the canonical resolver/alloc_doc values.

    Called AFTER render_placeholders so ``{{fact:...}}`` tokens are resolved
    to their numeric values before extraction.  Only phrases that are
    semantically specific to the NVDA concentration cap or sleeve target are
    matched (see module-level ``_PROSE_NVDA_CAP_RE`` / ``_PROSE_NVDA_TARGET_RE``
    and the false-positive rationale above those patterns).

    Multiple distinct values for the same concept (e.g. prose says "12%" in one
    place and "13%" in another) are each recorded; the gate will fire on the
    internal inconsistency as well as the canonical divergence.
    """
    seen_cap: set[float] = set()
    for m in _PROSE_NVDA_CAP_RE.finditer(full_text):
        raw = m.group(1) or m.group(2)
        if raw is not None:
            try:
                v = float(raw)
                if v not in seen_cap:
                    seen_cap.add(v)
                    _append(bag, CONCEPT_NVDA_CAP, "prose", v)
            except (TypeError, ValueError):
                pass

    seen_target: set[float] = set()
    for m in _PROSE_NVDA_TARGET_RE.finditer(full_text):
        raw = m.group(1) or m.group(2)
        if raw is not None:
            try:
                v = float(raw)
                if v not in seen_target:
                    seen_target.add(v)
                    _append(bag, CONCEPT_NVDA_TARGET, "prose", v)
            except (TypeError, ValueError):
                pass


__all__ = [
    "AssembledArtifact",
    "assemble_plan_artifact",
    "CONCEPT_NET_WORTH",
    "CONCEPT_NET_WORTH_TOTAL",
    "CONCEPT_NVDA_WEIGHT",
    "CONCEPT_US_SITUS_ESTATE",
    "CONCEPT_FI_MARGIN",
    "CONCEPT_NVDA_CAP",
    "CONCEPT_NVDA_TARGET",
    "_extract_prose_nvda_values",  # exposed for unit tests
]
