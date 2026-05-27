"""Daily-briefer agent — single-pass one-pager markdown (T4.5).

A lightweight Sonnet-class agent that composes the user's daily brief
into a one-pager markdown document. Reads:

  - The current plan context (label + the plan's own markdown)
  - The current pending draft (if one exists; otherwise current plan)
  - Overnight market deltas (FRED + Finnhub + yfinance snapshots)
  - Top positions summary

Produces a structured ``DailyBriefMarkdown`` with one top-line teaser
plus the full markdown body. The body is what lands on the home page.

Cost discipline: ``max_tokens`` is capped at 4096. At Sonnet rates
($15/M output) the worst-case cost is ~$0.06 — well inside the T4.5
~$1/brief cap. The runner additionally validates ``cost_usd`` and
emits a warning if the cap is exceeded.

This agent is INDEPENDENT of the Phase 2 ``DailyBriefLoop`` four-agent
flow. The Phase 2 loop runs the news / macro / concentration /
plan-critique fleet and writes a different shape; T4.5 is a slim
companion that lives on the same table via the migration 0034 columns
(``brief_date`` + ``content_md`` + ``decision_run_id``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class DailyBriefMarkdown(BaseModel):
    """Structured one-pager output for the daily brief."""

    top_line: str = Field(
        default="",
        description=(
            "One-sentence teaser for the home-page card title strip. "
            "Should mention the most important overnight signal (a "
            "concentration breach, a macro regime shift, a key holding "
            "headline) in <140 characters."
        ),
    )
    content_md: str = Field(
        default="",
        description=(
            "The full one-pager markdown body. Sections (in order, "
            "ALL OPTIONAL — omit a section if there's nothing useful "
            "to say): Overnight macro, Headlines on holdings, Plan "
            "alignment, What to watch today. No more than ~400 words "
            "total; this is a glance, not a research note."
        ),
    )
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description=(
            "Source ids (e.g. 'macro/FRED/VIXCLS', 'news/NVDA', "
            "'plan:<label>') backing any specific numeric claim."
        ),
    )


class DailyBrieferAgent(BaseAgent[DailyBriefMarkdown]):
    """Sonnet one-pager composer for the daily brief.

    Single-pass — no debate, no fanout. Reads four named blocks via
    document sources so the Citations API can attach offset-level
    citations back into the input data.
    """

    agent_role = "daily_briefer"
    output_model = DailyBriefMarkdown
    require_citations = False  # graceful degradation: empty inputs → empty cites
    # max_tokens driven by DEFAULT_MAX_TOKENS_BY_ROLE (8000).

    def build_prompt(
        self,
        *,
        plan_label: str,
        plan_markdown: str,
        positions_summary: str,
        macro_snapshot: dict[str, float],
        news_payload: dict[str, list[dict[str, Any]]],
    ) -> tuple[str, str, list[tuple[str, str]]]:
        """Build the daily-briefer prompt.

        Args:
            plan_label: human-readable label of the plan/draft being
                followed (e.g. ``Jacobs_Wealth_Plan v2.0`` or
                ``draft-2026-05-26``). Used in the prompt + the
                home-page card title.
            plan_markdown: the markdown of the plan/draft. Threaded as
                a document source so the agent can cite it.
            positions_summary: one-line-per-position text snapshot of
                today's portfolio.
            macro_snapshot: numeric macro readings (vix, ust_10y,
                usd_nis, etc).
            news_payload: per-ticker list of headline dicts as
                returned by ``FinnhubAdapter.get_company_news``.

        Returns ``(system, user, sources)`` per ``BaseAgent.run``.
        """
        system = (
            "You are the daily-brief composer on the Argosy fleet. "
            "Produce a single one-pager markdown that the user reads "
            "first thing in the morning. Discipline:\n\n"
            "  - <400 words total. This is a glance, not research.\n"
            "  - Lead with the most important signal (a concentration\n"
            "    breach, a macro regime shift, a key holding headline).\n"
            "  - If an input section is empty (no overnight data, no\n"
            "    news, no plan), say so plainly in one short sentence —\n"
            "    don't fabricate or pad.\n"
            "  - Cite the source for any specific numeric claim. Inputs\n"
            "    are attached as document blocks with ids like\n"
            "    `macro/FRED/VIXCLS`, `news/NVDA`, or `plan:<label>`;\n"
            "    use those ids in ``cited_sources``.\n"
            "  - ``top_line`` is the headline you'd put under the card\n"
            "    title on the home page — <140 chars, no markdown.\n"
            "  - ``content_md`` carries the full body in plain Markdown.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{DailyBriefMarkdown.model_json_schema()}\n"
        )

        sources: list[tuple[str, str]] = []

        # Plan block.
        if plan_markdown:
            sources.append((f"plan:{plan_label}", plan_markdown))
        # Macro readings as a single document so the agent reads them as
        # one block; per-series cites are still possible via the
        # source-ids embedded in the body lines.
        if macro_snapshot:
            macro_body = "\n".join(
                f"{k}: {v}" for k, v in sorted(macro_snapshot.items())
            )
            sources.append(("macro/snapshot", macro_body))
        # News, one source per ticker.
        for ticker, headlines in sorted(news_payload.items()):
            if not headlines:
                continue
            lines = []
            for h in headlines[:5]:  # 5 headlines per ticker max
                title = h.get("headline") or h.get("title") or ""
                summ = h.get("summary") or ""
                lines.append(f"- {title}\n  {summ}".rstrip())
            sources.append((f"news/{ticker}", "\n".join(lines)))

        # Roster of sources for the user prompt.
        roster_lines: list[str] = []
        if plan_markdown:
            roster_lines.append(f"  - plan:{plan_label}")
        if macro_snapshot:
            roster_lines.append("  - macro/snapshot")
        for ticker in sorted(news_payload):
            if news_payload[ticker]:
                roster_lines.append(f"  - news/{ticker}")
        roster_block = "\n".join(roster_lines) or "  (no inputs available)"

        positions_block = positions_summary or "(no portfolio snapshot)"

        user = (
            f"PLAN: {plan_label or '(none)'}\n\n"
            "AVAILABLE DOCUMENT SOURCES (cite by source_id):\n"
            f"{roster_block}\n\n"
            "PORTFOLIO POSITIONS (one line per holding):\n"
            f"{positions_block}\n\n"
            "Produce the DailyBriefMarkdown JSON now. If everything\n"
            "above is empty, write a graceful one-line content_md\n"
            "stating 'No overnight data available' and set confidence\n"
            "to LOW.\n"
        )
        return system, user, sources


__all__ = ["DailyBrieferAgent", "DailyBriefMarkdown"]
