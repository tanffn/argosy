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
from argosy.services.contracts import (
    AllocationCandidateDTO,
    DeploymentPlanDTO,
    ExecutableTaskDTO,
    candidate_to_dto,
    deployment_plan_to_dto,
    task_to_dto,
)
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

    Best-effort: the projection is additive, so any failure reading the plan
    (e.g. an unmigrated DB without plan_versions) falls back to the snapshot
    pie rather than breaking /portfolio."""
    try:
        from argosy.services.target_allocation_doc import load_plan_target_allocation
        from argosy.state.queries import get_current_plan

        pv = get_current_plan(db, user_id)
        doc = load_plan_target_allocation(pv) if pv is not None else None
        if doc is None or not doc.glide:
            return dto
        return dto.model_copy(update={"allocations": _allocations_from_doc(doc)})
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
            user_id=user_id, error=str(exc),
        )
        row = None
    if row is not None:
        try:
            snap = row_to_snapshot(row)
            return _project_canonical_allocations(_snapshot_to_dto(snap), db, user_id)
        except Exception as exc:  # noqa: BLE001 - defensive
            _log.warning(
                "portfolio_snapshot.db_hydrate_failed",
                user_id=user_id, row_id=row.id, error=str(exc),
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
        write_through_if_changed(db, user_id=user_id, snapshot=snap)
    except Exception as exc:  # noqa: BLE001 - defensive
        _log.warning(
            "portfolio_snapshot.write_through_failed",
            user_id=user_id, error=str(exc),
        )
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
        250_000.0, ge=0.0, le=100_000_000.0,
        description="Cash being redeployed; the sleeve is sleeve_pct of this.",
    ),
    sleeve_pct: float = Query(
        5.0, ge=0.0, le=25.0,
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
        4, ge=1, le=10,
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
            "is non-US-situs; single-name carve-out adds estate-tax exposure."
            + radar_note
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
        8.0, ge=0.5, le=500.0,
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
                ticker=c.ticker, name=c.name, score=c.score,
                families=list(c.families), reasons=list(c.reasons),
                price=c.price, market_cap=c.market_cap,
                dollar_volume=c.dollar_volume, pct_change=c.pct_change,
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
        syms = [
            c.ticker for c in _SEED_CANDIDATES
            if c.vehicle == "single_name" and c.held_today
        ]
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


@router.get("/allocation-breakdown", response_model=AllocationBreakdownDTO)
def get_allocation_breakdown(
    user_id: str = Query("ariel"),
    exclude_nvda: bool = Query(False),
    db: Session = Depends(get_db),
) -> AllocationBreakdownDTO:
    """LIVE current allocation (from the snapshot holdings, grouped by class)
    vs the canonical plan's class targets, with the per-symbol drill-down. This
    is the real 'current vs plan target' — not the plan glide's modelled anchor."""
    from argosy.services.allocation_breakdown import build_allocation_breakdown
    from argosy.services.target_allocation_doc import load_plan_target_allocation
    from argosy.state.queries import get_current_plan

    row = get_latest_snapshot_row(db, user_id)
    if row is None:
        return AllocationBreakdownDTO(rows=[], total_value_k=0.0,
                                      note="No portfolio snapshot found.")
    snap = row_to_snapshot(row)
    pv = get_current_plan(db, user_id)
    doc = load_plan_target_allocation(pv) if pv is not None else None
    rows = build_allocation_breakdown(snap, doc, exclude_nvda=exclude_nvda)
    note = ("Current = your live holdings grouped by asset class; target = the "
            "canonical plan's class targets. Click a class to see its symbols. "
            + ("" if doc is not None
               else "No current plan — targets shown blank."))
    return AllocationBreakdownDTO(
        rows=[CategoryBreakdownDTO(
            label=r.label, current_pct=r.current_pct, target_pct=r.target_pct,
            current_value_k=r.current_value_k,
            holdings=[HoldingRowDTO(symbol=h.symbol, name=h.name,
                      value_k=h.value_k, pct=h.pct, account=h.account)
                      for h in r.holdings],
        ) for r in rows],
        total_value_k=round(sum(r.current_value_k for r in rows), 2),
        note=note,
    )


# --- Real-estate net equity (net worth, separate from the investable book) --

class PropertyEquityDTO(BaseModel):
    name: str
    currency: str
    home_local: float | None
    loan_local: float | None
    net_local: float | None
    net_usd_k: float | None
    warnings: list[str]


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
    from argosy.services.real_estate_equity import compute_real_estate_equity

    row = get_latest_snapshot_row(db, user_id)
    if row is None:
        return RealEstateEquityDTO(properties=[], total_net_usd_k=0.0,
                                   note="No portfolio snapshot found.")
    snap = row_to_snapshot(row)
    eq = compute_real_estate_equity(
        snap.real_estate, fx_usd_nis=snap.fx_usd_nis, fx_usd_eur=snap.fx_usd_eur,
    )
    return RealEstateEquityDTO(
        properties=[PropertyEquityDTO(
            name=p.name, currency=p.currency, home_local=p.home_local,
            loan_local=p.loan_local, net_local=p.net_local,
            net_usd_k=p.net_usd_k, warnings=list(p.warnings),
        ) for p in eq.properties],
        total_net_usd_k=eq.total_net_usd_k,
        note=("Net equity = current value − outstanding loan, converted to USD. "
              "Net-worth context; not part of the investable allocation target."),
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


def _load_current_doc_and_holdings(user_id: str):
    """(TargetAllocationDoc | None, holdings_by_symbol, cash_usd) from the user's
    current accepted plan (PlanVersion role='current') + latest snapshot.
    Best-effort; ({}, 0.0) on miss — never raises."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from argosy.services.allocation_engine import tradeable_holdings
    from argosy.services.target_allocation_doc import load_plan_target_allocation
    from argosy.state.queries import get_current_plan

    url = str(get_settings().database_url).replace("+aiosqlite", "")
    factory = sessionmaker(
        bind=create_engine(url, connect_args={"check_same_thread": False}),
        expire_on_commit=False)
    with factory() as db:
        pv = get_current_plan(db, user_id)
        doc = load_plan_target_allocation(pv) if pv is not None else None
        row = get_latest_snapshot_row(db, user_id)
        holdings, cash = ({}, 0.0)
        if row is not None:
            holdings, cash = tradeable_holdings(row_to_snapshot(row))
    return doc, holdings, cash


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
        return AllocationTasksDTO(mode=mode, cash_usd=cash_usd, candidates=[],
                                  note="No current canonical plan — accept a plan first.")
    deploy_cash = cash_usd or snap_cash
    try:
        cands = compute_allocation(doc, holdings, AllocationMode(mode),
                                   cash_usd=deploy_cash)
    except ValueError as exc:
        # Fail loud: a non-conserving / malformed plan must surface, never
        # silently produce a mis-sized allocation.
        _log.warning("allocation-tasks could not size plan: %s", exc)
        return AllocationTasksDTO(
            mode=mode, cash_usd=deploy_cash, candidates=[],
            note=f"Could not size allocation from the current plan: {exc}")

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
            tasks = _aa.order_and_explain(cands, verdicts=verdicts,
                                          market_context=market_context,
                                          user_id=user_id)
            executable_tasks = [task_to_dto(t) for t in tasks]
        except Exception as exc:  # noqa: BLE001 — agent pass is additive
            _log.warning("allocation-tasks agent pass failed: %s", exc)
            executable_tasks = None
            agent_note = (" (agent ordering unavailable this run; showing the "
                          "deterministic candidates only.)")

    return AllocationTasksDTO(
        mode=mode, cash_usd=deploy_cash,
        candidates=[candidate_to_dto(c) for c in cands],
        executable_tasks=executable_tasks,
        note=("Plan-bound (canonical TargetAllocationDoc, glide-aware). Amounts "
              "deterministic; legs advisory (account/currency best-effort) and "
              "tax shown as advisory only. The agent (Slice 1b) orders + "
              "explains these." + agent_note),
    )


@router.get("/deploy-cash", response_model=DeploymentPlanDTO)
def get_deploy_cash(
    cash_usd: float | None = Query(None, ge=0.0),
    user_id: str = Query("ariel"),
    live: bool = Query(False),
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
    """
    from datetime import date as _date

    from argosy.services.deployment_advisor import assemble_deployment_plan

    doc, holdings, snap_cash = _load_current_doc_and_holdings(user_id)
    amount = cash_usd if cash_usd is not None else snap_cash

    ctx = None
    if live:
        from argosy.services.deployment_market_context import (
            assemble_deployment_market_context,
        )
        ctx = assemble_deployment_market_context(db)

    plan = assemble_deployment_plan(
        doc=doc, holdings=holdings, deploy_amount_usd=amount, as_of=_date.today(),
        market_context=ctx,
    )
    return deployment_plan_to_dto(plan, market_context=ctx)


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


class DiscoveryEstimateDTO(BaseModel):
    ticker: str
    go: bool
    conviction: str
    sentiment: float
    one_line: str


class DiscoveryDTO(BaseModel):
    picks: list[DiscoveryPickDTO]
    estimated: list[DiscoveryEstimateDTO]
    last_refreshed_at: str | None
    note: str


_DISCOVERY_NOTE = (
    "Fleet-graded high-potential discovery: radar -> cheap estimator triage -> "
    "Opus fleet grade. Conviction/verdict only (no dollar sizing). Refresh is "
    "smart — only new/changed names are re-researched."
)


def _load_discovery_state(user_id: str):
    """(picks, estimated, last_refreshed_at) from the persisted ScanState — only
    ``active`` rows (dropped/quarantined are filtered, codex #8). Returns domain
    objects (FleetPick / EstimatorVerdict); the route maps them to DTOs.
    Best-effort."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from argosy.services.high_potential_funnel import (
        _pick_from_json,
        _verdict_from_json,
    )
    from argosy.state.models import ScanState

    url = str(get_settings().database_url).replace("+aiosqlite", "")
    factory = sessionmaker(bind=create_engine(
        url, connect_args={"check_same_thread": False}))
    picks = []
    estimated = []
    last: str | None = None
    with factory() as db:
        rows = db.execute(select(ScanState).where(
            ScanState.user_id == user_id, ScanState.status == "active",
        )).scalars().all()
        for r in rows:
            if r.last_radar_at is not None:
                iso = r.last_radar_at.isoformat()
                last = iso if last is None or iso > last else last
            if r.estimator_json:
                try:
                    estimated.append(_verdict_from_json(r.estimator_json))
                except (ValueError, KeyError):
                    pass
            if r.fleet_json:
                try:
                    picks.append(_pick_from_json(r.fleet_json))
                except (ValueError, KeyError):
                    pass
    return picks, estimated, last


@router.get("/discovery", response_model=DiscoveryDTO)
def get_discovery(user_id: str = Query("ariel")) -> DiscoveryDTO:
    """Cached discovery highlights (instant): fleet picks + estimator shortlist
    from the persisted ScanState. Use POST /discovery/refresh to re-run."""
    picks, estimated, last = _load_discovery_state(user_id)
    return DiscoveryDTO(
        picks=[DiscoveryPickDTO(ticker=p.ticker, conviction=p.conviction,
               verdict=p.verdict, thesis_md=p.thesis_md, cites=list(p.cites))
               for p in picks],
        estimated=[DiscoveryEstimateDTO(ticker=v.ticker, go=v.go,
                   conviction=v.conviction, sentiment=v.sentiment,
                   one_line=v.one_line) for v in estimated],
        last_refreshed_at=last, note=_DISCOVERY_NOTE)


@router.post("/discovery/refresh", response_model=DiscoveryDTO)
async def refresh_discovery(
    user_id: str = Query("ariel"),
    force: bool = Query(False),
) -> DiscoveryDTO:
    """Run the discovery funnel (smart by default; ``force=true`` re-researches
    everything) and return the refreshed highlights."""
    from argosy.services.high_potential_funnel import run_funnel

    result = await run_funnel(user_id, force=force)
    return DiscoveryDTO(
        picks=[DiscoveryPickDTO(ticker=p.ticker, conviction=p.conviction,
               verdict=p.verdict, thesis_md=p.thesis_md, cites=list(p.cites))
               for p in result.picks],
        estimated=[DiscoveryEstimateDTO(ticker=v.ticker, go=v.go,
                   conviction=v.conviction, sentiment=v.sentiment,
                   one_line=v.one_line) for v in result.estimated],
        last_refreshed_at=result.last_refreshed_at, note=_DISCOVERY_NOTE,
    )


def _allocation_agent_context(user_id: str) -> tuple[dict, dict]:
    """(per-position verdicts, market-context snapshot) for the allocation agent.

    Best-effort: both degrade to ``{}`` so the agent pass never fails the
    deterministic surface (codex #15 — under-specified inputs get an explicit
    empty fallback)."""
    verdicts: dict = {}
    market_context: dict = {}
    return verdicts, market_context


__all__ = ["router"]
