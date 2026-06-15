"""S22 — deterministic cross-surface coherence check.

A single concept (net worth, NVDA weight, FI margin, estate exposure) read off
multiple surfaces (body prose, dashboard, appendices) must carry the SAME value
everywhere — or those surfaces are contradicting each other. This catches the
class of defect that no per-surface specialist owns: NVDA 62.5% in the body vs
56.9% on the dashboard; the FI margin shown +118,020 on one surface and
-118,020 (sign-flipped) on another.

Coherence is a property of the whole artifact, so it is checked deterministically
over the AssembledArtifact's `surface_values` map, not eyeballed by an LLM.
"""
from __future__ import annotations

import re

from argosy.quality.gate_types import GateCheck, GateViolation

_REL_TOL = 0.01  # 1% relative tolerance for "same concept, same value across surfaces"

# An unqualified assertion that capital sufficiency / FI is reached.
_REACHED_RE = re.compile(
    r"(?:capital sufficiency reached|sufficiency reached|fi reached|"
    r"financial independence reached|\bfi\b[^.]{0,40}\breached\b)",
    re.IGNORECASE,
)
# A caveat that the "reached" claim is conditional on the NVDA mark / tail.
_SHOCK_QUALIFIER_RE = re.compile(
    r"(?:nvda[^.]{0,40}(?:shock|tail|drawdown|down|−30|-30|mark)|"
    r"(?:shock|tail|drawdown)[^.]{0,40}nvda|"
    r"only at the full nvda mark|at the full nvda mark|"
    r"robust to|conditional on the nvda)",
    re.IGNORECASE,
)


def check_fi_sufficiency_under_shock(*, shock_result: dict, plan_text: str) -> list[GateViolation]:
    """Fail an unqualified "FI reached" claim that the plan's own NVDA tail breaks.

    The plan text asserts sufficiency ("capital sufficiency reached" / "FI
    reached") but the −30% NVDA shock row of ``shock_result`` shows net worth no
    longer clears the perpetuity base — and the text carries NO shock/tail
    caveat. That is a sufficiency claim that is true only at the full NVDA mark;
    no single agent owns the composition, so it is gated deterministically.

    Pass ``shock_result`` from ``fi_sufficiency_under_shock``. A qualified claim
    (one that already mentions the NVDA shock/tail caveat) is not flagged.
    """
    violations: list[GateViolation] = []
    text = plan_text or ""
    shock_30 = shock_result.get("shock_0.30") or {}
    breaks_perpetuity = shock_30.get("perpetuity_reached") is False
    if not breaks_perpetuity:
        return violations
    if not _REACHED_RE.search(text):
        return violations
    if _SHOCK_QUALIFIER_RE.search(text):
        return violations
    nw = shock_30.get("net_worth_nis")
    violations.append(
        GateViolation(
            check=GateCheck.FI_SHOCK_SUFFICIENCY,
            detail=(
                "plan asserts capital/FI sufficiency 'reached' without a NVDA-tail "
                f"qualifier, but a −30% NVDA shock drops net worth to {nw} — below "
                "the perpetuity base. The 'reached' claim is true only at the full "
                "NVDA mark; qualify it with the shock or do not claim it unconditionally."
            ),
            locator="capital_sufficiency",
        )
    )
    return violations


def check_cross_surface_coherence(artifact) -> list[GateViolation]:
    """Every concept stated on >1 surface must agree within tolerance (and not
    flip sign). A concept that two surfaces report differently is a coherence
    defect — the surfaces must bind to one source or carry distinct labels."""
    violations: list[GateViolation] = []
    for concept, pairs in (getattr(artifact, "surface_values", None) or {}).items():
        vals = [(s, v) for s, v in pairs if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if len(vals) < 2:
            continue
        lo = min(v for _, v in vals)
        hi = max(v for _, v in vals)
        base = max(abs(lo), abs(hi), 1.0)
        sign_flip = lo < 0 < hi
        if sign_flip or (hi - lo) / base > _REL_TOL:
            listing = "; ".join(f"{s}={v}" for s, v in vals)
            violations.append(
                GateViolation(
                    check=GateCheck.CROSS_SURFACE_COHERENCE,
                    detail=(
                        f"concept `{concept}` disagrees across surfaces "
                        f"({'SIGN FLIP - ' if sign_flip else ''}{listing}). "
                        "Bind all surfaces to one source or give them distinct labels."
                    ),
                    locator=concept,
                )
            )
    return violations
