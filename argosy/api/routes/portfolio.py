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
import os
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.config import get_settings
from argosy.ingest.tsv import parse_portfolio_tsv
from argosy.logging import get_logger
from argosy.services.portfolio_snapshot_store import (
    get_latest_snapshot_row,
    row_to_snapshot,
    write_through_if_changed,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])
_log = get_logger(__name__)


class PositionDTO(BaseModel):
    location: str
    currency: str
    asset_type: str
    details: str
    symbol: str
    shares: float | None
    current_price: float | None
    usd_value_k: float | None


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


_PORTFOLIO_TSV_HEADER_MARKER = "Bank account / funds allocation"


def _find_latest_tsv() -> Path | None:
    """Return the newest portfolio TSV under ARGOSY_HOME or None.

    Filters by the presence of the ``"Bank account / funds allocation"``
    header marker so stray small uploads (e.g. attachment placeholders
    under ``uploads/<user>/.../<timestamp>__<hash>__p.tsv``) don't shadow
    the real ``Family Finances Status - <date>.tsv`` file.
    """
    settings = get_settings()
    home = settings.home
    candidates: list[tuple[float, Path]] = []
    for tsv in home.rglob("*.tsv"):
        # Skip our own scratch / temp directories.
        s = str(tsv).lower()
        if any(seg in s for seg in (".venv", "node_modules", "__pycache__")):
            continue
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


def _snapshot_to_dto(snap) -> PortfolioSnapshotDTO:
    """Translate a parsed/hydrated PortfolioSnapshot to the route DTO."""
    positions: list[PositionDTO] = []
    for p in snap.positions:
        positions.append(
            PositionDTO(
                location=p.location,
                currency=p.currency,
                asset_type=p.asset_type,
                details=p.details,
                symbol=p.symbol,
                shares=p.shares,
                current_price=p.current_price,
                usd_value_k=p.usd_value_k,
            )
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
    return PortfolioSnapshotDTO(
        snapshot_date=snap.snapshot_date.isoformat() if snap.snapshot_date else None,
        fx_usd_nis=snap.fx_usd_nis,
        fx_usd_eur=snap.fx_usd_eur,
        total_usd_value_k=snap.total_usd_value_k,
        positions=positions,
        allocations=allocations,
        source_path=snap.source_path,
        parse_warnings=snap.parse_warnings,
    )


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
            user_id=user_id, error=str(exc),
        )
        row = None
    if row is not None:
        try:
            snap = row_to_snapshot(row)
            return _snapshot_to_dto(snap)
        except Exception as exc:  # noqa: BLE001 - defensive
            _log.warning(
                "portfolio_snapshot.db_hydrate_failed",
                user_id=user_id, row_id=row.id, error=str(exc),
            )
            # Fall through to filesystem walk.

    # 2. Filesystem fallback + write-through.
    tsv = _find_latest_tsv()
    if tsv is None:
        return PortfolioSnapshotDTO(
            snapshot_date=None,
            fx_usd_nis=None,
            fx_usd_eur=None,
            total_usd_value_k=0.0,
            positions=[],
            allocations=[],
            source_path=None,
            parse_warnings=["No TSV found under ARGOSY_HOME."],
        )

    snap = parse_portfolio_tsv(tsv)
    try:
        write_through_if_changed(db, user_id=user_id, snapshot=snap)
    except Exception as exc:  # noqa: BLE001 - defensive
        _log.warning(
            "portfolio_snapshot.write_through_failed",
            user_id=user_id, error=str(exc),
        )
    return _snapshot_to_dto(snap)


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
    r"Family Finances Status\s*-\s*(\d{2})\s*([A-Za-z]{3})", re.IGNORECASE,
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


@router.post("/upload-snapshot", response_model=UploadSnapshotResponse)
def upload_snapshot(
    file: UploadFile = File(...),
    user_id: str = Form("ariel"),
    fire_detector: bool = Form(True),
    db: Session = Depends(get_db),
) -> UploadSnapshotResponse:
    """Upload a monthly portfolio snapshot.

    Accepts two upload shapes, selected by content sniffing:

      * **Family Finances Status TSV** -- the long-standing path. Parsed
        by ``parse_portfolio_tsv``, persisted under the scan root,
        detector fires.
      * **Leumi monthly portfolio XLS** (SpreadsheetML 2003 envelope) --
        positions-only export from Leumi web banking. Routed through
        ``xls_osh_pair.handle_xls_upload`` which either synthesizes a
        merged TSV from a paired Leumi Osh statement + the most-recent
        prior TSV (positions / cash / allocation block recomputed,
        non-Leumi rows preserved verbatim) and fires the detector, OR
        queues the snapshot as ``status=pending_pair`` when no Osh
        statement is in window. The Osh-side hook resolves the pair
        when a matching Osh subsequently arrives. (Codex zigzag
        2026-05-29, session xls-osh-pair-design.)

    The route's contract:
      1. Read the multipart file bytes; SHA-256 returned to caller.
      2. Sniff content shape.
      3. Dispatch to the TSV path or XLS path.
      4. Optionally fire the windfall detector synchronously
         (default on; pass ``fire_detector=false`` to suppress).
    """
    contents = file.file.read()
    sha = hashlib.sha256(contents).hexdigest()

    # Cheap sniff against the first ~4KB.
    head_text = contents[:4096].decode("utf-8", errors="ignore")

    # XLS sniff first: SpreadsheetML envelope + Leumi-specific Hebrew
    # title marker. is_leumi_portfolio_xls handles both.
    from argosy.services.portfolio_ingest.xls_osh_pair import (
        handle_xls_upload,
        is_leumi_portfolio_xls,
    )
    if is_leumi_portfolio_xls(contents):
        return _handle_xls_branch(
            db=db, user_id=user_id, contents=contents,
            fire_detector=fire_detector, sha=sha,
        )

    # Write to a temp file so parse_portfolio_tsv can read by path.
    # The parser is path-based today; refactoring it to accept bytes
    # would be a larger change.
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=".tsv", delete=False,
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
                tsv_persisted=False, persisted_path=None,
                snapshot_date=None,
                detect_status="skipped", event=None, plan=None,
                detail=f"parse_portfolio_tsv raised: {exc}",
                sha256=sha,
            )

        target_root = _resolve_snapshot_root()
        target_root.mkdir(parents=True, exist_ok=True)
        target_name = _normalize_tsv_filename(file.filename or "", snap)
        target_path = target_root / target_name
        target_path.write_bytes(contents)
        _log.info(
            "portfolio_snapshot.uploaded",
            user_id=user_id, path=str(target_path),
            sha=sha[:8], size=len(contents),
        )

        # Best-effort write-through into the DB-backed snapshot store so
        # the next GET /snapshot returns the freshest data without a
        # filesystem walk.
        try:
            write_through_if_changed(db, user_id=user_id, snapshot=snap)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "portfolio_snapshot.write_through_failed",
                user_id=user_id, error=str(exc),
            )

        # Synchronous windfall detection. Codex zigzag flagged the
        # failure contract: TSV persist succeeds even when the
        # detector fails; we report the outcome independently.
        event_payload: dict | None = None
        plan_payload: dict | None = None
        detect_status = "skipped"
        if fire_detector:
            try:
                from argosy.services.retirement.windfall_allocator import (
                    propose_allocations,
                )
                from argosy.services.retirement.windfall_detector import (
                    DEFAULT_THRESHOLD_NIS, DEFAULT_THRESHOLD_USD, detect_windfall,
                )

                # Find a previous TSV to diff against -- the most-recent
                # other TSV under the scan root that isn't this one.
                prev_candidates = sorted(
                    (p for p in target_root.glob("Family Finances Status*.tsv")
                     if p.resolve() != target_path.resolve()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                prev = prev_candidates[0] if prev_candidates else None
                if prev is None:
                    detect_status = "skipped"
                else:
                    event = detect_windfall(
                        target_path, prev,
                        threshold_usd=DEFAULT_THRESHOLD_USD,
                        threshold_nis=DEFAULT_THRESHOLD_NIS,
                    )
                    detect_status = "ok"
                    if event is not None:
                        plan = propose_allocations(event)
                        event_payload = {
                            "detected_at": event.detected_at.isoformat(),
                            "cash_delta_usd": event.cash_delta_usd,
                            "cash_delta_nis": event.cash_delta_nis,
                            "cash_delta_total_usd_equiv": event.cash_delta_total_usd_equiv,
                            "fx_usd_nis": event.fx_usd_nis,
                            "classified_source": event.classified_source,
                            "requires_user_classification": event.requires_user_classification,
                            "matching_sales": [
                                {
                                    "symbol": s.symbol,
                                    "shares_sold": s.shares_sold,
                                    "current_price": s.current_price,
                                    "value_usd": round(s.value_usd, 2),
                                }
                                for s in event.matching_sales
                            ],
                            "allocation_delta_table": [
                                {
                                    "asset_class": l.asset_class,
                                    "current_pct": l.current_pct,
                                    "current_k_usd": l.current_k_usd,
                                    "target_pct": l.target_pct,
                                    "target_k_usd": l.target_k_usd,
                                    "delta_k_usd": l.delta_k_usd,
                                }
                                for l in event.allocation_delta_table
                            ],
                            "source_tsv": Path(event.source_tsv).name,
                            "previous_tsv": (
                                Path(event.previous_tsv).name
                                if event.previous_tsv else None
                            ),
                        }
                        plan_payload = plan.to_dict()
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "portfolio_snapshot.detector_failed",
                    user_id=user_id, error=str(exc),
                )
                detect_status = "failed"

        return UploadSnapshotResponse(
            tsv_persisted=True,
            persisted_path=str(target_path),
            snapshot_date=(
                snap.snapshot_date.isoformat() if snap.snapshot_date else None
            ),
            detect_status=detect_status,
            event=event_payload,
            plan=plan_payload,
            detail=None,
            sha256=sha,
        )
    finally:
        # Clean up the temp file regardless of success/failure path.
        try:
            tmp_path.unlink()
        except OSError:
            pass


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
            user_id=user_id, error=str(exc),
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
                resolution.snapshot_date.isoformat()
                if resolution.snapshot_date else None
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
                str(resolution.resolved_tsv_path)
                if resolution.resolved_tsv_path else None
            ),
            snapshot_date=(
                resolution.snapshot_date.isoformat()
                if resolution.snapshot_date else None
            ),
            detect_status="skipped",
            event=None,
            plan=None,
            detail=resolution.detail,
            sha256=sha,
            pending_pair_id=resolution.pending_pair_id,
        )

    # Resolved -- fire the detector against the freshly synthesized TSV.
    event_payload: dict | None = None
    plan_payload: dict | None = None
    detect_status = "skipped"
    target_path = resolution.resolved_tsv_path

    if fire_detector and target_path is not None:
        try:
            from argosy.services.retirement.windfall_allocator import (
                propose_allocations,
            )
            from argosy.services.retirement.windfall_detector import (
                DEFAULT_THRESHOLD_NIS, DEFAULT_THRESHOLD_USD, detect_windfall,
            )
            prev_candidates = sorted(
                (p for p in target_path.parent.glob("Family Finances Status*.tsv")
                 if p.resolve() != target_path.resolve()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            prev = prev_candidates[0] if prev_candidates else None
            if prev is None:
                detect_status = "skipped"
            else:
                event = detect_windfall(
                    target_path, prev,
                    threshold_usd=DEFAULT_THRESHOLD_USD,
                    threshold_nis=DEFAULT_THRESHOLD_NIS,
                )
                detect_status = "ok"
                if event is not None:
                    plan = propose_allocations(event)
                    event_payload = _event_to_dict(event)
                    plan_payload = plan.to_dict()
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "portfolio_snapshot.xls_detector_failed",
                user_id=user_id, error=str(exc),
            )
            detect_status = "failed"

    return UploadSnapshotResponse(
        tsv_persisted=True,
        persisted_path=str(target_path) if target_path else None,
        snapshot_date=(
            resolution.snapshot_date.isoformat()
            if resolution.snapshot_date else None
        ),
        detect_status=detect_status,
        event=event_payload,
        plan=plan_payload,
        detail=resolution.detail,
        sha256=sha,
        pending_pair_id=resolution.pending_pair_id,
    )


def _event_to_dict(event) -> dict:
    """Project a WindfallEvent into the JSON payload shape used by the
    response model + the existing /retirement/windfall/detect route."""
    return {
        "detected_at": event.detected_at.isoformat(),
        "cash_delta_usd": event.cash_delta_usd,
        "cash_delta_nis": event.cash_delta_nis,
        "cash_delta_total_usd_equiv": event.cash_delta_total_usd_equiv,
        "fx_usd_nis": event.fx_usd_nis,
        "classified_source": event.classified_source,
        "requires_user_classification": event.requires_user_classification,
        "matching_sales": [
            {
                "symbol": s.symbol,
                "shares_sold": s.shares_sold,
                "current_price": s.current_price,
                "value_usd": round(s.value_usd, 2),
            }
            for s in event.matching_sales
        ],
        "allocation_delta_table": [
            {
                "asset_class": l.asset_class,
                "current_pct": l.current_pct,
                "current_k_usd": l.current_k_usd,
                "target_pct": l.target_pct,
                "target_k_usd": l.target_k_usd,
                "delta_k_usd": l.delta_k_usd,
            }
            for l in event.allocation_delta_table
        ],
        "source_tsv": Path(event.source_tsv).name,
        "previous_tsv": (
            Path(event.previous_tsv).name
            if event.previous_tsv else None
        ),
    }


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
        db, user_id=user_id, snapshot_root=snapshot_root,
    )
    return GenerateTsvResponse(
        tsv_persisted=result.tsv_persisted,
        persisted_path=str(result.persisted_path) if result.persisted_path else None,
        snapshot_date=(
            result.snapshot_date.isoformat() if result.snapshot_date else None
        ),
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
        db, user_id=user_id, overage_ratio=overage_ratio,
    )
    if event is None:
        return None
    payload = event.to_dict()
    return UnallocatedCashProposalDTO(**payload)


__all__ = ["router"]
