"""Researcher agents — bull and bear (SDD §3.2, Appendix B.2, Phase 3).

Adversarial debate. Each side reads the analyst reports + the prior
debate rounds and marshals the strongest case from the evidence. Both
sides default to Opus per SDD §3.8 (adversarial debate is exactly the
case that justifies the spend).

The output is a `ResearcherTurn`: the position summary, 3-5 cited
points, and a direct response to the strongest opposing point from the
prior round (empty for round 1).

Independence (2026-08 stream B / TRLV scar, fix iteration 1):
``CitedPoint.independence`` is DERIVED from the tool-use record after
parse — a point is ``independent`` only when it cites an http(s) URL
that appeared in a real WebSearch/WebFetch tool result for that
invocation. The model's self-attestation is never sufficient.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class CitedPoint(BaseModel):
    """One argument with its supporting evidence."""

    claim: str = Field(description="One sentence stating the argument.")
    evidence: str = Field(
        description="Concrete evidence drawn from analyst reports or "
        "independently retrieved primary sources. Should include numbers "
        "or specific quotations where possible."
    )
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Analyst-report identifiers, domain_knowledge paths, "
        "or live primary-source URLs supporting this claim. Required "
        "(the citation gate enforces).",
    )
    independence: Literal["independent", "shared_payload"] = Field(
        default="shared_payload",
        description=(
            "Hint only — the orchestrator DERIVES the final value from the "
            "tool-use record. Prefer citing live primary-source URLs you "
            "actually retrieved via WebSearch; points whose URLs do not "
            "appear in tool results are forced to shared_payload."
        ),
    )


#: Shared status vocabulary with PremiseCheckAgent (keep in sync).
CatalystStatusClaimValue = Literal[
    "already_happened",
    "pending",
    "rejected",
    "delayed",
    "not_applicable",
    "unclear",
]


class CatalystStatusClaim(BaseModel):
    """Structured status claim for one premise — keyed by stable premise_id.

    Free-text catalyst labels are NOT used for matching. Key by the
    ``premise_id`` stamped by premise_check (p0, p1, …).

    Structural disagreement promotion requires claim-level http(s) URLs
    that appear in this turn's tool-retrieved URL set — empty cites or
    unre retrieved URLs are dropped, never elevated to the trader.
    """

    premise_id: str = Field(
        description="Exact premise_id from the PREMISE CHECK block "
        "(e.g. 'p0'). Must match an id listed there — unknown ids fail."
    )
    status: CatalystStatusClaimValue = Field(
        description="Your claimed CURRENT status for this premise, using "
        "the same vocabulary as premise_check."
    )
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Live http(s) URLs supporting THIS claim. Required for "
        "the claim to be promoted to a structural disagreement — and each "
        "URL must appear in this turn's WebSearch/WebFetch tool results. "
        "Top-level turn citations do not count. Blank/non-URL tokens do not "
        "count.",
    )


class ResearcherTurn(BaseModel):
    """Output of one round from one side of the debate."""

    side: Literal["bull", "bear"]
    round_index: int = Field(ge=1, description="1-indexed round counter.")
    position_summary: str = Field(
        description="One-sentence statement of the side's overall position."
    )
    points: list[CitedPoint] = Field(
        default_factory=list,
        description="3-5 strongest cited points the side advances this round.",
    )
    response_to_opposing: str = Field(
        default="",
        description="Direct response to the strongest opposing point from "
        "the prior round; empty string for round 1.",
    )
    # Structural — None = omitted (silence); [] = asserted empty answer.
    # When premise_check listed premises, flow requires a non-empty list
    # covering every premise_id. Omission/empty is recoverable unverified,
    # not "no disagreement".
    catalyst_status_claims: list[CatalystStatusClaim] | None = Field(
        default=None,
        description="One claim per premise_id from the PREMISE CHECK block "
        "(exact id equality). REQUIRED whenever premises were listed — "
        "omit/null is silence (failure), not agreement. Use [] only when "
        "premise_check reported no catalysts (premises=[]).",
    )
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Top-level distinct cited sources across all points; "
        "required for the citation gate.",
    )


def derive_point_independence(
    cited_sources: list[str],
    tool_retrieved_urls: list[str] | None,
) -> Literal["independent", "shared_payload"]:
    """Derive independence from the tool-use record — never from self-claim.

    A point is ``independent`` iff at least one of its cited http(s) URLs
    matches (after normalisation) a URL observed in a successful
    WebSearch/WebFetch tool **result** for this invocation.
    """
    from argosy.agents.base import urls_match

    tool_list = [
        u for u in (tool_retrieved_urls or [])
        if isinstance(u, str) and u.startswith(("http://", "https://"))
    ]
    if not tool_list:
        return "shared_payload"
    for sid in cited_sources or []:
        if isinstance(sid, str) and sid.startswith(("http://", "https://")):
            if any(urls_match(sid, u) for u in tool_list):
                return "independent"
    return "shared_payload"


def derive_turn_independence(
    turn: ResearcherTurn,
    tool_retrieved_urls: list[str] | None,
) -> ResearcherTurn:
    """Rewrite every point's independence from the tool-use record."""
    new_points = [
        p.model_copy(
            update={
                "independence": derive_point_independence(
                    list(p.cited_sources or []), tool_retrieved_urls,
                )
            }
        )
        for p in (turn.points or [])
    ]
    return turn.model_copy(update={"points": new_points})


def premise_ids_from_status(premise_status: dict | None) -> list[str]:
    """Ordered stable premise_ids from a premise_check report dict."""
    if not premise_status or premise_status.get("status") == "unverified":
        return []
    out: list[str] = []
    for p in premise_status.get("premises") or []:
        if not isinstance(p, dict):
            continue
        pid = (p.get("premise_id") or "").strip()
        if pid:
            out.append(pid)
    return out


def missing_catalyst_status_claims_reason(
    premise_status: dict | None,
    *,
    bear_turns: list[dict] | None = None,
    bull_turns: list[dict] | None = None,
) -> str | None:
    """Return an error reason when claims are omitted/invalid; else None.

    Distinguishes "model answered (claims present)" from "model never
    answered (claims null/omitted/empty while premises exist)". Unknown
    premise_ids are also failures — not silent skips.
    """
    required = premise_ids_from_status(premise_status)
    if not required:
        return None
    known = set(required)
    for side, turns in (("bear", bear_turns or []), ("bull", bull_turns or [])):
        if not turns:
            continue
        for idx, turn in enumerate(turns):
            # None / missing key = omitted silence; [] = empty answer.
            # Both are failures when premises exist — neither is "no disagreement".
            if "catalyst_status_claims" not in turn:
                return (
                    f"{side} turn[{idx}] omitted catalyst_status_claims while "
                    f"premise_check listed {required} — cannot distinguish "
                    "agreement from silence"
                )
            claims = turn.get("catalyst_status_claims")
            if claims is None:
                return (
                    f"{side} turn[{idx}] catalyst_status_claims is null "
                    f"while premise_check listed {required} — silence is not "
                    "a valid answer (emit one claim per premise_id)"
                )
            if not claims:
                return (
                    f"{side} turn[{idx}] has empty catalyst_status_claims "
                    f"while premise_check listed {required} — silent-empty "
                    "is not a valid answer (emit one claim per premise_id)"
                )
            seen: set[str] = set()
            for claim in claims:
                if not isinstance(claim, dict):
                    return (
                        f"{side} turn[{idx}] has a non-object "
                        "catalyst_status_claim"
                    )
                pid = (claim.get("premise_id") or "").strip()
                if not pid:
                    return (
                        f"{side} turn[{idx}] catalyst_status_claim missing "
                        "premise_id"
                    )
                if pid not in known:
                    return (
                        f"{side} turn[{idx}] references unknown premise_id "
                        f"{pid!r} (valid: {sorted(known)})"
                    )
                status = (claim.get("status") or "").strip()
                if not status:
                    return (
                        f"{side} turn[{idx}] claim for {pid!r} missing status"
                    )
                seen.add(pid)
            missing = [i for i in required if i not in seen]
            if missing:
                return (
                    f"{side} turn[{idx}] catalyst_status_claims missing "
                    f"premise_ids {missing}"
                )
    return None


def claim_has_independent_http_cite(
    claim: dict,
    tool_retrieved_urls: list[str] | None,
) -> bool:
    """True iff the claim cites ≥1 well-formed http(s) URL that was retrieved.

    Reuses ``is_well_formed_http_url`` and ``urls_match`` — the same ground
    truth as independence derivation / hallucination exemption. A top-level
    turn citation does not count; only ``claim["cited_sources"]``.
    """
    from argosy.agents.base import urls_match
    from argosy.agents.premise_check import is_well_formed_http_url

    tool_list = [
        u for u in (tool_retrieved_urls or [])
        if isinstance(u, str) and u.startswith(("http://", "https://"))
    ]
    if not tool_list:
        return False
    for sid in claim.get("cited_sources") or []:
        if not isinstance(sid, str):
            continue
        if not is_well_formed_http_url(sid):
            continue
        if any(urls_match(sid, u) for u in tool_list):
            return True
    return False


def detect_premise_disagreements(
    premise_status: dict | None,
    *,
    bear_turns: list[dict] | None = None,
    bull_turns: list[dict] | None = None,
) -> list[str]:
    """Detect researcher contradictions keyed by exact ``premise_id``.

    A status conflict is promoted to a structural disagreement ONLY when
    the claim carries ≥1 well-formed http(s) URL that appears in that
    turn's ``tool_retrieved_urls`` (independent retrieval). Uncited or
    unre retrieved claims are **dropped** — not weakened into a
    "do not ignore" signal. Rationale: elevating an unsourced assertion
    is the TRLV disease; a softer channel still risks laundering it into
    the trader prompt. Completeness (``missing_catalyst_status_claims_reason``)
    still accepts id+status so silence ≠ drop; only promotion is gated.
    """
    from argosy.logging import get_logger

    _log = get_logger("argosy.agents.researcher")

    if not premise_status or premise_status.get("status") == "unverified":
        return []
    premises = premise_status.get("premises") or []
    if not premises:
        return []

    by_id: dict[str, dict] = {}
    for prem in premises:
        if not isinstance(prem, dict):
            continue
        pid = (prem.get("premise_id") or "").strip()
        if pid:
            by_id[pid] = prem

    out: list[str] = []
    for side, turns in (("bear", bear_turns or []), ("bull", bull_turns or [])):
        for turn in turns:
            tool_urls = list(turn.get("tool_retrieved_urls") or [])
            claims = turn.get("catalyst_status_claims") or []
            for claim in claims:
                if not isinstance(claim, dict):
                    continue
                pid = (claim.get("premise_id") or "").strip()
                claim_status = (claim.get("status") or "").lower().strip()
                prem = by_id.get(pid)
                if prem is None or not claim_status:
                    continue
                prem_status = (prem.get("status") or "").lower().strip()
                if not prem_status or claim_status == prem_status:
                    continue
                # Drop — do not promote uncited / unre retrieved claims.
                if not claim_has_independent_http_cite(claim, tool_urls):
                    _log.info(
                        "researcher.premise_claim_dropped",
                        premise_id=pid,
                        side=side,
                        claim_status=claim_status,
                        premise_status=prem_status,
                        reason="missing_independent_http_cite",
                        claim_cites=list(claim.get("cited_sources") or [])[:5],
                        tool_retrieved_n=len(tool_urls),
                    )
                    continue
                cat = prem.get("catalyst") or pid
                cites = [
                    c for c in (claim.get("cited_sources") or [])
                    if isinstance(c, str) and c.strip()
                ]
                out.append(
                    f"{side} disagrees with premise_check on "
                    f"{cat!r} (premise_id={pid}): "
                    f"premise_check={prem_status!r}, {side}={claim_status!r}"
                    + (f" (cites {cites[0]})" if cites else "")
                )
    seen: set[str] = set()
    deduped: list[str] = []
    for item in out:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def authoritative_premise_disagreements(
    validated: list[str],
    facilitator_entries: list[str] | None,
) -> list[str]:
    """Return the trader-facing structural disagreement list.

    **Validated entries only.** Facilitator prose never contributes content —
    a matching ``premise_id`` token does not ground arbitrary attached text
    (a grounded topic is not a grounded instruction). Facilitator entries
    may influence **ordering** when they exactly equal a validated string,
    and non-identical facilitator text is logged then discarded.
    """
    from argosy.logging import get_logger

    _log = get_logger("argosy.agents.researcher")

    validated_list = [v for v in (validated or []) if isinstance(v, str) and v.strip()]
    fac_list = [
        e for e in (facilitator_entries or []) if isinstance(e, str) and e.strip()
    ]
    validated_set = set(validated_list)

    # Ordering hint: exact matches in facilitator order first, then any
    # remaining validated entries in detection order.
    ordered: list[str] = []
    seen: set[str] = set()
    for entry in fac_list:
        if entry in validated_set:
            if entry not in seen:
                ordered.append(entry)
                seen.add(entry)
            continue
        _log.info(
            "researcher.facilitator_disagreement_dropped",
            reason="facilitator_content_not_in_validated_set",
            entry=entry[:240],
        )
    for v in validated_list:
        if v not in seen:
            ordered.append(v)
            seen.add(v)
    return ordered


def _render_premise_block(premise_status: dict | None) -> str:
    """Format the premise-check report as CONTESTABLE evidence (not gospel)."""
    if not premise_status:
        return ""
    if premise_status.get("status") == "unverified":
        reason = premise_status.get("reason") or "premise check failed"
        return (
            "=== PREMISE CHECK (UNVERIFIED — contestable; NOT ground truth) ===\n"
            f"Status: unverified. Reason: {reason}\n"
            "The premise checker did not complete. Do NOT treat any catalyst "
            "as confirmed pending or already_happened from this block. The "
            "bear MUST attempt independent primary-source verification.\n"
            "=== END PREMISE CHECK ===\n\n"
        )
    premises = premise_status.get("premises") or []
    summary = (premise_status.get("summary") or "").strip()
    confidence = premise_status.get("confidence") or ""
    top_sources = premise_status.get("cited_sources") or []
    lines: list[str] = [
        "=== PREMISE CHECK (contestable evidence — NOT ground truth) ===",
        "Another fleet agent checked dated/pending catalysts and reports "
        "the following WITH its sources and uncertainty. This is ONE input, "
        "not an authority. Bull and bear may challenge it; the bear MUST "
        "re-derive material catalyst status against primary sources via "
        "WebSearch. Disagreement with this block must survive into the "
        "facilitator transcript — do not suppress it.",
    ]
    if confidence:
        lines.append(f"Premise-check confidence: {confidence}")
    if summary:
        lines.append(f"Summary: {summary}")
    if top_sources:
        lines.append(f"Premise-check sources: {top_sources}")
    if not premises:
        lines.append("(no dated/pending catalysts identified by premise_check)")
    else:
        lines.append(
            "Debaters MUST emit catalyst_status_claims keyed by the exact "
            "premise_id below (one claim per id). Empty/omitted claims are "
            "a failure, not 'no disagreement'."
        )
        for i, p in enumerate(premises, start=1):
            if not isinstance(p, dict):
                continue
            pid = (p.get("premise_id") or "").strip() or f"p{i-1}"
            cat = p.get("catalyst") or "?"
            status = p.get("status") or "unclear"
            as_of = p.get("as_of") or ""
            evidence = p.get("evidence") or ""
            cites = p.get("cited_sources") or []
            lines.append(
                f"  [{i}] premise_id: {pid}\n"
                f"      catalyst: {cat}\n"
                f"      reported_status: {status}"
                + (f" (as_of {as_of})" if as_of else "")
                + (f"\n      evidence: {evidence}" if evidence else "")
                + (f"\n      sources: {cites}" if cites else "")
            )
    lines.append("=== END PREMISE CHECK ===\n")
    return "\n".join(lines) + "\n"


def _analyst_source_id(role: str, ticker: str) -> str:
    role_key = (role or "analyst").strip() or "analyst"
    t = (ticker or "").strip().upper()
    return f"{role_key}/{t}" if t else role_key


class _ResearcherAgent(BaseAgent[ResearcherTurn]):
    """Shared base. Concrete subclasses set `_side` to 'bull' or 'bear'."""

    output_model = ResearcherTurn
    require_citations = True
    # max_tokens driven by DEFAULT_MAX_TOKENS_BY_ROLE (16000 for both
    # bull_researcher and bear_researcher).

    _side: ClassVar[Literal["bull", "bear"]] = "bull"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Populated by build_prompt when premise_check listed premises.
        self._required_premise_ids: list[str] = []

    def _derive_output_independence(
        self,
        output: BaseModel,
        *,
        tool_retrieved_urls: list[str] | None = None,
    ) -> BaseModel:
        if not isinstance(output, ResearcherTurn):
            return output
        return derive_turn_independence(output, tool_retrieved_urls)

    # NOTE: do NOT raise AgentRunError here for missing catalyst_status_claims.
    # DecisionFlow's post-debate gate routes that to premise_unverified +
    # reevaluation. An agent-level raise would abort the run before the gate.

    def build_prompt(
        self,
        *,
        analyst_reports: list[dict],
        prior_rounds: list[dict] | None = None,
        round_index: int = 1,
        n_max: int = 2,
        ticker: str = "",
        user_directive: str = "",
        premise_status: dict | None = None,
    ) -> tuple[str, str] | tuple[str, str, list[tuple[str, str]]]:
        """Build the prompt for one debate turn."""
        prior_rounds = prior_rounds or []
        opposite = "bear" if self._side == "bull" else "bull"
        self._required_premise_ids = premise_ids_from_status(premise_status)

        if self._side == "bear":
            independence_rules = (
                "  - INDEPENDENT RETRIEVAL (mandatory for the bear): you have "
                "the WebSearch tool. You MUST run 1-3 targeted searches for "
                "PRIMARY sources on this ticker — SEC filings, company IR / "
                "press releases, regulator publications — and ground at least "
                "one point on what you retrieve. Do NOT merely restate the "
                "shared analyst payload; when the payload is wrong, restating "
                "it makes both sides wrong in the same direction.\n"
                "  - Cite live URLs for independent findings. Independence is "
                "DERIVED by the system from whether those URLs appear in your "
                "actual WebSearch tool results — self-labelling a point "
                "`independent` without a real retrieval does nothing.\n"
                "  - PREMISE CHECK CHALLENGE (mandatory): if a PREMISE CHECK "
                "block lists premises, you MUST emit `catalyst_status_claims` "
                "with one entry per `premise_id` (exact id from the block — "
                "not a restated label). Confirm or contradict each status. "
                "Empty/omitted claims are a parse failure. Unknown premise_ids "
                "are a parse failure. Key by premise_id only.\n"
            )
        else:
            independence_rules = (
                "  - Cite analyst reports and specific facts. Live primary-"
                "source URLs are allowed when grounded; independence is "
                "derived from tool retrieval, not from self-labelling.\n"
                "  - A PREMISE CHECK block (when present) is contestable "
                "evidence from another agent — not ground truth. When the "
                "block lists premises, you MUST emit `catalyst_status_claims` "
                "with one entry per exact `premise_id` (confirm or disagree). "
                "Empty/omitted claims are a parse failure.\n"
            )

        system = (
            f"You are the {self._side} researcher on the Argosy fleet. "
            f"You marshal the strongest possible {self._side}ish case from the "
            "evidence in the analyst reports"
            + (
                " AND from primary sources you retrieve yourself"
                if self._side == "bear"
                else ""
            )
            + ". The other side argues the opposite case.\n\n"
            "Rules:\n"
            "  - Cite specific facts. Do NOT invent facts.\n"
            "  - Address the strongest opposing point from the prior round; "
            "if this is round 1, leave `response_to_opposing` empty.\n"
            "  - Length: 200-400 words across the points.\n"
            "  - Each point must carry evidence and at least one citation.\n"
            + independence_rules
            + "\nOUTPUT must be a JSON object conforming to this schema:\n"
            f"{ResearcherTurn.model_json_schema()}\n"
        )

        if user_directive:
            system = system + (
                "\nUSER DIRECTIVE PRESENT: a USER DIRECTIVE block appears in the "
                "user message below capturing the human's per-objection stances "
                f"from the prior round. As the {self._side} researcher:\n"
                f"  - If the user's stance favors your {self._side} side on "
                "this horizon (AGREED with your earlier position, or DISAGREED "
                "with the opposite side), lean into it and reinforce the case.\n"
                "  - If the user's stance opposes your side, you may still "
                "argue your case but acknowledge the user's position "
                "explicitly so the facilitator + synthesizer downstream see "
                "where the disagreement lies.\n"
                "  - For DEFERRED stances, argue your case normally.\n"
                "  - Do NOT re-litigate points the user has resolved against "
                "your side without acknowledging that resolution.\n"
            )

        report_blocks: list[str] = []
        sources: list[tuple[str, str]] = []
        for r in analyst_reports:
            role = r.get("agent_role") or r.get("role") or "?"
            payload = {k: v for k, v in r.items() if k not in ("agent_role", "role")}
            report_blocks.append(f"### Analyst: {role}\n{payload}")
            if self._side == "bear":
                sid = _analyst_source_id(str(role), ticker)
                sources.append((sid, str(payload)))

        prior_block = ""
        if prior_rounds:
            chunks: list[str] = []
            for i, t in enumerate(prior_rounds, start=1):
                side = t.get("side", "?")
                summary = t.get("position_summary", "")
                points = t.get("points", [])
                chunks.append(
                    f"--- prior turn #{i} ({side}) ---\n"
                    f"summary: {summary}\n"
                    f"points: {points}"
                )
            prior_block = (
                "\n\nPRIOR DEBATE ROUNDS (most recent last):\n"
                + "\n".join(chunks)
                + f"\n\nThe LAST {opposite} turn is the one you must respond to."
            )

        directive_prefix = ""
        if user_directive:
            directive_prefix = (
                "=== USER DIRECTIVE (authoritative human input on this run) ===\n"
                + user_directive
                + "\n\n"
            )

        premise_prefix = _render_premise_block(premise_status)

        user = (
            f"{directive_prefix}"
            f"{premise_prefix}"
            f"Ticker under debate: {ticker or '(unspecified)'}\n"
            f"Round {round_index} of {n_max}; you argue the {self._side} case.\n\n"
            "ANALYST REPORTS (shared debate payload"
            + (
                " — verify material figures against primary sources via WebSearch"
                if self._side == "bear"
                else ""
            )
            + "):\n\n"
            + "\n\n".join(report_blocks)
            + prior_block
            + "\n\nProduce the ResearcherTurn JSON now. Set `side` to "
            f"{self._side!r} and `round_index` to {round_index}."
        )
        if self._side == "bear":
            return system, user, sources
        return system, user


class BullResearcherAgent(_ResearcherAgent):
    """Bull-side researcher. Default Opus."""

    agent_role = "bull_researcher"
    _side = "bull"


class BearResearcherAgent(_ResearcherAgent):
    """Bear-side researcher. Default Opus.

    Carries WebSearch so the bear can gather primary-source evidence
    independently of the shared analyst payload (TRLV scar fix).
    """

    agent_role = "bear_researcher"
    _side = "bear"
    claude_code_allowed_tools: ClassVar[tuple[str, ...]] = ("WebSearch",)

    def _finalize_sources_json(
        self,
        sources_json: str | None,
        output: BaseModel,
        *,
        tool_retrieved_urls: list[str] | None = None,
    ) -> str | None:
        """Merge tool-retrieved URLs that the bear actually cited.

        Only URLs present in the tool-use record AND cited on an
        independently-derived point are appended — fabricated URLs never
        enter sources_json.
        """
        tool_set_raw = [
            u for u in (tool_retrieved_urls or [])
            if isinstance(u, str) and u.startswith(("http://", "https://"))
        ]
        from argosy.agents.base import urls_match

        entries: list[dict[str, Any]] = []
        if sources_json:
            try:
                parsed = json.loads(sources_json)
                if isinstance(parsed, list):
                    entries = [e for e in parsed if isinstance(e, dict)]
            except (TypeError, ValueError, json.JSONDecodeError):
                entries = []
        known = {
            str(e.get("source_id"))
            for e in entries
            if e.get("source_id") is not None
        }

        points = getattr(output, "points", None) or []
        for point in points:
            if getattr(point, "independence", "shared_payload") != "independent":
                continue
            for sid in getattr(point, "cited_sources", None) or []:
                if not isinstance(sid, str):
                    continue
                matched = next(
                    (u for u in tool_set_raw if urls_match(sid, u)), None
                )
                if matched is None:
                    continue
                if sid in known or matched in known:
                    continue
                entries.append(
                    {
                        "source_id": matched,
                        "content": (
                            "(independent primary-source citation observed "
                            "in bear_researcher successful WebSearch tool results)"
                        ),
                    }
                )
                known.add(matched)
                known.add(sid)

        if not entries:
            return sources_json
        return json.dumps(entries, ensure_ascii=False)


__all__ = [
    "BearResearcherAgent",
    "BullResearcherAgent",
    "CatalystStatusClaim",
    "CitedPoint",
    "ResearcherTurn",
    "authoritative_premise_disagreements",
    "claim_has_independent_http_cite",
    "detect_premise_disagreements",
    "derive_point_independence",
    "derive_turn_independence",
    "missing_catalyst_status_claims_reason",
    "premise_ids_from_status",
]
