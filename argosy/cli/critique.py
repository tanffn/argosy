"""`argosy critique` — run plan-critique on the latest plan + portfolio.

Looks up the latest `plan_versions` row for the user, takes the snapshot
either from a `--snapshot` TSV path or from the most-recently-imported
plan's directory `Resources/` (if present), and asks the plan-critique
agent for a structured critique.

The result is persisted to `plan_critiques` and pretty-printed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from argosy.agents.errors import AgentRunError, MissingAPIKeyError
from argosy.agents.plan_critique import PlanCritiqueAgent
from argosy.config import get_settings
from argosy.ingest.plan import parse_plan_markdown
from argosy.ingest.tsv import parse_portfolio_tsv


def critique(
    plan: Path | None = typer.Option(
        None,
        "--plan",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Optional path to a plan markdown. If omitted, uses the latest "
        "stored plan_version for --user-id.",
    ),
    snapshot: Path | None = typer.Option(
        None,
        "--snapshot",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Optional path to a Family Finances Status TSV. If omitted, no "
        "snapshot is included (the agent will note the missing data).",
    ),
    user_id: str = typer.Option("ariel", "--user-id"),
    save: bool = typer.Option(True, "--save/--no-save", help="Persist the critique to DB."),
) -> None:
    """Run the plan-critique agent and print findings."""
    asyncio.run(_run_critique(user_id=user_id, plan=plan, snapshot=snapshot, save=save))


async def _run_critique(
    *, user_id: str, plan: Path | None, snapshot: Path | None, save: bool
) -> None:
    from sqlalchemy import desc, select

    from argosy.state import db as db_mod
    from argosy.state.models import PlanCritique, PlanVersion, UserContext

    db_mod.init_engine()

    plan_label = "(unknown)"
    plan_markdown = ""
    plan_version_id: int | None = None

    if plan is not None:
        doc = parse_plan_markdown(plan)
        plan_label = plan.stem
        plan_markdown = doc.raw_markdown
    else:
        async with db_mod.get_session() as session:
            row = (
                await session.execute(
                    select(PlanVersion)
                    .where(PlanVersion.user_id == user_id)
                    .order_by(desc(PlanVersion.imported_at))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                typer.echo(
                    "No plan_versions found for this user. Run "
                    "'argosy ingest plan <path>' first, or pass --plan."
                )
                raise typer.Exit(code=2)
            plan_label = row.version_label or f"plan_version_id={row.id}"
            plan_markdown = row.raw_markdown
            plan_version_id = row.id

    snapshot_label = "(no snapshot provided)"
    snapshot_summary = "(no portfolio snapshot was supplied to this run)"
    if snapshot is not None:
        snap = parse_portfolio_tsv(snapshot)
        snapshot_label = snapshot.name
        snapshot_summary = snap.summary_text()

    # Pull user context YAML.
    async with db_mod.get_session() as session:
        ctx = (
            await session.execute(
                select(UserContext).where(UserContext.user_id == user_id)
            )
        ).scalar_one_or_none()
    user_context_yaml = ""
    if ctx is not None:
        for label in ("identity_yaml", "goals_yaml", "constraints_yaml"):
            v = getattr(ctx, label, "") or ""
            if v.strip():
                user_context_yaml += f"# --- {label.replace('_yaml', '')} ---\n{v}\n\n"

    # Load the relevant domain_knowledge files.
    kb_files = _load_relevant_kb_for_israeli_user()

    agent = PlanCritiqueAgent(user_id=user_id)

    try:
        report = await agent.run(
            plan_label=plan_label,
            plan_markdown=plan_markdown,
            snapshot_label=snapshot_label,
            snapshot_summary=snapshot_summary,
            user_context_yaml=user_context_yaml,
            domain_kb_files=kb_files,
        )
    except MissingAPIKeyError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2) from exc
    except AgentRunError as exc:
        typer.echo(f"Plan-critique agent error: {exc}")
        raise typer.Exit(code=3) from exc

    out = report.output
    typer.echo("\n=== PLAN CRITIQUE ===")
    typer.echo(f"Plan: {out.plan_label}")
    typer.echo(f"Snapshot: {out.snapshot_label}")
    typer.echo(f"Confidence: {out.confidence.value}")
    typer.echo("")
    typer.echo("Overall summary:")
    typer.echo(out.overall_summary)
    typer.echo("")
    for f in out.findings:
        typer.echo(f"[{f.severity}] {f.topic} — {f.plan_item_ref}")
        typer.echo(f"  {f.summary}")
        for ev in f.evidence:
            typer.echo(f"   • {ev}")
        if f.cited_sources:
            typer.echo(f"  cite: {', '.join(f.cited_sources)}")
        if f.recommended_action:
            typer.echo(f"  action: {f.recommended_action}")
        typer.echo("")

    typer.echo(
        f"Tokens in/out: {report.tokens_in}/{report.tokens_out}; cost ≈ ${report.cost_usd:.4f}"
    )

    if save and plan_version_id is not None:
        async with db_mod.get_session() as session:
            session.add(
                PlanCritique(
                    user_id=user_id,
                    plan_version_id=plan_version_id,
                    critique_json=out.model_dump_json(),
                    model=report.model,
                )
            )
            await session.commit()
        typer.echo("Critique saved to plan_critiques.")


def _load_relevant_kb_for_israeli_user() -> dict[str, str]:
    """Return the Israeli/US tax KB files, keyed by repo-relative path.

    Phase 1 hardcodes the priority-1 file set per SDD §7.6. Phase 2+ will
    let the orchestrator pick a file set based on `user_context`.
    """
    settings = get_settings()
    root = settings.domain_knowledge_dir
    targets = [
        "tax/israel/brackets_2026.md",
        "tax/israel/national_insurance.md",
        "tax/israel/capital_gains.md",
        "tax/israel/surtax.md",
        "tax/israel/treaties/us_israel.md",
        "tax/us/nonresident_withholding.md",
        "tax/us/estate_tax_nonresidents.md",
        "tax/israel/retirement/keren_hishtalmut.md",
        "tax/israel/retirement/kupat_gemel.md",
        "tax/israel/retirement/section_102.md",
    ]
    out: dict[str, str] = {}
    for rel in targets:
        path = root / rel
        if path.is_file():
            out[rel] = path.read_text(encoding="utf-8")
    return out
