"""The canonical, instrument-level, time-varying target-allocation document.

This is the single structured object every surface reads. It is authored by the
deterministic ``allocation_plan`` engine (not an LLM), persisted on the plan
version, and projected — never recomputed — by ``/plan``, ``/portfolio`` and
``/retirement``. Three properties make it the source of truth:

- **instrument-level** — each class names its tickers (``instruments``),
- **canonical** — engine-authored with the panel's agreement/dissent recorded,
- **time-varying** — a quarterly ``glide`` from today's book to the target.

See ``docs/design/SDD.md`` section 20 (the allocation model) and the realignment
roadmap. T1.1 defines the schema; ``build_target_allocation_doc`` (T1.3) fills it.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel


class AllocationInstrument(BaseModel):
    """A named holding within an asset class (e.g. ``VOO`` inside the core sleeve)."""

    symbol: str
    role: Literal["primary", "alt", "hold", "exit"]
    weight_within_class_pct: float  # sums to 100 within its class
    rationale: str = ""


class AllocationClassDoc(BaseModel):
    """One asset class: its target weight, its instruments, and the panel's notes."""

    label: str  # "US broad-market core"
    snapshot_category: str  # "Core Equity" — the exact snapshot-anchor key
    sigma_class: str
    target_pct: float  # % of the FULL tradeable book (classes sum to ~100)
    instruments: list[AllocationInstrument]
    agreement: str = ""
    rationale: str = ""
    dissent: str = ""


class GlideWaypoint(BaseModel):
    """The target composition at one quarter on the transition path."""

    quarter: int
    date: date
    composition_pct_by_class: dict[str, float]  # sums to 100 each quarter


class TargetAllocationDoc(BaseModel):
    """The canonical plan-level allocation: classes + their instruments + the glide."""

    schema_version: int = 1
    basis: str = "full tradeable book"
    anchor_sigma: float
    blended_sigma: float
    nvda_cap_pct: float  # the 13% ceiling
    fi_pct: float  # derived
    provenance: str
    classes: list[AllocationClassDoc]
    glide: list[GlideWaypoint]  # today -> target over N quarters
