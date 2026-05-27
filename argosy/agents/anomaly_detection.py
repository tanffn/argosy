"""Anomaly-detection agent (EX2).

Watches the user's incoming bank/card statements for the disappearance
of expected patterns — first user-driven entry is the Discount Bank
card 2923 fee-waiver promotion (charge + matching discount that should
net to ₪0; if the discount line vanishes, the user starts paying fees
again).

Receives:
  - The latest statement that triggered the run (parsed transactions
    for the account in question), threaded as a document source so
    the Citations API can attach offset-level citations to specific
    transactions when the agent flags them.
  - The applicable watchlist entries (filtered by the runner to those
    that match the account/issuer of the triggering statement, or all
    entries on a daily/manual run).

Produces a structured AnomalyDetectionReport with per-anomaly severity
and per-watchlist-entry state.

Cost discipline: Opus 4.7 with thinking_budget=8000, max_tokens=16000
per the EX2 spec — anomaly detection is judgment-heavy (the agent has
to recognize Hebrew merchant strings, match charge/discount pairs, and
distinguish a missing line from a name-change) so this is one of the
"accuracy over LLM cost" agents.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class Anomaly(BaseModel):
    """One detected anomaly — what changed and how serious it is."""

    severity: Literal["RED", "AMBER", "YELLOW"] = Field(
        description=(
            "RED = the user is losing money / will soon (act now). "
            "AMBER = something looks off, investigate within a week. "
            "YELLOW = informational drift, no immediate action."
        ),
    )
    watchlist_entry_name: str = Field(
        description=(
            "The `name` field of the watchlist entry this anomaly applies "
            "to. Used to back-link the anomaly to its rule."
        ),
    )
    observation: str = Field(
        description=(
            "What changed, in one or two sentences. State the expected "
            "state, the observed state, and (if known) the period in "
            "which it changed."
        ),
    )
    last_seen: str = Field(
        default="",
        description=(
            "When the expected state was last observed, ISO-8601 date "
            "(YYYY-MM-DD) or 'never' if the pattern was never seen. "
            "The runner may also overwrite this from DB history."
        ),
    )
    suggested_action: str = Field(
        description=(
            "Plain-English action the user can take. Examples: 'Call "
            "Discount Bank and ask about the fee-waiver promotion '"
            "expiry date.' or 'Open the statement and verify the "
            "discount line is still present.'"
        ),
    )


class WatchlistEntryStatus(BaseModel):
    """Per-watchlist-entry status snapshot. One row per entry the
    agent evaluated this run (whether or not it triggered an anomaly)."""

    name: str
    state: Literal["NORMAL", "ALERT", "RESOLVED", "UNKNOWN"] = Field(
        description=(
            "NORMAL  = expected pattern observed in the latest period. "
            "ALERT   = expected pattern missing or drifted; an "
            "          ``Anomaly`` row exists for this entry. "
            "RESOLVED= an ALERT entry in a prior run is back to NORMAL "
            "          this run. "
            "UNKNOWN = insufficient data to evaluate (e.g. the statement "
            "          for this account hasn't arrived yet)."
        ),
    )
    last_evidence: str = Field(
        default="",
        description=(
            "Short citation pointer (a merchant string + amount) that "
            "justifies the state. Empty when state=UNKNOWN."
        ),
    )


class AnomalyDetectionReport(BaseModel):
    """Structured output of one anomaly-check run."""

    anomalies: list[Anomaly] = Field(
        default_factory=list,
        description=(
            "Zero or more detected anomalies. Empty when every "
            "watchlist entry is NORMAL."
        ),
    )
    watchlist_status: list[WatchlistEntryStatus] = Field(
        default_factory=list,
        description=(
            "One status row per watchlist entry the agent evaluated "
            "this run. The runner uses this for the per-entry trend "
            "view."
        ),
    )
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description=(
            "Source ids (e.g. 'statement:1234', 'watchlist:<name>') "
            "backing specific claims in the report."
        ),
    )


# System prompt — kept as a module constant so tests / docs can read it
# verbatim without instantiating the agent. The handover note for EX2
# requires the prompt be returned to the caller (verbatim) alongside
# the implementation.
ANOMALY_DETECTION_SYSTEM_PROMPT = (
    "You are the anomaly-detection agent on the Argosy fleet. Your "
    "job is to compare the user's INCOMING bank/card statement "
    "against a watchlist of expected patterns and surface any "
    "deviation as a structured Anomaly.\n\n"
    "PRINCIPLES:\n"
    "  - Be CONSERVATIVE. The user reads every RED you raise; spurious "
    "    alerts erode trust. If the evidence is thin, set "
    "    confidence=LOW and state UNKNOWN — don't fabricate.\n"
    "  - Hebrew is the source language for Israeli statements. The "
    "    watchlist patterns may include Hebrew regex (e.g. 'עמלת "
    "    כרטיס'). Match them literally; do NOT translate or normalize.\n"
    "  - Money math: when a rule says 'two amounts must sum to within "
    "    ₪0.01 of zero', verify that arithmetically. State the actual "
    "    numbers you summed in the observation.\n"
    "  - If the latest statement does NOT cover the account named in a "
    "    watchlist entry, mark that entry state=UNKNOWN. Don't ALERT "
    "    on absence of evidence when the data simply hasn't arrived.\n\n"
    "INPUT SHAPE:\n"
    "  - One document block per WATCHLIST entry, source_id "
    "    'watchlist:<name>'. Carries the entry's expected_pattern + "
    "    alert_when text.\n"
    "  - One document block per STATEMENT (usually one — the "
    "    triggering statement on event-driven runs; possibly several "
    "    on a daily backstop). source_id 'statement:<statement_id>'. "
    "    Carries the period, account label, and the parsed "
    "    transaction list (one line per tx: date | merchant | amount | "
    "    direction).\n"
    "  - Optionally a 'recent_history:<name>' block with prior periods' "
    "    matches for context.\n\n"
    "OUTPUT must be a JSON object conforming to this schema:\n"
    f"{AnomalyDetectionReport.model_json_schema()}\n"
)


class AnomalyDetectionAgent(BaseAgent[AnomalyDetectionReport]):
    """Opus-class agent that flags missing patterns in user statements.

    Single-pass — no debate, no fanout. Reads watchlist + statement
    document sources via the Citations API so it can attach offset-level
    citations back to the specific charge/discount transactions it's
    reasoning about.
    """

    agent_role = "watchlist"  # reuses the existing 'watchlist' role config
    output_model = AnomalyDetectionReport
    require_citations = False  # graceful: a NORMAL run has no citations
    # max_tokens / thinking_budget are bumped in __init__ below — this
    # subclass needs 16K out + 8K thinking, beyond the role-table
    # defaults for 'watchlist' (which is otherwise a thin agent).

    def __init__(self, *, user_id: str, model: str | None = None) -> None:
        super().__init__(user_id=user_id, model=model)
        # EX2 override — the spec calls for thinking_budget=8000 and a
        # 16K max_tokens cap. The 'watchlist' role's table entries are
        # smaller defaults (no thinking, 8K out). Bump both here without
        # touching the role table. Re-validate the Anthropic invariant
        # (thinking < max_tokens) after the bump.
        self.thinking_budget = max(self.thinking_budget, 8000)
        self.max_tokens = max(self.max_tokens, 16000)
        if self.thinking_budget >= self.max_tokens:
            raise ValueError(
                f"{self.agent_role}: thinking_budget ({self.thinking_budget}) "
                f"must be less than max_tokens ({self.max_tokens}) — Anthropic "
                f"API constraint."
            )

    def build_prompt(
        self,
        *,
        watchlist_entries: list[dict[str, Any]],
        statements: list[dict[str, Any]],
        recent_history: dict[str, str] | None = None,
    ) -> tuple[str, str, list[tuple[str, str]]]:
        """Build the anomaly-detection prompt.

        Args:
            watchlist_entries: list of dicts with keys
                ``name``, ``description``, ``expected_pattern``,
                ``alert_when``, ``severity``.
            statements: list of dicts with keys
                ``statement_id``, ``account_label``, ``period_start``,
                ``period_end``, ``transactions`` (list of one-line
                strings: "YYYY-MM-DD | merchant | amount NIS | direction").
            recent_history: optional ``{watchlist_name: prior_periods_text}``
                mapping for context.

        Returns ``(system, user, sources)`` per ``BaseAgent.run``.
        """
        recent_history = recent_history or {}
        sources: list[tuple[str, str]] = []

        # Watchlist entries — one source per entry so the agent can
        # cite the specific rule it's evaluating.
        for entry in watchlist_entries:
            name = entry.get("name", "(unnamed)")
            body = (
                f"NAME: {name}\n"
                f"DESCRIPTION:\n{entry.get('description', '')}\n\n"
                f"EXPECTED PATTERN:\n{entry.get('expected_pattern', '')}\n\n"
                f"ALERT WHEN:\n{entry.get('alert_when', '')}\n\n"
                f"DEFAULT SEVERITY: {entry.get('severity', 'AMBER')}\n"
            )
            sources.append((f"watchlist:{name}", body))

        # Statements — one source per statement.
        for stmt in statements:
            sid = stmt.get("statement_id", "unknown")
            account = stmt.get("account_label", "(unknown account)")
            period_start = stmt.get("period_start", "")
            period_end = stmt.get("period_end", "")
            txs = stmt.get("transactions", []) or []
            body_lines = [
                f"STATEMENT_ID: {sid}",
                f"ACCOUNT: {account}",
                f"PERIOD: {period_start} -> {period_end}",
                f"TRANSACTION COUNT: {len(txs)}",
                "",
                "TRANSACTIONS:",
            ]
            body_lines.extend(txs)
            sources.append((f"statement:{sid}", "\n".join(body_lines)))

        # Recent history (optional).
        for name, history_text in sorted(recent_history.items()):
            if history_text:
                sources.append((f"recent_history:{name}", history_text))

        # Roster lines for the user prompt so the model knows what it
        # has access to without re-listing every body.
        roster_lines: list[str] = []
        for entry in watchlist_entries:
            roster_lines.append(f"  - watchlist:{entry.get('name', '(?)')}")
        for stmt in statements:
            roster_lines.append(f"  - statement:{stmt.get('statement_id', '?')}")
        for name in sorted(recent_history):
            if recent_history.get(name):
                roster_lines.append(f"  - recent_history:{name}")
        roster_block = "\n".join(roster_lines) or "  (no inputs available)"

        n_entries = len(watchlist_entries)
        n_stmts = len(statements)

        user = (
            f"You have {n_entries} watchlist entr{'y' if n_entries == 1 else 'ies'} "
            f"and {n_stmts} statement{'' if n_stmts == 1 else 's'} to evaluate.\n\n"
            "AVAILABLE DOCUMENT SOURCES (cite by source_id):\n"
            f"{roster_block}\n\n"
            "TASK:\n"
            "  1. For EACH watchlist entry, decide whether the expected\n"
            "     pattern is present in the statement(s) for the matching\n"
            "     account. Produce one WatchlistEntryStatus per entry.\n"
            "  2. For EACH entry whose pattern is missing or drifted,\n"
            "     produce one Anomaly with severity = the entry's\n"
            "     default severity (unless the actual deviation warrants\n"
            "     a downgrade — e.g. a ₪0.03 drift is YELLOW, not RED).\n"
            "  3. If a watchlist entry's account is not present in any\n"
            "     attached statement, mark state=UNKNOWN — do NOT alert.\n\n"
            "Produce the AnomalyDetectionReport JSON now.\n"
        )

        return ANOMALY_DETECTION_SYSTEM_PROMPT, user, sources


__all__ = [
    "ANOMALY_DETECTION_SYSTEM_PROMPT",
    "Anomaly",
    "AnomalyDetectionAgent",
    "AnomalyDetectionReport",
    "WatchlistEntryStatus",
]
