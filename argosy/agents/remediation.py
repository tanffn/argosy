"""Inter-agent remediation requests — analyst → orchestrator signal.

Per [[feedback_agents_talk_to_each_other]] (binding memory, 2026-05-31):
when an analyst detects an issue another agent / data fetcher can fix
(stale data, missing payload, inconsistent input), it emits a
structured ``RemediationRequest`` on its output. The orchestrator
inspects requests after the analyst phase, dispatches each, and
re-runs the requesting analyst with refreshed inputs.

This replaces the prior anti-pattern of analysts writing "I'd
recommend the Domain Refresh agent re-pull..." into free-text
prose — that prose surfaces to the user as a verdict, which is the
fleet asking the user to do the fleet's job.

The schema is intentionally small for v1. Extend the ``kind``
discriminator as new remediation paths land (e.g. Domain Refresh for
tax/policy facts; alpha-report re-ingest; 13F refetch).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


RemediationKind = Literal[
    # The supplied price reference (current_price / regularMarketPrice)
    # is materially inconsistent with prevailing levels — either stale
    # cache or pre-split. Orchestrator re-fetches via the gather with a
    # cache bypass.
    "price_stale",
    # The fundamentals payload is empty / incomplete — orchestrator
    # re-runs the gather + yfinance fallback.
    "fundamentals_stale",
    # The news payload is empty / missing — orchestrator re-fetches.
    "news_empty",
    # General "the input data looks wrong, please re-fetch" — used when
    # the analyst can't classify the issue more precisely. Orchestrator
    # re-fires the full per-ticker gather.
    "data_refresh",
]


class RemediationRequest(BaseModel):
    """Structured request from an analyst back to the orchestrator.

    The analyst NEVER writes this as prose — it goes on the analyst's
    structured output (``FundamentalsReport.remediation_requests``,
    ``NewsReport.remediation_requests``, etc.). The orchestrator
    inspects + dispatches.
    """

    kind: RemediationKind = Field(
        description=(
            "Discriminator for which remediation path the orchestrator "
            "should take. ``data_refresh`` is the catch-all when the "
            "analyst can't be more specific."
        ),
    )
    target_role: str = Field(
        description=(
            "Which analyst role is requesting (informational — the "
            "orchestrator already knows from the output, but having it "
            "in the structured field makes logs + audit easier)."
        ),
    )
    reason: str = Field(
        description=(
            "One-sentence reason the analyst is requesting remediation. "
            "Surfaces in the orchestrator's log + audit trail."
        ),
    )
    ticker: str | None = Field(
        default=None,
        description=(
            "Which ticker needs refresh. ``None`` means the whole gather "
            "(e.g. macro / fx-wide refresh)."
        ),
    )


__all__ = ["RemediationKind", "RemediationRequest"]
