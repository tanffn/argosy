"""Input-assembly helpers for plan synthesis."""

from __future__ import annotations


def _assemble_portfolio_summary(*, session, user_id) -> str:
    """Build a compact portfolio-state summary for synthesis input.

    Wave 2: read latest TSV/CSV ingest + IBKR positions per SDD §8.
    Tests stub this.
    """
    return "(portfolio snapshot — wired against existing positions ingest)"


def _assemble_fills_summary(*, session, user_id) -> str:
    """Last 90 days of fills + decisions, summarized."""
    return "(fills summary — wired against fills + proposals tables)"


def _load_user_context_yaml(*, session, user_id) -> str:
    """Concatenate identity + goals + constraints YAML for the user."""
    from argosy.state.models import UserContext
    ctx = session.get(UserContext, user_id)
    if ctx is None:
        return ""
    parts = []
    if ctx.identity_yaml:
        parts.append(ctx.identity_yaml)
    if ctx.goals_yaml:
        parts.append(ctx.goals_yaml)
    if ctx.constraints_yaml:
        parts.append(ctx.constraints_yaml)
    return "\n".join(parts)
