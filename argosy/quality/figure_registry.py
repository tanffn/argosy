"""Canonical figure registry — metadata layer (Phase 1a).

Wraps the deterministic resolver output (``ResolvedPlanNumbers``) with the
ownership + classification + evidence + validation metadata that makes
"one accountable owner per figure" enforceable. Pure: no DB, no LLM, no new
derivation math. See docs/superpowers/specs/2026-06-19-financial-advisory-team-design.md.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from enum import Enum


class FigureKind(str, Enum):
    """How a figure is produced — sets the validation it must clear."""

    SOURCE_FACT = "source_fact"
    ASSUMPTION = "assumption"
    FORMULA_RESULT = "formula_result"
    MODEL_PROJECTION = "model_projection"
    INTERPRETATION = "interpretation"
    RECOMMENDATION = "recommendation"


class Materiality(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class OwnerRole(str, Enum):
    """Generalized firm roster — client-agnostic."""

    LEAD_PLANNER = "lead_planner"
    CLIENT_DISCOVERY = "client_discovery"
    DATA_STEWARD = "data_steward"
    BALANCE_SHEET = "balance_sheet"
    CASH_FLOW = "cash_flow"
    TAX = "tax"
    INVESTMENT = "investment"
    RETIREMENT_FI = "retirement_fi"
    INSURANCE_RISK = "insurance_risk"
    ESTATE = "estate"
    EQUITY_COMP = "equity_comp"
    COMPLIANCE = "compliance"
    COMMITTEE = "committee"
    OPERATIONS = "operations"


@dataclass(frozen=True)
class FigureRecord:
    """One owned figure (or non-numeric claim) with full semantic identity,
    provenance, and validation status. See the spec's FigureRecord shape."""

    id: str
    value: float | str | None
    unit: str
    kind: FigureKind
    owner: OwnerRole
    consult: tuple[OwnerRole, ...] = ()
    basis: str | None = None
    scenario: str | None = None
    as_of: str | None = None
    jurisdiction: str | None = None
    policy_version: str | None = None
    precision: str | None = None
    inputs: tuple[str, ...] = ()
    method: str | None = None
    evidence: tuple[str, ...] = ()
    source_freshness: str | None = None
    confidence: str | None = None
    materiality: Materiality = Materiality.MEDIUM
    validated_by: str = "none"   # none | resolver | recompute | cross_model_rederivation
    status: str = "pending"      # pending | resolved | blocked
    version: int = 0
    timestamp: str | None = None
    # True when no owner could be assigned (owner_for fell back to the Lead). A
    # registry consumer MUST fail-closed on these — an un-owned figure can never
    # be `resolved` (codex impl review #2).
    uncategorized: bool = False

    def __post_init__(self) -> None:
        # Coerce the tuple fields so a caller passing a mutable list cannot later
        # mutate this "frozen" record's telemetry (codex impl review #7).
        for fld in ("consult", "inputs", "evidence"):
            val = getattr(self, fld)
            if not isinstance(val, tuple):
                object.__setattr__(self, fld, tuple(val))
        # Normalize materiality to the enum so `==`/`is` are robust after a
        # JSON/string hydration (codex impl review #5).
        if not isinstance(self.materiality, Materiality):
            object.__setattr__(self, "materiality", Materiality(self.materiality))


@dataclass(frozen=True)
class OwnerSpec:
    owner: OwnerRole
    kind: FigureKind
    materiality: Materiality = Materiality.MEDIUM
    consult: tuple[OwnerRole, ...] = ()
    basis: str | None = None
    uncategorized: bool = False


_R, _T, _I, _B, _C = (
    OwnerRole.RETIREMENT_FI, OwnerRole.TAX, OwnerRole.INVESTMENT,
    OwnerRole.BALANCE_SHEET, OwnerRole.CASH_FLOW,
)
_FR, _AS, _MP = FigureKind.FORMULA_RESULT, FigureKind.ASSUMPTION, FigureKind.MODEL_PROJECTION
_HI, _MED, _LO = Materiality.HIGH, Materiality.MEDIUM, Materiality.LOW

# Explicit per-key owners (override prefix rules).
OWNER_MAP: dict[str, OwnerSpec] = {
    "portfolio.net_worth_nis": OwnerSpec(_B, _FR, _HI, basis="investable"),
    "portfolio.liquid_net_worth_nis": OwnerSpec(_B, _FR, _HI, basis="liquid"),
    "portfolio.total_net_worth_incl_residence_nis": OwnerSpec(_B, _FR, _HI, basis="total"),
    "portfolio.usd_exposure_nis": OwnerSpec(_B, _FR, _MED),
    "retirement.fi_target_nis": OwnerSpec(_R, _FR, _HI),
    "retirement.fi_total_capital_nis": OwnerSpec(_R, _FR, _HI),
    "retirement.fi_margin_signed_nis": OwnerSpec(_R, _FR, _HI),
    "retirement.fire_bridge_nis": OwnerSpec(_R, _FR, _MED),
    "retirement.fi_age": OwnerSpec(_R, _MP, _HI),
    "retirement.earliest_safe_age": OwnerSpec(_R, _MP, _HI),
    "retirement.preservation_age": OwnerSpec(_R, _MP, _MED),
    "retirement.pension_unlock_age": OwnerSpec(_R, _AS, _LO),
    "retirement.mc_horizon_age": OwnerSpec(_R, _AS, _LO),
    "retirement.required_real_yield_pct": OwnerSpec(_R, _AS, _HI),
    "retirement.return_assumption_pct": OwnerSpec(_I, _AS, _HI),
    "retirement.liquidity_reserve_nis": OwnerSpec(_R, _FR, _MED),
    "spend.fi_basis_nis": OwnerSpec(_C, _FR, _HI),
    "savings.annual_net_nis": OwnerSpec(_C, _FR, _HI),
    "spend.annual_t12_nis": OwnerSpec(_C, _FR, _MED),
    "concentration.nvda_cap_pct": OwnerSpec(_I, _FR, _HI, consult=(_T, _R)),
    "concentration.nvda_target_pct": OwnerSpec(_I, _AS, _HI, consult=(_T, _R)),
    "concentration.nvda_current_pct": OwnerSpec(_I, _FR, _MED),
    "concentration.nvda_target_sh": OwnerSpec(_I, _FR, _MED, consult=(_T,)),
    "concentration.nvda_sell_sh": OwnerSpec(_I, _FR, _MED, consult=(_T,)),
    "concentration.nvda_eligible_now_sh": OwnerSpec(_T, _FR, _MED),
    "concentration.nvda_analyst_floor_pct": OwnerSpec(_I, _FR, _MED),
    "concentration.us_situs_estate_exposure_nis": OwnerSpec(OwnerRole.ESTATE, _FR, _HI),
    # The collections/graph also key the same estate-tax figure differently;
    # own it explicitly so the broad `concentration.` prefix can't claim it as
    # an Investment figure (codex impl review #3).
    "concentration.us_situs_estate_nis": OwnerSpec(OwnerRole.ESTATE, _FR, _HI),
    "spend.mc_central_nis": OwnerSpec(_C, _MP, _HI),
    "spend.mc_stress_nis": OwnerSpec(_C, _MP, _MED),
}

# Prefix rules own whole namespaces (incl. dynamic keys). First match wins.
_PREFIX_RULES: tuple[tuple[str, OwnerSpec], ...] = (
    ("estate.", OwnerSpec(OwnerRole.ESTATE, _FR, _HI)),  # before concentration.
    ("allocation.", OwnerSpec(_I, _FR, _MED)),
    ("concentration.", OwnerSpec(_I, _FR, _MED)),
    ("fx.", OwnerSpec(_B, _FR, _MED)),
    ("statutory.", OwnerSpec(_R, _AS, _LO)),
    ("mc.", OwnerSpec(_R, _AS, _LO)),
    ("spend.", OwnerSpec(_C, _FR, _MED)),
    ("savings.", OwnerSpec(_C, _FR, _MED)),
    ("retirement.", OwnerSpec(_R, _FR, _MED)),
    ("portfolio.", OwnerSpec(_B, _FR, _MED)),
    ("tax.", OwnerSpec(_T, _AS, _MED)),
)


def owner_for(key: str) -> OwnerSpec:
    """Resolve a figure-id to its OwnerSpec: explicit OWNER_MAP first, then a
    prefix rule, else a flagged fallback (LEAD_PLANNER, uncategorized=True) so a
    completeness test catches a genuinely un-owned key — never a hard crash."""
    spec = OWNER_MAP.get(key)
    if spec is not None:
        return spec
    for prefix, rule in _PREFIX_RULES:
        if key.startswith(prefix):
            return rule
    return OwnerSpec(OwnerRole.LEAD_PLANNER, FigureKind.FORMULA_RESULT,
                     Materiality.MEDIUM, uncategorized=True)


_DETERMINISTIC_KINDS = {FigureKind.FORMULA_RESULT, FigureKind.SOURCE_FACT}
_DETERMINISTIC_CLEARANCE = {"resolver", "recompute"}


def _publishable_value(value) -> bool:
    """A value may be published only if it is a non-empty claim string or a
    FINITE number — never nan/inf (codex impl review #4)."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bool):  # bool is an int subclass; not a figure value
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    return False


def validate_figure(record: FigureRecord) -> FigureRecord:
    """Return ``record`` with ``status`` set by the materiality-gated rules.

    Fail-closed throughout. None/non-finite value -> pending. An un-owned
    (``uncategorized``) figure -> blocked (it has no accountable owner). Every
    publishable figure — deterministic OR judgment — requires evidence. A
    deterministic kind also needs resolver/recompute clearance; a judgment kind
    with LOW materiality resolves on evidence, HIGH/MEDIUM needs the cross-model
    re-derivation (else pending, awaiting the Phase-3 validator)."""
    if record.value is None or not _publishable_value(record.value):
        return dataclasses.replace(record, status="pending")

    # An un-owned figure can never ship — fail-closed (codex #2).
    if record.uncategorized:
        return dataclasses.replace(record, status="blocked")

    # Every published figure must carry evidence (codex #1).
    if not record.evidence:
        return dataclasses.replace(record, status="blocked")

    if record.kind in _DETERMINISTIC_KINDS:
        status = "resolved" if record.validated_by in _DETERMINISTIC_CLEARANCE else "pending"
        return dataclasses.replace(record, status=status)

    # judgment kinds (evidence already confirmed present)
    if record.materiality == Materiality.LOW:
        return dataclasses.replace(record, status="resolved")
    status = "resolved" if record.validated_by == "cross_model_rederivation" else "pending"
    return dataclasses.replace(record, status=status)


def build_figure_registry(resolved, *, today: str | None = None) -> dict[str, FigureRecord]:
    """Wrap a ``ResolvedPlanNumbers`` manifest into validated ``FigureRecord``s.

    Each value is annotated from ``owner_for`` (owner/kind/materiality/consult/
    basis). Deterministic kinds are stamped ``validated_by="resolver"`` (the
    resolver's deterministic computation — Phase 1c/3 upgrades this to a true
    raw-source recompute); judgment kinds get ``"none"`` so material ones land
    ``pending`` for the Phase-3 cross-model validator. ``owner_for`` never raises,
    so no produced key crashes the build; an uncategorized key is owned by the
    Lead with ``uncategorized=True`` (caught by the coverage test)."""
    out: dict[str, FigureRecord] = {}
    for key, rv in resolved.values.items():
        spec = owner_for(key)
        validated_by = "resolver" if spec.kind in _DETERMINISTIC_KINDS else "none"
        loc = getattr(rv, "source_locator", None)
        rec = FigureRecord(
            id=key,
            value=rv.value,
            unit=rv.unit,
            kind=spec.kind,
            owner=spec.owner,
            consult=spec.consult,
            basis=spec.basis,
            method=getattr(rv, "formula", None),
            evidence=(loc,) if loc else (),
            confidence=getattr(rv, "confidence", None),
            materiality=spec.materiality,
            validated_by=validated_by,
            as_of=today,
            uncategorized=spec.uncategorized,
        )
        out[key] = validate_figure(rec)
    return out
