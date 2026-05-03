"""Watchlist agent (SDD §3.6, Phase 7).

Maintains the universe of tickers tracked: positions + candidates +
reduce-list. Reads positions snapshots; persists to a new `watchlists`
table.

Output: `WatchlistReport` with current_tickers, added_today,
removed_today, candidates_under_review. **Haiku**.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand

WatchKind = Literal["position", "candidate", "reduce"]


class WatchlistEntry(BaseModel):
    ticker: str
    kind: WatchKind
    note: str = ""


class WatchlistReport(BaseModel):
    current_tickers: list[WatchlistEntry] = Field(default_factory=list)
    added_today: list[str] = Field(default_factory=list)
    removed_today: list[str] = Field(default_factory=list)
    candidates_under_review: list[str] = Field(
        default_factory=list,
        description="Tickers being considered for entry — not yet positions.",
    )
    summary: str = Field(default="")
    confidence: ConfidenceBand = ConfidenceBand.HIGH
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Snapshot path / plan version backing the changes.",
    )


class WatchlistAgent(BaseAgent[WatchlistReport]):
    """Haiku-class watchlist maintainer.

    Diffs today's positions snapshot against yesterday's saved watchlist
    and proposes adds/removes/kind-changes. Cheap and deterministic-ish.
    """

    agent_role = "watchlist"
    output_model = WatchlistReport
    require_citations = True
    max_tokens = 2048

    def build_prompt(
        self,
        *,
        positions_tickers: list[str],
        prior_watchlist: list[dict],
        plan_candidates: list[str] | None = None,
        plan_reduce_list: list[str] | None = None,
        snapshot_label: str = "(unknown)",
    ) -> tuple[str, str]:
        """Build the prompt.

        Args:
            positions_tickers: tickers from today's snapshot.
            prior_watchlist: yesterday's saved watchlist rows
                (`[{ticker, kind, note}, ...]`).
            plan_candidates: tickers the plan flags for potential entry.
            plan_reduce_list: tickers the plan flags for trimming.
            snapshot_label: identifier of the source snapshot.
        """
        plan_candidates = plan_candidates or []
        plan_reduce_list = plan_reduce_list or []

        system = (
            "You are the watchlist agent on the Argosy fleet. You "
            "maintain the universe of tracked tickers — splitting them "
            "into 'position' (held), 'candidate' (under review for "
            "entry), and 'reduce' (held but flagged for trimming).\n\n"
            "Rules:\n"
            "  - Every ticker held in the snapshot must be in "
            "current_tickers with kind='position' (or 'reduce' if it "
            "appears in plan_reduce_list).\n"
            "  - plan_candidates that aren't already positions go in "
            "candidates_under_review and current_tickers as "
            "kind='candidate'.\n"
            "  - added_today: tickers in current that weren't in "
            "prior_watchlist.\n"
            "  - removed_today: tickers in prior_watchlist that aren't "
            "in current.\n"
            "  - Cite the snapshot label in cited_sources.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{WatchlistReport.model_json_schema()}\n"
        )

        prior_lines = (
            "\n".join(
                f"  - {p.get('ticker')} kind={p.get('kind')} "
                f"note={p.get('note', '')}"
                for p in prior_watchlist
            )
            or "  (none)"
        )

        user = (
            f"SNAPSHOT LABEL: {snapshot_label}\n\n"
            "POSITIONS TICKERS:\n"
            + ("\n".join(f"  - {t}" for t in positions_tickers) or "  (none)")
            + "\n\nPRIOR WATCHLIST:\n"
            + prior_lines
            + "\n\nPLAN CANDIDATES:\n"
            + ("\n".join(f"  - {t}" for t in plan_candidates) or "  (none)")
            + "\n\nPLAN REDUCE-LIST:\n"
            + ("\n".join(f"  - {t}" for t in plan_reduce_list) or "  (none)")
            + "\n\nProduce a WatchlistReport JSON now."
        )
        return system, user


__all__ = ["WatchKind", "WatchlistAgent", "WatchlistEntry", "WatchlistReport"]
