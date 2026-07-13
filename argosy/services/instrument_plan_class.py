"""Durable instrument → plan-class mapping (Block H).

Canonical sleeve resolution for the Sleeve column, Current-allocation-vs-
plan-target, and deploy sleeve-gap table. Precedence:

    1. live plan instrument list (TargetAllocationDoc)
    2. DB row source=owner
    3. DB row source=fleet
    4. DB row source=plan
    5. Unmapped — needs classification

Cash (asset_type) maps to the Cash & T-bills sleeve without a ticker row.
No asset_type→US-broad catch-all remains.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.state.models import InstrumentPlanClass

UNMAPPED_LABEL = "Unmapped — needs classification"
CASH_LABEL = "Cash & T-bills (incl. ILS tranche)"

SOURCE_PLAN = "plan"
SOURCE_FLEET = "fleet"
SOURCE_OWNER = "owner"
_SOURCE_RANK = {SOURCE_OWNER: 3, SOURCE_FLEET: 2, SOURCE_PLAN: 1}

# Deterministic known-holdings seed (acceptance gate). Ambiguous names stay
# Unmapped for owner review — do NOT invent a global-core class the plan lacks.
# IBTA is NOT here: plan-first keeps it on Cash.
_DETERMINISTIC_FLEET_SEED: dict[str, tuple[str, str, str]] = {
    # symbol: (plan_class_label, what_it_is, why_held)
    "SCHD": (
        "Dividend-quality income",
        "Schwab US Dividend Equity ETF — high-quality US dividend payers.",
        "Held substitute covering the Dividend-quality income sleeve (plan primary: FUSA).",
    ),
    "VOO": (
        "US broad-market core",
        "Vanguard S&P 500 ETF (US-domiciled).",
        "Held US-core exposure; plan primary is estate-safe CSPX (UCITS).",
    ),
    "SGOV": (
        CASH_LABEL,
        "iShares 0–3 Month Treasury Bond ETF — T-bill cash equivalent.",
        "Cash & T-bills runway / dry-powder sleeve.",
    ),
    "O": (
        "Real assets (REIT/TIPS)",
        "Realty Income — US-listed equity REIT.",
        "Real-assets sleeve exposure (listed property).",
    ),
    "IWDP": (
        "Real assets (REIT/TIPS)",
        "iShares Developed Markets Property Yield UCITS ETF.",
        "Real-assets sleeve — estate-safe listed property.",
    ),
    "CNDX": (
        "Global quality growth (ex-NVDA-dense)",
        "iShares NASDAQ-100 UCITS ETF.",
        "Growth/momentum sleeve exposure (UCITS).",
    ),
    "QQQM": (
        "Global quality growth (ex-NVDA-dense)",
        "Invesco NASDAQ-100 ETF (US-domiciled).",
        "Growth sleeve exposure; prefer UCITS CNDX for new cash when migrating.",
    ),
    "SCHG": (
        "Global quality growth (ex-NVDA-dense)",
        "Schwab US Large-Cap Growth ETF.",
        "Growth sleeve exposure (US-domiciled).",
    ),
    "SPMO": (
        "Global quality growth (ex-NVDA-dense)",
        "Invesco S&P 500 Momentum ETF.",
        "Momentum factor within the growth sleeve.",
    ),
    "SPMV": (
        "US low-volatility equity",
        "Invesco S&P 500 Low Volatility ETF.",
        "Plan US low-volatility sleeve (or held substitute).",
    ),
}


@dataclass(frozen=True)
class ClassificationEntry:
    symbol: str
    plan_class_label: str
    source: str
    confidence: str = "HIGH"
    what_it_is: str = ""
    why_held: str = ""
    updated_at: datetime | None = None


def classification_fingerprint(session: Session, user_id: str) -> tuple:
    """Cheap staleness key for ``user_id``'s instrument→class map.

    ``(row_count, max_updated_at_iso)`` — busts a derived-cache key on any
    seed / owner-reassign / fleet write, so cached surfaces that group by
    ``resolve_sleeve_label`` recompute. Returns ``(0, None)`` on any error
    (degrades to a stable-but-recomputable key). NOT a security boundary.
    """
    from sqlalchemy import func

    try:
        row = session.execute(
            select(
                func.count(InstrumentPlanClass.id),
                func.max(InstrumentPlanClass.updated_at),
            ).where(InstrumentPlanClass.user_id == user_id)
        ).one()
        stamp = row[1].isoformat() if row[1] is not None else None
        return (int(row[0] or 0), stamp)
    except Exception:  # noqa: BLE001 — never raise from a cache-key helper
        return (0, None)


def load_classification_map(
    session: Session, user_id: str
) -> dict[str, ClassificationEntry]:
    """Load all map rows for ``user_id``, keyed by uppercased symbol."""
    rows = session.execute(
        select(InstrumentPlanClass).where(InstrumentPlanClass.user_id == user_id)
    ).scalars().all()
    out: dict[str, ClassificationEntry] = {}
    for r in rows:
        sym = (r.symbol or "").strip().upper()
        if not sym:
            continue
        out[sym] = ClassificationEntry(
            symbol=sym,
            plan_class_label=r.plan_class_label,
            source=r.source,
            confidence=r.confidence or "HIGH",
            what_it_is=r.what_it_is or "",
            why_held=r.why_held or "",
            updated_at=r.updated_at,
        )
    return out


def resolve_sleeve_label(
    symbol: str,
    asset_type: str = "",
    details: str = "",
    plan_symbol_labels: dict[str, str] | None = None,
    classification_map: dict[str, ClassificationEntry] | None = None,
) -> str:
    """Single canonical sleeve label — Sleeve column / allocation / deploy gaps.

    Never dumps unknown equity into US-broad. Cash asset_type is the only
    non-map structural shortcut (cash rows often carry symbol ``-``).
    """
    at = (asset_type or "").strip().lower()
    if at in ("cash", "money market") or at.startswith("cash"):
        return CASH_LABEL

    sym = (symbol or "").strip().upper()
    # Blank / non-tradable markers with no cash type → unmapped (not a dump).
    if not sym or sym in {"-", "—", "N/A", "NA", "NONE"}:
        return UNMAPPED_LABEL

    if plan_symbol_labels and sym in plan_symbol_labels:
        return plan_symbol_labels[sym]

    if classification_map and sym in classification_map:
        entry = classification_map[sym]
        # Prefer owner > fleet > plan among stored rows (single row per symbol,
        # but source encodes who last wrote; owner always wins if present).
        if entry.source in (SOURCE_OWNER, SOURCE_FLEET, SOURCE_PLAN):
            return entry.plan_class_label

    return UNMAPPED_LABEL


def _upsert_row(
    session: Session,
    *,
    user_id: str,
    symbol: str,
    plan_class_label: str,
    source: str,
    confidence: str = "HIGH",
    what_it_is: str = "",
    why_held: str = "",
    overwrite_sources: frozenset[str] | None = None,
) -> InstrumentPlanClass | None:
    """Upsert one mapping row. Never overwrites ``owner`` unless source=owner.

    ``overwrite_sources`` limits which existing sources may be replaced
    (default: same source or lower rank than the incoming source).
    """
    sym = (symbol or "").strip().upper()
    if not sym or sym in {"-", "—"}:
        return None
    now = datetime.now(timezone.utc)
    existing = session.execute(
        select(InstrumentPlanClass).where(
            InstrumentPlanClass.user_id == user_id,
            InstrumentPlanClass.symbol == sym,
        )
    ).scalar_one_or_none()

    if existing is not None:
        if existing.source == SOURCE_OWNER and source != SOURCE_OWNER:
            return existing  # owner outranks fleet/plan writes
        allowed = overwrite_sources
        if allowed is not None and existing.source not in allowed:
            return existing
        if source != SOURCE_OWNER:
            # Don't let plan seed clobber a fleet classification.
            if _SOURCE_RANK.get(existing.source, 0) > _SOURCE_RANK.get(source, 0):
                return existing
        existing.plan_class_label = plan_class_label
        existing.source = source
        existing.confidence = confidence
        if what_it_is:
            existing.what_it_is = what_it_is
        if why_held:
            existing.why_held = why_held
        existing.updated_at = now
        return existing

    row = InstrumentPlanClass(
        user_id=user_id,
        symbol=sym,
        plan_class_label=plan_class_label,
        source=source,
        confidence=confidence,
        what_it_is=what_it_is or "",
        why_held=why_held or "",
        updated_at=now,
    )
    session.add(row)
    return row


def seed_from_plan(
    session: Session, user_id: str, doc
) -> int:
    """Upsert plan instruments as source=plan. Returns rows written/updated."""
    from argosy.services.allocation_plan import normalize_sleeve_label

    n = 0
    if doc is None:
        return 0
    for c in getattr(doc, "classes", []) or []:
        label = normalize_sleeve_label(c.label)
        rationale = (getattr(c, "rationale", None) or "")[:500]
        for inst in getattr(c, "instruments", []) or []:
            sym = (getattr(inst, "symbol", "") or "").strip().upper()
            if not sym:
                continue
            what = (
                f"Plan instrument for '{label}'"
                + (f" ({getattr(inst, 'role', '')})" if getattr(inst, "role", None) else "")
            )
            why = rationale or f"Named in the current plan under {label}."
            if _upsert_row(
                session,
                user_id=user_id,
                symbol=sym,
                plan_class_label=label,
                source=SOURCE_PLAN,
                confidence="HIGH",
                what_it_is=what,
                why_held=why,
            ):
                n += 1
    session.flush()
    return n


def seed_deterministic_known_holdings(
    session: Session,
    user_id: str,
    *,
    held_symbols: set[str] | None = None,
) -> int:
    """Seed fleet-source rows for known held substitutes (acceptance gate).

    Skips symbols not in ``held_symbols`` when that set is provided. Never
    overwrites owner rows. Does not invent classes the plan lacks (world
    funds without a global-core sleeve stay Unmapped for owner review).
    """
    n = 0
    held = {s.strip().upper() for s in (held_symbols or set()) if s}
    for sym, (label, what, why) in _DETERMINISTIC_FLEET_SEED.items():
        if held and sym not in held:
            continue
        if _upsert_row(
            session,
            user_id=user_id,
            symbol=sym,
            plan_class_label=label,
            source=SOURCE_FLEET,
            confidence="HIGH",
            what_it_is=what,
            why_held=why,
        ):
            n += 1
    session.flush()
    return n


def owner_reassign(
    session: Session,
    user_id: str,
    symbol: str,
    plan_class_label: str,
    *,
    what_it_is: str | None = None,
    why_held: str | None = None,
) -> InstrumentPlanClass:
    """Owner edit — always source=owner, outranks fleet/plan rows."""
    sym = (symbol or "").strip().upper()
    label = (plan_class_label or "").strip()
    if not sym or not label:
        raise ValueError("symbol and plan_class_label are required")
    existing = session.execute(
        select(InstrumentPlanClass).where(
            InstrumentPlanClass.user_id == user_id,
            InstrumentPlanClass.symbol == sym,
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if existing is None:
        row = InstrumentPlanClass(
            user_id=user_id,
            symbol=sym,
            plan_class_label=label,
            source=SOURCE_OWNER,
            confidence="HIGH",
            what_it_is=what_it_is or "",
            why_held=why_held or "",
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return row
    existing.plan_class_label = label
    existing.source = SOURCE_OWNER
    existing.confidence = "HIGH"
    if what_it_is is not None:
        existing.what_it_is = what_it_is
    if why_held is not None:
        existing.why_held = why_held
    existing.updated_at = now
    session.flush()
    return existing


def list_unmapped_held(
    *,
    held_symbols: set[str],
    plan_symbol_labels: dict[str, str],
    classification_map: dict[str, ClassificationEntry],
) -> list[str]:
    """Held tradable symbols that resolve to Unmapped."""
    out: list[str] = []
    for raw in sorted(held_symbols):
        sym = (raw or "").strip().upper()
        if not sym or sym in {"-", "—"}:
            continue
        label = resolve_sleeve_label(
            sym,
            plan_symbol_labels=plan_symbol_labels,
            classification_map=classification_map,
        )
        if label == UNMAPPED_LABEL:
            out.append(sym)
    return out


def seed_all(
    session: Session,
    user_id: str,
    doc,
    held_symbols: set[str] | None = None,
    *,
    commit: bool = True,
) -> dict[str, int]:
    """Plan seed + deterministic known-holdings. Fleet LLM blurbs = follow-up."""
    n_plan = seed_from_plan(session, user_id, doc)
    n_fleet = seed_deterministic_known_holdings(
        session, user_id, held_symbols=held_symbols,
    )
    if commit:
        session.commit()
    else:
        session.flush()
    return {"plan": n_plan, "fleet_deterministic": n_fleet}


__all__ = [
    "UNMAPPED_LABEL",
    "CASH_LABEL",
    "SOURCE_PLAN",
    "SOURCE_FLEET",
    "SOURCE_OWNER",
    "ClassificationEntry",
    "classification_fingerprint",
    "load_classification_map",
    "resolve_sleeve_label",
    "seed_from_plan",
    "seed_deterministic_known_holdings",
    "owner_reassign",
    "list_unmapped_held",
    "seed_all",
]
