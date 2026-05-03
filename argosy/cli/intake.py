"""`argosy intake` — runs the intake interview interactively in the terminal.

Loop:
  1. Load (or create) the user row + user_context.
  2. Determine the current_stage (default: stage_1).
  3. For each turn:
       a. Build the prompt with accumulated context + last user message.
       b. Call `IntakeAgent.run(...)`.
       c. Apply context_updates to user_context (merging YAML).
       d. Print the agent's question; read user input.
       e. If stage_complete, advance current_stage.
  4. Stop when current_stage transitions to "complete" or user types
     `/quit`.

Phase 1 does NOT call Claude unless the API key is configured. If the key
is missing, `IntakeAgent.run` raises `MissingAPIKeyError` with a clear
message; we catch and surface it.
"""

from __future__ import annotations

import asyncio
import json

import typer
import yaml

from argosy.agents.errors import AgentRunError, MissingAPIKeyError
from argosy.agents.intake import INTAKE_STAGES, IntakeAgent


def intake(
    user_id: str = typer.Option("ariel", "--user-id", help="Tenant id (default 'ariel')."),
    max_turns: int = typer.Option(
        50, "--max-turns", help="Hard cap to prevent runaway sessions."
    ),
) -> None:
    """Run the intake interview for one user, interactively."""
    asyncio.run(_run_intake_loop(user_id=user_id, max_turns=max_turns))


async def _run_intake_loop(*, user_id: str, max_turns: int) -> None:
    from sqlalchemy import select

    from argosy.state import db as db_mod
    from argosy.state.models import User, UserContext

    db_mod.init_engine()

    async with db_mod.get_session() as session:
        # Ensure user + user_context rows exist.
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user is None:
            user = User(id=user_id)
            session.add(user)
            await session.flush()
        ctx = (
            await session.execute(select(UserContext).where(UserContext.user_id == user_id))
        ).scalar_one_or_none()
        if ctx is None:
            ctx = UserContext(user_id=user_id, current_stage="stage_1")
            session.add(ctx)
        elif ctx.current_stage is None:
            ctx.current_stage = "stage_1"
        await session.commit()
        await session.refresh(ctx)

    typer.echo(f"=== Argosy intake interview for user_id={user_id!r} ===")
    typer.echo("Type your answer, then press Enter. Type /quit to exit.")
    typer.echo("")

    agent = IntakeAgent(user_id=user_id)
    last_user_message = ""

    for turn_idx in range(max_turns):
        async with db_mod.get_session() as session:
            ctx = (
                await session.execute(
                    select(UserContext).where(UserContext.user_id == user_id)
                )
            ).scalar_one()

            current_stage = ctx.current_stage or "stage_1"
            if current_stage == "complete":
                typer.echo("Intake complete. All 6 stages finished.")
                return

            accumulated = _accumulated_context_yaml(ctx)

        try:
            report = await agent.run(
                current_stage=current_stage,
                accumulated_context=accumulated,
                last_user_message=last_user_message,
            )
        except MissingAPIKeyError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=2) from exc
        except AgentRunError as exc:
            typer.echo(f"Intake agent error: {exc}")
            raise typer.Exit(code=3) from exc

        turn = report.output  # IntakeTurnOutput

        # Apply context updates.
        await _apply_context_updates(user_id=user_id, updates=turn.context_updates)

        # Persist the agent report (audit log).
        await _persist_agent_report(report=report)

        # Advance stage if requested.
        if turn.stage_complete:
            new_stage = turn.next_stage or _next_stage(current_stage)
            await _set_current_stage(user_id=user_id, stage=new_stage)
            typer.echo(f"\n[stage {current_stage} complete → {new_stage}]")
            if new_stage == "complete":
                typer.echo("Intake complete. All 6 stages finished.")
                return
            last_user_message = ""  # fresh start for the new stage
            if turn.notes_for_orchestrator:
                typer.echo(f"Note: {turn.notes_for_orchestrator}")
            continue

        # Otherwise, ask the question.
        if turn.question_for_user:
            typer.echo(f"\nIntake [{current_stage}, turn {turn_idx + 1}]:")
            typer.echo(turn.question_for_user)
        else:
            typer.echo("(Intake agent returned no question; ending.)")
            return

        try:
            user_input = typer.prompt("You", default="", show_default=False)
        except (EOFError, KeyboardInterrupt):
            typer.echo("\nIntake interrupted; progress saved.")
            return
        if user_input.strip().lower() == "/quit":
            typer.echo("Bye.")
            return
        last_user_message = user_input

    typer.echo(f"Reached max-turns cap ({max_turns}); ending.")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _accumulated_context_yaml(ctx: object) -> str:
    """Combine the three YAML payloads on user_context into one block."""
    pieces: list[str] = []
    for label in ("identity_yaml", "goals_yaml", "constraints_yaml"):
        val = getattr(ctx, label, "") or ""
        if val.strip():
            pieces.append(f"# --- {label.replace('_yaml', '')} ---\n{val}")
    return "\n\n".join(pieces)


def _next_stage(current: str) -> str:
    if current not in INTAKE_STAGES:
        return "stage_1"
    idx = INTAKE_STAGES.index(current)
    if idx + 1 >= len(INTAKE_STAGES):
        return "complete"
    return INTAKE_STAGES[idx + 1]


async def _set_current_stage(*, user_id: str, stage: str) -> None:
    from sqlalchemy import select

    from argosy.state import db as db_mod
    from argosy.state.models import UserContext

    async with db_mod.get_session() as session:
        ctx = (
            await session.execute(select(UserContext).where(UserContext.user_id == user_id))
        ).scalar_one()
        ctx.current_stage = stage
        await session.commit()


async def _apply_context_updates(*, user_id: str, updates: list) -> None:
    if not updates:
        return
    from sqlalchemy import select

    from argosy.state import db as db_mod
    from argosy.state.models import UserContext

    async with db_mod.get_session() as session:
        ctx = (
            await session.execute(select(UserContext).where(UserContext.user_id == user_id))
        ).scalar_one()
        for upd in updates:
            section = getattr(upd, "target_section", None)
            patch_yaml = getattr(upd, "yaml_patch", "") or ""
            if not section or not patch_yaml.strip():
                continue
            field = f"{section}_yaml"
            existing = getattr(ctx, field, "") or ""
            merged = _merge_yaml(existing, patch_yaml)
            setattr(ctx, field, merged)
        await session.commit()


def _merge_yaml(existing: str, patch: str) -> str:
    """Best-effort YAML merge.

    Both inputs are parsed; we deep-merge dicts. On parse failure we just
    append the patch as a comment + the patch content so nothing is lost.
    """
    try:
        e = yaml.safe_load(existing) or {}
    except Exception:
        e = {}
    try:
        p = yaml.safe_load(patch) or {}
    except Exception:
        p = {}
    if not isinstance(e, dict) or not isinstance(p, dict):
        return existing + ("\n" if existing else "") + patch
    merged = _deep_merge_dicts(e, p)
    return yaml.safe_dump(merged, allow_unicode=True, sort_keys=False).rstrip() + "\n"


def _deep_merge_dicts(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge_dicts(out[k], v)
        else:
            out[k] = v
    return out


async def _persist_agent_report(*, report) -> None:
    """Write an `agent_reports` row + optional output JSON blob."""
    from argosy.state import db as db_mod
    from argosy.state.models import AgentReport, AgentReportBlob

    async with db_mod.get_session() as session:
        row = AgentReport(
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
        )
        session.add(row)
        await session.flush()
        # Persist the parsed structured output as a blob for later inspection.
        try:
            output_json = report.output.model_dump_json()
        except Exception:
            output_json = json.dumps({"error": "could not serialize output"})
        session.add(AgentReportBlob(report_id=row.id, key="output_json", value=output_json))
        await session.commit()
