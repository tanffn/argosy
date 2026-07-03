"""The deterministic plan Risk/Constraint Kernel.

Re-derives portfolio physics from RAW holdings and returns violations — blind to any
fleet/LLM reasoning. This is the shared foundation for TWO things:

  * the incremental-refinement money-safety gate (checked after every tier), and
  * Argosy's anti-correlation review gate — a reviewer that re-derives from raw source
    and BLOCKs on divergence, instead of ratifying the fleet's manifest
    (see docs/superpowers/specs/2026-07-03-incremental-plan-refinement.md and the
    "adversarial review must re-derive blind" doctrine).

Determinism is the point: it cannot be talked into agreeing. Slice 1 implements the
single-name cap on a DIRECT + FUND-LOOK-THROUGH basis — the exact constraint the plan
fleet missed (12% direct NVDA can breach a 13% total cap once embedded NVDA in
CSPX/R1GR is counted). Later slices add allocation-sum / band / US-situs checks to the
same module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# $ tolerance so floating-point noise never fabricates a breach.
_PCT_EPS = 0.05  # percentage points


@dataclass(frozen=True)
class Violation:
    code: str
    detail: str
    severity: str = "block"


@dataclass(frozen=True)
class RiskKernelResult:
    single_name_lookthrough_pct: float
    single_name_lookthrough_usd: float
    book_usd: float
    cap_pct: float
    violations: tuple[Violation, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.violations


def _default_effective_fn(sym: str, val: float) -> float:
    """Look-through single-name (NVDA) exposure of `val` USD held in `sym`, reusing the
    canonical primitive so this kernel and the deploy funnel agree by construction."""
    from argosy.services.deployment_funnel.look_through import effective_nvda_usd
    return float(effective_nvda_usd(sym, val))


def evaluate_single_name_cap(
    *,
    holdings_usd: dict[str, float],
    cap_pct: float,
    proposed_buys: dict[str, float] | None = None,
    cash_usd: float = 0.0,
    effective_fn: Callable[[str, float], float] = _default_effective_fn,
) -> RiskKernelResult:
    """Re-derive the POST-TRADE single-name look-through exposure and flag a cap breach.

    ``holdings_usd`` and ``proposed_buys`` map symbol -> USD; the post-trade book adds
    the proposed buys (and ``cash_usd``, which carries no single-name exposure). The
    single-name exposure is the look-through sum (direct + fund-embedded) via
    ``effective_fn``. A breach (> ``cap_pct`` beyond a small epsilon) is a BLOCK.
    """
    post: dict[str, float] = {k: float(v) for k, v in (holdings_usd or {}).items()}
    for sym, amt in (proposed_buys or {}).items():
        post[sym] = post.get(sym, 0.0) + float(amt)

    book_usd = round(sum(post.values()) + float(cash_usd), 2)
    single_usd = round(sum(effective_fn(sym, val) for sym, val in post.items()), 2)
    pct = round((single_usd / book_usd * 100.0) if book_usd > 0 else 0.0, 4)

    violations: list[Violation] = []
    if pct > cap_pct + _PCT_EPS:
        violations.append(Violation(
            code="single_name_lookthrough_cap",
            detail=(
                f"single-name look-through exposure {pct:.1f}% "
                f"(${single_usd:,.0f} of ${book_usd:,.0f}) exceeds the {cap_pct:.1f}% cap "
                "— direct + fund-embedded (e.g. NVDA inside CSPX/R1GR)."
            ),
        ))

    return RiskKernelResult(
        single_name_lookthrough_pct=pct,
        single_name_lookthrough_usd=single_usd,
        book_usd=book_usd,
        cap_pct=float(cap_pct),
        violations=tuple(violations),
    )


def target_holdings_from_doc(doc, *, scale: float = 100.0) -> dict[str, float]:
    """Convert a plan's TARGET allocation into a symbol->notional map (scale-invariant;
    defaults to a 100-unit book so the values read as target %s). Each class's
    ``target_pct`` is split across its instruments by ``weight_within_class_pct`` (equal
    weights if unset). This lets the kernel measure the *target end-state* on a
    look-through basis — a 12% direct NVDA sleeve plus embedded NVDA in the US sleeves."""
    out: dict[str, float] = {}
    for c in getattr(doc, "classes", []) or []:
        tp = float(getattr(c, "target_pct", 0.0) or 0.0)
        instrs = getattr(c, "instruments", []) or []
        if not instrs:
            continue
        wsum = sum(float(getattr(i, "weight_within_class_pct", 0.0) or 0.0) for i in instrs)
        for i in instrs:
            sym = (getattr(i, "symbol", "") or "").strip()
            if not sym:
                continue
            w = float(getattr(i, "weight_within_class_pct", 0.0) or 0.0)
            share = (w / wsum) if wsum > 0 else (1.0 / len(instrs))
            out[sym] = out.get(sym, 0.0) + tp * share * (scale / 100.0)
    return out


def evaluate_plan_target_single_name_cap(
    doc,
    *,
    cap_pct: float | None = None,
    effective_fn: Callable[[str, float], float] = _default_effective_fn,
) -> RiskKernelResult:
    """Does the plan's TARGET allocation breach its own single-name cap on a look-through
    basis? (The plan-fleet miss: 12% direct NVDA + embedded NVDA in CSPX/R1GR can exceed
    the 13% cap.) Blind: re-derived from the doc, not from any fleet claim."""
    cap = float(cap_pct if cap_pct is not None else getattr(doc, "nvda_cap_pct", 13.0) or 13.0)
    target = target_holdings_from_doc(doc)
    return evaluate_single_name_cap(holdings_usd=target, cap_pct=cap, effective_fn=effective_fn)


__all__ = [
    "Violation", "RiskKernelResult", "evaluate_single_name_cap",
    "target_holdings_from_doc", "evaluate_plan_target_single_name_cap",
]
