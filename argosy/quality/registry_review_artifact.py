"""Phase 2a — render the whole-artifact reviewer's input FROM the canonical
derivation graph.

The reviewer today reads the from-scratch prose only; contradictions live in
surfaces the prose-editor never touches (dashboard FI-crossing, net-worth basis,
retention split). This module anchors the reviewer's artifact with a CANONICAL
reconciliation block rendered from the Phase-1c canonical surfaces — the single
registry value every other surface must agree with — so a prose figure that
disagrees is a finding routed to that figure's owner.

Flag-gated (``ARGOSY_REGISTRY_REVIEW_ARTIFACT``, default OFF): off -> the reader
sees exactly today's assembled artifact (zero live change). Pure render; no new
math. See docs/superpowers/specs/2026-06-19-financial-advisory-team-design.md
(Phase 2).

SOURCE-AUTHORITATIVE GATE (codex plan review): ``build_base_graph`` seeds 0.0 for
PENDING canonical scalars, so a surface can render a VALID string from a
fail-closed seed (₪0 net worth, age 0, "reached with ₪0 margin"). The renderer
therefore renders a surface only when its resolver SOURCE key is genuinely
``resolved`` (when a manifest is supplied), or — with no manifest — when the
surface's inbound graph value is a real (non-None, non-zero) figure. A pending
figure is OMITTED from the anchor, never shown as authoritative.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# The canonical surfaces (sentence/statement form) that anchor the review, in a
# stable reading order. Each is a NodeKind.SURFACE built by live_surfaces; only
# present + valid + source-authoritative ones render.
CANONICAL_REVIEW_SURFACES: tuple[str, ...] = (
    "surface:retirement_age_headline",
    "surface:fi_verdict",
    "surface:fi_crossing_statement",
    "surface:dashboard.net_worth_liquid_tile",
    "surface:dashboard.net_worth_investable_tile",
    "surface:dashboard.net_worth_total_tile",
    "surface:retention_at_vest_statement",
    "surface:retention_capital_track_statement",
    "surface:us_situs_estate_headline",
)

# surface_key -> the resolver MANIFEST key whose `resolved` status authorizes the
# surface. The live graph may alias the source node (SUBJECT_NODE_MAP), but the
# authoritative resolution status is the resolver manifest's.
_SURFACE_RESOLVER_SOURCE: dict[str, str] = {
    "surface:retirement_age_headline": "retirement.earliest_safe_age",
    "surface:fi_verdict": "retirement.fi_margin_signed_nis",
    "surface:fi_crossing_statement": "retirement.fi_crossing_year",
    "surface:dashboard.net_worth_liquid_tile": "portfolio.liquid_net_worth_nis",
    "surface:dashboard.net_worth_investable_tile": "portfolio.net_worth_nis",
    "surface:dashboard.net_worth_total_tile": "portfolio.total_net_worth_incl_residence_nis",
    "surface:retention_at_vest_statement": "tax.retention_at_vest_pct",
    "surface:retention_capital_track_statement": "tax.retention_capital_track_pct",
    "surface:us_situs_estate_headline": "concentration.us_situs_estate_exposure_nis",
}

_HEADER = "## Reviewer-only canonical reconciliation anchor (registry single source)"
_INTRO = (
    "NOT client-facing plan prose — a reviewer-only reference. These are the "
    "authoritative registry figures (one owner each). Every other surface in "
    "this plan must AGREE with them; a figure that disagrees is a finding to "
    "route to that figure's owner, not a value to average. The three net-worth "
    "bases are DISTINCT measures, not a contradiction."
)

FLAG_ENV = "ARGOSY_REGISTRY_REVIEW_ARTIFACT"


def _flag_on() -> bool:
    """True when the registry-review-artifact anchor is enabled. An explicit env
    var wins; else the configured default (default OFF). Mirrors
    incremental_plan._flag_on truthiness (1/true/yes/on)."""
    env = os.environ.get(FLAG_ENV)
    if env is not None:
        return str(env).strip().lower() in {"1", "true", "yes", "on"}
    try:
        from argosy.config import get_settings

        val = getattr(get_settings(), FLAG_ENV.lower(), None)
    except Exception:  # noqa: BLE001
        val = None
    if isinstance(val, bool):
        return val
    return str(val or "").strip().lower() in {"1", "true", "yes", "on"}


def _source_authoritative(surface_key: str, *, graph, resolved) -> bool:
    """A surface may anchor the review only when its SOURCE is genuinely resolved.

    With a resolver manifest: the mapped resolver key must be ``resolved`` with a
    non-None value. Without one (hermetic tests): the surface's inbound graph
    value must be a real figure — non-None and not the 0.0 fail-closed seed —
    which guards the seeded-zero leak for money/age subjects."""
    src = _SURFACE_RESOLVER_SOURCE.get(surface_key)
    if resolved is not None and src is not None:
        rv = resolved.get(src)
        return (
            rv is not None
            and getattr(rv, "status", None) == "resolved"
            and getattr(rv, "value", None) is not None
        )
    try:
        node = graph.get(surface_key)
    except Exception:  # noqa: BLE001
        return False
    inputs = getattr(node, "inputs", ()) or ()
    if not inputs:
        return False
    for ik in inputs:
        try:
            v = graph.get(ik).value
        except Exception:  # noqa: BLE001
            return False
        if v is None:
            return False
        if isinstance(v, bool):
            return False
        if isinstance(v, (int, float)) and float(v) == 0.0:
            return False
    return True


def render_canonical_reconciliation_block(
    graph, *, resolved=None, keys: tuple[str, ...] = CANONICAL_REVIEW_SURFACES
) -> str:
    """Render the canonical surfaces that are present, valid, AND
    source-authoritative in ``graph`` into a reviewer-only markdown anchor block.

    Skips absent/invalid/non-string surfaces and — crucially — any surface whose
    source is pending/seeded-zero (see ``_source_authoritative``), so a
    fail-closed 0.0 seed never appears as a canonical fact. Returns "" when none
    render (fail-safe — never a header with no figures)."""
    bullets: list[str] = []
    for key in keys:
        try:
            node = graph.get(key)
        except Exception:  # noqa: BLE001 — absent surface is skipped
            continue
        if not graph.is_valid(key):
            continue
        if not _source_authoritative(key, graph=graph, resolved=resolved):
            continue
        val = node.value
        if isinstance(val, str) and val.strip():
            bullets.append(f"- {val.strip()}")
    if not bullets:
        return ""
    return "\n".join([_HEADER, "", _INTRO, "", *bullets]) + "\n"


def _resolver_values_for_graph(resolved) -> dict[str, float]:
    """The resolved canonical source values to seed the graph with — taken from
    the SAME manifest that authorizes rendering, so the graph the surfaces render
    from and the gate that authorizes them can never come from two different
    resolver passes (codex impl review)."""
    values: dict[str, float] = {}
    for key in set(_SURFACE_RESOLVER_SOURCE.values()):
        rv = resolved.get(key)
        if (
            getattr(rv, "status", None) == "resolved"
            and getattr(rv, "value", None) is not None
        ):
            values[key] = float(rv.value)
    return values


def build_reader_anchor_block(
    session, *, user_id: str, decision_run_id: int, graph=None, resolved=None,
) -> str:
    """Render the reviewer-only canonical reconciliation anchor block (or "" when
    no canonical surface is authoritative). ``graph`` / ``resolved`` injectable
    for tests; in production the manifest is resolved ONCE and the graph is seeded
    from THAT manifest (build_base_graph ignores keys outside its scalar tuple),
    so the rendered values and the authorizing manifest are one snapshot."""
    if graph is None:
        # Production: resolve the manifest ONCE and seed the graph from THAT same
        # manifest, so the rendered values and the authorizing gate are one
        # resolver snapshot. (When a graph is injected — tests — we never resolve.)
        if resolved is None:
            from argosy.services.plan_numeric_resolver import resolve_plan_numbers
            resolved = resolve_plan_numbers(
                session, user_id=user_id, decision_run_id=decision_run_id,
                include_canonical_ages=True,
            )
        from argosy.orchestrator.flows.incremental_plan import build_base_graph
        graph = build_base_graph(
            session, user_id, decision_run_id=decision_run_id,
            resolver_values=_resolver_values_for_graph(resolved),
        )
    return render_canonical_reconciliation_block(graph, resolved=resolved)


def assemble_registry_review_artifact(
    session, *, user_id: str, decision_run_id: int,
    base_text: str | None = None, graph=None, resolved=None,
) -> str:
    """The reader-candidate artifact as ONE string: today's assembled from-scratch
    text with a reviewer-only canonical reconciliation anchor APPENDED (append-only
    — ``base_text`` is always an exact prefix of the result). Returns ``base_text``
    UNCHANGED when no canonical surface renders. (The orchestrator passes the anchor
    SEPARATELY to the reader; this single-string form is for non-prompt consumers.)"""
    if base_text is None:
        from argosy.services.assembled_artifact import assemble_plan_artifact
        base_text = assemble_plan_artifact(session, user_id=user_id).full_text or ""
    block = build_reader_anchor_block(
        session, user_id=user_id, decision_run_id=decision_run_id,
        graph=graph, resolved=resolved)
    if not block:
        return base_text
    if not base_text:
        return block
    sep = "" if base_text.endswith("\n\n") else "\n" if base_text.endswith("\n") else "\n\n"
    return base_text + sep + block


def compute_reader_anchor(
    session, *, user_id: str, decision_run_id: int, _builder=None,
) -> str:
    """Return the reviewer-only canonical anchor block for the whole-artifact
    reader, or "" when the flag is OFF / nothing is authoritative / anything
    fails. Flag default OFF ⇒ "" ⇒ the reader prompt's anchor section shows its
    "no anchor on this run" sentinel and the reader path is unchanged. Fail-soft:
    a graph/resolver error never loses the from-scratch review. ``_builder``
    injects the graph builder for tests."""
    if not _flag_on():
        return ""
    try:
        graph = None
        if _builder is not None:
            graph = _builder(session, user_id, decision_run_id=decision_run_id)
        return build_reader_anchor_block(
            session, user_id=user_id, decision_run_id=decision_run_id, graph=graph)
    except Exception as exc:  # noqa: BLE001 — fail-soft, keep the from-scratch review
        log.warning(
            "registry_review.anchor_failed user_id=%s decision_run_id=%s err=%s",
            user_id, decision_run_id, exc)
        return ""


__all__ = [
    "CANONICAL_REVIEW_SURFACES",
    "FLAG_ENV",
    "render_canonical_reconciliation_block",
    "build_reader_anchor_block",
    "assemble_registry_review_artifact",
    "compute_reader_anchor",
]
