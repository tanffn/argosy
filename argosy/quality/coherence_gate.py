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

from argosy.quality.gate_types import GateCheck, GateViolation

_REL_TOL = 0.01  # 1% relative tolerance for "same concept, same value across surfaces"


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
