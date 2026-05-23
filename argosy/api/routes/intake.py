"""Intake wizard API (SDD §11.1 #9, Phase 7).

Endpoints:
  - POST /api/intake/turn      — drive one Q→A turn
  - POST /api/intake/upload    — upload a plan markdown to pre-populate
                                 user_context (Phase 7 additive feature)
  - GET  /api/intake/status    — lightweight stage status

The page presents the question, collects the answer, advances stages,
shows confidence flags and missing-data warnings. The CLI logic (intake
agent) is the same; this route exposes it via HTTP.

For Phase 7 we only wire the prompt-builder + a stub agent path: tests
inject a mocked `IntakeAgent` / `IntakeExtractorAgent`. Production wires
through the real agents.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import yaml
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from argosy.agents.gap_tracker import STAGE_FIELDS, gap_status
from argosy.agents.intake import IntakeAgent, IntakeTurnOutput
from argosy.agents.intake_extractor import IntakeExtraction, IntakeExtractorAgent
from argosy.agents.intake_fields import stage_status
from argosy.ingest.file_to_text import (
    FileTooLargeError,
    UnsupportedFileTypeError,
    convert_to_text,
)
from argosy.ingest.plan import parse_plan_markdown_text
from argosy.logging import get_logger
from argosy.state import db as db_mod
from argosy.state.models import (
    AgentReport as AgentReportRow,
    PlanVersion,
    User,
    UserContext,
)

_log = get_logger("argosy.api.intake")
router = APIRouter(prefix="/intake", tags=["intake"])


# ----------------------------------------------------------------------
# DI hooks so tests can mock the agents without spinning Anthropic.
# ----------------------------------------------------------------------

_AGENT_FACTORY = None
_EXTRACTOR_FACTORY = None


def set_intake_agent_factory(factory) -> None:
    """Override the agent factory for tests. Called as
    `set_intake_agent_factory(lambda user_id: MyMock(user_id=user_id))`."""
    global _AGENT_FACTORY
    _AGENT_FACTORY = factory


def reset_intake_agent_factory() -> None:
    global _AGENT_FACTORY
    _AGENT_FACTORY = None


def set_intake_extractor_factory(factory) -> None:
    """Override the extractor factory for tests."""
    global _EXTRACTOR_FACTORY
    _EXTRACTOR_FACTORY = factory


def reset_intake_extractor_factory() -> None:
    global _EXTRACTOR_FACTORY
    _EXTRACTOR_FACTORY = None


# ----------------------------------------------------------------------
# /turn
# ----------------------------------------------------------------------


class TurnRequest(BaseModel):
    user_id: str = "ariel"
    last_user_message: str = ""
    history_excerpt: str = ""
    # Optional: explicit current_stage; if absent, we read from
    # user_context.current_stage (or default to stage_1).
    current_stage: str | None = None


class TurnResponse(BaseModel):
    stage: str
    question_for_user: str
    stage_complete: bool
    next_stage: str | None
    confidence: str
    cited_sources: list[str]
    notes_for_orchestrator: str
    context_updates: list[dict[str, Any]]
    intake_session_id: str


@router.post("/turn", response_model=TurnResponse)
async def post_turn(req: TurnRequest) -> TurnResponse:
    """Drive one intake turn. Resolves current_stage if absent.

    Phase 7 keeps the actual `user_context` mutation in the CLI path
    (see `argosy.cli.intake`); this route is the surface the dashboard
    talks to. The orchestrator merges `context_updates` after the user
    confirms.
    """
    # Resolve current stage AND intake_session_id.
    # Session lifecycle: rotated on every stage_1 entry; carried through
    # stages 2-6; preserved (last value sticks) once stage_complete.
    stage = req.current_stage
    session_id: str | None = None
    accumulated = ""
    async with db_mod.get_session() as session:
        ctx = (
            await session.execute(
                select(UserContext).where(UserContext.user_id == req.user_id)
            )
        ).scalar_one_or_none()
        if stage is None:
            if ctx is None or ctx.current_stage is None:
                stage = "stage_1"
            elif ctx.current_stage == "complete":
                # Existing-user backwards-compat: stage_11 (special
                # situations) was added after some users had already
                # finished intake. Only re-enter stage_11 if there's
                # actually an open gap there; otherwise stay "complete".
                _post = gap_status(
                    identity_yaml=ctx.identity_yaml or "",
                    goals_yaml=ctx.goals_yaml or "",
                    constraints_yaml=ctx.constraints_yaml or "",
                )
                _stage_11_paths = {f.path for f in STAGE_FIELDS.get("stage_11", [])}
                _missing = {f.path for f in _post.missing}
                _stale = {f.path for f, _t in _post.stale}
                if (_missing | _stale) & _stage_11_paths:
                    stage = "stage_11"
                else:
                    stage = "complete"
            else:
                stage = ctx.current_stage

        # Rotate the session id on stage_1 entry; otherwise reuse.
        if stage == "stage_1" and (ctx is None or ctx.current_stage in (None, "complete")):
            session_id = uuid4().hex
            if ctx is not None:
                ctx.intake_session_id = session_id
                await session.commit()
        elif ctx is not None:
            session_id = ctx.intake_session_id or uuid4().hex
            if ctx.intake_session_id is None:
                ctx.intake_session_id = session_id
                await session.commit()
        else:
            session_id = uuid4().hex

        if ctx is not None:
            parts = []
            if ctx.identity_yaml:
                parts.append("# identity\n" + ctx.identity_yaml)
            if ctx.goals_yaml:
                parts.append("# goals\n" + ctx.goals_yaml)
            if ctx.constraints_yaml:
                parts.append("# constraints\n" + ctx.constraints_yaml)
            accumulated = "\n\n".join(parts)

    factory = _AGENT_FACTORY
    if factory is None:
        agent = IntakeAgent(user_id=req.user_id)
    else:
        agent = factory(req.user_id)

    # ``stage_status`` and the agent both reject ``"complete"`` — it's a
    # synthetic stage representing "nothing to do". Map to stage_11 (the
    # last real stage) for both calls; the veto in ``_persist_turn``
    # keeps the user pinned at ``complete`` when no actual gap remains.
    agent_stage = "stage_11" if stage == "complete" else stage

    # Compute the structured "answered / missing" lists for the stage so
    # the agent receives an explicit checklist instead of having to derive
    # one from free-form YAML (Haiku is unreliable at that).
    pre_status = stage_status(
        identity_yaml=(ctx.identity_yaml if ctx is not None else "") or "",
        goals_yaml=(ctx.goals_yaml if ctx is not None else "") or "",
        constraints_yaml=(ctx.constraints_yaml if ctx is not None else "") or "",
        stage=agent_stage,
    )

    try:
        report = await agent.run(
            current_stage=agent_stage,
            accumulated_context=accumulated,
            last_user_message=req.last_user_message,
            history_excerpt=req.history_excerpt,
            answered_fields=pre_status["answered"],
            missing_fields=pre_status["missing"],
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.exception("intake.turn_failed", intake_session_id=session_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    out: IntakeTurnOutput = report.output  # type: ignore[assignment]

    # Persist the conversation turn via the shared helper (also used by
    # /api/advisor/turn). The helper stamps agent_reports, merges
    # context_updates into user_context.*_yaml, and auto-advances
    # current_stage when the post-update checklist is empty (overriding
    # the agent's own stage_complete claim if needed).
    from argosy.api.routes.advisor import _persist_turn

    await _persist_turn(
        user_id=req.user_id,
        stage=stage,
        session_id=session_id,
        report=report,
        out=out,
        apply_turn_update=_apply_turn_update,
    )
    return TurnResponse(
        stage=out.stage,
        question_for_user=out.question_for_user,
        stage_complete=out.stage_complete,
        next_stage=out.next_stage,
        confidence=out.confidence.value if out.confidence else "MEDIUM",
        cited_sources=out.cited_sources,
        notes_for_orchestrator=out.notes_for_orchestrator,
        context_updates=[u.model_dump() for u in out.context_updates],
        intake_session_id=session_id,
    )


# ----------------------------------------------------------------------
# /upload
# ----------------------------------------------------------------------

# Hard cap on uploaded plan size. 1 MB is comfortably above any realistic
# plan markdown (Jacobs_Wealth_Plan.md is ~25KB) and well below the
# Anthropic context-window concern.
_MAX_UPLOAD_BYTES = 1_000_000


class UploadResponse(BaseModel):
    plan_version_id: int
    intake_session_id: str
    fields_extracted: list[str]
    fields_missing: list[str]
    confidence: str
    notes: str
    summary_for_user: str


def _merge_yaml_additive(existing_yaml: str, extracted_yaml: str) -> str:
    """Merge `extracted_yaml` into `existing_yaml` with existing winning.

    Both inputs are YAML strings (possibly empty). Returns a YAML string.
    Existing values WIN over extracted values — we never overwrite anything
    the user already typed in the conversational interview. Used by the
    /upload path where the new content is *extracted* and the existing
    content may be *typed by the user*.

    On parse failure of either side, we degrade gracefully:
      - If the existing YAML is unparseable, we keep it unchanged (don't
        clobber the user's hand-written content).
      - If the extracted YAML is unparseable, we skip the merge and return
        the existing YAML unchanged.
    """
    try:
        existing_obj = yaml.safe_load(existing_yaml) if existing_yaml.strip() else {}
    except yaml.YAMLError:
        return existing_yaml  # don't risk clobbering user-typed content
    try:
        extracted_obj = yaml.safe_load(extracted_yaml) if extracted_yaml.strip() else {}
    except yaml.YAMLError:
        return existing_yaml

    if not isinstance(existing_obj, dict):
        existing_obj = {} if existing_obj is None else {"_value": existing_obj}
    if not isinstance(extracted_obj, dict):
        extracted_obj = {} if extracted_obj is None else {"_value": extracted_obj}

    merged: dict[str, Any] = dict(extracted_obj)
    merged.update(existing_obj)  # existing keys win

    if not merged:
        return existing_yaml  # nothing new
    return yaml.safe_dump(merged, sort_keys=True, allow_unicode=True)


def _apply_turn_update(existing_yaml: str, patch_yaml: str) -> str:
    """Merge a turn's context_update yaml_patch into the section's YAML.

    Used by the /turn path. The agent's patch represents the user's
    authoritative answer for the fields it touches, so **patch values
    win over existing** here (the inverse of `_merge_yaml_additive`).

    Recursive merge for nested dicts; lists/scalars from the patch
    replace the existing value.

    On parse failure on either side, returns the existing string
    unchanged (don't clobber).
    """
    try:
        existing_obj = yaml.safe_load(existing_yaml) if existing_yaml.strip() else {}
    except yaml.YAMLError:
        return existing_yaml
    try:
        patch_obj = yaml.safe_load(patch_yaml) if patch_yaml.strip() else {}
    except yaml.YAMLError:
        return existing_yaml

    if not isinstance(existing_obj, dict):
        existing_obj = {} if existing_obj is None else {"_value": existing_obj}
    if not isinstance(patch_obj, dict):
        patch_obj = {} if patch_obj is None else {"_value": patch_obj}

    def _deep_merge(base: dict, override: dict) -> dict:
        out = dict(base)
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = _deep_merge(out[k], v)
            else:
                out[k] = v
        return out

    merged = _deep_merge(existing_obj, patch_obj)
    if not merged:
        return existing_yaml
    return yaml.safe_dump(merged, sort_keys=True, allow_unicode=True)


def _build_summary(
    extracted: list[str],
    missing: list[str],
) -> str:
    """One-sentence human-readable summary for the success panel."""
    if not extracted and not missing:
        return "Plan uploaded — no extractable fields found. The interview will continue."
    parts = []
    if extracted:
        head = ", ".join(extracted[:5])
        more = f" (+{len(extracted) - 5} more)" if len(extracted) > 5 else ""
        parts.append(f"Got your plan — extracted {head}{more}.")
    else:
        parts.append("Got your plan.")
    if missing:
        head = ", ".join(missing[:5])
        more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
        parts.append(f"I'll ask about {head}{more} next.")
    return " ".join(parts)


@router.post("/upload", response_model=UploadResponse)
async def post_upload(
    user_id: str = Form("ariel"),
    file: UploadFile = File(...),
) -> UploadResponse:
    """Upload a plan markdown to pre-populate `user_context`.

    Steps:
      1. Validate the upload (extension + size + UTF-8).
      2. Parse the markdown via `parse_plan_markdown_text` (sanity-only;
         we don't store the parsed structure in this iteration).
      3. Persist a `plan_versions` row with the raw markdown.
      4. Ensure the user/user_context rows exist.
      5. Build accumulated_context from existing YAML payloads.
      6. Run `IntakeExtractorAgent` to produce structured extraction.
      7. Merge each of the three YAML strings additively (existing wins).
      8. Stamp an agent_reports row with the intake_session_id (rotating
         the session if user_context.current_stage is None / "complete",
         same rule as /turn).
      9. Return UploadResponse.
    """
    # ---- 1. Validate the upload ---------------------------------------
    filename = file.filename or "uploaded.md"
    lower = filename.lower()
    content_type = (file.content_type or "").lower()
    if not lower.endswith(".md") and content_type not in (
        "text/markdown",
        "text/x-markdown",
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Plan must be a Markdown file (.md). Got: {filename!r} "
            f"(content-type={content_type!r}).",
        )

    raw_bytes = await file.read()
    if len(raw_bytes) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(raw_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Uploaded file is too large ({len(raw_bytes):,} bytes; "
            f"limit is {_MAX_UPLOAD_BYTES:,}).",
        )
    try:
        content_str = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"File is not valid UTF-8: {exc}",
        ) from exc

    # ---- 2. Parse for sanity (PlanDocument not persisted here) --------
    try:
        plan_doc = parse_plan_markdown_text(content_str, source_path=filename)
    except Exception as exc:  # pragma: no cover - parser is defensive
        raise HTTPException(
            status_code=400,
            detail=f"Failed to parse markdown: {exc}",
        ) from exc
    _ = plan_doc  # parsed for validation; we store the raw text below

    # ---- 3-5. Persist plan_versions, ensure user/user_context, gather context
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    version_label = f"from_intake_upload_{timestamp}"

    # Provenance Wave A — record every uploaded byte-blob in the catalog so
    # later UIs (Files page, Decision Replay) can answer "where did this
    # plan come from?". Idempotent: re-uploading the same bytes for the
    # same user collapses to the existing catalog row.
    from argosy.services.file_catalog import catalog_upload as _catalog_upload

    catalog_dto = await _catalog_upload(
        user_id=user_id,
        raw_bytes=raw_bytes,
        original_name=filename,
        mime_type=(content_type or "text/markdown"),
        kind="plan_markdown",
        source="intake_upload",
    )

    accumulated = ""
    plan_version_id: int
    session_id: str
    async with db_mod.get_session() as session:
        # Ensure user row.
        user = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if user is None:
            user = User(id=user_id)
            session.add(user)
            await session.flush()

        # Ensure user_context row.
        ctx = (
            await session.execute(
                select(UserContext).where(UserContext.user_id == user_id)
            )
        ).scalar_one_or_none()
        if ctx is None:
            ctx = UserContext(user_id=user_id)
            session.add(ctx)
            await session.flush()

        # Resolve / rotate intake_session_id with the same rule as /turn:
        # rotate if current_stage is None or "complete"; otherwise reuse.
        if ctx.current_stage in (None, "complete"):
            session_id = uuid4().hex
            ctx.intake_session_id = session_id
        else:
            session_id = ctx.intake_session_id or uuid4().hex
            if ctx.intake_session_id is None:
                ctx.intake_session_id = session_id

        # Build accumulated_context for the extractor.
        parts: list[str] = []
        if ctx.identity_yaml:
            parts.append("# identity\n" + ctx.identity_yaml)
        if ctx.goals_yaml:
            parts.append("# goals\n" + ctx.goals_yaml)
        if ctx.constraints_yaml:
            parts.append("# constraints\n" + ctx.constraints_yaml)
        accumulated = "\n\n".join(parts)

        # Persist the plan_versions row.
        pv = PlanVersion(
            user_id=user_id,
            version_label=version_label,
            source_path=filename,
            raw_markdown=content_str,
            source_file_id=catalog_dto.id,
        )
        session.add(pv)
        await session.flush()
        plan_version_id = pv.id

        # Snapshot current YAML payloads so we can merge after the agent runs
        # (we must commit and release the session before the long-running
        # agent call to avoid holding a transaction open).
        existing_identity = ctx.identity_yaml
        existing_goals = ctx.goals_yaml
        existing_constraints = ctx.constraints_yaml

        await session.commit()

    # ---- 5.5. Distill the baseline plan (non-fatal value-add) ----------
    # distill_baseline_plan_async opens its own async sessions and dispatches
    # the agent call (which calls asyncio.run internally) to a thread.
    # Failure is intentionally swallowed — the upload is still useful without
    # a distillate; the user can retry via the Re-distill button (T1.11).
    try:
        from argosy.services.plan_distiller_service import distill_baseline_plan_async

        await distill_baseline_plan_async(
            plan_version_id=plan_version_id,
            user_id=user_id,
            preserve_user_edits=False,  # initial import — no prior edits
        )
    except Exception as exc:  # noqa: BLE001 — non-fatal value-add
        _log.warning(
            "intake_upload.distill_failed",
            plan_version_id=plan_version_id,
            user_id=user_id,
            error=str(exc),
        )

    # ---- 6. Run the extractor (no DB session held) --------------------
    factory = _EXTRACTOR_FACTORY
    if factory is None:
        agent = IntakeExtractorAgent(user_id=user_id)
    else:
        agent = factory(user_id)

    try:
        report = await agent.run(
            plan_markdown=content_str,
            accumulated_context=accumulated,
        )
    except Exception as exc:
        _log.exception(
            "intake.upload.extractor_failed",
            user_id=user_id,
            plan_version_id=plan_version_id,
            intake_session_id=session_id,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    extraction: IntakeExtraction = report.output  # type: ignore[assignment]

    # ---- 7-8. Merge YAML additively + stamp agent_reports -------------
    merged_identity = _merge_yaml_additive(existing_identity, extraction.identity_yaml)
    merged_goals = _merge_yaml_additive(existing_goals, extraction.goals_yaml)
    merged_constraints = _merge_yaml_additive(
        existing_constraints, extraction.constraints_yaml
    )

    async with db_mod.get_session() as session:
        ctx = (
            await session.execute(
                select(UserContext).where(UserContext.user_id == user_id)
            )
        ).scalar_one_or_none()
        if ctx is not None:
            ctx.identity_yaml = merged_identity
            ctx.goals_yaml = merged_goals
            ctx.constraints_yaml = merged_constraints

        ar_row = AgentReportRow(
            user_id=user_id,
            agent_role=report.agent_role,
            decision_id=None,
            intake_session_id=session_id,
            prompt_hash=report.prompt_hash,
            response_text=report.response_text,
            tokens_in=report.tokens_in,
            tokens_out=report.tokens_out,
            cost_usd=report.cost_usd,
            model=report.model,
            confidence=(report.confidence.value if report.confidence else None),
            # Wave A — Anthropic Messages API telemetry (migration 0026).
            cache_input_tokens=report.cache_input_tokens,
            cache_creation_tokens=report.cache_creation_tokens,
            thinking_tokens=report.thinking_tokens,
            citations_json=report.citations_json,
            # Wave B-UI Task 9 — sources serialised from build_prompt (migration 0027).
            sources_json=report.sources_json,
            # Wave B-UI follow-up Item 2 — correlation id for O(1) WS↔DB
            # linking in useDecisionStream (migration 0028).
            run_correlation_id=report.run_correlation_id,
            # Wave B-UI follow-up Item B — full prompts for the Prompt tab
            # (migration 0029).
            system_prompt=report.system_prompt,
            user_prompt=report.user_prompt,
        )
        session.add(ar_row)
        await session.commit()

    summary = _build_summary(extraction.fields_extracted, extraction.fields_missing)
    return UploadResponse(
        plan_version_id=plan_version_id,
        intake_session_id=session_id,
        fields_extracted=list(extraction.fields_extracted),
        fields_missing=list(extraction.fields_missing),
        confidence=(
            extraction.confidence.value if extraction.confidence else "MEDIUM"
        ),
        notes=extraction.notes or "",
        summary_for_user=summary,
    )


# ----------------------------------------------------------------------
# /status
# ----------------------------------------------------------------------


@router.get("/status")
async def get_status(user_id: str = Query("ariel")) -> dict[str, Any]:
    """Lightweight status — what stage the user is on."""
    async with db_mod.get_session() as session:
        ctx = (
            await session.execute(select(UserContext).where(UserContext.user_id == user_id))
        ).scalar_one_or_none()
        user_exists = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none() is not None
    return {
        "user_id": user_id,
        "user_exists": user_exists,
        "current_stage": (ctx.current_stage if ctx else None) or "stage_1",
    }


# ----------------------------------------------------------------------
# /file-to-text
# ----------------------------------------------------------------------


class FileToTextResponse(BaseModel):
    """Result of converting an uploaded doc to plain text.

    Stateless. The frontend uses this to pre-process an attached file
    before posting `/api/intake/turn` so the user's typed answer can
    include the file contents inline.
    """

    filename: str
    content_type: str
    extracted_text: str
    warnings: list[str]
    page_or_sheet_count: int


@router.post("/file-to-text", response_model=FileToTextResponse)
async def post_file_to_text(
    file: UploadFile = File(...),
    user_id: str = Form("ariel"),
) -> FileToTextResponse:
    """Convert an uploaded doc (any supported type) to plain text.

    Provenance Wave A — the file is also recorded in the catalog (kind='other',
    source='intake_file_to_text') so the user can later see in /files which
    raw doc fed which intake answer. The text-extraction itself remains
    stateless from the caller's perspective.
    """
    filename = file.filename or "uploaded"
    content_type = (file.content_type or "").lower()
    raw_bytes = await file.read()
    if len(raw_bytes) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # Provenance Wave A: catalog the raw bytes BEFORE conversion so we have
    # a record even if conversion fails. Best-effort — catalog failure
    # must not break the conversion (which has been the contract since v1).
    try:
        from argosy.services.file_catalog import catalog_upload as _catalog_upload
        await _catalog_upload(
            user_id=user_id,
            raw_bytes=raw_bytes,
            original_name=filename,
            mime_type=(content_type or "application/octet-stream"),
            kind="other",
            source="intake_file_to_text",
        )
    except Exception as exc:  # noqa: BLE001 — non-fatal value-add
        _log.warning(
            "intake.file_to_text.catalog_failed",
            filename=filename, user_id=user_id, error=str(exc),
        )

    try:
        result = convert_to_text(
            filename=filename, content_type=content_type, data=raw_bytes
        )
    except FileTooLargeError as exc:
        # 413 Payload Too Large.
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - defensive
        _log.exception("intake.file_to_text.failed", filename=filename)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to extract text from {filename!r}: {exc}",
        ) from exc

    return FileToTextResponse(
        filename=result.filename,
        content_type=result.content_type,
        extracted_text=result.extracted_text,
        warnings=list(result.warnings),
        page_or_sheet_count=result.page_or_sheet_count,
    )


__all__ = [
    "router",
    "set_intake_agent_factory",
    "reset_intake_agent_factory",
    "set_intake_extractor_factory",
    "reset_intake_extractor_factory",
]
