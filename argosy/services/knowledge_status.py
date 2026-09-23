"""Current knowledge coverage from append-only, user-scoped research receipts.

No research on GET, no historical JobRun changes, no trading eligibility decisions.
Legacy reports remain visible, but cannot certify changed/current document bytes.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

import yaml
from sqlalchemy import select

from argosy.agents.domain_refresh import FileRefreshResult, has_current_evidence
from argosy.state.models import AgentReport, AgentReportBlob, UserFile

CODE_ROOT = Path(__file__).resolve().parents[2]


def resolve_code_reference(reference: str) -> Path | None:
    target = (CODE_ROOT / reference).resolve()
    return target if target.is_relative_to(CODE_ROOT / "argosy") and target.suffix == ".py" else None


def document_key(path: str) -> str:
    return path.replace("\\", "/").removeprefix("domain_knowledge/")


def input_fingerprint(item: dict) -> str:
    # Date-only verification writeback is not a material document edit.
    metadata = yaml.safe_load(item.get("frontmatter", "")) or {}
    if isinstance(metadata, dict):
        metadata = {k: v for k, v in metadata.items() if k not in {"last_verified", "next_refresh_due"}}
        for source in metadata.get("sources", []):
            if isinstance(source, dict) and "url" in source:
                source.pop("retrieved", None)
    value = json.dumps([metadata, item.get("content", "").replace("\r\n", "\n")],
                       sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _local_version(path, root):
    if path is None or root is None:
        return "unavailable"
    target = path.resolve()
    if not target.is_relative_to(root.resolve()):
        return "outside_allowed_root"
    try:
        with target.open("rb") as source:
            raw = source.read(8_000_001)
        if len(raw) > 8_000_000:
            return "oversize:" + str(target.stat().st_mtime_ns)
        return hashlib.sha256(raw).hexdigest()
    except OSError:
        return "unavailable"


async def dependency_versions(session, *, user_id, item):
    """Version only cited local/catalog/code sources. Never fetch remote URLs on GET."""
    from argosy.config import get_settings
    metadata = yaml.safe_load(item.get("frontmatter", "")) or {}
    if not isinstance(metadata, dict):
        return []
    versions = []
    from argosy.services.knowledge_profile import profile_evidence
    profile = await profile_evidence(session, user_id=user_id, item=item)
    if profile is not None:
        versions.append([profile['url'], profile['status'], profile['sha256']])
    configured = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")
    resources = Path(configured).resolve() if configured else None
    for source in metadata.get("sources", []):
        url = source.get("url", "") if isinstance(source, dict) else ""
        if isinstance(url, str) and url.startswith("file://Resources/"):
            path = resources / unquote(url.removeprefix("file://Resources/")) if resources else None
            versions.append([url, _local_version(path, resources)])
        elif isinstance(url, str) and url.startswith('file://Knowledge/'):
            knowledge_root = get_settings().domain_knowledge_dir.resolve()
            path = knowledge_root / unquote(url.removeprefix('file://Knowledge/'))
            versions.append([url, _local_version(path, knowledge_root) if path.suffix == '.md' else 'unsupported_format'])
    repo = Path(get_settings().home).resolve()
    for reference in metadata.get("code_references", []):
        if isinstance(reference, str):
            versions.append([reference, _local_version(resolve_code_reference(reference), CODE_ROOT / "argosy")])
    for reference in metadata.get("catalog_sources", []):
        source_id = reference.get("id") if isinstance(reference, dict) else None
        if type(source_id) is not int:
            continue
        row = (await session.execute(select(UserFile).where(UserFile.id == source_id,
            UserFile.user_id == user_id, UserFile.deleted_at.is_(None)))).scalars().first()
        actual = _local_version(Path(row.storage_path), repo / "uploads" / user_id) if row else "unavailable"
        versions.append([f"file://Catalog/{source_id}", row.sha256 if row else None, actual])
    return sorted(versions, key=lambda v: v[0])


async def collect_knowledge_status(session, *, user_id: str, files=None, now=None) -> dict:
    from argosy.orchestrator.loops.annual import _default_files_provider

    now = now or datetime.now(UTC)
    files = list(_default_files_provider() if files is None else files)
    rows = (await session.execute(select(AgentReport.id, AgentReport.created_at, AgentReportBlob.value)
        .join(AgentReportBlob, AgentReportBlob.report_id == AgentReport.id)
        .where(AgentReport.user_id == user_id, AgentReport.agent_role == "domain_refresh",
               AgentReportBlob.key == "output_json")
        .order_by(AgentReport.id.desc()))).all()
    ids = [row[0] for row in rows]
    provenance = {}
    if ids:
        for report_id, value in (await session.execute(select(AgentReportBlob.report_id, AgentReportBlob.value)
            .where(AgentReportBlob.report_id.in_(ids), AgentReportBlob.key == "knowledge_input"))).all():
            try:
                provenance[report_id] = json.loads(value)
            except ValueError:
                pass
    latest = {}
    for report_id, checked_at, value in rows:
        proof = provenance.get(report_id, {})
        try:
            output = json.loads(value)
            raw_results = output.get("per_file", [])
            if proof and (len(raw_results) != 1 or proof.get("coverage_valid") is not True):
                raise ValueError("Invalid document coverage")
            for raw in raw_results:
                result = FileRefreshResult.model_validate(raw)
                key = document_key(result.path)
                if key not in latest:
                    latest[key] = (report_id, checked_at, result)
        except (ValueError, TypeError, AttributeError):
            # Do not silently fall back to an older green result after a
            # malformed newer response for a known requested document.
            key = proof.get("path")
            if key and key not in latest:
                latest[key] = (report_id, checked_at, FileRefreshResult(path=key,
                    status="no_change", verification="unavailable", note="Latest review output was invalid; repair and recheck."))
    documents = []
    for item in files:
        key = document_key(item["path"])
        record = latest.get(key)
        entry = {"path": key, "state": "not_reviewed", "report_id": None,
                 "checked_at": None, "reported_verification": None, "findings": [],
                 "note": "No document review recorded.", "input_matches": False}
        if record:
            report_id, checked_at, result = record
            proof = provenance.get(report_id, {})
            matches = (proof.get("coverage_valid") is True and proof.get("path") == key
                       and proof.get("fingerprint") == input_fingerprint(item)
                       and proof.get("dependencies_stable", True)
                       and proof.get("dependencies", []) == await dependency_versions(session, user_id=user_id, item=item))
            eligible = (matches and result.verification == "verified" and result.status == "no_change"
                        and not result.findings and has_current_evidence(result, checked_at.date())
                        and result.next_refresh_due is not None and result.next_refresh_due >= now.date())
            entry.update(report_id=report_id, checked_at=checked_at.replace(tzinfo=UTC).isoformat(),
                         reported_verification=result.verification, input_matches=matches,
                         state="verified" if eligible else "needs_review" if not matches else "incomplete",
                         note=result.note, findings=[f.model_dump(mode="json") for f in result.findings])
            if not matches:
                entry["review_reason"] = "Document changed or legacy review has no input fingerprint; recheck before treating it as current."
        documents.append(entry)
    verified = sum(d["state"] == "verified" for d in documents)
    return {"status": "verified" if documents and verified == len(documents) else "incomplete",
            "total": len(documents), "verified": verified,
            "reported_verified": sum(d["reported_verification"] == "verified" for d in documents),
            "outstanding": len(documents) - verified, "documents": documents}


async def knowledge_advice_context(*, user_id: str, paths: list[str]) -> str:
    """Supply limitations to the judging fleet, never decide the investment."""
    from argosy.state import db as db_mod
    wanted = {document_key(p) for p in paths}
    try:
        async with db_mod.get_session() as session:
            state = await collect_knowledge_status(session, user_id=user_id)
        documents = []
        for doc in state["documents"]:
            if doc["path"] not in wanted:
                continue
            documents.append({k: doc[k] for k in ("path", "state", "checked_at", "input_matches")})
            # Stale findings must not reintroduce an already answered question.
            if doc["input_matches"]:
                documents[-1].update(findings=doc["findings"], note=doc["note"])
        return ("KNOWLEDGE EVIDENCE LIMITATIONS (not an investment verdict): "
                "Explain any limitation that materially affects your recommendation; "
                "re-derive its consequence from the supplied sources. A missing/legacy "
                "review is not proof the rule is false. Do not treat historical balances "
                "as current or ask again for a declaration already supplied in current "
                "household records. Changed-input findings are intentionally omitted; "
                "use the current document, not a superseded audit.\n" + json.dumps(documents, ensure_ascii=False))
    except Exception:
        return "KNOWLEDGE EVIDENCE STATUS UNAVAILABLE: disclose this limitation; independently verify material rules before relying on them."
