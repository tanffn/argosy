"""GET /api/portfolio/snapshot — latest portfolio snapshot for a user.

T1.5 call-site rewiring: this route now prefers the DB-backed
``portfolio_snapshots`` table when a row exists for the user; the
filesystem walk + TSV parse is the fallback path. On a fallback, the
route also write-throughs the parsed snapshot into the DB so subsequent
requests serve from the DB (idempotent — see
``portfolio_snapshot_store.write_through_if_changed``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from argosy.api.auth import require_admin_token
from argosy.api.routes.plan import get_db
from argosy.config import get_settings
from argosy.ingest.tsv import parse_portfolio_tsv
from argosy.logging import get_logger
from argosy.services.contracts import (
    AllocationCandidateDTO,
    DeploymentPlanDTO,
    ExecutableTaskDTO,
    candidate_to_dto,
    deployment_plan_to_dto,
    task_to_dto,
)
from argosy.services.portfolio_ingest.snapshot_change import (
    run_windfall_detection_on_snapshot,
)
from argosy.services.portfolio_snapshot_store import (
    get_latest_snapshot_row,
    row_to_snapshot,
    write_through_if_changed,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])
_log = get_logger(__name__)


def _warm_derived_cache(user_id: str) -> None:
    """Fire-and-forget pre-warm of the derived cache after a NEW snapshot.

    A new portfolio snapshot bumps the derived-cache version tuple, leaving
    every /retirement + /api/overview entry cold. Warm them on a background
    thread so the user's next page load is already fast. Best-effort: never
    raises into the snapshot path.
    """
    try:
        from argosy.services import derived_cache

        derived_cache.warm_async(user_id)
    except Exception as exc:  # noqa: BLE001 — warming must never break ingest
        _log.warning(
            "portfolio_snapshot.warm_failed",
            user_id=user_id,
            error=str(exc),
        )


class PositionDTO(BaseModel):
    location: str
    currency: str
    asset_type: str  # raw/normalized source Type (drives is-cash / is-real-estate logic)
    type_label: str = ""  # canonical "structure · exposure" from the §20.4 reference (display)
    # Plan-sleeve association — same mapping as allocation-breakdown buckets.
    sleeve: str = ""
    # Block H — stored blurbs from instrument_plan_classes (hover card).
    what_it_is: str = ""
    why_held: str = ""
    classification_source: str = ""  # plan | fleet | owner | "" (unmapped/cash)
    name: str = ""  # plain-English instrument name (the cryptic-ticker description line)
    details: str
    symbol: str
    shares: float | None
    current_price: float | None
    usd_value_k: float | None
    estate_safe: bool | None = None  # True=non-US-situs, False=US-situs, None=n/a (cash)
    classified: bool = True  # False = not in the instrument reference (fail-loud: needs curation)
    # True = this row's PRICE/mark is soft-stale (last-known close published
    # without a live reprice — weekend/holiday/transient quote miss). The value
    # is shown but a consumer must NOT treat it as HIGH-confidence current money
    # (Sol BLOCK-2: mark_stale was internal-only and invisible to consumers).
    mark_stale: bool = False


class AllocationDTO(BaseModel):
    category: str
    pct: float | None
    target_pct: float | None
    delta_k: float | None


class PortfolioSnapshotDTO(BaseModel):
    snapshot_date: str | None
    fx_usd_nis: float | None
    fx_usd_eur: float | None
    total_usd_value_k: float
    positions: list[PositionDTO]
    allocations: list[AllocationDTO]
    source_path: str | None
    parse_warnings: list[str]
    # Quote/FX refresh diagnostics are not parser failures.  They are split so
    # the UI can say "using prior marks" once instead of presenting dozens of
    # successful holdings as malformed rows.
    market_data_warnings: list[str] = Field(default_factory=list)
    data_notices: list[str] = Field(default_factory=list)
    # Fail-loud: held symbols with a real ticker that the §20.4 instrument
    # reference doesn't know — so their Type / sector / ESTATE-SAFETY are
    # un-curated (a US-domiciled holding would otherwise be silently
    # estate-unflagged). The UI surfaces these for the team to classify.
    classification_warnings: list[str] = []
    # Coverage ≠ emptiness: accounts the feed mentioned vs carried forward.
    accounts_covered: list[str] = []
    accounts_carried: list[str] = []
    # Fail-loud: total book could not publish current-money marks.
    book_degraded: bool = False
    degrade_reason: str | None = None


_PORTFOLIO_TSV_HEADER_MARKER = "Bank account / funds allocation"


def _find_latest_tsv() -> Path | None:
    """Return the newest portfolio TSV under ARGOSY_HOME or None.

    Filters by the presence of the ``"Bank account / funds allocation"``
    header marker so stray small uploads (e.g. attachment placeholders
    under ``uploads/<user>/.../<timestamp>__<hash>__p.tsv``) don't shadow
    the real ``Family Finances Status - <date>.tsv`` file.
    """
    import os

    settings = get_settings()
    home = settings.home
    # Prune heavy build/VCS dirs DURING the walk (os.walk + in-place dirs edit)
    # rather than rglob-then-filter — rglob descends into .git/.venv/node_modules
    # before we can skip them, which cost ~2-5s over ARGOSY_HOME. Pruning here
    # keeps the TSV scan sub-100ms.
    _PRUNE = {"node_modules", "__pycache__"}
    candidates: list[tuple[float, Path]] = []
    for root, dirs, files in os.walk(home):
        # Skip dot-dirs (.git, .venv, .next, .superpowers, …) and known heavy dirs.
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _PRUNE]
        for fn in files:
            if not fn.lower().endswith(".tsv"):
                continue
            tsv = Path(root) / fn
            try:
                mtime = tsv.stat().st_mtime
            except OSError:  # pragma: no cover - defensive
                continue
            candidates.append((mtime, tsv))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    for _, path in candidates:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                head = f.read(4096)
        except OSError:  # pragma: no cover - defensive
            continue
        if _PORTFOLIO_TSV_HEADER_MARKER in head:
            return path
    return None


def _snapshot_to_dto(snap, doc=None, classification_map=None) -> PortfolioSnapshotDTO:
    """Translate a parsed/hydrated PortfolioSnapshot to the route DTO.

    ``doc`` (optional TargetAllocationDoc) supplies plan-instrument → sleeve
    labels so the Sleeve column matches Current-allocation-vs-plan-target.
    ``classification_map`` is the Block H DB map (owner/fleet/plan rows).
    """
    from argosy.services import instrument_reference
    from argosy.services.allocation_breakdown import (
        _plan_symbol_labels,
        resolve_sleeve_label,
    )
    from argosy.services.instrument_plan_class import UNMAPPED_LABEL
    from argosy.services.wealth_dashboard import _classify_asset_class

    # Resolve one asset_type per ticker (prefer non-blank): the hand-maintained
    # Schwab rows sometimes leave Type blank on a lot of a ticker that's typed
    # elsewhere (the $3K Schwab SCHG vs the Leumi SCHG "Growth"). Fill the blank
    # from the sibling lot so the per-account table doesn't show an empty Type.
    eff_type: dict[str, str] = {}
    for p in snap.positions:
        sym = (p.symbol or "").strip().upper()
        at = (p.asset_type or "").strip()
        if sym and at and sym not in eff_type:
            eff_type[sym] = at

    plan_labels = _plan_symbol_labels(doc)
    cmap = classification_map or {}
    positions: list[PositionDTO] = []
    classification_warnings: list[str] = []
    for p in snap.positions:
        sym = (p.symbol or "").strip().upper()
        asset_type = (p.asset_type or "").strip() or eff_type.get(sym, "")
        ref = instrument_reference.lookup(p.symbol or "", p.details or "")
        if ref is not None and ref.asset_class != _classify_asset_class(asset_type, sym):
            asset_type = ref.sector
        estate_safe = instrument_reference.estate_safe_for(p.symbol or "", p.details or "")
        type_label = instrument_reference.type_label(
            p.symbol or "", p.details or "", fallback=asset_type
        )
        name = instrument_reference.name_for(p.symbol or "", p.details or "")
        if not name:
            # Symbol-less unmanaged rows (physical cash balances, the
            # owner-estimate real-estate stub) have no ticker to name — give
            # them a human label from account + currency + asset class so the
            # surface never shows a bare "-"/blank. Display only; does not
            # change value/currency/classification.
            name = instrument_reference.fallback_label(
                location=p.location or "",
                currency=p.currency or "",
                asset_type=asset_type,
                symbol=p.symbol or "",
            )
        sleeve = resolve_sleeve_label(
            p.symbol or "",
            asset_type=asset_type,
            details=p.details or "",
            plan_symbol_labels=plan_labels,
            classification_map=cmap,
        )
        entry = cmap.get(sym)
        what = entry.what_it_is if entry else ""
        why = entry.why_held if entry else ""
        if plan_labels and sym in plan_labels:
            src = "plan"
        elif entry is not None:
            src = entry.source
        elif sleeve == UNMAPPED_LABEL:
            src = ""
        else:
            src = "cash" if sleeve.startswith("Cash") else ""
        has_ticker = bool(sym) and sym != "-"
        classified = ref is not None or not has_ticker
        if not classified:
            classification_warnings.append(sym)
        positions.append(
            PositionDTO(
                location=p.location,
                currency=p.currency,
                asset_type=asset_type,
                type_label=type_label,
                sleeve=sleeve,
                what_it_is=what,
                why_held=why,
                classification_source=src,
                name=name,
                details=p.details,
                symbol=p.symbol,
                shares=p.shares,
                current_price=p.current_price,
                usd_value_k=p.usd_value_k,
                estate_safe=estate_safe,
                classified=classified,
                mark_stale=bool(getattr(p, "mark_stale", None)),
            )
        )
    if classification_warnings:
        _log.warning(
            "portfolio: %d held symbol(s) not in the instrument reference — "
            "Type/sector/estate-safety un-curated: %s",
            len(classification_warnings),
            ", ".join(sorted(set(classification_warnings))),
        )
    allocations: list[AllocationDTO] = []
    for a in snap.allocations:
        allocations.append(
            AllocationDTO(
                category=a.category,
                pct=a.pct,
                target_pct=a.target_pct,
                delta_k=a.delta_k,
            )
        )
    raw_warnings = list(snap.parse_warnings or [])
    market_prefixes = (
        "fx_miss:",
        "fx_suspect:",
        "reprice_miss:",
        "cash_overdraft:",
    )
    notice_prefixes = (
        "SYMBOL_RENAME ",
        "fill-applied:",
        "closed_loop_expectations:",
        "BOOK_RESTORE ",
    )
    market_data_warnings = [
        warning for warning in raw_warnings if warning.startswith(market_prefixes)
    ]
    data_notices = [
        warning for warning in raw_warnings if warning.startswith(notice_prefixes)
    ]
    parse_warnings = [
        warning
        for warning in raw_warnings
        if not warning.startswith(market_prefixes)
        and not warning.startswith(notice_prefixes)
        and not warning.startswith("BOOK_DEGRADED:")
    ]
    return PortfolioSnapshotDTO(
        snapshot_date=snap.snapshot_date.isoformat() if snap.snapshot_date else None,
        fx_usd_nis=snap.fx_usd_nis,
        fx_usd_eur=snap.fx_usd_eur,
        total_usd_value_k=snap.total_usd_value_k,
        positions=positions,
        allocations=allocations,
        source_path=snap.source_path,
        parse_warnings=parse_warnings,
        market_data_warnings=market_data_warnings,
        data_notices=data_notices,
        classification_warnings=classification_warnings,
        accounts_covered=list(getattr(snap, "accounts_covered", None) or []),
        accounts_carried=list(getattr(snap, "accounts_carried", None) or []),
        book_degraded=bool(getattr(snap, "book_degraded", False)),
        degrade_reason=getattr(snap, "degrade_reason", None),
    )


def _apply_total_book_to_snap(snap, db: Session, user_id: str):
    """Rewrite snap positions/total through ``load_total_book``.

    Every surface that publishes money must go through the book loader so
    stale/unpriceable marks degrade loudly instead of emitting last-known
    values as current. Attaches ``book_degraded`` / ``degrade_reason`` on
    the snap object for DTO propagation.
    """
    from argosy.ingest.tsv import PortfolioPosition
    from argosy.services.holding_books import load_total_book

    raw = [
        (p.model_dump() if hasattr(p, "model_dump") else dict(p)) for p in (snap.positions or [])
    ]
    book = load_total_book(
        db,
        user_id,
        raw,
        snapshot_date=getattr(snap, "snapshot_date", None),
    )
    rebuilt: list[PortfolioPosition] = []
    repriced_symbols: set[str] = set()
    for d in book.total:
        if d.get("repriced"):
            symbol = str(d.get("symbol") or "").strip().upper()
            if symbol:
                repriced_symbols.add(symbol)
        known = {f for f in PortfolioPosition.model_fields}
        payload = {k: v for k, v in d.items() if k in known}
        rebuilt.append(PortfolioPosition(**payload))
    snap.positions = rebuilt
    if repriced_symbols:
        healed_warnings: list[str] = []
        for warning in list(snap.parse_warnings or []):
            if warning.startswith("reprice_miss:"):
                warned_symbol = warning.split(":", 2)[1].strip().upper()
                if warned_symbol in repriced_symbols:
                    continue
            healed_warnings.append(warning)
        snap.parse_warnings = healed_warnings
    snap.book_degraded = bool(book.degraded)
    snap.degrade_reason = book.degrade_reason
    if book.degraded and book.degrade_reason:
        warns = list(snap.parse_warnings or [])
        note = f"BOOK_DEGRADED: {book.degrade_reason}"
        if note not in warns:
            warns.append(note)
        snap.parse_warnings = warns
    return snap


def _allocations_from_doc(doc) -> list[AllocationDTO]:
    """T2.2 — the /portfolio pie AS the plan: current % from the glide's today
    anchor (q0), target % from its endpoint, one row per glide label. Same
    labels + values the /plan glidepath renders, so the two reconcile by
    construction (the cross-surface guardrail)."""
    q0 = doc.glide[0].composition_pct_by_class
    qN = doc.glide[-1].composition_pct_by_class
    labels = list(dict.fromkeys(list(qN) + list(q0)))
    return [
        AllocationDTO(
            category=lbl,
            pct=round(q0.get(lbl, 0.0), 2),
            target_pct=round(qN.get(lbl, 0.0), 2),
            delta_k=None,
        )
        for lbl in labels
    ]


def _project_canonical_allocations(
    dto: PortfolioSnapshotDTO, db: Session, user_id: str
) -> PortfolioSnapshotDTO:
    """Override the snapshot's TSV allocation pie with the canonical doc's
    full-book composition when the plan carries one; else leave the TSV pie.

    Also re-resolves per-position ``sleeve`` (+ Block H blurbs) once the plan
    doc and classification map are available so the Sleeve column and
    allocation-breakdown buckets share one mapping.

    Best-effort: the projection is additive, so any failure reading the plan
    (e.g. an unmigrated DB without plan_versions) falls back to the snapshot
    pie rather than breaking /portfolio."""
    try:
        from argosy.services.allocation_breakdown import (
            _plan_symbol_labels,
            resolve_sleeve_label,
        )
        from argosy.services.instrument_plan_class import (
            UNMAPPED_LABEL,
            load_classification_map,
        )
        from argosy.services.target_allocation_doc import load_plan_target_allocation
        from argosy.state.queries import get_current_plan

        pv = get_current_plan(db, user_id)
        doc = load_plan_target_allocation(pv) if pv is not None else None
        cmap = load_classification_map(db, user_id)
        updates: dict = {}
        if doc is not None and doc.glide:
            updates["allocations"] = _allocations_from_doc(doc)
        if dto.positions:
            plan_labels = _plan_symbol_labels(doc)
            new_positions = []
            for p in dto.positions:
                sym = (p.symbol or "").strip().upper()
                sleeve = resolve_sleeve_label(
                    p.symbol or "",
                    asset_type=p.asset_type or "",
                    details=p.details or "",
                    plan_symbol_labels=plan_labels,
                    classification_map=cmap,
                )
                entry = cmap.get(sym)
                if plan_labels and sym in plan_labels:
                    src = "plan"
                elif entry is not None:
                    src = entry.source
                elif sleeve == UNMAPPED_LABEL:
                    src = ""
                else:
                    src = "cash" if sleeve.startswith("Cash") else ""
                new_positions.append(
                    p.model_copy(
                        update={
                            "sleeve": sleeve,
                            "what_it_is": entry.what_it_is if entry else p.what_it_is,
                            "why_held": entry.why_held if entry else p.why_held,
                            "classification_source": src,
                        }
                    )
                )
            updates["positions"] = new_positions
        if updates:
            return dto.model_copy(update=updates)
        return dto
    except Exception:  # noqa: BLE001 — additive projection, never break /portfolio
        return dto


@router.get("/snapshot", response_model=PortfolioSnapshotDTO)
def get_portfolio_snapshot(
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> PortfolioSnapshotDTO:
    """Return the latest portfolio snapshot for ``user_id``.

    T1.5 lookup order:
      1. Prefer the most recent ``portfolio_snapshots`` row for the user.
      2. Fallback: walk ``ARGOSY_HOME`` for the freshest TSV with the
         canonical header marker, parse it, write-through into the DB
         (idempotent — same source_path + date + size = no-op), and
         serve the parsed result.
      3. Empty DTO when neither path yields data.
    """
    # 1. DB-first.
    try:
        row = get_latest_snapshot_row(db, user_id)
    except Exception as exc:  # noqa: BLE001 - defensive
        _log.warning(
            "portfolio_snapshot.db_lookup_failed",
            user_id=user_id,
            error=str(exc),
        )
        row = None
    if row is not None:
        try:
            snap = row_to_snapshot(row)
            snap = _apply_total_book_to_snap(snap, db, user_id)
            return _project_canonical_allocations(_snapshot_to_dto(snap), db, user_id)
        except Exception as exc:  # noqa: BLE001 - defensive
            _log.warning(
                "portfolio_snapshot.db_hydrate_failed",
                user_id=user_id,
                row_id=row.id,
                error=str(exc),
            )
            # Fall through to filesystem walk.

    # 2. Filesystem fallback + write-through.
    tsv = _find_latest_tsv()
    if tsv is None:
        return _project_canonical_allocations(
            PortfolioSnapshotDTO(
                snapshot_date=None,
                fx_usd_nis=None,
                fx_usd_eur=None,
                total_usd_value_k=0.0,
                positions=[],
                allocations=[],
                source_path=None,
                parse_warnings=["No TSV found under ARGOSY_HOME."],
            ),
            db,
            user_id,
        )

    snap = parse_portfolio_tsv(tsv)
    try:
        from argosy.services.portfolio_snapshot_store import SnapshotIngestRejected

        written = write_through_if_changed(db, user_id=user_id, snapshot=snap)
        if written is not None:
            _warm_derived_cache(user_id)
    except SnapshotIngestRejected as exc:
        _log.warning(
            "portfolio_snapshot.ingest_rejected_on_get",
            user_id=user_id,
            code=exc.code,
            detail=exc.detail,
        )
        # Prefer an existing DB row over serving a rejected filesystem TSV.
        row = get_latest_snapshot_row(db, user_id)
        if row is not None:
            snap = _apply_total_book_to_snap(row_to_snapshot(row), db, user_id)
            return _project_canonical_allocations(
                _snapshot_to_dto(snap),
                db,
                user_id,
            )
        return _project_canonical_allocations(
            PortfolioSnapshotDTO(
                snapshot_date=None,
                fx_usd_nis=None,
                fx_usd_eur=None,
                total_usd_value_k=0.0,
                positions=[],
                allocations=[],
                source_path=None,
                parse_warnings=[
                    f"INGEST REJECTED ({exc.code}): {exc.detail} — refusing to serve untrusted TSV"
                ],
            ),
            db,
            user_id,
        )
    except Exception as exc:  # noqa: BLE001 - defensive
        _log.warning(
            "portfolio_snapshot.write_through_failed",
            user_id=user_id,
            error=str(exc),
        )
    snap = _apply_total_book_to_snap(snap, db, user_id)
    return _project_canonical_allocations(_snapshot_to_dto(snap), db, user_id)


# ---------------------------------------------------------------------------
# POST /upload-snapshot — Monthly portfolio snapshot upload (2026-05-29)
#
# Closes the "no UI surface for the portfolio XLS" flow gap identified in
# the 2026-05-28 session. The user's mental model: every month they drop
# bank statements (transactions) into /expenses AND a portfolio snapshot
# into /portfolio. The latter had no surface; users ran `update_leumi_tsv.py`
# manually outside Argosy.
#
# Scope of this route: accept the TSV directly (the format
# `argosy/ingest/tsv.py::parse_portfolio_tsv` already consumes). The raw
# XLS-to-TSV conversion remains the user's external script for now;
# porting that step is queued for a follow-up session and gated on
# either (a) a fresh in-repo Leumi XLS parser with parity tests against
# the external script, or (b) explicit user consent to ingest the
# Google Drive script as the canonical implementation.
# ---------------------------------------------------------------------------


class UploadSnapshotResponse(BaseModel):
    """Per-upload outcome surface.

    Tri-state explicit contract (codex-tandem zigzag finding,
    2026-05-28): the UI needs to distinguish three independent
    outcomes -- did the TSV persist, did the windfall detector run,
    and did the detector find an event. None of these imply the
    others.
    """

    tsv_persisted: bool
    persisted_path: str | None
    """Where the TSV landed under ARGOSY_EXPENSE_SAMPLES_ROOT (or the
    project's snapshot dir). Useful for the UI to confirm the file is
    where the windfall detector will look next."""
    snapshot_date: str | None
    """Parsed snapshot date from the TSV (the date in row 1 col B)."""
    detect_status: str
    """ok | skipped | failed | pending_pair -- whether the windfall detector
    ran. Skipped means no previous TSV to diff against; failed means it ran
    but raised (uncommon; logged). pending_pair means an XLS landed without
    a matching Leumi Osh statement; the snapshot is queued in
    portfolio_snapshot_parts and will auto-resolve when the Osh arrives.
    (Codex zigzag finding #10, 2026-05-29.)"""
    event: dict | None
    """When detect_status == 'ok' AND a qualifying event fired, the
    event payload (same shape as GET /retirement/windfall/detect)."""
    plan: dict | None
    """Allocation plan when an event fired (same shape as GET /detect)."""
    detail: str | None
    """Free-form note for the UI when the file couldn't be parsed or
    didn't match the expected portfolio-TSV header marker."""
    sha256: str
    """SHA-256 of the upload contents. Idempotency key the caller can
    use to detect "I just uploaded the same file twice" client-side."""
    pending_pair_id: int | None = None
    """When detect_status == 'pending_pair', the portfolio_snapshot_parts
    row id. The UI uses it for status polling / re-render after the Osh
    statement subsequently lands."""


_TSV_FILENAME_RE = re.compile(
    r"Family Finances Status\s*-\s*(\d{2})\s*([A-Za-z]{3})",
    re.IGNORECASE,
)


def _normalize_tsv_filename(original_name: str, snap) -> str:
    """Return the canonical 'Family Finances Status - YY MMM.tsv' name.

    Priority order:
      1. If the original filename already matches the canonical pattern,
         keep it verbatim.
      2. Otherwise, derive from the parsed snapshot_date in the TSV.
      3. Last resort: use today's date.
    """
    m = _TSV_FILENAME_RE.search(original_name)
    if m:
        return original_name if original_name.endswith(".tsv") else f"{original_name}.tsv"
    d = getattr(snap, "snapshot_date", None) or datetime.now().date()
    yy = f"{d.year % 100:02d}"
    mmm = d.strftime("%b")
    return f"Family Finances Status - {yy} {mmm}.tsv"


def _resolve_snapshot_root() -> Path:
    """The directory the windfall detector scans for TSVs.

    Matches the convention in argosy/api/routes/retirement.py::get_windfall_detect:
    prefers ARGOSY_EXPENSE_SAMPLES_ROOT (the user's Google Drive
    Resources folder) when set; falls back to a project-local
    ``snapshots/`` directory under ARGOSY_HOME so dev / CI / tests work.
    """
    env_root = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")
    if env_root:
        return Path(env_root)
    return get_settings().home / "snapshots"


@router.post(
    "/upload-snapshot",
    response_model=UploadSnapshotResponse,
)
def upload_snapshot(
    file: UploadFile = File(...),
    user_id: str = Form("ariel"),
    fire_detector: bool = Form(True),
    allow_stale: bool = Form(False),
    allow_catastrophic_drop: bool = Form(False),
    override_reason: str = Form(""),
    x_argosy_admin: str | None = Header(default=None, alias="X-Argosy-Admin"),
    db: Session = Depends(get_db),
) -> UploadSnapshotResponse:
    """Upload a monthly portfolio snapshot.

    Ordinary uploads are open to the signed-in client (no admin header).
    Privileged bypasses (`allow_stale` / `allow_catastrophic_drop`)
    require `X-Argosy-Admin` + a non-empty `override_reason`. Account
    closure remains on a separate admin-gated endpoint.
    """
    if allow_stale or allow_catastrophic_drop:
        import hmac as _hmac

        settings = get_settings()
        expected = settings.admin_token
        if not expected:
            raise HTTPException(
                status_code=401,
                detail={"error": "admin_token_unconfigured"},
            )
        if not x_argosy_admin:
            raise HTTPException(
                status_code=401,
                detail={"error": "admin_token_required"},
            )
        if not _hmac.compare_digest(x_argosy_admin, expected):
            raise HTTPException(
                status_code=401,
                detail={"error": "admin_token_invalid"},
            )
        if not (override_reason or "").strip():
            raise HTTPException(
                status_code=400,
                detail=(
                    "override_reason is required when allow_stale or allow_catastrophic_drop is set"
                ),
            )

    contents = file.file.read()
    sha = hashlib.sha256(contents).hexdigest()

    from argosy.services.portfolio_ingest.xls_osh_pair import (
        is_leumi_portfolio_xls,
    )

    if is_leumi_portfolio_xls(contents):
        return _handle_xls_branch(
            db=db,
            user_id=user_id,
            contents=contents,
            fire_detector=fire_detector,
            sha=sha,
        )

    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=".tsv",
        delete=False,
    ) as tmp:
        tmp.write(contents)
        tmp_path = Path(tmp.name)

    try:
        with tmp_path.open("r", encoding="utf-8", errors="ignore") as f:
            head = f.read(4096)
        if _PORTFOLIO_TSV_HEADER_MARKER not in head:
            return UploadSnapshotResponse(
                tsv_persisted=False,
                persisted_path=None,
                snapshot_date=None,
                detect_status="skipped",
                event=None,
                plan=None,
                detail=(
                    "Upload did not match a known portfolio shape. Expected "
                    f"either the TSV header marker '{_PORTFOLIO_TSV_HEADER_MARKER}' "
                    "or the Leumi portfolio XLS SpreadsheetML envelope."
                ),
                sha256=sha,
            )

        try:
            snap = parse_portfolio_tsv(tmp_path)
        except Exception as exc:  # noqa: BLE001
            return UploadSnapshotResponse(
                tsv_persisted=False,
                persisted_path=None,
                snapshot_date=None,
                detect_status="skipped",
                event=None,
                plan=None,
                detail=f"parse_portfolio_tsv raised: {exc}",
                sha256=sha,
            )

        target_root = _resolve_snapshot_root()
        target_root.mkdir(parents=True, exist_ok=True)
        target_name = _normalize_tsv_filename(file.filename or "", snap)
        target_path = target_root / target_name

        from argosy.services.portfolio_snapshot_store import (
            SnapshotIngestRejected,
            write_through_if_changed,
        )

        staging_path = target_path.with_suffix(target_path.suffix + ".staging")
        backup_path = target_path.with_suffix(target_path.suffix + ".bak")
        try:
            staging_path.write_bytes(contents)
        except OSError as exc:
            return UploadSnapshotResponse(
                tsv_persisted=False,
                persisted_path=None,
                snapshot_date=(snap.snapshot_date.isoformat() if snap.snapshot_date else None),
                detect_status="skipped",
                event=None,
                plan=None,
                detail=f"filesystem write failed before DB: {exc}",
                sha256=sha,
            )

        actor = "admin-token" if (allow_stale or allow_catastrophic_drop) else "upload"
        try:
            written = write_through_if_changed(
                db,
                user_id=user_id,
                snapshot=snap,
                commit=False,
                allow_stale=allow_stale,
                allow_catastrophic_drop=allow_catastrophic_drop,
                actor=actor,
                override_reason=override_reason or None,
            )
        except SnapshotIngestRejected as exc:
            try:
                staging_path.unlink()
            except OSError:
                pass
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            _log.warning(
                "portfolio_snapshot.ingest_rejected",
                user_id=user_id,
                code=exc.code,
                detail=exc.detail,
                actor=actor,
                override_reason=override_reason or None,
                allow_stale=allow_stale,
                allow_catastrophic_drop=allow_catastrophic_drop,
            )
            return UploadSnapshotResponse(
                tsv_persisted=False,
                persisted_path=None,
                snapshot_date=(snap.snapshot_date.isoformat() if snap.snapshot_date else None),
                detect_status="skipped",
                event=None,
                plan=None,
                detail=f"INGEST REJECTED ({exc.code}): {exc.detail}",
                sha256=sha,
            )
        except Exception as exc:  # noqa: BLE001
            try:
                staging_path.unlink()
            except OSError:
                pass
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            _log.warning(
                "portfolio_snapshot.write_through_failed",
                user_id=user_id,
                error=str(exc),
            )
            return UploadSnapshotResponse(
                tsv_persisted=False,
                persisted_path=None,
                snapshot_date=(snap.snapshot_date.isoformat() if snap.snapshot_date else None),
                detect_status="skipped",
                event=None,
                plan=None,
                detail=f"DB write-through failed: {exc}",
                sha256=sha,
            )

        had_prior = target_path.exists()
        try:
            if had_prior:
                try:
                    if backup_path.exists():
                        backup_path.unlink()
                except OSError:
                    pass
                target_path.replace(backup_path)
            staging_path.replace(target_path)
        except OSError as exc:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            try:
                staging_path.unlink()
            except OSError:
                pass
            if had_prior and backup_path.exists() and not target_path.exists():
                try:
                    backup_path.replace(target_path)
                except OSError:
                    pass
            return UploadSnapshotResponse(
                tsv_persisted=False,
                persisted_path=None,
                snapshot_date=(snap.snapshot_date.isoformat() if snap.snapshot_date else None),
                detect_status="skipped",
                event=None,
                plan=None,
                detail=(
                    f"filesystem finalize failed — DB rolled back, refusing to claim success: {exc}"
                ),
                sha256=sha,
            )

        try:
            db.commit()
        except Exception as exc:  # noqa: BLE001
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            try:
                if target_path.exists():
                    target_path.unlink()
            except OSError:
                pass
            if had_prior and backup_path.exists():
                try:
                    backup_path.replace(target_path)
                except OSError:
                    pass
            return UploadSnapshotResponse(
                tsv_persisted=False,
                persisted_path=None,
                snapshot_date=(snap.snapshot_date.isoformat() if snap.snapshot_date else None),
                detect_status="skipped",
                event=None,
                plan=None,
                detail=(
                    f"DB commit failed after staging — prior file restored, "
                    f"refusing to claim success: {exc}"
                ),
                sha256=sha,
            )

        try:
            if backup_path.exists():
                backup_path.unlink()
        except OSError:
            pass

        if written is not None:
            _warm_derived_cache(user_id)
        _log.info(
            "portfolio_snapshot.uploaded",
            user_id=user_id,
            path=str(target_path),
            sha=sha[:8],
            size=len(contents),
            allow_stale=allow_stale,
            allow_catastrophic_drop=allow_catastrophic_drop,
            actor=actor,
            override_reason=override_reason or None,
        )

        _det = run_windfall_detection_on_snapshot(
            db,
            user_id=user_id,
            target_path=target_path,
            fire=fire_detector,
        )
        event_payload, plan_payload, detect_status = (
            _det.event,
            _det.plan,
            _det.detect_status,
        )

        return UploadSnapshotResponse(
            tsv_persisted=True,
            persisted_path=str(target_path),
            snapshot_date=(snap.snapshot_date.isoformat() if snap.snapshot_date else None),
            detect_status=detect_status,
            event=event_payload,
            plan=plan_payload,
            detail=(
                None
                if not (allow_stale or allow_catastrophic_drop)
                else (
                    "INGEST_OVERRIDE "
                    f"actor={actor} "
                    f"reason={override_reason} "
                    f"allow_stale={allow_stale} "
                    f"allow_catastrophic_drop={allow_catastrophic_drop}"
                )
            ),
            sha256=sha,
        )
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


class CloseUnmanagedAccountResponse(BaseModel):
    account: str
    retired: int
    actor: str | None
    reason: str | None


@router.post(
    "/unmanaged-holdings/close-account",
    response_model=CloseUnmanagedAccountResponse,
    dependencies=[Depends(require_admin_token)],
)
def close_unmanaged_account(
    account_location: str = Form(...),
    reason: str = Form(...),
    user_id: str = Form("ariel"),
    db: Session = Depends(get_db),
) -> CloseUnmanagedAccountResponse:
    """Explicit single-account closure for durable unmanaged holdings.

    Does NOT retire other accounts — a partial feed must never wipe NVDA
    at ``schwab 876`` because ``schwab 999`` closed. Admin-gated.
    """
    from argosy.services.holding_books import retire_unmanaged_account

    result = retire_unmanaged_account(
        db,
        user_id,
        account_location=account_location,
        reason=reason,
        actor="admin-token",
        commit=True,
    )
    return CloseUnmanagedAccountResponse(**result)


def _handle_xls_branch(
    *,
    db: Session,
    user_id: str,
    contents: bytes,
    fire_detector: bool,
    sha: str,
) -> UploadSnapshotResponse:
    """XLS-shaped upload: hand off to xls_osh_pair, then fire the detector
    if (and only if) the pair resolved to a synthesized TSV."""
    from argosy.services.portfolio_ingest.xls_osh_pair import handle_xls_upload

    snapshot_root = _resolve_snapshot_root()
    try:
        resolution = handle_xls_upload(
            db=db,
            user_id=user_id,
            contents=contents,
            snapshot_root=snapshot_root,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "portfolio_snapshot.xls_handler_failed",
            user_id=user_id,
            error=str(exc),
        )
        return UploadSnapshotResponse(
            tsv_persisted=False,
            persisted_path=None,
            snapshot_date=None,
            detect_status="failed",
            event=None,
            plan=None,
            detail=f"XLS handler raised: {exc}",
            sha256=sha,
        )

    if resolution.status == "pending_pair":
        return UploadSnapshotResponse(
            tsv_persisted=False,
            persisted_path=None,
            snapshot_date=(
                resolution.snapshot_date.isoformat() if resolution.snapshot_date else None
            ),
            detect_status="pending_pair",
            event=None,
            plan=None,
            detail=resolution.detail,
            sha256=sha,
            pending_pair_id=resolution.pending_pair_id,
        )

    if resolution.status == "duplicate":
        # Already-resolved row -- return the prior synthesis as if
        # nothing happened, but report tsv_persisted=true so the UI
        # knows the file is durably on disk.
        return UploadSnapshotResponse(
            tsv_persisted=resolution.resolved_tsv_path is not None,
            persisted_path=(
                str(resolution.resolved_tsv_path) if resolution.resolved_tsv_path else None
            ),
            snapshot_date=(
                resolution.snapshot_date.isoformat() if resolution.snapshot_date else None
            ),
            detect_status="skipped",
            event=None,
            plan=None,
            detail=resolution.detail,
            sha256=sha,
            pending_pair_id=resolution.pending_pair_id,
        )

    # Resolved -- fire the shared snapshot-change detector against the freshly
    # synthesized TSV (the SAME routine the Osh-arrival path uses, so detection
    # is identical regardless of which path produced the snapshot).
    target_path = resolution.resolved_tsv_path
    _det = run_windfall_detection_on_snapshot(
        db,
        user_id=user_id,
        target_path=target_path,
        fire=fire_detector,
    )
    event_payload, plan_payload, detect_status = _det.event, _det.plan, _det.detect_status

    return UploadSnapshotResponse(
        tsv_persisted=True,
        persisted_path=str(target_path) if target_path else None,
        snapshot_date=(resolution.snapshot_date.isoformat() if resolution.snapshot_date else None),
        detect_status=detect_status,
        event=event_payload,
        plan=plan_payload,
        detail=resolution.detail,
        sha256=sha,
        pending_pair_id=resolution.pending_pair_id,
    )


class GenerateTsvResponse(BaseModel):
    """POST /api/portfolio/generate-tsv response shape."""

    tsv_persisted: bool
    persisted_path: str | None
    snapshot_date: str | None
    leumi_nis_cash: float | None
    leumi_usd_cash: float | None
    warnings: list[str]
    detail: str | None


@router.post("/generate-tsv", response_model=GenerateTsvResponse)
def generate_tsv(
    user_id: str = Form("ariel"),
    db: Session = Depends(get_db),
) -> GenerateTsvResponse:
    """Refresh the Family Finances Status TSV from Argosy's current state.

    Per the 2026-05-29 ask: Argosy generates the canonical TSV itself.
    Pulls position structure forward from the most recent prior TSV at
    the scan root + overrides Leumi NIS / Leumi USD cash rows with the
    latest closing balances from expense_statements + recomputes the
    Current-allocation block + bumps snapshot_date to today.

    See ``argosy.services.portfolio_ingest.tsv_generator``.
    """
    from argosy.services.portfolio_ingest.tsv_generator import (
        generate_family_finances_tsv,
    )

    snapshot_root = _resolve_snapshot_root()
    result = generate_family_finances_tsv(
        db,
        user_id=user_id,
        snapshot_root=snapshot_root,
    )
    return GenerateTsvResponse(
        tsv_persisted=result.tsv_persisted,
        persisted_path=str(result.persisted_path) if result.persisted_path else None,
        snapshot_date=(result.snapshot_date.isoformat() if result.snapshot_date else None),
        leumi_nis_cash=result.leumi_nis_cash,
        leumi_usd_cash=result.leumi_usd_cash,
        warnings=result.warnings,
        detail=result.detail,
    )


class UnallocatedCashProposalDTO(BaseModel):
    """Response shape for GET /api/portfolio/unallocated-cash-proposal.

    Mirrors UnallocatedCashEvent.to_dict shape exactly so the UI can
    consume it without a separate transform. None response means no
    overage detected (current cash is within plan-target tolerance).
    """

    detected_at: str
    snapshot_date: str | None
    current_cash_k_usd: float
    target_cash_k_usd: float
    overage_ratio: float
    excess_usd: float
    headline: str
    proposals: list[dict]
    allocation_delta_table: list[dict]


@router.get(
    "/unallocated-cash-proposal",
    response_model=UnallocatedCashProposalDTO | None,
)
def get_unallocated_cash_proposal(
    user_id: str = Query("ariel"),
    overage_ratio: float = Query(1.5, ge=1.0, le=10.0),
    db: Session = Depends(get_db),
) -> UnallocatedCashProposalDTO | None:
    """Return a proposed allocation for unallocated cash, or null.

    Self-tuning: triggers when current cash > plan-target cash by the
    given overage_ratio (default 1.5x). Reuses the windfall allocator's
    long-term proposal logic with the cash excess as input. UI surfaces
    this as a "$X above your cash target -> here's where it could go"
    callout on /portfolio.

    Returns null when:
      * No snapshot for the user.
      * No cash row in the snapshot's Current allocation block.
      * Current cash is below the overage_ratio threshold.

    See ``argosy.services.unallocated_cash_detector`` for the math.
    """
    from argosy.services.unallocated_cash_detector import (
        detect_unallocated_cash_overage,
    )

    event = detect_unallocated_cash_overage(
        db,
        user_id=user_id,
        overage_ratio=overage_ratio,
    )
    if event is None:
        return None
    payload = event.to_dict()
    return UnallocatedCashProposalDTO(**payload)


# ---------------------------------------------------------------------------
# GET /api/portfolio/high-potential-sleeve — the med-high-risk satellite slice
# the user asked to carve out of a cash deployment (≥5% of redeployed cash).
# Conviction-weighted, blend vehicle (UCITS thematic core + single-name
# carve-out). See argosy/services/high_potential_sleeve.py.
# ---------------------------------------------------------------------------


class SleeveCandidateDTO(BaseModel):
    ticker: str
    name: str
    vehicle: str  # ucits_thematic | single_name
    conviction: str  # HIGH | MEDIUM | LOW
    thesis: str
    us_situs: bool  # single US name/ETF → adds estate-tax exposure
    held_today: bool
    source: str  # advisor_seed | fleet_validated
    amount_usd: float
    pct_of_sleeve: float


class HighPotentialSleeveDTO(BaseModel):
    """GET /api/portfolio/high-potential-sleeve response."""

    cash_basis_usd: float
    sleeve_pct_of_cash: float
    sleeve_budget_usd: float
    vehicle_split: dict[str, float]
    candidates: list[SleeveCandidateDTO]
    note: str


@router.get(
    "/high-potential-sleeve",
    response_model=HighPotentialSleeveDTO,
)
def get_high_potential_sleeve(
    cash_usd: float = Query(
        250_000.0,
        ge=0.0,
        le=100_000_000.0,
        description="Cash being redeployed; the sleeve is sleeve_pct of this.",
    ),
    sleeve_pct: float = Query(
        5.0,
        ge=0.0,
        le=25.0,
        description="High-potential share of the redeployed cash (default 5%).",
    ),
    live_radar: bool = Query(
        False,
        description=(
            "When true, source the single-name carve-out LIVE from the trend "
            "radar (network) instead of the advisor seeds; the UCITS thematic "
            "core is always kept. Slower (~5s)."
        ),
    ),
    radar_names: int = Query(
        4,
        ge=1,
        le=10,
        description="How many radar single-names to include when live_radar.",
    ),
) -> HighPotentialSleeveDTO:
    """Conviction-weighted high-potential sleeve for a cash deployment.

    Blend vehicle: a UCITS thematic core (non-US-situs) + a single-name
    carve-out (US-situs — estate-tax accepted on that slice). Seed candidates
    are the advisor's first pass (``source='advisor_seed'``); with
    ``live_radar`` the carve-out is sourced from the trend radar
    (``source='trend_radar'``). The agent fleet validates + final-sizes on the
    next live synth.
    """
    from argosy.services.high_potential_sleeve import (
        build_high_potential_sleeve,
        sleeve_vehicle_split,
        ucits_thematic_seeds,
    )

    budget = cash_usd * sleeve_pct / 100.0
    candidates = None
    radar_note = ""
    if live_radar:
        try:
            from argosy.services.trend_radar import scan_trends, to_sleeve_candidates

            scan = scan_trends(limit=radar_names)
            single_names = to_sleeve_candidates(scan.shortlist, max_names=radar_names)
            if single_names:
                candidates = ucits_thematic_seeds() + single_names
                radar_note = (
                    f" Single-name carve-out sourced LIVE from the trend radar "
                    f"({len(single_names)} names, scored + liquidity-filtered + "
                    "pump-guarded). Pair every single name with the speculative "
                    "monitor (/api/portfolio/speculative-monitor) for stop-loss."
                )
        except Exception:  # noqa: BLE001 — radar is best-effort; fall back to seeds
            radar_note = " (live radar unavailable — showing advisor seeds.)"
    allocs = build_high_potential_sleeve(budget, candidates)
    return HighPotentialSleeveDTO(
        cash_basis_usd=round(cash_usd, 2),
        sleeve_pct_of_cash=sleeve_pct,
        sleeve_budget_usd=round(budget, 2),
        vehicle_split=sleeve_vehicle_split(allocs),
        candidates=[
            SleeveCandidateDTO(
                ticker=a.candidate.ticker,
                name=a.candidate.name,
                vehicle=a.candidate.vehicle,
                conviction=a.candidate.conviction,
                thesis=a.candidate.thesis,
                us_situs=a.candidate.us_situs,
                held_today=a.candidate.held_today,
                source=a.candidate.source,
                amount_usd=a.amount_usd,
                pct_of_sleeve=a.pct_of_sleeve,
            )
            for a in allocs
        ],
        note=(
            "Advisor first-pass seeds, conviction-weighted; the agent fleet "
            "validates + final-sizes on the next synthesis. UCITS thematic core "
            "is non-US-situs; single-name carve-out adds estate-tax exposure." + radar_note
        ),
    )


# ---------------------------------------------------------------------------
# GET /api/portfolio/trend-radar — live high-potential SOURCING. Fans out
# across no-API-key signal families and surfaces names corroborated by >= 2
# families and a clean liquidity profile. See argosy/services/trend_radar.py.
# ---------------------------------------------------------------------------


class TrendCandidateDTO(BaseModel):
    ticker: str
    name: str
    score: float
    families: list[str]
    reasons: list[str]
    price: float | None
    market_cap: float | None
    dollar_volume: float | None
    pct_change: float | None


class TrendRadarDTO(BaseModel):
    shortlist: list[TrendCandidateDTO]
    quarantine_count: int
    source_counts: dict[str, object]
    note: str


@router.get("/trend-radar", response_model=TrendRadarDTO)
def get_trend_radar(
    limit: int = Query(15, ge=1, le=50),
    cap_max_b: float = Query(
        8.0,
        ge=0.5,
        le=500.0,
        description="Max market cap in $B for the satellite band (default 8).",
    ),
) -> TrendRadarDTO:
    """Live trend-radar scan (network ~5s). High-risk SOURCING for the sleeve
    carve-out; every name needs the speculative monitor + stop-loss before
    acting. NOT advice — candidates require fleet validation + a backtest."""
    from argosy.services.trend_radar import LiquidityFilter, scan_trends

    scan = scan_trends(filters=LiquidityFilter(cap_max=cap_max_b * 1e9), limit=limit)
    return TrendRadarDTO(
        shortlist=[
            TrendCandidateDTO(
                ticker=c.ticker,
                name=c.name,
                score=c.score,
                families=list(c.families),
                reasons=list(c.reasons),
                price=c.price,
                market_cap=c.market_cap,
                dollar_volume=c.dollar_volume,
                pct_change=c.pct_change,
            )
            for c in scan.shortlist
        ],
        quarantine_count=len(scan.quarantine),
        source_counts=scan.source_counts,
        note=(
            "Cross-source momentum/attention/growth signal, pump-guarded "
            "(>=2 families) + liquidity-filtered. High-risk single names are "
            "US-situs; size small and pair with a stop-loss. Backtest / paper-"
            "trade before committing real capital."
        ),
    )


# ---------------------------------------------------------------------------
# GET /api/portfolio/speculative-monitor — daily exit-discipline read on the
# high-risk single names. Hard + trailing stop + momentum break per position.
# See argosy/services/speculative_monitor.py.
# ---------------------------------------------------------------------------


class MonitorSignalDTO(BaseModel):
    ticker: str
    name: str
    action: str  # SELL | TRIM | WATCH | HOLD
    reason: str
    current_price: float
    entry_price: float
    peak_price: float
    hard_stop_level: float
    trailing_stop_level: float
    binding_stop_level: float
    pct_from_entry: float
    pct_from_peak: float
    distance_to_stop_pct: float


class SpeculativeMonitorDTO(BaseModel):
    signals: list[MonitorSignalDTO]
    hard_stop_pct: float
    trailing_stop_pct: float
    note: str


@router.get("/speculative-monitor", response_model=SpeculativeMonitorDTO)
def get_speculative_monitor(
    tickers: str = Query(
        "",
        description=(
            "Comma-separated tickers to monitor. When omitted, monitors the "
            "currently-held single-name sleeve seeds."
        ),
    ),
    hard_stop_pct: float = Query(20.0, ge=1.0, le=90.0),
    trailing_stop_pct: float = Query(25.0, ge=1.0, le=90.0),
) -> SpeculativeMonitorDTO:
    """Live stop-loss / sell-signal read on speculative single names.

    Entry price defaults to today's price when a cost basis is unknown (so the
    stop levels are anchored from today); supply real entries once bought.
    Network-bound (yfinance per ticker)."""
    from datetime import date, timedelta

    from argosy.services.high_potential_sleeve import _SEED_CANDIDATES
    from argosy.services.speculative_monitor import (
        MonitorConfig,
        WatchEntry,
        run_monitor,
    )

    syms = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if not syms:
        syms = [c.ticker for c in _SEED_CANDIDATES if c.vehicle == "single_name" and c.held_today]
    # v1: entry unknown → anchor stops from today; peak tracked over ~90d.
    entry_date = date.today() - timedelta(days=90)
    watch = [WatchEntry(ticker=s, entry_price=0.0, entry_date=entry_date) for s in syms]
    cfg = MonitorConfig(
        hard_stop_pct=hard_stop_pct / 100.0,
        trailing_stop_pct=trailing_stop_pct / 100.0,
    )
    signals = run_monitor(watch, cfg=cfg)
    return SpeculativeMonitorDTO(
        signals=[MonitorSignalDTO(**vars(s)) for s in signals],
        hard_stop_pct=hard_stop_pct,
        trailing_stop_pct=trailing_stop_pct,
        note=(
            "Mechanical exit discipline for high-risk names. Entry defaults to "
            "the price ~90d ago when no cost basis is known. SELL = a stop "
            "breached; TRIM = momentum break below the 50d MA; WATCH = near a "
            "stop. Re-checked daily by the scheduler."
        ),
    )


# ---------------------------------------------------------------------------
# POST /api/portfolio/refresh-rsu-vests — explicit RSU vest ingest trigger
#
# The monthly cycle (``argosy/orchestrator/loops/monthly_cycle.py``)
# already calls ``rsu_vest_pull.ingest_samples_root`` on the 1st of every
# month. This route gives the user an on-demand "ingest now" button so
# they don't have to wait for the next cycle after dropping a fresh
# Schwab Equity Awards export under ``$ARGOSY_EXPENSE_SAMPLES_ROOT``.
# Idempotent — ``ingest_schwab_vest_events`` skips rows whose
# (user_id, grant_id, vest_date) tuple is already in rsu_vest_events.
# ---------------------------------------------------------------------------


class RsuVestIngestFileResult(BaseModel):
    """Per-CSV outcome surfaced in the refresh-rsu-vests response."""

    source_file: str
    parsed: int | None = None
    inserted: int | None = None
    duplicates: int | None = None
    error: str | None = None


class RefreshRsuVestsResponse(BaseModel):
    """POST /api/portfolio/refresh-rsu-vests response shape."""

    samples_root: str | None
    files_processed: int
    total_inserted: int
    total_duplicates: int
    results: list[RsuVestIngestFileResult]
    detail: str | None


@router.post("/refresh-rsu-vests", response_model=RefreshRsuVestsResponse)
def refresh_rsu_vests(
    user_id: str = Query("ariel"),
) -> RefreshRsuVestsResponse:
    """Scan ``$ARGOSY_EXPENSE_SAMPLES_ROOT`` for Schwab Equity Awards
    CSVs and ingest each into ``rsu_vest_events``.

    Closes the "no UI surface for rsu_vest ingest" gap: the
    monthly_cycle path is automatic but only fires on the 1st of the
    month; this route lets the user trigger ingest immediately after
    dropping a fresh export. Idempotent on the unique
    ``(user_id, grant_id, vest_date)`` constraint.
    """
    from argosy.services.rsu_vest_pull import (
        _resolve_samples_root,
        ingest_samples_root,
    )

    root = _resolve_samples_root()
    if root is None:
        return RefreshRsuVestsResponse(
            samples_root=os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT"),
            files_processed=0,
            total_inserted=0,
            total_duplicates=0,
            results=[],
            detail=(
                "ARGOSY_EXPENSE_SAMPLES_ROOT is unset or doesn't exist. "
                "Set the env var to the directory containing the Schwab "
                "Equity Awards CSV (filename pattern "
                "EquityAwardsCenter_Transactions_*.csv)."
            ),
        )

    results = ingest_samples_root(user_id)
    total_inserted = sum(r.get("inserted", 0) or 0 for r in results)
    total_duplicates = sum(r.get("duplicates", 0) or 0 for r in results)

    return RefreshRsuVestsResponse(
        samples_root=str(root),
        files_processed=len(results),
        total_inserted=total_inserted,
        total_duplicates=total_duplicates,
        results=[RsuVestIngestFileResult(**r) for r in results],
        detail=None,
    )


# --- Live current-allocation vs plan-target, by class, with drill-down -----


class HoldingRowDTO(BaseModel):
    symbol: str
    name: str
    value_k: float
    pct: float
    account: str = ""
    estate_safe: bool | None = None


class CategoryBreakdownDTO(BaseModel):
    label: str
    current_pct: float
    target_pct: float | None
    current_value_k: float
    holdings: list[HoldingRowDTO]


class AllocationBreakdownDTO(BaseModel):
    rows: list[CategoryBreakdownDTO]
    total_value_k: float
    note: str
    book_degraded: bool = False
    degrade_reason: str | None = None
    accounts_covered: list[str] = []
    accounts_carried: list[str] = []


@router.get("/allocation-breakdown", response_model=AllocationBreakdownDTO)
def get_allocation_breakdown(
    user_id: str = Query("ariel"),
    exclude_nvda: bool = Query(False),
    db: Session = Depends(get_db),
) -> AllocationBreakdownDTO:
    """LIVE current allocation (from the snapshot holdings, grouped by class)
    vs the canonical plan's class targets, with the per-symbol drill-down. This
    is the real 'current vs plan target' — not the plan glide's modelled anchor."""
    from argosy.services import derived_cache
    from argosy.services.allocation_breakdown import build_allocation_breakdown
    from argosy.services.target_allocation_doc import load_plan_target_allocation
    from argosy.state.queries import get_current_plan

    def _compute() -> AllocationBreakdownDTO:
        row = get_latest_snapshot_row(db, user_id)
        if row is None:
            return AllocationBreakdownDTO(
                rows=[], total_value_k=0.0, note="No portfolio snapshot found."
            )
        snap = _apply_total_book_to_snap(row_to_snapshot(row), db, user_id)
        pv = get_current_plan(db, user_id)
        doc = load_plan_target_allocation(pv) if pv is not None else None
        from argosy.services.instrument_plan_class import load_classification_map

        cmap = load_classification_map(db, user_id)
        rows = build_allocation_breakdown(
            snap,
            doc,
            exclude_nvda=exclude_nvda,
            classification_map=cmap,
        )
        note = (
            "Current = your live holdings grouped by asset class; target = the "
            "canonical plan's class targets. Click a class to see its symbols. "
            + ("" if doc is not None else "No current plan — targets shown blank.")
        )
        if getattr(snap, "book_degraded", False):
            reason = getattr(snap, "degrade_reason", None) or "total book degraded"
            note = f"BOOK DEGRADED — valuation unavailable ({reason}). " + note
        dto = _allocation_breakdown_dto(rows, note)
        dto.book_degraded = bool(getattr(snap, "book_degraded", False))
        dto.degrade_reason = getattr(snap, "degrade_reason", None)
        dto.accounts_covered = list(getattr(snap, "accounts_covered", None) or [])
        dto.accounts_carried = list(getattr(snap, "accounts_carried", None) or [])
        return dto

    # Pure function of (plan, snapshot, exclude_nvda) — no time/random/live-market
    # dependence — so memoize on the version tuple + the one query param.
    version = derived_cache.version_tuple(db, user_id)
    if version is not None:
        # The instrument→plan-class map is NOT in version_tuple, so a seed /
        # owner-reassign would otherwise serve stale buckets until the plan or
        # snapshot changed. Fold its fingerprint into the key.
        from argosy.services.instrument_plan_class import classification_fingerprint

        version = version + (
            "allocation-breakdown",
            exclude_nvda,
            classification_fingerprint(db, user_id),
        )
    return derived_cache.get_or_compute("portfolio.allocation-breakdown", version, _compute)


def _allocation_breakdown_dto(rows, note: str) -> AllocationBreakdownDTO:
    return AllocationBreakdownDTO(
        rows=[
            CategoryBreakdownDTO(
                label=r.label,
                current_pct=r.current_pct,
                target_pct=r.target_pct,
                current_value_k=r.current_value_k,
                holdings=[
                    HoldingRowDTO(
                        symbol=h.symbol,
                        name=h.name,
                        value_k=h.value_k,
                        pct=h.pct,
                        account=h.account,
                        estate_safe=h.estate_safe,
                    )
                    for h in r.holdings
                ],
            )
            for r in rows
        ],
        total_value_k=round(sum(r.current_value_k for r in rows), 2),
        note=note,
    )


# --- Block H: instrument → plan-class mapping (owner-visible + editable) ----


class InstrumentClassRowDTO(BaseModel):
    symbol: str
    plan_class_label: str
    source: str
    confidence: str
    what_it_is: str
    why_held: str
    updated_at: str | None = None
    resolved_label: str | None = None  # after live plan precedence


class InstrumentClassListDTO(BaseModel):
    rows: list[InstrumentClassRowDTO]
    unmapped_held: list[str]
    plan_classes: list[str]


class InstrumentClassReassignRequest(BaseModel):
    user_id: str = "ariel"
    plan_class_label: str
    what_it_is: str | None = None
    why_held: str | None = None


class InstrumentClassSeedResponse(BaseModel):
    plan: int
    fleet_deterministic: int
    unmapped_held: list[str]


@router.get("/instrument-classes", response_model=InstrumentClassListDTO)
def list_instrument_classes(
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> InstrumentClassListDTO:
    """Owner-visible classification map (nothing hidden)."""
    from argosy.services.allocation_breakdown import (
        _plan_symbol_labels,
        resolve_sleeve_label,
    )
    from argosy.services.instrument_plan_class import (
        list_unmapped_held,
        load_classification_map,
    )
    from argosy.services.target_allocation_doc import load_plan_target_allocation
    from argosy.state.queries import get_current_plan

    pv = get_current_plan(db, user_id)
    doc = load_plan_target_allocation(pv) if pv is not None else None
    plan_labels = _plan_symbol_labels(doc)
    cmap = load_classification_map(db, user_id)
    plan_classes = sorted(
        {
            getattr(c, "label", "")
            for c in (getattr(doc, "classes", None) or [])
            if getattr(c, "label", "")
        }
    )
    held: set[str] = set()
    row = get_latest_snapshot_row(db, user_id)
    if row is not None:
        for p in getattr(row_to_snapshot(row), "positions", []) or []:
            s = (getattr(p, "symbol", "") or "").strip().upper()
            if s and s not in {"-", "—"}:
                held.add(s)
    rows_out: list[InstrumentClassRowDTO] = []
    for sym, entry in sorted(cmap.items()):
        resolved = resolve_sleeve_label(
            sym,
            plan_symbol_labels=plan_labels,
            classification_map=cmap,
        )
        rows_out.append(
            InstrumentClassRowDTO(
                symbol=sym,
                plan_class_label=entry.plan_class_label,
                source=entry.source,
                confidence=entry.confidence,
                what_it_is=entry.what_it_is,
                why_held=entry.why_held,
                updated_at=entry.updated_at.isoformat() if entry.updated_at else None,
                resolved_label=resolved,
            )
        )
    unmapped = list_unmapped_held(
        held_symbols=held,
        plan_symbol_labels=plan_labels,
        classification_map=cmap,
    )
    return InstrumentClassListDTO(
        rows=rows_out,
        unmapped_held=unmapped,
        plan_classes=plan_classes,
    )


@router.put("/instrument-classes/{symbol}", response_model=InstrumentClassRowDTO)
def reassign_instrument_class(
    symbol: str,
    body: InstrumentClassReassignRequest,
    db: Session = Depends(get_db),
) -> InstrumentClassRowDTO:
    """Owner reassign — persists source=owner (outranks fleet)."""
    from argosy.services.instrument_plan_class import owner_reassign

    row = owner_reassign(
        db,
        body.user_id,
        symbol,
        body.plan_class_label,
        what_it_is=body.what_it_is,
        why_held=body.why_held,
    )
    db.commit()
    return InstrumentClassRowDTO(
        symbol=row.symbol,
        plan_class_label=row.plan_class_label,
        source=row.source,
        confidence=row.confidence,
        what_it_is=row.what_it_is,
        why_held=row.why_held,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        resolved_label=row.plan_class_label,
    )


@router.post("/instrument-classes/seed", response_model=InstrumentClassSeedResponse)
def seed_instrument_classes(
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> InstrumentClassSeedResponse:
    """Deterministic seed: plan instruments + known-holdings fleet rows.

    Live WebSearch fleet enrichment is a queued follow-up — not this gate.
    """
    from argosy.services.allocation_breakdown import _plan_symbol_labels
    from argosy.services.instrument_plan_class import (
        list_unmapped_held,
        load_classification_map,
        seed_all,
    )
    from argosy.services.target_allocation_doc import load_plan_target_allocation
    from argosy.state.queries import get_current_plan

    pv = get_current_plan(db, user_id)
    doc = load_plan_target_allocation(pv) if pv is not None else None
    held: set[str] = set()
    row = get_latest_snapshot_row(db, user_id)
    if row is not None:
        for p in getattr(row_to_snapshot(row), "positions", []) or []:
            s = (getattr(p, "symbol", "") or "").strip().upper()
            if s and s not in {"-", "—"}:
                held.add(s)
    counts = seed_all(db, user_id, doc, held_symbols=held)
    cmap = load_classification_map(db, user_id)
    unmapped = list_unmapped_held(
        held_symbols=held,
        plan_symbol_labels=_plan_symbol_labels(doc),
        classification_map=cmap,
    )
    return InstrumentClassSeedResponse(
        plan=counts["plan"],
        fleet_deterministic=counts["fleet_deterministic"],
        unmapped_held=unmapped,
    )


# --- Real-estate net equity (net worth, separate from the investable book) --


class RealEstatePaymentDTO(BaseModel):
    payment_date: str | None
    invoice_no: str | None
    amount_net_local: float
    vat_local: float
    kind: str
    description: str


class PropertyEquityDTO(BaseModel):
    name: str
    currency: str
    home_local: float | None
    loan_local: float | None
    net_local: float | None
    net_usd_k: float | None
    warnings: list[str]
    # Present only when the property has a payment ledger: how much has been paid
    # toward the contract price (net of VAT) + the per-payment audit trail. The
    # remaining (loan_local) is then computed as home_local − paid_to_date_local.
    paid_to_date_local: float | None = None
    vat_paid_local: float | None = None
    payments: list[RealEstatePaymentDTO] | None = None
    # Present only when the property has a durable override (impairment/write-off).
    status: str | None = None  # bust | sold | impaired
    recovery_expected_local: float | None = None  # contingent — NOT in net worth
    recovery_confidence: str | None = None
    note: str | None = None


class RealEstateEquityDTO(BaseModel):
    properties: list[PropertyEquityDTO]
    total_net_usd_k: float
    note: str


@router.get("/real-estate", response_model=RealEstateEquityDTO)
def get_real_estate(
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> RealEstateEquityDTO:
    """Per-property real-estate net equity (Home − Loan, FX-converted) from the
    snapshot's "Real estate details". Net WORTH context — deliberately separate
    from the investable allocation (a primary residence isn't investable)."""
    from argosy.services import derived_cache

    # Pure function of (plan, snapshot) — the snapshot's real-estate block + the
    # payment ledgers + the durable overrides, all keyed by the version tuple
    # (snapshot id/imported_at). No time/random/live-market dependence, so memoize.
    version = derived_cache.version_tuple(db, user_id)
    version = (version + ("real-estate",)) if version is not None else None
    return derived_cache.get_or_compute(
        "portfolio.real-estate", version, lambda: _compute_real_estate(db, user_id)
    )


def _compute_real_estate(db: Session, user_id: str) -> RealEstateEquityDTO:
    from argosy.services.real_estate_equity import compute_real_estate_equity
    from argosy.services.real_estate_ledger import (
        load_property_ledgers,
        load_real_estate_overrides,
    )

    row = get_latest_snapshot_row(db, user_id)
    if row is None:
        return RealEstateEquityDTO(
            properties=[], total_net_usd_k=0.0, note="No portfolio snapshot found."
        )
    snap = row_to_snapshot(row)

    # Canonical payment ledger: where a property has recorded payments, the
    # remaining-to-pay is COMPUTED (price − Σ net payments) and supersedes the
    # static snapshot Loan row, so it survives TSV re-imports and traces to the
    # source invoices. Contract price = the snapshot Home.
    price_by_prop = {
        r.location: r.value_local
        for r in snap.real_estate
        if (getattr(r, "role", "") or "").strip().lower() == "home"
        and getattr(r, "value_local", None) is not None
    }
    ccy_by_prop = {
        r.location: (getattr(r, "currency", "") or "").strip().upper()
        for r in snap.real_estate
        if (getattr(r, "role", "") or "").strip().lower() == "home"
    }
    ledgers = load_property_ledgers(
        db,
        user_id=user_id,
        total_price_by_property=price_by_prop,
        currency_by_property=ccy_by_prop,
    )
    # Build the override, but fail loud rather than silently misapply: skip a
    # ledger whose currency disagrees with the snapshot Home (would apply a
    # wrong local-currency remaining), and collect per-property ledger warnings.
    loan_override: dict[str, float] = {}
    ledger_warnings: dict[str, list[str]] = {}
    for key, lg in ledgers.items():
        snap_ccy = ccy_by_prop.get(key)
        if snap_ccy and (lg.currency or "").upper() != snap_ccy:
            ledger_warnings.setdefault(key, []).append(
                f"ledger ignored: currency {lg.currency} ≠ snapshot {snap_ccy}"
            )
            continue
        if lg.remaining_local is None:
            ledger_warnings.setdefault(key, []).append(
                "ledger ignored: no contract price (missing Home row)"
            )
            continue
        if lg.overpaid_local > 0:
            ledger_warnings.setdefault(key, []).append(
                f"payments exceed contract price by {lg.overpaid_local:.0f} — check for double-count"
            )
        loan_override[key] = lg.remaining_local

    # Durable per-property overrides (impairment / write-off, e.g. a developer-
    # bankruptcy property worth 0 whose mortgage was never drawn). These
    # supersede BOTH the snapshot Home (value) and Loan, and carry a contingent
    # recovery that is deliberately NOT added to net worth.
    overrides = load_real_estate_overrides(db, user_id=user_id)
    value_override = {
        k: o.current_value_local for k, o in overrides.items() if o.current_value_local is not None
    }
    for k, o in overrides.items():
        if o.loan_local is not None:
            loan_override[k] = o.loan_local

    eq = compute_real_estate_equity(
        snap.real_estate,
        fx_usd_nis=snap.fx_usd_nis,
        fx_usd_eur=snap.fx_usd_eur,
        loan_override=loan_override,
        value_override=value_override,
    )
    props: list[PropertyEquityDTO] = []
    for p in eq.properties:
        lg = ledgers.get(p.name)
        payments = None
        paid = vat_paid = None
        if lg is not None and lg.has_entries:
            paid = lg.paid_net_local
            vat_paid = lg.vat_paid_local
            payments = [
                RealEstatePaymentDTO(
                    payment_date=e.payment_date.isoformat() if e.payment_date else None,
                    invoice_no=e.invoice_no,
                    amount_net_local=e.amount_net_local,
                    vat_local=e.vat_local,
                    kind=e.kind,
                    description=e.description,
                )
                for e in lg.entries
            ]
        ovr = overrides.get(p.name)
        props.append(
            PropertyEquityDTO(
                name=p.name,
                currency=p.currency,
                home_local=p.home_local,
                loan_local=p.loan_local,
                net_local=p.net_local,
                net_usd_k=p.net_usd_k,
                warnings=list(p.warnings) + ledger_warnings.get(p.name, []),
                paid_to_date_local=paid,
                vat_paid_local=vat_paid,
                payments=payments,
                status=ovr.status if ovr else None,
                recovery_expected_local=ovr.recovery_expected_local if ovr else None,
                recovery_confidence=ovr.recovery_confidence if ovr else None,
                note=ovr.note if ovr else None,
            )
        )
    return RealEstateEquityDTO(
        properties=props,
        total_net_usd_k=eq.total_net_usd_k,
        note=(
            "Net equity = current value − outstanding loan, converted to USD. "
            "Where payments are tracked, the remaining-to-pay is computed from "
            "the payment ledger (survives re-imports). Net-worth context; not "
            "part of the investable allocation target."
        ),
    )


# --- Plan-bound deterministic allocation tasks (Slice 1a) ------------------
# 'Plan target' here is the canonical, glide-aware TargetAllocationDoc — never
# the TSV spreadsheet (the headline bug this slice fixes). The wire DTOs and the
# candidate->DTO mapping live in argosy.services.contracts (Phase 0).


class AllocationTasksDTO(BaseModel):
    mode: str
    cash_usd: float
    candidates: list[AllocationCandidateDTO]
    note: str
    # Slice 1b — present only when ``with_agent=true``: the agent's ordered,
    # paced, reconciled tasks (numbers all trace to ``candidates``). ``None``
    # means the agent pass wasn't requested (deterministic candidates are
    # always returned instantly).
    executable_tasks: list[ExecutableTaskDTO] | None = None


def _load_current_doc_and_holdings(user_id: str, db: Session | None = None):
    """(TargetAllocationDoc | None, holdings_by_symbol, cash_usd) from the user's
    current accepted plan (PlanVersion role='current') + latest snapshot.
    Best-effort; ({}, 0.0) on miss — never raises."""
    from argosy.services.allocation_engine import tradeable_holdings
    from argosy.services.target_allocation_doc import load_plan_target_allocation
    from argosy.state.queries import get_current_plan

    def _from_session(session: Session):
        try:
            pv = get_current_plan(session, user_id)
            doc = load_plan_target_allocation(pv) if pv is not None else None
            row = get_latest_snapshot_row(session, user_id)
            holdings, cash = ({}, 0.0)
            if row is not None:
                holdings, cash = tradeable_holdings(row_to_snapshot(row))
            return doc, holdings, cash
        except Exception:  # noqa: BLE001 — docstring: never raises
            return None, {}, 0.0

    if db is not None:
        return _from_session(db)

    # Legacy fallback for callers that don't have a request session.
    try:
        from sqlalchemy.orm import sessionmaker

        from argosy.state.db import create_sync_engine

        url = str(get_settings().database_url).replace("+aiosqlite", "")
        factory = sessionmaker(
            bind=create_sync_engine(url),
            expire_on_commit=False,
        )
        with factory() as own_db:
            return _from_session(own_db)
    except Exception:  # noqa: BLE001
        return None, {}, 0.0


@router.get("/allocation-tasks", response_model=AllocationTasksDTO)
def get_allocation_tasks(
    mode: str = Query("cash_only_deploy"),
    cash_usd: float = Query(0.0, ge=0.0),
    user_id: str = Query("ariel"),
    with_agent: bool = Query(False),
) -> AllocationTasksDTO:
    """Deterministic, plan-bound allocation candidates (no LLM). 'Plan target'
    is the canonical TargetAllocationDoc (glide-aware) — never the TSV. Amounts
    are deterministic; legs are ADVISORY (best-effort account/currency) and tax
    is advisory-only until lot/cash bucketing lands. When ``with_agent=true`` the
    Slice-1b agent additionally orders + paces + explains these (on demand;
    deterministic candidates still return instantly when false)."""
    from argosy.services.allocation_engine import AllocationMode, compute_allocation

    doc, holdings, snap_cash = _load_current_doc_and_holdings(user_id)
    if doc is None:
        return AllocationTasksDTO(
            mode=mode,
            cash_usd=cash_usd,
            candidates=[],
            note="No current canonical plan — accept a plan first.",
        )
    deploy_cash = cash_usd or snap_cash
    try:
        cands = compute_allocation(doc, holdings, AllocationMode(mode), cash_usd=deploy_cash)
    except ValueError as exc:
        # Fail loud: a non-conserving / malformed plan must surface, never
        # silently produce a mis-sized allocation.
        _log.warning("allocation-tasks could not size plan: %s", exc)
        return AllocationTasksDTO(
            mode=mode,
            cash_usd=deploy_cash,
            candidates=[],
            note=f"Could not size allocation from the current plan: {exc}",
        )

    executable_tasks = None
    agent_note = ""
    if with_agent and cands:
        # On-demand agent pass. Market context = the run's macro snapshot (incl. a
        # volatility proxy) + FX; per-position verdicts = the Portfolio Verdict
        # source. Both best-effort with an empty fallback (codex #15). The whole
        # pass is guarded: an agent/reconciliation failure must NOT 500 — the
        # deterministic candidates always return (codex 1b #4).
        try:
            from argosy.agents import allocation_agent as _aa

            verdicts, market_context = _allocation_agent_context(user_id)
            tasks = _aa.order_and_explain(
                cands, verdicts=verdicts, market_context=market_context, user_id=user_id
            )
            executable_tasks = [task_to_dto(t) for t in tasks]
        except Exception as exc:  # noqa: BLE001 — agent pass is additive
            _log.warning("allocation-tasks agent pass failed: %s", exc)
            executable_tasks = None
            agent_note = (
                " (agent ordering unavailable this run; showing the deterministic candidates only.)"
            )

    return AllocationTasksDTO(
        mode=mode,
        cash_usd=deploy_cash,
        candidates=[candidate_to_dto(c) for c in cands],
        executable_tasks=executable_tasks,
        note=(
            "Plan-bound (canonical TargetAllocationDoc, glide-aware). Amounts "
            "deterministic; legs advisory (account/currency best-effort) and "
            "tax shown as advisory only. The agent (Slice 1b) orders + "
            "explains these." + agent_note
        ),
    )


@router.get("/deploy-cash", response_model=DeploymentPlanDTO)
def get_deploy_cash(
    cash_usd: float | None = Query(None, ge=0.0),
    user_id: str = Query("ariel"),
    live: bool = Query(False),
    sleeve_pct: float = Query(
        5.0,
        ge=0.0,
        le=25.0,
        description=(
            "High-potential ('moonshot') share of the deploy amount carved off "
            "the top and routed to the high-risk tier (default 5%)."
        ),
    ),
    use_high_potential: bool = Query(
        True,
        description=(
            "When true (default) the high tier is filled from the conviction-"
            "weighted high-potential sleeve (cached discovery BUY picks, seed "
            "fallback). When false the high tier stays empty (plan-bound only)."
        ),
    ),
    fleet_review: bool = Query(
        False,
        description=(
            "When true, deployment judgment calls the deterministic layer refuses "
            "to invent (NEEDS_FLEET_REVIEW — e.g. adding NVDA-correlated exposure "
            "while the book is over the plan cap) are adjudicated LIVE by the agent "
            "fleet (RiskOfficer 3-perspective + FundManager). EXPENSIVE + slow "
            "(several LLM calls per held candidate, minutes); opt-in per call. "
            "Fail-closed: any error leaves the candidate held, never auto-approved. "
            "Requires the deployment_fleet_review_enabled master switch."
        ),
    ),
    include_order_sheet: bool = Query(
        False,
        description=(
            "Resolve live per-line facts and return the canonical quantity-bearing "
            "order sheet. Missing facts, portfolio coverage, or conflicting voices "
            "produce an explicit invalid/unavailable artifact, never a prose fallback."
        ),
    ),
    allow_sells: bool = Query(
        True,
        description="Permit an after-tax sell/trim to fund a higher-conviction buy.",
    ),
    horizon_years_min: int = Query(1, ge=1, le=30),
    horizon_years_max: int = Query(5, ge=1, le=30),
    db: Session = Depends(get_db),
) -> DeploymentPlanDTO:
    """Plan-bound, risk-tiered, estate-annotated deploy list for a net-of-tax amount.

    ``cash_usd`` is the deployable (net-of-tax) amount. Omit it (None) to default
    to the detected idle cash from the latest snapshot; pass an explicit ``0`` to
    get an empty/zero plan (no silent substitution).

    ``live=true`` assembles a live market context (S&P/VIX/FX/BoI/CPI + NVDA
    verification) and threads it through the deployment plan and its DTO. When
    ``live`` is omitted or false the route behaves exactly as in P1 (no live
    calls, ``market_context`` is null in the response).

    ``sleeve_pct`` / ``use_high_potential`` control the high-potential sleeve:
    ``sleeve_pct`` of the deploy amount is carved off the top into the high tier
    (conviction-weighted, estate-tagged). Cached discovery BUY picks feed the
    sleeve synchronously (no live funnel run); seed candidates are the fallback.
    """
    from datetime import date as _date

    from argosy.services.deployment_advisor import assemble_deployment_plan

    doc, holdings, snap_cash = _load_current_doc_and_holdings(user_id, db)
    amount = cash_usd if cash_usd is not None else snap_cash

    ctx = None
    if live:
        # Never compute an allocation on stale FX: refresh the BoI cache on demand
        # when the user requests a deploy (a quick fetch; the user waits rather
        # than getting a stale/zero rate). Best-effort — last-known rate remains.
        try:
            from argosy.services.fx import refresh_if_stale

            refresh_if_stale(db, currencies=("USD",), max_stale_days=1)
        except Exception as exc:  # noqa: BLE001 — never break deploy on FX refresh
            _log.warning("deploy_cash.fx_refresh_failed", error=str(exc)[:120])
        from argosy.services.deployment_market_context import (
            assemble_deployment_market_context,
        )

        ctx = assemble_deployment_market_context(db)

    # USD/ILS for the household operating floor (shekel-denominated). Best
    # effort: on failure the floor is skipped and the plan SAYS SO in a caveat
    # rather than guessing a rate and under-reserving the family's expense cash.
    _usd_ils: float | None = None
    _inferred = cash_usd is None  # balance-derived, so the floor applies
    try:
        from argosy.services.fx import rate as _fx_rate

        _usd_ils = float(_fx_rate(db, "USD", "ILS", _date.today()))
    except Exception as exc:  # noqa: BLE001 — never break deploy on FX
        _log.warning("deploy_cash.usd_ils_unavailable", error=str(exc)[:120])

    plan = assemble_deployment_plan(
        doc=doc,
        holdings=holdings,
        deploy_amount_usd=amount,
        as_of=_date.today(),
        market_context=ctx,
        sleeve_pct=sleeve_pct,
        use_high_potential=use_high_potential,
        user_id=user_id,
        usd_ils=_usd_ils,
        cash_is_inferred=_inferred,
    )
    dto = deployment_plan_to_dto(plan, market_context=ctx)

    # FUNDING BREAKDOWN — WHERE the deploy money sits (2026-07-06 incident:
    # the surface said HOW MUCH but not that the pool spanned Leumi USD +
    # Leumi NIS + Schwab USD; the client filled everything from Leumi USD and
    # went negative). Pure snapshot derivation (same cash classifier as the
    # deployable total, so the table sums to the same number). Best-effort —
    # never breaks the route.
    try:
        from argosy.services.contracts import funding_breakdown_to_dto
        from argosy.services.deployment_funding import derive_cash_funding

        _funding_dto = None
        _fund_row = get_latest_snapshot_row(db, user_id)
        if _fund_row is not None:
            _funding_dto = funding_breakdown_to_dto(
                derive_cash_funding(
                    row_to_snapshot(_fund_row),
                    float(plan.deploy_amount_usd),
                    discovery_reserve_usd=float(getattr(plan, "discovery_reserve_usd", 0.0) or 0.0),
                )
            )
        dto.funding = _funding_dto
    except Exception as exc:  # noqa: BLE001 — additive; never break the route
        _funding_dto = None
        _log.warning("deploy_cash.funding_failed", user_id=user_id, error=str(exc)[:200])

    # Shadow research preflight (deterministic; behind the kill switch). Annotates
    # the response with per-candidate status/reason (look-through cap, reserve,
    # plan-gap) WITHOUT altering `tiers`. Never breaks deploy-cash on failure.
    if doc is not None and get_settings().deployment_funnel_enabled:
        try:
            from argosy.services.contracts import preflight_result_to_dto
            from argosy.services.deployment_funnel.from_plan import (
                run_preflight_for_plan,
            )

            # Feed the snapshot's stored prices so HELD symbols resolve without a
            # live fetch (UCITS tickers often need an exchange suffix on yfinance);
            # only genuinely-new symbols fall through to a live quote.
            snapshot_prices: dict[str, float] = {}
            _snap_obj = None
            _row = get_latest_snapshot_row(db, user_id)
            if _row is not None:
                _snap_obj = row_to_snapshot(_row)
                for _p in getattr(_snap_obj, "positions", []) or []:
                    _sym = (getattr(_p, "symbol", "") or "").strip().upper()
                    _px = getattr(_p, "current_price", None)
                    if _sym and _px:
                        snapshot_prices[_sym] = float(_px)

            # Two-phase flow: when the fleet is available, phase-1 marks flagged
            # candidates as PENDING fleet judgment (deterministic layer decides
            # nothing about investments — only clean plan-fills auto-approve). The
            # `fleet_review=true` call is phase 2 (the fleet adjudicates below).
            _fleet_on = get_settings().deployment_fleet_review_enabled
            from argosy.services.instrument_plan_class import load_classification_map

            _cmap = load_classification_map(db, user_id)
            result = run_preflight_for_plan(
                plan,
                doc=doc,
                holdings_usd=holdings,
                cash_usd=snap_cash,
                deployable_usd=amount,
                snapshot_prices=snapshot_prices,
                fleet_available=_fleet_on,
                snapshot=_snap_obj,
                classification_map=_cmap,
            )

            # Deterministic redirect (no LLM, no gold, no plan change): cash the
            # funnel won't place in its natural sleeve — an over-cap instrument
            # (R1GR) or T-bills when the reserve is already funded — flows into the
            # plan's OWN zero-NVDA diversifier ETFs (ex-US / EM / real-assets) so the
            # full amount deploys into plan holdings instead of sitting as cash.
            from argosy.services.deployment_funnel.from_plan import (
                redirect_overflow_to_diversifiers,
            )

            _plan2, _redirect_note = redirect_overflow_to_diversifiers(plan, result, doc)
            if _redirect_note:
                from dataclasses import replace as _dcr

                plan = _dcr(_plan2, caveats=tuple(_plan2.caveats) + (_redirect_note,))
                result = run_preflight_for_plan(
                    plan,
                    doc=doc,
                    holdings_usd=holdings,
                    cash_usd=snap_cash,
                    deployable_usd=amount,
                    snapshot_prices=snapshot_prices,
                    fleet_available=_fleet_on,
                    snapshot=_snap_obj,
                    classification_map=_cmap,
                )

            # Increment 2: route the genuine judgment calls the deterministic
            # layer REFUSED to invent (NEEDS_FLEET_REVIEW) to the agent fleet
            # (RiskOfficer 3-perspective + FundManager). Only when explicitly
            # asked (live=True) and enabled — the fleet call is expensive and
            # async. Fail-open: any error leaves those candidates HELD + surfaced,
            # never silently approved. Runs before size/rerank so the corrected
            # plan reflects the fleet's bounded verdict.
            _disposition = None
            if fleet_review and get_settings().deployment_fleet_review_enabled:
                try:
                    from argosy.services.deployment_funnel.fleet_review import (
                        DeploymentContext,
                        recommend_disposition_sync,
                    )
                    from argosy.services.deployment_funnel.from_plan import (
                        build_gate_inputs,
                    )

                    _gi = build_gate_inputs(
                        doc=doc,
                        holdings_usd=holdings,
                        cash_usd=snap_cash,
                    )
                    _plan_menu = tuple(
                        {
                            "sleeve": c.label,
                            "target_pct": float(getattr(c, "target_pct", 0.0) or 0.0),
                            "tickers": [
                                getattr(i, "symbol", None) for i in getattr(c, "instruments", [])
                            ],
                        }
                        for c in doc.classes
                    )
                    _dep_ctx = DeploymentContext(
                        book_usd=_gi.book_usd,
                        current_effective_nvda_usd=_gi.current_effective_nvda_usd,
                        nvda_cap_pct=_gi.nvda_cap_pct,
                        plan_classes=tuple(sorted(_gi.plan_classes)),
                        plan_menu=_plan_menu,
                        user_constraints=(
                            "Reduce NVDA concentration toward the plan cap of "
                            f"{_gi.nvda_cap_pct:.0f}%; prime directive is earliest "
                            "safe retirement (do not over-hold on caution alone)."
                        ),
                        market_note=(
                            ctx.summary if ctx is not None and hasattr(ctx, "summary") else ""
                        ),
                    )
                    # Phase 2 is a SINGLE fleet call: the affirmative disposition
                    # ("what to do with the full amount") — computed from the
                    # phase-1 enriched (clean fills vs flagged facts), so cash is
                    # never silent residue. We deliberately do NOT run a
                    # per-candidate adjudication here: that was 6x4 = ~24 agent
                    # calls whose VOLUME (not concurrency) triggered a claude.exe
                    # exit-1 storm on the Claude Code session, and it is redundant —
                    # the disposition already decides the disposition of every
                    # dollar. The bounded per-line adjudicator (fleet_review
                    # .adjudicate_*) stays available for a future low-volume use.
                    _disposition = recommend_disposition_sync(
                        result.enriched,
                        context=_dep_ctx,
                        deployable_usd=amount,
                        user_id=user_id,
                    )
                except Exception as exc:  # noqa: BLE001 — fail-open; keep held
                    _log.warning(
                        "deploy_cash.fleet_review_failed",
                        user_id=user_id,
                        error=str(exc),
                    )

            # Non-shadow: re-rank the actual buy list from the verdict (drop
            # vetoed/deferred, resize capped) so the UI shows the CORRECTED plan,
            # not just annotations. Shadow keeps the original list + annotation.
            if not get_settings().deployment_funnel_shadow:
                from argosy.services.deployment_funnel.from_plan import rerank_plan
                from argosy.services.deployment_funnel.sizer import size_deployment

                sized = size_deployment(list(result.enriched), deployable_usd=amount)
                dto = deployment_plan_to_dto(rerank_plan(plan, sized), market_context=ctx)
                dto.funding = _funding_dto  # rebuild must not drop the table
            dto.preflight = preflight_result_to_dto(result)
            if _disposition is not None:
                from argosy.services.contracts import disposition_to_dto

                dto.disposition = disposition_to_dto(_disposition)
        except Exception as exc:  # noqa: BLE001 — additive; never break the route
            _log.warning(
                "deploy_cash.preflight_failed",
                user_id=user_id,
                error=str(exc),
            )

    # Fleet-authors / determinism-verifies pivot (behind deployment_author_enabled):
    # the LLM AUTHORS the allocation and the deterministic verifier gates it. On an
    # accepted proposal `dto.authored` is the primary recommendation; on
    # rejected/unavailable it is marked degraded and the deterministic `tiers` above
    # are the labelled fallback. Fully additive — never breaks the fast GET path.
    _has_actionable_recommendations = False
    if doc is not None and allow_sells and float(amount or 0.0) <= 0:
        try:
            from argosy.services.current_recommendations import (
                load_actionable_recommendations,
            )

            _has_actionable_recommendations = bool(
                load_actionable_recommendations(db, user_id=user_id)
            )
        except Exception as exc:  # noqa: BLE001 - explicit false on read failure
            _log.warning(
                "deploy_cash.current_recommendations_failed",
                user_id=user_id,
                error=str(exc)[:120],
            )
    _has_pending_research = False
    if doc is not None and include_order_sheet:
        from argosy.services.allocation_research import pending_tasks

        _has_pending_research = bool(pending_tasks(db, user_id))
    if (
        doc is not None
        and (float(amount or 0.0) > 0 or _has_actionable_recommendations or _has_pending_research)
        and get_settings().deployment_author_enabled
    ):
        try:
            from argosy.services.allocation_author.packet_assembly import (
                assemble_author_packet,
            )
            from argosy.services.allocation_author.reliable import authored_allocation
            from argosy.services.contracts import authored_outcome_to_dto

            # The ONE packet wiring (NVDA look-through, sleeve gaps, market regime,
            # per-candidate research) — shared with the daily period-directive job
            # so the proactive push and the on-demand pull feed the author the
            # same holistic view. See allocation_author/packet_assembly.py.
            packet = assemble_author_packet(
                db,
                user_id=user_id,
                doc=doc,
                holdings_usd=holdings,
                cash_usd=float(amount),
                deployable_usd=amount,
                additional_user_constraints=(
                    "SELLS ARE FORBIDDEN for this run; allocate only the stated new cash."
                    if not allow_sells
                    else "After-tax sells may fund a higher-conviction switch when justified."
                ),
            )
            packet["allow_sells"] = allow_sells
            packet["horizon_years"] = [horizon_years_min, horizon_years_max]
            # Resolve authored funding sells against the same live quote/tax result
            # later used by the order sheet.  Cache by exact gross amount so a
            # revision and final projection cannot drift onto different prices.
            _sale_resolutions = {}
            _sale_execution_facts = {}
            _review_execution_facts = {}

            def _resolve_sale(symbol: str, gross_usd: float):
                from argosy.services.order_sheet_facts import collect_execution_facts
                from argosy.services.sale_tax_facts import resolve_authoritative_sale

                _key = (symbol.strip().upper(), round(float(gross_usd), 2))
                if _key in _sale_resolutions:
                    return _sale_resolutions[_key]
                # A revised amount is priced against the same run-scoped
                # quote, not a moving target. Final projection reuses it too.
                if _key[0] not in _sale_execution_facts:
                    _facts, _failures = collect_execution_facts([_key[0]], doc=doc)
                    if _failures or _key[0] not in _facts:
                        raise ValueError(
                            f"{_key[0]} live sale facts unavailable: "
                            + "; ".join(_failures.values())
                        )
                    _sale_execution_facts[_key[0]] = _facts[_key[0]]
                _resolved = resolve_authoritative_sale(
                    db,
                    user_id=user_id,
                    symbol=_key[0],
                    gross_proceeds_usd=_key[1],
                    current_price_usd=_sale_execution_facts[_key[0]].evidence.price_usd,
                )
                _sale_resolutions[_key] = _resolved
                return _resolved

            from argosy.services.allocation_author.verifier import (
                GateFailure as _GateFailure,
            )
            from argosy.services.allocation_author.verifier import (
                GateReport as _GateReport,
            )
            from argosy.services.allocation_author.verifier import (
                GateStatus as _GateStatus,
            )
            from argosy.services.allocation_author.verifier import (
                verify_allocation_proposal as _verify_allocation_proposal,
            )

            _team = None
            _team_history = []
            _core_research_recovery = False
            _recovery_symbols = set()
            _recovery_objections = []

            def _verify_with_team(proposal, pkt):
                """Run arithmetic first, then bounce stop-level judgment
                objections to the same author for one coherent voice."""
                nonlocal _team, _core_research_recovery
                if proposal.research_separation_blocker:
                    return _GateReport(status=_GateStatus.BLOCK, failures=[_GateFailure(
                        code="core_research_not_independent",
                        detail=proposal.research_separation_blocker, severity="block",
                    )])
                if _core_research_recovery and not proposal.pending_research:
                    return _GateReport(status=_GateStatus.BLOCK, failures=[_GateFailure(
                        code="core_research_recovery_incomplete",
                        detail="Recovery must retain typed pending research or explicitly report why core cannot be separated.",
                        severity="block",
                    )])
                pending_symbols = {s for item in proposal.pending_research for s in item.tickers}
                missing_research = _recovery_symbols - pending_symbols
                if missing_research:
                    return _GateReport(status=_GateStatus.BLOCK, failures=[_GateFailure(
                        code="core_research_coverage_missing",
                        detail="Recovery dropped disputed alternatives: " + ", ".join(sorted(missing_research)),
                        severity="block",
                    )])
                report = _verify_allocation_proposal(
                    proposal,
                    pkt,
                    sale_resolver=_resolve_sale,
                )
                if report.status != _GateStatus.ACCEPT:
                    return report
                from argosy.services.deploy_decision_team import (
                    run_deploy_decision_team,
                )

                # Give blind reviewers current incorporation/situs evidence for
                # every proposed name. Static reference coverage is incomplete for
                # newly selected/discovered symbols; missing wiring must not be
                # mistaken for a judgment that the name is unsafe.
                from argosy.services.order_sheet_facts import (
                    collect_execution_facts,
                )

                proposed_symbols = {
                    buy.symbol.strip().upper() for buy in (proposal.buys or [])
                }
                missing_review_facts = sorted(
                    proposed_symbols - set(_review_execution_facts)
                )
                if missing_review_facts:
                    live_facts, _ = collect_execution_facts(
                        missing_review_facts,
                        doc=doc,
                    )
                    _review_execution_facts.update(live_facts)
                team_packet = {**pkt}
                if _core_research_recovery:
                    # Earlier independent objections are evidence, not the
                    # current author's rationale. Preserve portfolio-wide
                    # concerns as well as named alternatives for fresh review.
                    team_packet["recovery_review_objections"] = _recovery_objections
                instrument_facts = [
                    dict(item) for item in (pkt.get("instrument_facts") or [])
                ]
                facts_by_symbol = {
                    str(item.get("symbol", "")).upper(): item
                    for item in instrument_facts
                }
                for symbol in sorted(proposed_symbols):
                    execution = _review_execution_facts.get(symbol)
                    if execution is None:
                        continue
                    evidence = execution.evidence
                    row = facts_by_symbol.setdefault(symbol, {"symbol": symbol})
                    row.update(
                        {
                            "incorporation_country": evidence.incorporation_country,
                            "domicile": evidence.incorporation_country,
                            "live_price_usd": evidence.price_usd,
                            "market_cap_usd": evidence.market_cap_usd,
                            "facts_as_of": evidence.price_as_of.isoformat(),
                        }
                    )
                    if execution.estate_situs != "unknown":
                        row["us_situs"] = execution.estate_situs == "US"
                team_packet["instrument_facts"] = list(facts_by_symbol.values())

                _team = run_deploy_decision_team(
                    team_packet,
                    proposal,
                    user_id=user_id,
                )
                _team_history.append(_team)
                if _team.degraded:
                    return _GateReport(
                        status=_GateStatus.BLOCK,
                        failures=[
                            _GateFailure(
                                code="team_review_incomplete",
                                detail=(
                                    "Deployment review incomplete: "
                                    f"{_team.reviewers_ran}/"
                                    f"{_team.reviewers_expected} independent "
                                    "lenses returned valid judgments. Refusing "
                                    "to validate with a missing voice."
                                ),
                                severity="block",
                            )
                        ],
                    )
                if not _team.material_flagged:
                    return report
                failures = []
                persistent_disagreement = len(_team_history) >= 2
                # One bounded opportunity to author an independent core/reserve
                # decision. No old line is promoted or removed by code; all
                # lenses must freshly agree with the replacement.
                separation_revision = (
                    len(_team_history) == 2 and not proposal.pending_research
                )
                if separation_revision:
                    _core_research_recovery = True
                    for item in _team.material_flagged:
                        _recovery_objections.append(item)
                        symbol = str(item.get("symbol") or "").strip().upper()
                        if symbol and symbol != "PORTFOLIO":
                            _recovery_symbols.add(symbol)
                        for objection in item.get("objections", []):
                            if objection.get("impact", "advisory_only") == "advisory_only" and objection.get("severity") != "block":
                                continue
                            alternative = str(objection.get("recommended_ticker") or "").strip().upper()
                            if alternative and alternative != "PORTFOLIO":
                                _recovery_symbols.add(alternative)
                    failures.append(_GateFailure(
                        code="core_research_recovery", severity="revision",
                        detail="Enter independent-core/pending-research recovery, not another moonshot selection attempt. "
                        "Re-author core funding with a shared research reserve and typed pending issues, "
                        "or set research_separation_blocker if shared dependencies prevent it. "
                        "Preserve these disputed alternatives in pending_research: " + ", ".join(sorted(_recovery_symbols)),
                    ))
                for item in _team.material_flagged:
                    concerns = []
                    for objection in item.get("objections", []):
                        if (
                            objection.get("impact", "advisory_only") == "advisory_only"
                            and objection.get("severity") != "block"
                        ):
                            continue
                        recommendation = ""
                        if objection.get("recommended_amount_usd") is not None:
                            recommendation += (
                                f" recommended amount "
                                f"${float(objection['recommended_amount_usd']):,.0f}"
                            )
                        if objection.get("recommended_ticker"):
                            recommendation += (
                                f" recommended ticker {objection['recommended_ticker']}"
                            )
                        concerns.append(
                            f"[{objection.get('impact')}] "
                            f"{objection.get('concern', '')}{recommendation}"
                        )
                    failures.append(
                        _GateFailure(
                            code=(
                                "team_disagreement_unresolved"
                                if persistent_disagreement
                                else "team_judgment_objection"
                            ),
                            detail=(
                                (
                                    f"{item['symbol']} still has a material team "
                                    "disagreement after re-review: "
                                    if persistent_disagreement
                                    else f"{item['symbol']} must be re-authored: "
                                )
                                + " | ".join(concerns)
                                + (
                                    " Re-author an independent core allocation with explicit "
                                    "pending_research and conserved reserve if defensible. "
                                    "Otherwise retain the objection; no partial approval."
                                    if separation_revision else ""
                                )
                            ),
                            severity=(
                                "block" if persistent_disagreement and not separation_revision else "revision"
                            ),
                        )
                    )
                return _GateReport(
                    status=(
                        _GateStatus.BLOCK
                        if persistent_disagreement and not separation_revision
                        else _GateStatus.REVISION_REQUIRED
                    ),
                    failures=failures,
                )

            outcome = authored_allocation(
                packet,
                user_id=user_id,
                verify=_verify_with_team,
                max_revisions=get_settings().deployment_author_max_revisions,
            )
            dto.authored = authored_outcome_to_dto(
                outcome,
                held_symbols={s.upper() for s in holdings},
            )
            # The estate HEADLINE must describe the allocation the client will
            # actually act on: when the author's proposal is ACCEPTED, recompute
            # us_situs_exposed/sanctioned from the AUTHORED buys (situs by
            # instrument-reference facts, conservative for uncurated) instead of
            # the legacy deterministic `tiers` list. The tiers-based number
            # stays only when there is no accepted authored allocation.
            if dto.authored is not None and dto.authored.status == "accepted" and dto.authored.buys:
                try:
                    from argosy.services.deployment_advisor import (
                        authored_estate_exposure,
                    )

                    _exp, _sanc = authored_estate_exposure(dto.authored.buys, doc)
                    dto.us_situs_exposed_usd = _exp
                    dto.us_situs_sanctioned_usd = _sanc
                except Exception as exc:  # noqa: BLE001 — keep tiers-based number
                    _log.warning(
                        "deploy_cash.authored_estate_failed",
                        error=str(exc)[:120],
                    )
            # The decision TEAM reviews the author's proposal by JUDGMENT (blind
            # reviewers re-derive from raw facts and object). Blocking objections
            # bounce to the author; advisory notes annotate the accepted voice.
            _proposal = getattr(outcome, "proposal", None)
            if _proposal is not None and (
                getattr(_proposal, "buys", None)
                or getattr(_proposal, "sells", None)
                or _team is not None
            ):
                try:
                    from argosy.services.contracts import (
                        TeamFlaggedBuyDTO,
                        TeamObjectionDTO,
                        TeamReviewDTO,
                    )
                    if _team is None:
                        from argosy.services.deploy_decision_team import (
                            run_deploy_decision_team,
                        )

                        _team = run_deploy_decision_team(
                            packet,
                            _proposal,
                            user_id=user_id,
                        )
                        _team_history.append(_team)
                    # This GET is diagnostic/compositional and must not create
                    # separate inbox actions. It returns the complete team result
                    # below; only the canonical daily workflow publishes a
                    # unified client-facing artifact.
                    dto.team_review = TeamReviewDTO(
                        reviewers_ran=_team.reviewers_ran,
                        reviewers_expected=_team.reviewers_expected,
                        degraded=_team.degraded,
                        approved=[b.symbol for b in _team.approved],
                        flagged=[
                            TeamFlaggedBuyDTO(
                                symbol=f["symbol"],
                                amount_usd=f["amount_usd"],
                                proposed=f.get("proposed", True),
                                objections=[TeamObjectionDTO(**o) for o in f["objections"]],
                            )
                            for f in _team.flagged
                        ],
                    )
                except Exception as exc:  # noqa: BLE001 — team review is additive
                    _log.warning("deploy_cash.team_review_failed", error=str(exc)[:120])

            # The author chose the instruments above. This optional projection
            # adds only live facts, quantities, full-book NO-ACTION coverage and
            # conflict/arithmetic validation; it never substitutes a ticker.
            if include_order_sheet and outcome.status == "accepted" and _proposal is not None:
                from argosy.services.contracts import OrderSheetArtifactDTO

                if _team is not None and getattr(_team, "material_flagged", None):
                    dto.order_sheet = OrderSheetArtifactDTO(
                        status="invalid",
                        failures=[
                            "deploy decision team has unresolved objections: "
                            + ", ".join(
                                sorted(f["symbol"] for f in _team.material_flagged)
                            )
                        ],
                    )
                else:
                    try:
                        from sqlalchemy import func as _func
                        from sqlalchemy import select as _select

                        from argosy.services.deploy_decision_team import (
                            build_review_resolution,
                        )
                        from argosy.services.order_sheet_builder import build_order_sheet
                        from argosy.services.order_sheet_facts import (
                            collect_execution_facts,
                        )
                        from argosy.services.order_sheet_state import (
                            build_no_action_lines,
                            load_portfolio_voices,
                        )
                        from argosy.state.models import ScanState

                        _symbols = [b.symbol for b in (_proposal.buys or [])] + [
                            s.symbol for s in (_proposal.sells or [])
                        ]
                        _symbols_to_fetch = [
                            s
                            for s in _symbols
                            if s.upper() not in _sale_execution_facts
                            and s.upper() not in _review_execution_facts
                        ]
                        _facts, _fact_failures = collect_execution_facts(
                            _symbols_to_fetch,
                            doc=doc,
                        )
                        _facts.update(_review_execution_facts)
                        _facts.update(_sale_execution_facts)
                        if _fact_failures:
                            dto.order_sheet = OrderSheetArtifactDTO(
                                status="unavailable",
                                failures=[
                                    f"{s}: {reason}" for s, reason in sorted(_fact_failures.items())
                                ],
                            )
                        else:
                            _portfolio_symbols = {s.upper() for s in holdings}
                            _voices = load_portfolio_voices(
                                db,
                                user_id=user_id,
                                portfolio_symbols=_portfolio_symbols,
                            )
                            _acted = {s.upper() for s in _symbols}
                            _no_action = build_no_action_lines(
                                holdings_usd=holdings,
                                acted_symbols=_acted,
                                voices_by_symbol=_voices,
                            )
                            _discovered = set(
                                db.execute(
                                    _select(ScanState.ticker).where(
                                        ScanState.user_id == user_id,
                                        ScanState.status == "active",
                                        _func.coalesce(
                                            ScanState.last_fleet_at,
                                            ScanState.last_estimated_at,
                                            ScanState.last_seen_at,
                                        )
                                        >= datetime.now(UTC) - timedelta(days=3),
                                    )
                                )
                                .scalars()
                                .all()
                            )
                            _resolved_sales = {
                                s.symbol.upper(): _resolve_sale(s.symbol, s.amount_usd)
                                for s in (_proposal.sells or [])
                            }
                            _built = build_order_sheet(
                                _proposal,
                                user_id=user_id,
                                new_cash_usd=float(amount),
                                holdings_usd=holdings,
                                book_usd=sum(float(v) for v in holdings.values()),
                                facts_by_symbol=_facts,
                                no_action=_no_action,
                                discovered_symbols=_discovered,
                                voices_by_symbol=_voices,
                                sale_resolutions=_resolved_sales,
                                staged_sell_policies=packet.get(
                                    "staged_sell_policies"
                                ),
                                horizon_years=(horizon_years_min, horizon_years_max),
                                review_resolution=build_review_resolution(
                                    _team_history, _proposal.pending_research,
                                ),
                            )
                            dto.order_sheet = OrderSheetArtifactDTO(
                                status=("validated" if _built.validation.valid else "invalid"),
                                sheet=_built.sheet,
                                validation=_built.validation,
                                failures=[
                                    f"{f.code}: {f.detail}" for f in _built.validation.failures
                                ],
                            )
                    except Exception as exc:  # noqa: BLE001 - fail explicit
                        dto.order_sheet = OrderSheetArtifactDTO(
                            status="unavailable",
                            failures=[f"order-sheet construction failed: {exc}"],
                        )
            if include_order_sheet and dto.order_sheet is None:
                from argosy.services.contracts import OrderSheetArtifactDTO

                _author_failures = []
                if getattr(outcome, "report", None) is not None:
                    _author_failures = [
                        f"{failure.code}: {failure.detail}"
                        for failure in (outcome.report.failures or [])
                    ]
                dto.order_sheet = OrderSheetArtifactDTO(
                    status=(
                        "invalid" if outcome.status == "rejected" else "unavailable"
                    ),
                    failures=(
                        [f"deployment author status is {outcome.status}"]
                        + _author_failures
                    ),
                )
        except Exception as exc:  # noqa: BLE001 — additive; never break the route
            _log.warning(
                "deploy_cash.author_failed",
                user_id=user_id,
                error=str(exc),
            )
    return dto


class MaterializeOrderSheetRequest(BaseModel):
    sheet: dict
    funding_account_id: str = Field(min_length=1)
    sell_accounts_by_symbol: dict[str, str] = Field(default_factory=dict)


@router.post("/deploy-cash/order-sheet/materialize")
def materialize_validated_order_sheet(
    body: MaterializeOrderSheetRequest,
    db: Session = Depends(get_db),
    _admin: None = Depends(require_admin_token),
) -> dict:
    """Put a validated sheet into the existing approval/execution/fill spine."""

    from argosy.services.order_sheet import OrderSheet
    from argosy.services.order_sheet_materializer import (
        materialize_order_sheet,
        order_sheet_fingerprint,
    )

    sheet = OrderSheet.model_validate(body.sheet)
    try:
        rows = materialize_order_sheet(
            db,
            sheet,
            funding_account_id=body.funding_account_id,
            sell_accounts_by_symbol=body.sell_accounts_by_symbol,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "status": "awaiting_human",
        "order_sheet_fingerprint": order_sheet_fingerprint(sheet),
        "proposal_ids": [row.id for row in rows],
    }


@router.get("/deploy-cash/order-sheet/{fingerprint}/audit")
def get_order_sheet_audit(
    fingerprint: str,
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> dict:
    from argosy.services.order_sheet_audit import audit_order_sheet

    return audit_order_sheet(
        db,
        user_id=user_id,
        fingerprint=fingerprint,
    ).model_dump(mode="json")


# --- Combined high-potential discovery surface (Slice 2) -------------------
# A NEW DTO + endpoints (codex #12): the existing $-based high-potential-sleeve
# endpoint/card stay until consumers migrate. This surface is CONVICTION-only
# (no dollar sizing) — fleet-graded picks + the cheap estimator shortlist.


class DiscoveryPickDTO(BaseModel):
    ticker: str
    conviction: str
    verdict: str
    thesis_md: str
    cites: list[str] = []
    # Additive provenance (2026-07-12 §7.1) — same shape as PositionThesisDTO
    falsifier_state: str = "none_recorded"
    falsifiers: list[str] = []
    next_validation: str | None = None
    last_fleet_check_at: str | None = None


def _attach_pick_provenance(
    db: Session, user_id: str, picks: list[DiscoveryPickDTO]
) -> list[DiscoveryPickDTO]:
    """Overlay verdict-registry provenance onto discovery pick DTOs."""
    if not picks:
        return picks
    from argosy.services.verdict_registry import provenance_for_subjects

    prov_map = provenance_for_subjects(db, user_id=user_id, subjects=[p.ticker for p in picks])
    out: list[DiscoveryPickDTO] = []
    for p in picks:
        prov = prov_map.get((p.ticker or "").upper())
        if prov is None:
            out.append(p)
            continue
        out.append(
            p.model_copy(
                update={
                    "falsifier_state": prov.falsifier_state,
                    "falsifiers": list(prov.falsifiers),
                    "next_validation": prov.next_validation,
                    "last_fleet_check_at": prov.last_fleet_check_at,
                }
            )
        )
    return out


class DiscoveryEstimateDTO(BaseModel):
    ticker: str
    go: bool
    conviction: str
    sentiment: float
    one_line: str


class SignalHorizonScorecardDTO(BaseModel):
    scored_outcomes: int
    win_rate: float | None
    avg_pnl_pct: float | None


class Signal180dScorecardDTO(SignalHorizonScorecardDTO):
    always_long_same_tickers_win_rate: float | None


class SignalHorizonsDTO(BaseModel):
    short: SignalHorizonScorecardDTO = Field(alias="30d")
    thesis: Signal180dScorecardDTO = Field(alias="180d")


class SignalScorecardDTO(BaseModel):
    source: str
    scored_outcomes: int
    win_rate: float | None
    avg_pnl_pct: float | None
    observation_days: int
    calibration: str
    horizons: SignalHorizonsDTO
    funnel_context_enabled: bool
    kill_reason: str | None


class DiscoverySourceDTO(BaseModel):
    key: str
    label: str
    tracked_count: int
    active_count: int
    quarantined_count: int
    dropped_stale_count: int
    scorecard: SignalScorecardDTO | None = None


class DiscoveryStagesDTO(BaseModel):
    tracked: int
    active: int
    quarantined: int
    dropped_stale: int
    estimated: int
    estimator_go: int
    fleet_graded: int
    fleet_buy: int
    open_trade_proposals: int


class DiscoveryTradeProposalDTO(BaseModel):
    id: int
    action: str
    confidence: str | None
    status: str
    decision_run_id: int | None
    created_at: str


class DiscoveryCandidateDTO(BaseModel):
    ticker: str
    status: str
    rank: int | None
    radar_score: float
    source_keys: list[str]
    source_labels: list[str]
    quarantine_reason: str
    estimator: DiscoveryEstimateDTO | None
    fleet: DiscoveryPickDTO | None
    latest_trade_proposal: DiscoveryTradeProposalDTO | None


class DiscoveryDTO(BaseModel):
    picks: list[DiscoveryPickDTO]
    estimated: list[DiscoveryEstimateDTO]
    last_refreshed_at: str | None
    note: str
    sources: list[DiscoverySourceDTO]
    stages: DiscoveryStagesDTO
    candidates: list[DiscoveryCandidateDTO]


_DISCOVERY_NOTE = (
    "Persisted high-potential discovery: active sources -> estimator triage -> "
    "research fleet grade -> trade proposal. Research asymmetry and trade "
    "confidence are separate stages; no dollar sizing. Refresh is smart — only "
    "new/changed names are re-researched."
)

_DISCOVERY_SOURCE_LABELS = {
    "attention": "Attention",
    "growth": "Growth fundamentals",
    "gov_contracts": "Government contracts",
    "insider_cluster": "Insider clusters",
    "momentum": "Momentum",
}
_OPEN_TRADE_PROPOSAL_STATUSES = (
    "draft",
    "cooling",
    "awaiting_human",
    "approved",
)


def _enabled_signal_stream_keys(config) -> set[str]:
    """Discover configured stream fields so future adapters appear generically."""
    if not getattr(config, "enabled", False):
        return set()
    fields = getattr(type(config), "model_fields", None)
    if fields is None:
        fields = {key for key in vars(config) if key != "enabled"} or {"gov_contracts"}
    keys: set[str] = set()
    for key in fields:
        if key == "enabled":
            continue
        value = getattr(config, key, None)
        if value is None or getattr(value, "enabled", True):
            keys.add(key)
    return keys


def _configured_signal_stream_keys(config) -> set[str]:
    fields = getattr(type(config), "model_fields", None)
    if fields is None:
        fields = {key for key in vars(config) if key != "enabled"} or {"gov_contracts"}
    return {key for key in fields if key != "enabled"}


def _discovery_source_ref(value: str) -> tuple[str, str] | None:
    """Return a stable source key + plain label for one persisted family."""
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.upper().startswith("SIGNAL_STREAM:"):
        raw = raw.split(":", 1)[1]
    key = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    if not key:
        return None
    label = _DISCOVERY_SOURCE_LABELS.get(
        key,
        key.replace("_", " ").capitalize(),
    )
    return key, label


def _discovery_sources_for_row(row) -> list[tuple[str, str]]:
    """Parse source families/evidence without trusting either persisted blob."""
    refs: set[tuple[str, str]] = set()
    fingerprint = row.radar_fingerprint or ""
    try:
        for part in fingerprint.split("|"):
            if part.startswith("f="):
                for family in part[2:].split(","):
                    ref = _discovery_source_ref(family)
                    if ref is not None:
                        refs.add(ref)
    except (AttributeError, TypeError):
        pass

    if row.nomination_evidence_json:
        try:
            evidence = json.loads(row.nomination_evidence_json)
            if isinstance(evidence, dict):
                stream = evidence.get("stream")
                if isinstance(stream, str):
                    ref = _discovery_source_ref(stream)
                    if ref is not None:
                        refs.add(ref)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return sorted(refs)


def _discovery_estimate(blob: str | None) -> DiscoveryEstimateDTO | None:
    if not blob:
        return None
    try:
        verdict = json.loads(blob)
        return DiscoveryEstimateDTO(
            ticker=verdict["ticker"],
            go=verdict["go"],
            conviction=verdict["conviction"],
            sentiment=verdict["sentiment"],
            one_line=verdict["one_line"],
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _discovery_pick(blob: str | None) -> DiscoveryPickDTO | None:
    if not blob:
        return None
    try:
        pick = json.loads(blob)
        return DiscoveryPickDTO(
            ticker=pick["ticker"],
            conviction=pick["conviction"],
            verdict=pick["verdict"],
            thesis_md=pick["thesis_md"],
            cites=list(pick.get("cites") or []),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _load_discovery_transparency(user_id: str):
    """Persisted source/stage/candidate trace. Never performs a live scan."""
    from sqlalchemy import func, select
    from sqlalchemy.orm import sessionmaker

    from argosy.config import load_signal_streams_config
    from argosy.state.db import create_sync_engine
    from argosy.state.models import Proposal, ScanState

    url = str(get_settings().database_url).replace("+aiosqlite", "")
    factory = sessionmaker(bind=create_sync_engine(url))
    signal_config = load_signal_streams_config(user_id)
    configured_signal_keys = _configured_signal_stream_keys(signal_config)
    enabled_signal_keys = _enabled_signal_stream_keys(signal_config)
    signal_scorecards: dict[str, dict[str, object]] = {}
    with factory() as db:
        rows = list(
            db.execute(
                select(ScanState).where(
                    ScanState.user_id == user_id,
                )
            ).scalars()
        )
        tickers = {row.ticker.upper() for row in rows}
        proposals = []
        if tickers:
            proposals = list(
                db.execute(
                    select(Proposal)
                    .where(
                        Proposal.user_id == user_id,
                        func.upper(Proposal.ticker).in_(tickers),
                    )
                    .order_by(
                        Proposal.created_at.desc(),
                        Proposal.id.desc(),
                    )
                ).scalars()
            )
        if enabled_signal_keys:
            from argosy.services.predictions.reliability import (
                signal_source_scorecard,
            )

            signal_scorecards = {
                key: signal_source_scorecard(db, user_id, key) for key in enabled_signal_keys
            }

    latest_proposal_by_ticker = {}
    for proposal in proposals:
        latest_proposal_by_ticker.setdefault(proposal.ticker.upper(), proposal)

    enabled_source_keys = {"attention", "growth", "momentum"}
    enabled_source_keys.update(enabled_signal_keys)
    source_tickers: dict[tuple[str, str], dict[str, set[str]]] = {
        (
            key,
            _DISCOVERY_SOURCE_LABELS.get(key, key.replace("_", " ").capitalize()),
        ): {
            "active": set(),
            "quarantined": set(),
            "dropped": set(),
        }
        for key in enabled_source_keys
    }
    candidates: list[DiscoveryCandidateDTO] = []
    estimated = estimator_go = fleet_graded = fleet_buy = 0
    status_order = {"active": 0, "quarantined": 1, "dropped": 2}
    for row in sorted(
        rows,
        key=lambda item: (
            status_order.get(item.status, 9),
            item.rank is None,
            item.rank or 0,
            item.ticker,
        ),
    ):
        refs = _discovery_sources_for_row(row)
        is_active = row.status == "active"
        for ref in refs:
            if ref[0] in configured_signal_keys and ref[0] not in enabled_signal_keys:
                continue
            counts = source_tickers.setdefault(
                ref,
                {"active": set(), "quarantined": set(), "dropped": set()},
            )
            if row.status in counts:
                counts[row.status].add(row.ticker.upper())

        estimate = _discovery_estimate(row.estimator_json)
        pick = _discovery_pick(row.fleet_json)
        estimated += is_active and estimate is not None
        estimator_go += is_active and estimate is not None and estimate.go
        fleet_graded += is_active and pick is not None
        fleet_buy += is_active and pick is not None and pick.verdict.upper() == "BUY"

        proposal = latest_proposal_by_ticker.get(row.ticker.upper())
        proposal_dto = None
        if proposal is not None:
            proposal_dto = DiscoveryTradeProposalDTO(
                id=proposal.id,
                action=proposal.action,
                confidence=proposal.confidence,
                status=proposal.status,
                decision_run_id=proposal.decision_run_id,
                created_at=proposal.created_at.isoformat(),
            )
        candidates.append(
            DiscoveryCandidateDTO(
                ticker=row.ticker,
                status=row.status,
                rank=row.rank,
                radar_score=row.last_score,
                source_keys=[key for key, _label in refs],
                source_labels=[label for _key, label in refs],
                quarantine_reason=row.quarantine_reason or "",
                estimator=estimate,
                fleet=pick,
                latest_trade_proposal=proposal_dto,
            )
        )

    sources = []
    for (key, label), status_tickers in sorted(
        source_tickers.items(),
        key=lambda item: item[0][0],
    ):
        tracked_tickers = set().union(*status_tickers.values())
        sources.append(
            DiscoverySourceDTO(
                key=key,
                label=label,
                tracked_count=len(tracked_tickers),
                active_count=len(status_tickers["active"]),
                quarantined_count=len(status_tickers["quarantined"]),
                dropped_stale_count=len(status_tickers["dropped"]),
                scorecard=signal_scorecards.get(key),
            )
        )
    statuses = [row.status for row in rows]
    stages = DiscoveryStagesDTO(
        tracked=len(rows),
        active=statuses.count("active"),
        quarantined=statuses.count("quarantined"),
        dropped_stale=statuses.count("dropped"),
        estimated=estimated,
        estimator_go=estimator_go,
        fleet_graded=fleet_graded,
        fleet_buy=fleet_buy,
        open_trade_proposals=sum(
            proposal.status in _OPEN_TRADE_PROPOSAL_STATUSES
            for proposal in latest_proposal_by_ticker.values()
        ),
    )
    return sources, stages, candidates


def _load_discovery_state(user_id: str):
    """(picks, estimated, last_refreshed_at) from the persisted ScanState — only
    ``active`` rows (dropped/quarantined are filtered, codex #8). Returns domain
    objects (FleetPick / EstimatorVerdict); the route maps them to DTOs.
    Best-effort."""
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker

    from argosy.services.high_potential_funnel import (
        _pick_from_json,
        _verdict_from_json,
    )
    from argosy.state.db import create_sync_engine
    from argosy.state.models import ScanState

    url = str(get_settings().database_url).replace("+aiosqlite", "")
    factory = sessionmaker(bind=create_sync_engine(url))
    picks = []
    estimated = []
    last: str | None = None
    with factory() as db:
        rows = (
            db.execute(
                select(ScanState).where(
                    ScanState.user_id == user_id,
                    ScanState.status == "active",
                )
            )
            .scalars()
            .all()
        )
        for r in rows:
            if r.last_radar_at is not None:
                iso = r.last_radar_at.isoformat()
                last = iso if last is None or iso > last else last
            if r.estimator_json:
                try:
                    estimated.append(_verdict_from_json(r.estimator_json))
                except (ValueError, KeyError, TypeError):
                    pass
            if r.fleet_json:
                try:
                    picks.append(_pick_from_json(r.fleet_json))
                except (ValueError, KeyError, TypeError):
                    pass
    return picks, estimated, last


@router.get("/discovery", response_model=DiscoveryDTO)
def get_discovery(
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> DiscoveryDTO:
    """Cached discovery highlights (instant): fleet picks + estimator shortlist
    from the persisted ScanState. Use POST /discovery/refresh to re-run."""
    picks, estimated, last = _load_discovery_state(user_id)
    sources, stages, candidates = _load_discovery_transparency(user_id)
    pick_dtos = _attach_pick_provenance(
        db,
        user_id,
        [
            DiscoveryPickDTO(
                ticker=p.ticker,
                conviction=p.conviction,
                verdict=p.verdict,
                thesis_md=p.thesis_md,
                cites=list(p.cites),
            )
            for p in picks
        ],
    )
    return DiscoveryDTO(
        picks=pick_dtos,
        estimated=[
            DiscoveryEstimateDTO(
                ticker=v.ticker,
                go=v.go,
                conviction=v.conviction,
                sentiment=v.sentiment,
                one_line=v.one_line,
            )
            for v in estimated
        ],
        last_refreshed_at=last,
        note=_DISCOVERY_NOTE,
        sources=sources,
        stages=stages,
        candidates=candidates,
    )


@router.post("/discovery/refresh", response_model=DiscoveryDTO)
async def refresh_discovery(
    user_id: str = Query("ariel"),
    force: bool = Query(False),
    db: Session = Depends(get_db),
) -> DiscoveryDTO:
    """Run the discovery funnel (smart by default; ``force=true`` re-researches
    everything) and return the refreshed highlights."""
    from argosy.services.high_potential_funnel import run_funnel

    result = await run_funnel(user_id, force=force)
    sources, stages, candidates = _load_discovery_transparency(user_id)
    pick_dtos = _attach_pick_provenance(
        db,
        user_id,
        [
            DiscoveryPickDTO(
                ticker=p.ticker,
                conviction=p.conviction,
                verdict=p.verdict,
                thesis_md=p.thesis_md,
                cites=list(p.cites),
            )
            for p in result.picks
        ],
    )
    return DiscoveryDTO(
        picks=pick_dtos,
        estimated=[
            DiscoveryEstimateDTO(
                ticker=v.ticker,
                go=v.go,
                conviction=v.conviction,
                sentiment=v.sentiment,
                one_line=v.one_line,
            )
            for v in result.estimated
        ],
        last_refreshed_at=result.last_refreshed_at,
        note=_DISCOVERY_NOTE,
        sources=sources,
        stages=stages,
        candidates=candidates,
    )


@router.post("/rebalance-review")
def post_rebalance_review(
    user_id: str = Query("ariel"),
    write_proposal: bool = Query(True),
    db: Session = Depends(get_db),
) -> dict:
    """Compose a holistic, plan-driven, news-supported whole-portfolio rebalance
    review (trim over-target sleeves to fund under-target ones, thesis-gated) and
    optionally persist it as a ``rebalance`` ActionProposal.

    Synchronous + deterministic — no LLM call. The review is ALWAYS returned;
    ``proposal_written`` reflects whether a proposal row was persisted (False on
    cannot_review / no-legs / write disabled). It NEVER executes.
    """
    from argosy.services.holistic_rebalance_review import (
        run_holistic_rebalance_review,
    )

    review, written = run_holistic_rebalance_review(
        user_id,
        db,
        write_proposal=write_proposal,
    )
    return {"review": review.to_dict(), "proposal_written": written}


def _allocation_agent_context(user_id: str) -> tuple[dict, dict]:
    """(per-position verdicts, market-context snapshot) for the allocation agent.

    Best-effort: both degrade to ``{}`` so the agent pass never fails the
    deterministic surface (codex #15 — under-specified inputs get an explicit
    empty fallback)."""
    verdicts: dict = {}
    market_context: dict = {}
    return verdicts, market_context


__all__ = ["router"]
