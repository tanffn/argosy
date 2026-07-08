"""Domain-refresh agent (SDD §3.6, Appendix B.8, Phase 7).

Re-verifies `domain_knowledge/*.md` files against current sources.
Produces structured proposals for human review — NEVER auto-edits files.

Inputs: list of files due for refresh (each carries current content +
frontmatter). Output: `DomainRefreshReport` with one
`FileRefreshResult` per file. Status is `no_change` (bump
`last_verified`, compute `next_refresh_due`) or `change_proposed`
(diff + cited evidence go to the review queue).

**Sonnet**. Tools: WebFetch / WebSearch (mocked in tests).

Design notes:
  - Tier-1 sources required for material change proposals (per SDD §7.4).
  - Date arithmetic for `next_refresh_due` follows the file's frontmatter
    `refresh_policy` field; defaults to 90 days when absent.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class CitedSource(BaseModel):
    url: str
    retrieved_at: str = Field(description="ISO date when the source was fetched.")
    excerpt: str = Field(default="", description="Short verbatim quote from the source.")
    tier: int = Field(
        default=2,
        description="Source-credibility tier 1-3 (1 = primary regulator/issuer).",
    )


class FileRefreshResult(BaseModel):
    path: str = Field(description="Path under `domain_knowledge/`.")
    status: str = Field(description="'no_change' | 'change_proposed'")
    diff: str | None = Field(
        default=None,
        description="Unified-diff-style proposed update; null when no change.",
    )
    evidence: list[CitedSource] = Field(default_factory=list)
    next_refresh_due: date | None = Field(
        default=None,
        description="ISO date when this file is next due for refresh.",
    )
    note: str = Field(default="")


class DomainRefreshReport(BaseModel):
    per_file: list[FileRefreshResult] = Field(default_factory=list)
    summary: str = Field(default="")
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Top-level distinct cited URLs across all per-file evidence.",
    )


class DomainRefreshAgent(BaseAgent[DomainRefreshReport]):
    """Re-verifies domain knowledge files against current sources.

    NEVER auto-edits files. Produces proposals for human approve/reject.
    Tier-1 sources required for material changes; never propose a change
    based solely on Tier-3 sources.
    """

    agent_role = "domain_refresh"
    output_model = DomainRefreshReport
    require_citations = True
    # The prompt requires live re-fetches, so the claude_code backend must
    # actually GRANT the web tools. Without this the agent ran tool-less and
    # (correctly) refused to fabricate verification — observed live 2026-07-07:
    # "I cannot fabricate verification I did not perform. I have no live
    # web-tool results" → empty `cited_sources` → the citation gate raised
    # AgentRunError on every annual tick.
    claude_code_allowed_tools: tuple[str, ...] = ("WebSearch", "WebFetch")
    # max_tokens driven by DEFAULT_MAX_TOKENS_BY_ROLE (8192).

    def build_prompt(
        self,
        *,
        files_due: list[dict[str, str]],
    ) -> tuple[str, str]:
        """Build the prompt.

        Args:
            files_due: list of `{path, frontmatter, content}` dicts. The
                caller (loop) reads each file and computes whether it's
                due based on its `next_refresh_due` frontmatter.
        """
        system = (
            "You are the domain-refresh agent on the Argosy fleet. You "
            "verify domain_knowledge files against current authoritative "
            "sources and propose updates for human review. You NEVER "
            "auto-edit files — you only produce structured proposals.\n\n"
            "Rules per file:\n"
            "  1. Re-fetch each cited source via web tools (WebFetch / "
            "WebSearch).\n"
            "  2. Compare current source content with the file's claims.\n"
            "  3. If material change detected:\n"
            "     - Generate a structured diff (current vs proposed).\n"
            "     - Cite the specific source language (excerpt) driving "
            "the change.\n"
            "     - Set `status='change_proposed'`.\n"
            "  4. If no material change:\n"
            "     - Set `status='no_change'`.\n"
            "     - Bump `next_refresh_due` per the file's "
            "`refresh_policy` (default: 90 days from today).\n\n"
            "  5. CITATIONS ARE MANDATORY. Every per_file entry must carry "
            "at least one `evidence` item with the URL you actually "
            "consulted — a `no_change` verdict still cites the source you "
            "verified the claims against (with today's `retrieved_at`). "
            "Copy every distinct evidence URL into the top-level "
            "`cited_sources` list. An output with an empty `cited_sources` "
            "fails validation and the whole run is discarded. Never invent "
            "a URL you did not fetch — if a source is unreachable, say so "
            "in the file's `note` and cite the sources you DID reach.\n\n"
            "  Tier-1 sources REQUIRED for material change proposals "
            "(primary regulator / issuer publication). Never propose a "
            "change based solely on Tier-3+ commentary sources.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{DomainRefreshReport.model_json_schema()}\n"
        )

        if not files_due:
            user = "No files due for refresh. Return an empty per_file list."
            return system, user

        blocks: list[str] = []
        for f in files_due:
            blocks.append(
                f"=== {f.get('path', '?')} ===\n"
                f"FRONTMATTER:\n{f.get('frontmatter', '(none)')}\n\n"
                f"CONTENT:\n{f.get('content', '(empty)')}"
            )
        user = (
            f"Files due for refresh ({len(files_due)}):\n\n"
            + "\n\n".join(blocks)
            + "\n\nProduce a DomainRefreshReport JSON now. One per_file "
            "entry per file above."
        )
        return system, user


__all__ = [
    "CitedSource",
    "DomainRefreshAgent",
    "DomainRefreshReport",
    "FileRefreshResult",
]
