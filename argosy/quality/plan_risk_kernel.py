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


__all__ = ["Violation", "RiskKernelResult", "evaluate_single_name_cap"]
