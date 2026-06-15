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
    ``WealthDashboard`` dataclass fields. Concept extraction is wired to NAMED
    fields, never regex over prose.

One responsibility: assemble + extract. Pure/deterministic over its inputs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — type-checker hint only
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# Short, downstream-stable concept keys. A coherence gate depends on these
# EXACT strings — do not rename without updating the gate.
CONCEPT_NET_WORTH = "net_worth_nis"
CONCEPT_NVDA_WEIGHT = "nvda_weight_pct"
CONCEPT_US_SITUS_ESTATE = "us_situs_estate_nis"
CONCEPT_FI_MARGIN = "fi_margin_signed_nis"


@dataclass
class AssembledArtifact:
    """Every user-facing surface, concatenated, plus a per-surface headline map.

    ``full_text``     : exact concatenation of every surface the user reads.
    ``surface_values``: concept name -> [(surface_name, value)] for every
                        surface that states a value for that concept.
    """

    full_text: str
    surface_values: dict[str, list[tuple[str, float]]] = field(default_factory=dict)


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


def assemble_plan_artifact(session: Session, *, user_id: str) -> AssembledArtifact:
    """Concatenate every surface the user reads + extract per-surface headlines.

    Surfaces: the plan body + ``## Wealth Dashboard`` + horizon blocks (and the
    appendices baked into the long-horizon markdown) via the real export render
    path, plus the deterministic resolver (the body's single source of truth)
    and the typed ``WealthDashboard`` dataclass (the dashboard's own numbers).
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
    full_text = build_plan_export_markdown(session, user_id=user_id)

    surface_values: dict[str, list[tuple[str, float]]] = {}

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
        except Exception as exc:  # noqa: BLE001 — body values just stay absent
            log.warning("assembled_artifact.resolver_failed err=%s", exc)
            resolved = None

        if resolved is not None:
            _add_body_values(resolved, surface_values)

    # ----- Dashboard surface: the typed WealthDashboard dataclass -----------
    try:
        dash = compute_wealth_dashboard(session, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 — dashboard values just stay absent
        log.warning("assembled_artifact.dashboard_failed err=%s", exc)
        dash = None

    if dash is not None:
        _add_dashboard_values(dash, surface_values)

    return AssembledArtifact(full_text=full_text, surface_values=surface_values)


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
        _append(bag, CONCEPT_NET_WORTH, "dashboard", getattr(ret, "net_worth_nis", None))

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


__all__ = [
    "AssembledArtifact",
    "assemble_plan_artifact",
    "CONCEPT_NET_WORTH",
    "CONCEPT_NVDA_WEIGHT",
    "CONCEPT_US_SITUS_ESTATE",
    "CONCEPT_FI_MARGIN",
]
