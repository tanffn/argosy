"""Annual loop (SDD §5.1, Phase 7).

Cron `0 8 2 1 *` (January 2nd). Surfaces annual prompts to the user:
  - Tax-filing prep
  - W-8BEN refresh prompt
  - Insurance renewal prompt

Triggers a full domain re-verify (calls `DomainRefreshAgent` over every
file regardless of `next_refresh_due`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from argosy.agents.domain_refresh import (
    DomainRefreshAgent,
    DomainRefreshReport,
    has_current_evidence,
    write_back_refresh_results,
)
from argosy.api.events import publish_event
from argosy.config import get_settings
from argosy.execution.audit import record_audit_event
from argosy.logging import get_logger
from argosy.orchestrator.cost_guard import get_cost_guard
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule

_log = get_logger("argosy.loops.annual")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AnnualLoop(CadenceLoop):
    """Year-start prompts + full domain re-verify."""

    name = "annual"

    def __init__(
        self,
        *,
        schedule: LoopSchedule,
        enabled: bool = True,
        user_id: str = "ariel",
        domain_refresh_factory: Callable[[], DomainRefreshAgent] | None = None,
        domain_files_provider: Callable[[], Iterable[dict[str, str]]] | None = None,
        pension_refresh_callable: Callable[[str], Any] | None = None,
        domain_knowledge_root: Path | None = None,
    ) -> None:
        super().__init__(schedule=schedule, enabled=enabled)
        self.user_id = user_id
        self._refresh_factory = domain_refresh_factory or (
            lambda: DomainRefreshAgent(user_id=user_id)
        )
        self._files_provider = domain_files_provider or _default_files_provider
        # 2026-07-08 write-back fix: where the frontmatter verification
        # stamps land. Injectable so tests never touch the real
        # `domain_knowledge/` tree; defaults to settings at tick time.
        self._domain_knowledge_root = domain_knowledge_root
        # Phase 3: pluggable pension snapshot job (gemelnet adapter).
        # Defaults to None — when omitted the loop attempts the job
        # lazily and silently no-ops if the user has no `pensions`
        # block. Tests can inject a fake to avoid network access.
        self._pension_refresh: Callable[[str], Any] | None = pension_refresh_callable
        # Per-step outcome side-channel read by RegisteredScheduler
        # (`_safe_output_summary`) so `job_runs.output_summary` records
        # which sub-step failed even when tick() raises. Same contract
        # as NewsDailyJob's multi-stage summary.
        self.last_output_summary: dict[str, Any] | None = None

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict | None:
        # Reset the side-channel BEFORE any work so a raise never leaves the
        # adapter reading a PRIOR tick's summary (news_daily precedent).
        self.last_output_summary = None
        if os.environ.get("ARGOSY_KILL") == "1":
            _log.info("annual.kill_switch_skip")
            return None

        guard = get_cost_guard(user_id=self.user_id)
        if await guard.should_pause_non_routine(loop_name=self.name):
            _log.info("annual.cost_guard_paused")
            return None

        moment = (now or _utcnow)()

        prompts = [
            {"kind": "tax_filing_prep", "message": "Prepare prior-year tax filing (דוח שנתי)."},
            {
                "kind": "w8ben_refresh",
                "message": "Refresh W-8BEN at Schwab (3-year cycle).",
            },
            {"kind": "insurance_renewal", "message": "Review insurance policy renewals."},
        ]
        for p in prompts:
            try:
                await publish_event(
                    "annual.prompt",
                    {"user_id": self.user_id, "run_at": moment.isoformat(), **p},
                )
            except Exception:  # pragma: no cover - defensive
                _log.exception("annual.publish_failed")

        # Full domain re-verify. A sub-step failure here is captured (never
        # swallowed into a green job run) and re-raised at the END of the
        # tick, after the remaining independent steps + the audit event have
        # completed — so RegisteredScheduler closes the `job_runs` row with
        # status='error' (→ /api/jobs health red) while `last_output_summary`
        # still records the partial progress. Previously the exception was
        # logged and dropped, leaving the domain_refresh agent silently dead
        # for days while the annual job reported ok.
        refresh_error: str | None = None
        try:
            files = list(self._files_provider())
        except Exception as exc:  # noqa: BLE001 — captured, re-raised at end
            _log.exception("annual.files_provider_failed")
            files = []
            refresh_error = f"files_provider failed — {type(exc).__name__}: {exc}"

        refresh_summary: str | None = None
        writeback: dict[str, Any] | None = None
        agent_report_id: int | None = None
        agent_report_ids: list[int] = []
        file_results: list[dict[str, Any]] = []
        discrepancy_count = 0
        if files:
            combined = DomainRefreshReport()
            # Each document gets its own bounded research budget and durable
            # receipt. One failed source must not discard the other 18 files.
            async for item, outcome in _research_documents(files, self._refresh_factory):
                item_report_id: int | None = None
                try:
                    if isinstance(outcome, Exception):
                        raise outcome
                    report = outcome
                    # Persist before validating coverage: retain even malformed
                    # model output for diagnosis, but never let it write KB dates.
                    agent_report_id = await _persist_refresh_report(report, item=item)
                    item_report_id = agent_report_id
                    if agent_report_id is not None:
                        agent_report_ids.append(agent_report_id)
                    outputs = report.output.per_file
                    def normalize(path):
                        return path.replace("\\", "/").strip()
                    if len(outputs) != 1 or normalize(outputs[0].path) != normalize(item["path"]):
                        raise ValueError("Refresh report must cover exactly the requested document")
                    result = outputs[0]
                    if result.verification == "verified" and result.findings:
                        raise ValueError("Verified result contains unresolved material findings")
                    if item.get("dependencies_stable") is False:
                        raise ValueError("Cited local evidence changed during verification")
                    if result.verification == "verified" and not has_current_evidence(result, datetime.now().date()):
                        raise ValueError("Verified document is missing current-dated source evidence")
                    combined.per_file.append(result)
                    combined.cited_sources.extend(report.output.cited_sources)
                    file_results.append({"path": item["path"], "verification": result.verification,
                                         "status": result.status, "note": result.note,
                                         "report_id": agent_report_id,
                                         "local_source_receipts": item.get("local_source_receipts", []),
                                         "source_receipts": item.get("source_receipts", [])})
                except Exception as exc:  # noqa: BLE001 — captured, re-raised at end
                    _log.exception("annual.domain_refresh_file_failed", path=item["path"])
                    file_results.append({"path": item["path"], "verification": "unavailable",
                                         "report_id": item_report_id,
                                         "local_source_receipts": item.get("local_source_receipts", []),
                                         "source_receipts": item.get("source_receipts", []),
                                         "error": f"{type(exc).__name__}: {exc}"})
            incomplete = [r for r in file_results if r["verification"] != "verified"]
            refresh_summary = f"{len(files) - len(incomplete)}/{len(files)} documents fully verified; {len(incomplete)} incomplete"
            combined.summary = refresh_summary
            if incomplete:
                refresh_error = f"verification_incomplete: {refresh_summary}; " + "; ".join(
                    f"{r['path']}: {r.get('error') or r.get('note') or r['verification']}" for r in incomplete)
            try:
                root = self._domain_knowledge_root or get_settings().domain_knowledge_dir
                writeback = write_back_refresh_results(combined, root=root, source_hashes={
                    r["path"].replace("\\", "/").strip(): r["content_sha256"]
                    for r in files if r.get("content_sha256")
                })
                discrepancy_count = await _surface_refresh_discrepancies(
                    user_id=self.user_id, output=combined, now=moment,
                )
                if writeback["missing"]:
                    refresh_error = f"writeback missing documents: {writeback['missing']}"
                if writeback["changed_since_review"]:
                    refresh_error = f"Documents changed during verification: {writeback['changed_since_review']}"
            except Exception as exc:  # noqa: BLE001 — preserve partial work
                _log.exception("annual.domain_refresh_writeback_failed")
                refresh_error = f"writeback: {type(exc).__name__}: {exc}"

        # Phase 3: opportunistic gemelnet pension snapshot.
        # We do NOT bubble exceptions — pensions data is auxiliary; an
        # unreachable MoF site shouldn't fail the annual loop. The outcome
        # is still recorded in the output summary so it is observable.
        pensions_refreshed: int | None = None
        pension_error: str | None = None
        try:
            if self._pension_refresh is not None:
                outcome = self._pension_refresh(self.user_id)
                if hasattr(outcome, "__await__"):
                    outcome = await outcome  # type: ignore[assignment]
                if isinstance(outcome, int):
                    pensions_refreshed = outcome
                elif isinstance(outcome, dict):
                    pensions_refreshed = int(outcome.get("refreshed", 0))
        except Exception as exc:  # noqa: BLE001 — auxiliary; recorded, not raised
            _log.exception("annual.pension_refresh_failed")
            pension_error = f"{type(exc).__name__}: {exc}"

        await record_audit_event(
            user_id=self.user_id,
            event_type="annual.completed",
            entity_type="cadence",
            entity_id="annual",
            payload={
                "now": moment.isoformat(),
                "prompts_count": len(prompts),
                "files_reviewed": len(files),
                "refresh_summary": refresh_summary,
                "pensions_refreshed": pensions_refreshed,
            },
        )

        self.last_output_summary = {
            "prompts_count": len(prompts),
            "files_reviewed": len(files),
            "steps": {
                "prompts": "ok",
                "domain_refresh": (
                    "error" if refresh_error else ("ok" if files else "skipped_no_files")
                ),
                "pension_refresh": (
                    "error"
                    if pension_error
                    else ("ok" if self._pension_refresh is not None else "skipped")
                ),
            },
            "refresh_summary": refresh_summary,
            "domain_refresh_error": refresh_error,
            "domain_refresh_writeback": writeback,
            "domain_refresh_report_id": agent_report_id,
            "domain_refresh_report_ids": agent_report_ids,
            "domain_refresh_files": file_results,
            "domain_refresh_discrepancies": discrepancy_count,
            "pension_refresh_error": pension_error,
            "pensions_refreshed": pensions_refreshed,
        }

        if refresh_error:
            # Fail LOUD so the job run lands not-ok and the existing
            # /api/jobs health derivation surfaces red — no bespoke
            # detector. Partial progress remains readable via
            # `last_output_summary` (RegisteredScheduler exception path).
            raise RuntimeError(
                f"annual: domain_refresh sub-step failed — {refresh_error}"
            )
        return self.last_output_summary


async def _research_documents(files, factory):
    """Bounded model concurrency; persist each small batch before starting more."""
    async def run_one(item):
        agent = factory()
        from argosy.services.knowledge_status import dependency_versions
        from argosy.state import db as db_mod
        async with db_mod.get_session() as session:
            item["dependencies"] = await dependency_versions(session, user_id=agent.user_id, item=item)
        prepared, attachments = _attach_local_sources(item)
        prepared = await _attach_catalog_sources(prepared, user_id=agent.user_id)
        from argosy.services.knowledge_profile import profile_evidence
        async with db_mod.get_session() as session:
            profile = await profile_evidence(session, user_id=agent.user_id, item=item)
        if profile is not None:
            prepared['local_source_evidence'] = [*prepared.get('local_source_evidence', []), profile]
        item['local_source_receipts'] = [{k: v for k, v in source.items() if k != 'content'}
                                         for source in prepared.get('local_source_evidence', [])]
        if item.get("prefetch_sources"):
            from argosy.services.domain_sources import prepare_sources
            source_root = get_settings().domain_knowledge_dir.parent / 'db' / 'domain_sources'
            prepared = await prepare_sources(prepared, root=source_root)
            prepared, attachments = _attach_captured_pdfs(prepared, attachments, source_root)
            item['source_receipts'] = [{k: v for k, v in source.items() if k != 'text'}
                                       for source in prepared['source_packets']]
        report = await agent.run(files_due=[prepared], pdf_attachments=attachments)
        async with db_mod.get_session() as session:
            item["dependencies_stable"] = item["dependencies"] == await dependency_versions(
                session, user_id=agent.user_id, item=item)
        return report
    for offset in range(0, len(files), 3):
        batch = files[offset:offset + 3]
        results = await asyncio.gather(*(run_one(item) for item in batch), return_exceptions=True)
        for item, result in zip(batch, results, strict=True):
            if isinstance(result, BaseException) and not isinstance(result, Exception):
                raise result
            yield item, result


def _attach_captured_pdfs(item, attachments, root):
    """Native fallback for truncated/image-only public PDFs, within the same cap."""
    attachments = list(attachments)
    size = sum(Path(attachment['path']).stat().st_size for attachment in attachments)
    if size > 4_000_000:
        raise ValueError('Cited private PDF attachments exceed the 4 MB aggregate limit; verification incomplete')
    notes = [item.get('local_source_notes', '')]
    seen = set()
    for source in item.get('source_packets', []):
        digest = source.get('sha256', '')
        if (source.get('fetch_status') != 'captured'
                or not (source.get('pdf_pages') or source.get('truncated') or source.get('prompt_truncated') or source.get('extraction_status') == 'unavailable')
                or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)):
            continue
        blob = root / (digest + '.source')
        if not blob.is_file():
            continue
        raw = blob.read_bytes()
        if not raw.startswith(b'%PDF-') or hashlib.sha256(raw).hexdigest() != digest:
            continue
        selection = source.get('pdf_pages')
        if selection is not None:
            from argosy.services.domain_sources import validate_pdf_pages

            validate_pdf_pages(selection)
        identity = (digest, tuple(selection) if selection is not None else None)
        if identity in seen:
            continue
        seen.add(identity)
        if selection is not None:
            from io import BytesIO

            from pypdf import PdfReader, PdfWriter

            reader, writer, buffer = PdfReader(BytesIO(raw)), PdfWriter(), BytesIO()
            if any(number > len(reader.pages) for number in selection):
                raise ValueError('Selected PDF page does not exist')
            for number in selection:
                writer.add_page(reader.pages[number - 1])
            writer.write(buffer)
            raw = buffer.getvalue()
            excerpt_hash = hashlib.sha256(raw).hexdigest()
            blob = root / (excerpt_hash + '.pdf-excerpt')
            try:
                with blob.open('xb') as output:
                    output.write(raw)
            except FileExistsError:
                if blob.read_bytes() != raw:
                    raise ValueError('Existing PDF excerpt hash mismatch') from None
            source['native_excerpt'] = {'sha256': excerpt_hash, 'source_sha256': digest,
                                        'source_pdf_pages': selection, 'bytes': len(raw)}
        if size + len(raw) > 4_000_000:
            notes.append(f'Native PDF not attached: {source["url"]} exceeds remaining attachment budget. Text gaps remain unverified.')
            continue
        attachments.append({'path': str(blob)})
        size += len(raw)
        if selection is not None:
            notes.append(f'Attached PDF #{len(attachments)} contains ONLY original one-based pages {selection} of {source["url"]}; original SHA256 {digest}, excerpt SHA256 {excerpt_hash}. Other pages are NOT supplied or verified. Attachment does not upgrade source authority.')
        else:
            notes.append(f'Attached PDF #{len(attachments)} is the full captured cited source {source["url"]}, SHA256 {digest}. Attachment does not upgrade source authority; assess authorship and declared tier. Read this for material passages missing from truncated text.')
    return {**item, 'local_source_notes': '\n'.join(notes)}, attachments


async def _attach_catalog_sources(item, *, user_id):
    """Resolve explicit catalog IDs with tenant, deletion, path and hash checks."""
    import yaml
    from sqlalchemy import select

    from argosy.services.domain_local_sources import read_local_record
    from argosy.state import db as db_mod
    from argosy.state.models import UserFile
    metadata = yaml.safe_load(item.get('frontmatter') or '') or {}
    references = metadata.get('catalog_sources', []) if isinstance(metadata, dict) else []
    if not isinstance(references, list) or not references:
        return item
    evidence = list(item.get('local_source_evidence', []))
    notes = [item.get('local_source_notes', '')]
    used = sum(len(r.get('content', '')) for r in evidence)
    uploads = (Path(get_settings().home) / 'uploads' / str(user_id)).resolve()
    async with db_mod.get_session(user_id=user_id) as session:
        for reference in references[:8]:
            source_id = reference.get('id') if isinstance(reference, dict) else None
            url = f'file://Catalog/{source_id}'
            if type(source_id) is not int or source_id <= 0:
                notes.append('Invalid catalog source ID; affected attribution is unverified.')
                continue
            record = {'url': url, 'source_as_of': str(reference.get('as_of', '')),
                      'accessed_at': datetime.now().astimezone().isoformat(),
                      'status': 'unavailable', 'content': ''}
            row = (await session.execute(select(UserFile).where(
                UserFile.id == source_id, UserFile.user_id == user_id,
                UserFile.deleted_at.is_(None)))).scalars().first()
            if row is None:
                record['error'] = 'Catalog source unavailable for this user'
            elif len(evidence) >= 8 or used >= 180_000:
                record['error'] = 'Local source budget exhausted'
            else:
                target = Path(row.storage_path).resolve()
                if not target.is_relative_to(uploads):
                    record['error'] = 'Catalog path outside this user upload directory'
                else:
                    record = read_local_record(target, url=url, as_of=reference.get('as_of'))
                    if record.get('sha256') and record['sha256'] != row.sha256:
                        record.update(status='unavailable', content='', error='Catalog content hash mismatch')
                    if len(record['content']) > 180_000 - used:
                        record.update(content=record['content'][:180_000 - used], truncated=True)
                    used += len(record['content'])
            evidence.append(record)
            if record['status'] != 'captured' or record.get('truncated'):
                notes.append(f'Catalog source incomplete: {url}; {record.get("error", "truncated")}. Affected claims remain unverified.')
    if len(references) > 8:
        notes.append('Additional catalog sources omitted by input budget; affected claims remain unverified.')
    return {**item, 'local_source_evidence': evidence, 'local_source_notes': '\n'.join(notes)}


def _attach_local_sources(item):
    """Read only explicitly cited PDFs below the configured portfolio resources."""
    from urllib.parse import unquote

    import yaml
    metadata = yaml.safe_load(item.get("frontmatter") or "") or {}
    sources = metadata.get("sources", []) if isinstance(metadata, dict) else []
    configured = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")
    root = Path(configured).resolve() if configured else None
    attachments, notes, local_evidence = [], [], []
    record_chars = 0
    for source in sources if isinstance(sources, list) else []:
        url = source.get("url", "") if isinstance(source, dict) else ""
        if not isinstance(url, str) or not url.startswith("file://"):
            continue
        target = None
        if root and url.startswith("file://Resources/"):
            candidate = (root / unquote(url.removeprefix("file://Resources/"))).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                target = candidate
        elif url.startswith('file://Knowledge/'):
            knowledge_root = get_settings().domain_knowledge_dir.resolve()
            candidate = (knowledge_root / unquote(url.removeprefix('file://Knowledge/'))).resolve()
            if candidate.is_relative_to(knowledge_root) and candidate.is_file() and candidate.suffix == '.md':
                target = candidate
        if target and target.suffix.lower() == '.pdf':
            attachments.append({"path": str(target)})
            notes.append(f"Attached PDF #{len(attachments)} is the existing source {url}; use its actual content, not the claim in the memo.")
        elif target and target.suffix.lower() in ('.xlsx', '.xls', '.csv', '.tsv', '.txt', '.md'):
            from argosy.services.domain_local_sources import read_local_record
            if len(local_evidence) >= 8 or record_chars >= 180_000:
                notes.append(f'Source NOT included (local record budget): {url}. Affected claims remain unverified.')
                continue
            try:
                record = read_local_record(target, url=url, as_of=source.get('as_of'))
                if url.startswith('file://Knowledge/'):
                    record['provenance_note'] = 'Internal sibling knowledge document, not independent legal evidence. Reconcile cross-references; verify material public rules against their original authorities.'
                remaining = 180_000 - record_chars
                if len(record['content']) > remaining:
                    record['content'] = record['content'][:remaining]
                    record['truncated'] = True
                record_chars += len(record['content'])
                local_evidence.append(record)
                if record['status'] != 'captured':
                    notes.append(f'Local source unavailable: {url}: {record.get("error")}. Affected claims remain unverified.')
                if record['truncated']:
                    notes.append(f'Local source truncated: {url}; omitted content is NOT verified.')
            except Exception as exc:
                notes.append(f'Source NOT read: {url} ({type(exc).__name__}: {exc}). Affected claims remain unverified.')
        else:
            notes.append(f"Source NOT attached (missing or outside configured resources): {url}. Mark affected claims unverified.")
    code_evidence = []
    from argosy.services.knowledge_status import resolve_code_reference
    references = metadata.get('code_references', []) if isinstance(metadata, dict) else []
    for reference in references[:6] if isinstance(references, list) else []:
        if not isinstance(reference, str):
            continue
        candidate = resolve_code_reference(reference)
        if candidate is not None and candidate.is_file():
            raw = candidate.read_bytes()
            if len(raw) <= 150_000:
                code_evidence.append({'url': candidate.as_uri(), 'retrieved_at': datetime.now().date().isoformat(),
                                      'sha256': hashlib.sha256(raw).hexdigest(), 'content': raw.decode('utf-8')})
            else:
                notes.append(f'Code source not included: {reference} exceeds size limit.')
        else:
            notes.append(f'Code source not included: {reference} is unavailable or outside allowed Python source tree.')
    return {**item, "local_source_notes": "\n".join(notes), 'code_evidence': code_evidence,
            'local_source_evidence': local_evidence}, attachments


async def _persist_refresh_report(report: Any, *, item: dict | None = None) -> int | None:
    """Write the refresh run to `agent_reports` (+ output blob).

    Standard cross-cutting-agent persistence pattern (same shape as the
    intake CLI's `_persist_agent_report`): BaseAgent.run() does NOT persist
    for standalone callers, so before this the annual loop's refresh run
    left no auditable row at all.
    """
    from argosy.state import db as db_mod
    from argosy.state.models import AgentReport as AgentReportRow
    from argosy.state.models import AgentReportBlob

    async with db_mod.get_session() as session:
        row = AgentReportRow(
            user_id=report.user_id,
            agent_role=report.agent_role,
            decision_id=report.decision_id,
            prompt_hash=report.prompt_hash,
            response_text=report.response_text,
            tokens_in=report.tokens_in,
            tokens_out=report.tokens_out,
            cost_usd=report.cost_usd,
            model=report.model,
            confidence=report.confidence.value if report.confidence else None,
            cache_input_tokens=report.cache_input_tokens,
            cache_creation_tokens=report.cache_creation_tokens,
            thinking_tokens=report.thinking_tokens,
            citations_json=report.citations_json,
        )
        session.add(row)
        await session.flush()
        try:
            output_json = report.output.model_dump_json()
        except Exception:  # noqa: BLE001 - defensive serialization fallback
            output_json = json.dumps({"error": "could not serialize output"})
        session.add(
            AgentReportBlob(report_id=row.id, key="output_json", value=output_json)
        )
        if item is not None:
            from argosy.services.knowledge_status import document_key, input_fingerprint
            session.add(AgentReportBlob(report_id=row.id, key="knowledge_input", value=json.dumps({
                "path": document_key(item["path"]), "fingerprint": input_fingerprint(item),
                "content_sha256": item.get("content_sha256"),
                "dependencies": item.get("dependencies", []),
                "dependencies_stable": item.get("dependencies_stable", False),
                "coverage_valid": (len(report.output.per_file) == 1
                    and report.output.per_file[0].path.replace("\\", "/") == item["path"].replace("\\", "/")),
            })))
        await session.commit()
        return row.id


async def _surface_refresh_discrepancies(
    *, user_id: str, output: DomainRefreshReport, now: datetime, document_scope: str | None = None
) -> int:
    """One aggregated note_only ActionProposal for changed/outdated params.

    The refresh agent NEVER auto-edits file content; any `change_proposed`
    verdict is a decision for the user. Aggregated into ONE open proposal
    (idempotent per dedup_key, refreshed in place) — same pattern as the
    critique-reconcile escalation aggregation. Returns the number of
    discrepancies surfaced (0 → no proposal touched).
    """
    from sqlalchemy import select

    from argosy.state import db as db_mod
    from argosy.state.models import ActionProposal

    discrepancies = [r for r in output.per_file
                     if r.status == "change_proposed" and r.verification == "verified"]
    if not discrepancies:
        return 0

    lines: list[str] = []
    for r in discrepancies:
        detail = (r.note or "").strip() or "(no note)"
        lines.append(f"- **{r.path}** — {detail}")
        if r.diff:
            lines.append(f"  ```diff\n{r.diff.strip()}\n  ```")
    summary = (
        f"Domain-knowledge refresh found {len(discrepancies)} file"
        f"{'s' if len(discrepancies) != 1 else ''} with changed/outdated "
        "parameters"
    )
    rationale_md = (
        "The domain-refresh agent re-verified `domain_knowledge/` against "
        "live sources and reports these parameter discrepancies. Files were "
        "NOT auto-edited — approve the updates (or dismiss) here:\n\n"
        + "\n".join(lines)
    )
    payload = {"discrepancies": [r.model_dump(mode="json") for r in discrepancies]}
    dedup_key = f"domain_refresh_discrepancies:{user_id}"
    if document_scope is not None:
        dedup_key += ":" + document_scope

    async with db_mod.get_session() as session:
        existing = (
            await session.execute(
                select(ActionProposal).where(
                    ActionProposal.dedup_key == dedup_key,
                    ActionProposal.status == "open",
                )
            )
        ).scalars().first()
        if existing is not None:
            existing.summary = summary
            existing.rationale_md = rationale_md
            existing.suggested_payload = json.dumps(payload)
            existing.severity = "warning"
            existing.surfaced_at = now
            existing.expires_at = now + timedelta(days=30)
        else:
            session.add(
                ActionProposal(
                    user_id=user_id,
                    summary=summary,
                    rationale_md=rationale_md,
                    suggested_payload=json.dumps(payload),
                    severity="warning",
                    surfaced_at=now,
                    expires_at=now + timedelta(days=30),
                    status="open",
                    kind="note_only",
                    dedup_key=dedup_key,
                    execution_state="proposed",
                )
            )
        await session.commit()
    return len(discrepancies)


def _default_files_provider() -> list[dict[str, str]]:
    """Return substantive knowledge documents; validate explicit pointer aliases."""
    import yaml
    out: list[dict[str, str]] = []
    settings = get_settings()
    root = settings.domain_knowledge_dir
    if not root.is_dir():
        raise FileNotFoundError(f"Domain knowledge directory unavailable: {root}")
    for p in sorted(root.rglob("*.md")):
        try:
            raw = p.read_bytes()
            content = raw.decode("utf-8").replace("\r\n", "\n")
        except OSError as exc:  # pragma: no cover - defensive
            raise OSError(f"Cannot read domain knowledge document: {p}") from exc
        # Split frontmatter (optional `---\n...\n---` at the top).
        frontmatter = ""
        body = content
        if content.startswith("---\n"):
            end = content.find("\n---\n", 4)
            if end > 0:
                frontmatter = content[4:end]
                body = content[end + 5 :]
        metadata = yaml.safe_load(frontmatter) or {}
        if isinstance(metadata, dict) and metadata.get('knowledge_kind') == 'alias':
            target = (root.parent / str(metadata.get('canonical_location', ''))).resolve()
            if not target.is_relative_to(root.resolve()) or target == p.resolve() or target.suffix != '.md' or not target.is_file():
                raise ValueError(f'Invalid knowledge alias {p}: {target}')
            target_content = target.read_text(encoding='utf-8')
            target_metadata = yaml.safe_load(target_content.split('---', 2)[1]) if target_content.startswith('---\n') else {}
            if isinstance(target_metadata, dict) and target_metadata.get('knowledge_kind') == 'alias':
                raise ValueError(f'Knowledge alias chains are not supported: {p}')
            _log.info('annual.knowledge_alias_resolved', path=str(p), canonical=str(target))
            continue
        out.append(
            {
                "path": str(p.relative_to(root.parent)),
                "frontmatter": frontmatter,
                "content": body,
                "content_sha256": hashlib.sha256(raw).hexdigest(),
                "prefetch_sources": True,
            }
        )
    return out


__all__ = ["AnnualLoop"]
