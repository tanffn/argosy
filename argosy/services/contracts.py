"""Versioned cross-phase domain contracts — defined once, imported everywhere.

These value objects are the seams between the deterministic allocation engine
(Slice 1a), the allocation agent (Slice 1b), and the high-potential discovery
funnel (Slice 2). Centralising them here gives three guarantees the codex review
required:

- **One canonical candidate identity.** :func:`candidate_fingerprint` is the
  single fingerprint used by both the engine and the agent's reconciliation
  gate, so a task can never wrap a same-dollar-but-different-instrument
  candidate or silently duplicate one. Identity = kind + every leg's
  (side, symbol, account, currency, funding, rounded notional); NOT
  notional-only.
- **Versioned serialization.** :func:`serialize_candidate` stamps
  :data:`CONTRACTS_SCHEMA_VERSION`; :func:`deserialize_candidate` refuses a
  newer schema rather than silently mis-reading it.
- **Stable names.** Everything downstream imports these types from here, so the
  engine/agent/funnel modules re-export rather than redefine them.

All value objects are frozen dataclasses (the repo convention for pure value
objects, e.g. ``ProposalDelta``); pydantic wire DTOs for the API surface live at
the bottom of the module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

CONTRACTS_SCHEMA_VERSION = 1


# --- allocation value objects (Slice 1a output / 1b input) -----------------

@dataclass(frozen=True)
class AllocationLeg:
    side: str                 # "BUY" | "SELL"
    symbol: str
    account_id: str
    currency: str
    notional_usd: float
    funding_source: str       # "cash" | "trim_proceeds"
    quantity: float | None = None


@dataclass(frozen=True)
class AllocationCandidate:
    kind: str                 # "BUY" | "TRIM" | "SWAP"
    legs: tuple[AllocationLeg, ...]
    horizon: str              # "now" | "this_quarter" | "later"
    est_tax_nis: float | None = None
    surtax_split_suggested: bool = False
    rationale: str = ""
    cites: tuple[str, ...] = ()

    @property
    def total_notional_usd(self) -> float:
        return round(sum(abs(l.notional_usd) for l in self.legs), 2)

    def fingerprint(self) -> tuple:
        return candidate_fingerprint(self)


# --- allocation agent output (Slice 1b) ------------------------------------

@dataclass(frozen=True)
class ExecutableTask:
    seq: int
    candidate: AllocationCandidate
    horizon: Literal["now", "this_quarter", "later"]
    pace: Literal["lump", "tranched"]
    pace_rationale: str
    rationale: str
    cites: tuple[str, ...] = ()


# --- discovery funnel value objects (Slice 2) ------------------------------

@dataclass(frozen=True)
class EstimatorVerdict:
    """Cheap Sonnet triage screen for a single radar ticker."""

    ticker: str
    go: bool
    conviction: str           # "HIGH" | "MED" | "LOW"
    sentiment: float          # -1.0 .. 1.0
    one_line: str


@dataclass(frozen=True)
class FleetPick:
    """A radar ticker that survived to a full Opus fleet grading."""

    ticker: str
    conviction: str           # "HIGH" | "MED" | "LOW"
    thesis_md: str
    verdict: str              # "BUY" | "WATCH" | "PASS"
    cites: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScanState:
    """Per-(user, ticker) discovery memory for smart-refresh diffing.

    The domain shape; the Slice-2 persistence layer mirrors these fields onto a
    SQLAlchemy row (migration 0066). Timestamps are ISO-8601 strings so the
    contract stays serialization-clean and clock-free."""

    user_id: str
    ticker: str
    last_score: float
    estimator: EstimatorVerdict | None = None
    fleet: FleetPick | None = None
    status: str = "active"            # "active" | "quarantined" | "dropped"
    rank: int | None = None
    quarantine_reason: str = ""
    radar_fingerprint: str = ""
    last_radar_at: str | None = None
    last_estimated_at: str | None = None
    last_fleet_at: str | None = None
    last_seen_at: str | None = None


# --- canonical candidate identity ------------------------------------------

def candidate_fingerprint(c: AllocationCandidate) -> tuple:
    """The identity of a candidate — kind + the material numeric fields
    (est_tax_nis, surtax flag) + every leg's (side, symbol, account, currency,
    funding, rounded notional, rounded quantity), order-insensitive across legs.

    Notional-only matching is NOT enough (codex): it would let an agent swap a
    same-dollar ticker, duplicate a candidate, or alter tax/quantity and still
    reconcile. The gate enforces the "agent invents no numbers" guarantee, so it
    must key on every number a tampered candidate could change."""
    return (
        c.kind,
        round(c.est_tax_nis, 2) if c.est_tax_nis is not None else None,
        bool(c.surtax_split_suggested),
        tuple(sorted(
            (l.side, l.symbol, l.account_id, l.currency, l.funding_source,
             round(l.notional_usd, 2),
             # total-orderable quantity key: (is_none, value) — None and float
             # legs must sort without a TypeError while staying distinguishable
             # (codex 1b r2).
             (l.quantity is None, round(l.quantity, 6) if l.quantity is not None else 0.0))
            for l in c.legs)),
    )


# --- versioned serialization -----------------------------------------------

def serialize_candidate(c: AllocationCandidate) -> dict[str, Any]:
    """A version-stamped plain-dict form of a candidate (for persistence / wire)."""
    return {
        "schema_version": CONTRACTS_SCHEMA_VERSION,
        "kind": c.kind,
        "horizon": c.horizon,
        "est_tax_nis": c.est_tax_nis,
        "surtax_split_suggested": c.surtax_split_suggested,
        "rationale": c.rationale,
        "cites": list(c.cites),
        "legs": [
            {
                "side": l.side, "symbol": l.symbol, "account_id": l.account_id,
                "currency": l.currency, "notional_usd": l.notional_usd,
                "funding_source": l.funding_source, "quantity": l.quantity,
            }
            for l in c.legs
        ],
    }


def deserialize_candidate(blob: dict[str, Any]) -> AllocationCandidate:
    """Inverse of :func:`serialize_candidate`. Refuses a newer schema version."""
    ver = blob.get("schema_version", CONTRACTS_SCHEMA_VERSION)
    if ver > CONTRACTS_SCHEMA_VERSION:
        raise ValueError(
            f"candidate schema_version {ver} is newer than this build "
            f"({CONTRACTS_SCHEMA_VERSION}); refusing to mis-read it")
    legs = tuple(
        AllocationLeg(
            side=l["side"], symbol=l["symbol"], account_id=l["account_id"],
            currency=l["currency"], notional_usd=l["notional_usd"],
            funding_source=l["funding_source"], quantity=l.get("quantity"),
        )
        for l in blob.get("legs", [])
    )
    return AllocationCandidate(
        kind=blob["kind"], legs=legs, horizon=blob["horizon"],
        est_tax_nis=blob.get("est_tax_nis"),
        surtax_split_suggested=blob.get("surtax_split_suggested", False),
        rationale=blob.get("rationale", ""),
        cites=tuple(blob.get("cites", ())),
    )


# --- pydantic wire DTOs (API surface) --------------------------------------

class AllocationLegDTO(BaseModel):
    side: str
    symbol: str
    account_id: str
    currency: str
    notional_usd: float
    funding_source: str
    quantity: float | None = None


class AllocationCandidateDTO(BaseModel):
    kind: str
    legs: list[AllocationLegDTO]
    horizon: str
    est_tax_nis: float | None = None
    surtax_split_suggested: bool = False
    rationale: str = ""
    cites: list[str] = []


def candidate_to_dto(c: AllocationCandidate) -> AllocationCandidateDTO:
    """Map a domain candidate onto its wire DTO (the one place this mapping lives)."""
    return AllocationCandidateDTO(
        kind=c.kind, horizon=c.horizon, est_tax_nis=c.est_tax_nis,
        surtax_split_suggested=c.surtax_split_suggested, rationale=c.rationale,
        cites=list(c.cites),
        legs=[
            AllocationLegDTO(
                side=l.side, symbol=l.symbol, account_id=l.account_id,
                currency=l.currency, notional_usd=l.notional_usd,
                funding_source=l.funding_source, quantity=l.quantity,
            )
            for l in c.legs
        ],
    )


class ExecutableTaskDTO(BaseModel):
    seq: int
    candidate: AllocationCandidateDTO
    horizon: str
    pace: str
    pace_rationale: str = ""
    rationale: str = ""
    cites: list[str] = []


def task_to_dto(t: ExecutableTask) -> ExecutableTaskDTO:
    """Map a domain ExecutableTask onto its wire DTO."""
    return ExecutableTaskDTO(
        seq=t.seq, candidate=candidate_to_dto(t.candidate), horizon=t.horizon,
        pace=t.pace, pace_rationale=t.pace_rationale, rationale=t.rationale,
        cites=list(t.cites),
    )


class EstateTagDTO(BaseModel):
    domicile: str | None
    status: str
    note: str


class DeploymentLineDTO(BaseModel):
    symbol: str
    type: str
    amount_usd: float
    timing: str
    is_new: bool
    tier: str
    horizon: str
    estate: EstateTagDTO
    cap_note: str
    net_of_tax_caveat: str
    rationale: str
    cites: list[str] = []
    held_value_usd: float = 0.0
    pace_rationale: str = ""


class DeploymentTierDTO(BaseModel):
    name: str
    cap_pct: float
    total_usd: float
    lines: list[DeploymentLineDTO]


class DataFreshnessDTO(BaseModel):
    field: str
    fetched_at: str          # ISO-8601 string
    age_seconds: float
    source: str
    is_stale: bool


class NvdaVerificationDTO(BaseModel):
    price: float
    shares: float | None
    market_cap: float | None
    consistent: bool | None
    note: str


class DeploymentMarketContextDTO(BaseModel):
    snapshot: dict[str, float]
    freshness: list[DataFreshnessDTO]
    nvda: NvdaVerificationDTO | None
    overall_age_label: str
    is_any_stale: bool


def market_context_to_dto(ctx) -> DeploymentMarketContextDTO:
    """Convert a DeploymentMarketContext dataclass to its wire DTO.

    Coerces snapshot values to plain floats (the live path stores
    (float, DataFreshness) tuples; the cached path stores plain floats).
    """
    coerced_snapshot: dict[str, float] = {}
    for k, v in ctx.snapshot.items():
        if isinstance(v, tuple):
            coerced_snapshot[k] = float(v[0])
        else:
            coerced_snapshot[k] = float(v)

    freshness_dtos = [
        DataFreshnessDTO(
            field=f.field,
            fetched_at=f.fetched_at,
            age_seconds=f.age_seconds,
            source=f.source,
            is_stale=f.is_stale,
        )
        for f in ctx.freshness
    ]

    nvda_dto: NvdaVerificationDTO | None = None
    if ctx.nvda is not None:
        nvda_dto = NvdaVerificationDTO(
            price=ctx.nvda.price,
            shares=ctx.nvda.shares,
            market_cap=ctx.nvda.market_cap,
            consistent=ctx.nvda.consistent,
            note=ctx.nvda.note,
        )

    return DeploymentMarketContextDTO(
        snapshot=coerced_snapshot,
        freshness=freshness_dtos,
        nvda=nvda_dto,
        overall_age_label=ctx.overall_age_label,
        is_any_stale=ctx.is_any_stale,
    )


class DeploymentPlanDTO(BaseModel):
    deploy_amount_usd: float
    as_of: str
    deployed_total_usd: float
    us_situs_exposed_usd: float
    us_situs_sanctioned_usd: float
    undeployed_remainder_usd: float
    market_context_age: str | None = None
    market_context: DeploymentMarketContextDTO | None = None
    tiers: list[DeploymentTierDTO]
    caveats: list[str]
    note: str = ""


def deployment_plan_to_dto(plan, market_context=None) -> DeploymentPlanDTO:
    ctx_dto = market_context_to_dto(market_context) if market_context is not None else None
    return DeploymentPlanDTO(
        deploy_amount_usd=plan.deploy_amount_usd,
        as_of=plan.as_of.isoformat(),
        deployed_total_usd=plan.deployed_total_usd,
        us_situs_exposed_usd=plan.us_situs_exposed_usd,
        us_situs_sanctioned_usd=plan.us_situs_sanctioned_usd,
        undeployed_remainder_usd=plan.undeployed_remainder_usd,
        market_context_age=plan.market_context_age,
        market_context=ctx_dto,
        tiers=[DeploymentTierDTO(
            name=t.name, cap_pct=t.cap_pct, total_usd=t.total_usd,
            lines=[DeploymentLineDTO(
                symbol=l.symbol, type=l.type, amount_usd=l.amount_usd, timing=l.timing,
                is_new=l.is_new, tier=l.tier, horizon=l.horizon,
                estate=EstateTagDTO(domicile=l.estate.domicile, status=l.estate.status,
                                    note=l.estate.note),
                cap_note=l.cap_note, net_of_tax_caveat=l.net_of_tax_caveat,
                rationale=l.rationale, cites=list(l.cites), held_value_usd=l.held_value_usd,
                pace_rationale=l.pace_rationale,
            ) for l in t.lines],
        ) for t in plan.tiers],
        caveats=list(plan.caveats), note=plan.note,
    )


__all__ = [
    "CONTRACTS_SCHEMA_VERSION",
    "AllocationLeg",
    "AllocationCandidate",
    "ExecutableTask",
    "EstimatorVerdict",
    "FleetPick",
    "ScanState",
    "candidate_fingerprint",
    "serialize_candidate",
    "deserialize_candidate",
    "AllocationLegDTO",
    "AllocationCandidateDTO",
    "candidate_to_dto",
    "ExecutableTaskDTO",
    "task_to_dto",
    "EstateTagDTO",
    "DeploymentLineDTO",
    "DeploymentTierDTO",
    "DataFreshnessDTO",
    "NvdaVerificationDTO",
    "DeploymentMarketContextDTO",
    "market_context_to_dto",
    "DeploymentPlanDTO",
    "deployment_plan_to_dto",
]
