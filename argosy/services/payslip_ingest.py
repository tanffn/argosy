"""Payslip ingestion + §102 withholding closed-loop service.

This is the wiring that turns the already-built payslip parser
(:func:`argosy.services.payslip_parser.parse_payslip`) and the §102 equity-tax
withholding check
(:func:`argosy.services.rsu_reconciliation.withholding_check.check_withholding`)
into a *live loop*: Argosy discovers the user's monthly Hilan payslip PDFs,
catalogs the raw bytes, parses them, runs the adequacy check, and persists the
facts + verdict so the question "is my RSU withholding adequate?" is **answered
by Argosy itself** rather than asked of the user — and re-answered on each new
payslip.

Flow per discovered PDF
-----------------------
1. Read the raw bytes off disk.
2. Route them through
   :func:`argosy.services.file_catalog.catalog_upload` (``source="payslip_ingest"``,
   ``kind="pdf"``) — the SINGLE boundary every user byte-blob ingest path must
   use (SDD §17.1). This gives a content-addressed, deduped catalog row + audit
   trail; the returned ``sha256`` is our idempotency key.
3. Skip the parse/persist work when a ``payslip_facts`` row already exists for
   this ``(user, period)`` AND its stored ``source_sha256`` matches — i.e. the
   exact bytes were already ingested. A *changed* PDF (new sha) re-parses and
   updates the row in place.
4. Parse via :func:`parse_payslip`, run :func:`check_withholding`, and UPSERT a
   ``payslip_facts`` row (parsed facts + verdict, serialized to JSON).

Discovery
---------
Payslips live at
``$ARGOSY_EXPENSE_SAMPLES_ROOT/<year>/Payslip/<Name>/<YYYY>_<MM>.pdf``. Only
Ariel has RSUs, so the default discovery targets ``Ariel`` (Noga's payslips —
``תלוש_שכר__YYYY_MM.pdf`` — carry no equity and are not the subject of this
check). Discovery is deterministic (sorted oldest→newest) and tolerant of a
missing samples root (returns empty + a skip note rather than raising).

Read-mostly + idempotent. Tests inject a ``session_factory`` bound to a scratch
SQLite engine and a ``samples_root`` override; the real run reads
``$ARGOSY_EXPENSE_SAMPLES_ROOT`` and the production DB.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from argosy.config import get_settings
from argosy.logging import get_logger
from argosy.services.payslip_parser import PayslipFacts, parse_payslip
from argosy.services.rsu_reconciliation.withholding_check import (
    WithholdingVerdict,
    check_withholding,
)
from argosy.state.models import PayslipFactRow

log = get_logger(__name__)

# The catalog channel + kind for payslip PDFs (see file_catalog._ALLOWED_*).
_CATALOG_SOURCE = "payslip_ingest"
_CATALOG_KIND = "pdf"
_MIME_PDF = "application/pdf"

# Filename → period key. Ariel's payslips are ``YYYY_MM.pdf``.
_PERIOD_RE = re.compile(r"(\d{4})[_-](\d{2})")

# Directories that should never shadow a real export (mirrors rsu_vest_pull).
_SKIP_SEGMENTS = (".venv", "node_modules", "__pycache__")


def _resolve_samples_root() -> Path | None:
    """Return the configured samples root, or ``None`` if unconfigured.

    Reads ``ARGOSY_EXPENSE_SAMPLES_ROOT`` directly — same convention as
    ``argosy/services/rsu_vest_pull.py::_resolve_samples_root`` and the
    expenses / portfolio routes.
    """
    env_root = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")
    if not env_root:
        return None
    root = Path(env_root)
    if not root.exists():
        return None
    return root


def discover_payslip_pdfs(root: Path, *, name: str = "Ariel") -> list[Path]:
    """Find ``name``'s payslip PDFs under the samples root.

    Looks under every ``<year>/Payslip/<name>/`` directory for ``YYYY_MM.pdf``
    files (the Hilan filename convention). Deterministic ordering
    (oldest→newest) so re-runs are stable. Scratch/build directories are
    filtered out so a stray copy never shadows a real export.
    """
    seen: set[Path] = set()
    out: list[Path] = []
    # ``<year>/Payslip/<name>/*.pdf`` — glob across all year folders.
    for pdf in root.glob(f"*/Payslip/{name}/*.pdf"):
        if pdf in seen:
            continue
        s = str(pdf).lower()
        if any(seg in s for seg in _SKIP_SEGMENTS):
            continue
        if not pdf.is_file():
            continue
        if not _PERIOD_RE.match(pdf.stem):
            continue  # only YYYY_MM-named payslips are period-keyable
        seen.add(pdf)
        out.append(pdf)
    out.sort()
    return out


def _build_sync_session_factory() -> tuple[sa.Engine, sessionmaker]:
    """Build a sync ``(engine, sessionmaker)`` bound to the production DB.

    Same shape as ``argosy/services/rsu_vest_pull.py``: strip the
    ``+aiosqlite`` driver and open a sync engine. Caller disposes the engine.
    """
    settings = get_settings()
    sync_url = settings.database_url.replace("+aiosqlite", "")
    engine = sa.create_engine(
        sync_url, connect_args={"check_same_thread": False}
    )
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, SessionLocal


def _serialize_facts(facts: PayslipFacts) -> str:
    return json.dumps(dataclasses.asdict(facts), ensure_ascii=False)


def _serialize_verdict(verdict: WithholdingVerdict) -> str:
    return json.dumps(dataclasses.asdict(verdict), ensure_ascii=False)


def deserialize_verdict(verdict_json: str) -> WithholdingVerdict:
    """Reconstruct a :class:`WithholdingVerdict` from its stored JSON."""
    data = json.loads(verdict_json)
    return WithholdingVerdict(**data)


async def _catalog_pdf(*, user_id: str, raw_bytes: bytes, name: str) -> Any:
    """Route the raw PDF bytes through the catalog boundary (SDD §17.1)."""
    from argosy.services.file_catalog import catalog_upload

    return await catalog_upload(
        user_id=user_id,
        raw_bytes=raw_bytes,
        original_name=name,
        mime_type=_MIME_PDF,
        kind=_CATALOG_KIND,
        source=_CATALOG_SOURCE,
    )


def _catalog_pdf_sync(*, user_id: str, raw_bytes: bytes, name: str) -> Any:
    """Sync wrapper so the (sync) ingest loop can call the async catalog helper.

    ``catalog_upload`` opens its own async sessions, so it must run on a fresh
    event loop, never nested inside one. The ingest service is invoked from the
    sync scheduler tick / route thread, so a plain ``asyncio.run`` is correct.
    """
    import asyncio

    return asyncio.run(
        _catalog_pdf(user_id=user_id, raw_bytes=raw_bytes, name=name)
    )


def ingest_payslips(
    user_id: str = "ariel",
    *,
    session_factory: sessionmaker | None = None,
    samples_root: Path | None = None,
    name: str = "Ariel",
) -> dict[str, Any]:
    """Discover + ingest ``name``'s payslip PDFs and persist facts + verdict.

    For each NEW/changed PDF: catalog the bytes (``catalog_upload``), parse,
    run the §102 withholding check, and UPSERT a ``payslip_facts`` row. A PDF
    whose bytes already match the stored ``source_sha256`` for its period is
    skipped (idempotent re-run). Returns a summary dict::

        {"ingested": int, "updated": int, "skipped": int, "errors": [str],
         "periods": [{"year": int, "month": int, "status": str}],
         "skipped_reason": str | None}

    Args:
        user_id: tenant id the facts are persisted under (DB key, lowercase).
        session_factory: optional sync sessionmaker (tests inject a scratch
            engine). When ``None``, a production-bound factory is built.
        samples_root: optional override of ``$ARGOSY_EXPENSE_SAMPLES_ROOT``.
        name: the payslip-owner folder name (default ``Ariel`` — the only
            person with RSUs).
    """
    summary: dict[str, Any] = {
        "ingested": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
        "periods": [],
        "skipped_reason": None,
    }

    root = samples_root or _resolve_samples_root()
    if root is None:
        summary["skipped_reason"] = "samples_root_unconfigured"
        log.info("payslip_ingest.skipped", reason="samples_root_unconfigured")
        return summary

    pdfs = discover_payslip_pdfs(root, name=name)
    if not pdfs:
        summary["skipped_reason"] = "no_payslips_found"
        log.info("payslip_ingest.skipped", reason="no_payslips_found", root=str(root))
        return summary

    engine: sa.Engine | None = None
    factory = session_factory
    if factory is None:
        engine, factory = _build_sync_session_factory()

    try:
        for pdf in pdfs:
            try:
                _ingest_one(user_id, pdf, name=name, factory=factory, summary=summary)
            except Exception as exc:  # noqa: BLE001 — one bad PDF never sinks the batch
                summary["errors"].append(f"{pdf.name}: {exc}")
                log.warning(
                    "payslip_ingest.file_failed", file=str(pdf), error=str(exc)
                )
    finally:
        if engine is not None:
            engine.dispose()

    log.info(
        "payslip_ingest.done",
        user_id=user_id,
        ingested=summary["ingested"],
        updated=summary["updated"],
        skipped=summary["skipped"],
        errors=len(summary["errors"]),
    )
    return summary


def _ingest_one(
    user_id: str,
    pdf: Path,
    *,
    name: str,
    factory: sessionmaker,
    summary: dict[str, Any],
) -> None:
    """Ingest a single payslip PDF. Idempotent on (period, sha256)."""
    raw = pdf.read_bytes()

    # 1) Catalog the bytes (SDD §17.1 — never bypass catalog_upload). The
    #    returned DTO carries the deduped sha256 + the catalog row id.
    dto = _catalog_pdf_sync(user_id=user_id, raw_bytes=raw, name=pdf.name)
    sha256 = dto.sha256
    file_id = dto.id

    # 2) Parse the period from the filename (authoritative) so we can check for
    #    an existing row BEFORE the (cheaper-to-skip) full parse.
    m = _PERIOD_RE.match(pdf.stem)
    if m is None:
        summary["errors"].append(f"{pdf.name}: unparseable period")
        return
    year, month = int(m.group(1)), int(m.group(2))

    session: Session = factory()
    try:
        existing = (
            session.execute(
                select(PayslipFactRow).where(
                    PayslipFactRow.user_id == user_id,
                    PayslipFactRow.period_year == year,
                    PayslipFactRow.period_month == month,
                )
            )
        ).scalar_one_or_none()

        if existing is not None and existing.source_sha256 == sha256:
            # Same bytes already ingested for this period — nothing to do.
            summary["skipped"] += 1
            summary["periods"].append(
                {"year": year, "month": month, "status": "skipped"}
            )
            return

        # 3) Parse + run the §102 withholding adequacy check.
        facts = parse_payslip(pdf)
        verdict = check_withholding(facts)
        parsed_json = _serialize_facts(facts)
        verdict_json = _serialize_verdict(verdict)
        now = datetime.now(timezone.utc)

        if existing is None:
            session.add(
                PayslipFactRow(
                    user_id=user_id,
                    period_year=year,
                    period_month=month,
                    source_file_id=file_id,
                    source_sha256=sha256,
                    parsed_json=parsed_json,
                    verdict_json=verdict_json,
                    ingested_at=now,
                )
            )
            summary["ingested"] += 1
            status = "ingested"
        else:
            existing.source_file_id = file_id
            existing.source_sha256 = sha256
            existing.parsed_json = parsed_json
            existing.verdict_json = verdict_json
            existing.ingested_at = now
            summary["updated"] += 1
            status = "updated"

        session.commit()
        summary["periods"].append(
            {"year": year, "month": month, "status": status}
        )
    finally:
        session.close()


def latest_withholding_verdict(
    user_id: str,
    session: Session,
) -> dict[str, Any]:
    """Return the most-recent period's withholding verdict for ``user_id``.

    Shape::

        {"has_verdict": bool,
         "period_year": int | None, "period_month": int | None,
         "ingested_at": str | None,  # ISO
         "verdict": dict | None,     # serialized WithholdingVerdict
         "status": str}              # the verdict status, or "no_data"

    "Most recent" = highest ``(period_year, period_month)``. Returns a
    ``has_verdict=False`` / ``status="no_data"`` shape when nothing has been
    ingested (never raises) so callers can render an honest empty state.
    """
    row = (
        session.execute(
            select(PayslipFactRow)
            .where(PayslipFactRow.user_id == user_id)
            .order_by(
                PayslipFactRow.period_year.desc(),
                PayslipFactRow.period_month.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    if row is None:
        return {
            "has_verdict": False,
            "period_year": None,
            "period_month": None,
            "ingested_at": None,
            "verdict": None,
            "status": "no_data",
        }

    verdict = json.loads(row.verdict_json)
    return {
        "has_verdict": True,
        "period_year": row.period_year,
        "period_month": row.period_month,
        "ingested_at": (
            row.ingested_at.isoformat() if row.ingested_at is not None else None
        ),
        "verdict": verdict,
        "status": str(verdict.get("status", "no_data")),
    }


# Statuses on which the withholding action item is considered satisfied by
# Argosy's own evidence. "reconciled" (the §102 tax accounted matches the model,
# refund-or-adequate) is the only positive, honest case. A "discrepancy",
# "low_confidence", or "no_equity_yet" must NOT silently satisfy the task.
_SATISFYING_STATUSES = frozenset({"reconciled"})


def withholding_action_status(
    user_id: str,
    session: Session,
) -> dict[str, Any]:
    """Closed-loop tie-in for the "Verify ... RSU withholding" action item.

    Returns ``{"has_verdict": bool, "status": str, "summary": str,
    "satisfied": bool, "period_year": int | None, "period_month": int | None}``.

    ``satisfied`` is True ONLY when Argosy has a verdict whose status is
    "reconciled" — i.e. the §102 equity tax accounted through the payslip
    matches the model (a top-up may be flagged but the reconciliation holds, and
    a refund is even possible). A "discrepancy" or low-confidence parse is NEVER
    silently satisfied — the action item must stay open so the user investigates.
    """
    latest = latest_withholding_verdict(user_id, session)
    if not latest["has_verdict"]:
        return {
            "has_verdict": False,
            "status": "no_data",
            "summary": (
                "No payslip has been ingested yet, so Argosy cannot verify the"
                " §102 RSU withholding. It will check automatically once a"
                " payslip is available."
            ),
            "satisfied": False,
            "period_year": None,
            "period_month": None,
        }

    verdict = latest["verdict"] or {}
    status = str(verdict.get("status", "no_data"))
    summary = str(verdict.get("summary", ""))
    return {
        "has_verdict": True,
        "status": status,
        "summary": summary,
        "satisfied": status in _SATISFYING_STATUSES,
        "period_year": latest["period_year"],
        "period_month": latest["period_month"],
    }


__all__ = [
    "ingest_payslips",
    "discover_payslip_pdfs",
    "latest_withholding_verdict",
    "withholding_action_status",
    "deserialize_verdict",
]
