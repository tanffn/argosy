"""`argosy decide` — run a one-shot decision flow on demand.

Phase 3 helper. Pulls analyst reports for the ticker, runs the full
decision pipeline at the requested tier (or 'auto'), and prints the
outcome (proposal id + status, or block reason).

The flow itself is in `argosy.decisions.flow`; this file only handles
CLI plumbing + I/O.
"""

from __future__ import annotations

import asyncio
import json

import typer
from sqlalchemy import desc, select

from argosy.agent_settings import load_agent_settings
from argosy.agents.base import AgentReport, ConfidenceBand
from argosy.agents.errors import AgentRunError, MissingAPIKeyError
from argosy.decisions.flow import (
    ApprovedProposal,
    BlockedProposal,
    DecisionFlow,
)
from argosy.decisions.tiers import (
    Tier,
    TierContext,
    apply_override_mode,
    resolve_tier,
)
from argosy.logging import configure_logging
from argosy.state import db as db_mod
from argosy.state.models import AgentReport as AgentReportRow


def decide(
    ticker: str = typer.Option(..., "--ticker", help="Ticker under decision."),
    tier: str = typer.Option(
        "auto",
        "--tier",
        help="Tier override; one of auto|T0|T1|T2|T3.",
    ),
    user_id: str = typer.Option("ariel", "--user-id"),
    proposed_value: float = typer.Option(
        0.0,
        "--proposed-value",
        help="Proposed dollar size (used when --tier=auto).",
    ),
    portfolio_value: float = typer.Option(
        1.0,
        "--portfolio-value",
        help="Total portfolio value (used when --tier=auto).",
    ),
    account_class: str = typer.Option(
        "main", "--account-class", help="'main' or 'limited'."
    ),
    account_value: float = typer.Option(
        0.0, "--account-value",
        help="Account-class value (limited acct 20% rule).",
    ),
) -> None:
    """Trigger a decision flow for `--ticker` at `--tier`."""
    configure_logging()
    db_mod.init_engine()

    async def _main() -> int:
        settings = load_agent_settings(user_id)

        if tier.lower() == "auto":
            ctx = TierContext(
                proposed_value_usd=proposed_value,
                portfolio_value_usd=portfolio_value,
                account_class=account_class,  # type: ignore[arg-type]
                ticker=ticker,
                is_nvda=ticker.upper() == "NVDA",
                is_plan_structural=False,
                crosses_concentration_cap=False,
                recent_red_flag=False,
                account_value_usd=account_value,
            )
            auto_tier = resolve_tier(ctx, settings)
            chosen = apply_override_mode(auto_tier, settings)
        else:
            try:
                chosen = Tier.from_str(tier)
            except ValueError:
                typer.echo(f"Unknown tier: {tier!r}; use auto|T0|T1|T2|T3.")
                return 2

        # Pull most recent analyst reports for the user (limit to 10).
        async with db_mod.get_session() as session:
            rows = (
                await session.execute(
                    select(AgentReportRow)
                    .where(AgentReportRow.user_id == user_id)
                    .order_by(desc(AgentReportRow.created_at))
                    .limit(10)
                )
            ).scalars().all()
        analyst_reports = [_row_to_report(r) for r in rows]

        # H7: feed the risk team REAL user constraints + configured risk
        # caps instead of empties. ``resolve_risk_inputs`` is best-effort
        # (returns ("", {}) on any failure) and opens its own short-lived
        # sync session. Lazy import to avoid a module-load circular import.
        from argosy.orchestrator.flows.plan_synthesis import resolve_risk_inputs

        user_constraints, risk_caps = resolve_risk_inputs(user_id)

        flow = DecisionFlow(user_id=user_id, settings=settings)
        outcome = await flow.run(
            ticker=ticker,
            tier=chosen,
            analyst_reports=analyst_reports,
            positions_summary="",
            user_constraints=user_constraints,
            risk_caps=risk_caps,
            account_class=account_class,  # type: ignore[arg-type]
        )

        if isinstance(outcome, ApprovedProposal):
            typer.echo(
                f"Approved (decision_run={outcome.decision_run_id}, "
                f"proposal={outcome.proposal.id}, tier={chosen.value})"
            )
            typer.echo(f"  reason: {outcome.fund_manager.reason}")
            return 0
        assert isinstance(outcome, BlockedProposal)
        typer.echo(
            f"Blocked by {outcome.blocked_by} "
            f"(decision_run={outcome.decision_run_id}, tier={chosen.value})"
        )
        typer.echo(f"  reason: {outcome.reason}")
        return 1

    try:
        rc = asyncio.run(_main())
        if rc != 0:
            raise typer.Exit(code=rc)
    except MissingAPIKeyError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2) from exc
    except AgentRunError as exc:
        typer.echo(f"Decision flow failed: {exc}")
        raise typer.Exit(code=3) from exc


def _row_to_report(r: AgentReportRow) -> AgentReport:
    """Reconstruct a minimal AgentReport from an `agent_reports` row."""
    from pydantic import BaseModel

    class _Anonymous(BaseModel):
        agent_role: str = "analyst"
        cited_sources: list[str] = []
        confidence: ConfidenceBand = ConfidenceBand.MEDIUM
        report: str = ""

    payload_str = r.response_text
    cs: list[str] = ["agent_reports"]
    try:
        payload = json.loads(r.response_text)
        if isinstance(payload, dict):
            cs = list(payload.get("cited_sources", cs))
    except Exception:
        pass

    obj = _Anonymous(
        agent_role=r.agent_role,
        cited_sources=cs,
        confidence=ConfidenceBand(r.confidence) if r.confidence else ConfidenceBand.MEDIUM,
        report=payload_str,
    )
    return AgentReport(
        agent_role=r.agent_role,
        user_id=r.user_id,
        model=r.model,
        response_text=r.response_text,
        tokens_in=r.tokens_in,
        tokens_out=r.tokens_out,
        cost_usd=float(r.cost_usd),
        prompt_hash=r.prompt_hash,
        confidence=ConfidenceBand(r.confidence) if r.confidence else None,
        output=obj,
        decision_id=r.decision_id,
    )
