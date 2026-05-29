"""Static mapping helpers for agent roles + source IDs to user-friendly labels.

Closes Codex-zigzag-style "config-key leak" gaps where the UI was
rendering internal agent names like ``(fundamentals_analyst)`` and raw
source IDs like ``indicators/NVDA`` or ``fundamentals:NVDA:2026-05-29``
verbatim to the user.

Not LLM-backed -- the universe of agent roles and source-id namespaces
is small and stable, so a dict is faster + more predictable than a
translator agent. The complementary LLM-translation layer (see
``delta_item_translation_cache.py``) handles the open-ended cases
(plan-change ``item_id`` fields like ``long.targets.us_situs_taxable_assets_cap``).

Add new entries here when a new agent role lands or a new source-id
namespace ships. If you find yourself wanting a long-form description
rather than a short label, switch that surface to the LLM-translation
path instead.
"""
from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Agent role -> user-friendly label
# ---------------------------------------------------------------------------

_AGENT_ROLE_LABELS: dict[str, str] = {
    # Single-ticker analysts (Phase 1)
    "fundamentals_analyst": "fundamentals",
    "sentiment_analyst": "sentiment",
    "technical_analyst": "technical",
    "news_analyst": "news",
    "concentration_analyst": "concentration",
    "fx_analyst": "FX",
    "tax_analyst": "tax",
    "macro_analyst": "macro",
    "household_budget_analyst": "household budget",
    # Phase 2 debate roles
    "bull_researcher": "bull case",
    "bear_researcher": "bear case",
    "researcher_facilitator": "research synthesis",
    # Phase 3 / 4 synthesis + risk
    "plan_synthesizer": "plan synthesizer",
    "plan_critique": "plan critique",
    "risk_officer": "risk review",
    "risk_facilitator": "risk synthesis",
    "fund_manager": "fund manager",
    "fund_manager_dialogue_verdict": "fund manager verdict",
    # Other roles
    "audit": "audit",
    "trader": "trader",
    "advisor": "advisor",
    "analyst_responder": "analyst",
    "intake_extractor": "intake",
    "objection_translator": "translator",
    "daily_briefer": "daily brief",
    "household_categorizer": "category resolver",
    "watchlist": "watchlist",
    "fleet_self_review": "fleet review",
    "anomaly_detector": "anomaly detector",
}


def friendly_agent_role(role: str | None) -> str:
    """Map an internal agent role to a user-friendly label.

    Unknown roles fall back to the role string with ``_analyst`` /
    ``_researcher`` suffixes stripped + underscores replaced with spaces.
    """
    if not role:
        return "analyst"
    label = _AGENT_ROLE_LABELS.get(role)
    if label is not None:
        return label
    cleaned = role
    for suffix in ("_analyst", "_researcher", "_officer"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    return cleaned.replace("_", " ")


# ---------------------------------------------------------------------------
# Source ID -> user-friendly label
# ---------------------------------------------------------------------------

# Known prefix patterns. Order matters -- more specific first.
_SOURCE_PREFIX_LABELS: list[tuple[str, str]] = [
    ("indicators/", "{rest} technical indicators"),
    ("fundamentals:", "{rest} fundamentals"),
    ("fundamentals/", "{rest} fundamentals"),
    ("news/", "{rest} news"),
    ("sentiment/", "{rest} sentiment"),
    ("agent_report:", "agent report #{rest}"),
    ("fx/", "FX {rest}"),
    ("rates/", "FX rate {rest}"),
    ("macro/", "macro {rest}"),
    ("tax/", "tax rules {rest}"),
    ("policy/", "policy {rest}"),
    ("doc:", "doc {rest}"),
    ("plan_critique:", "plan critique {rest}"),
    ("synth:", "synthesis {rest}"),
]


# Dated patterns the real producers emit. Codex zigzag (c) impl
# review #B1 (2026-05-29): the colon-only regex from the v1 implementation
# matched no real source IDs (real producers emit slash form). Both forms
# accepted defensively.
_DATED_COLON_RE = re.compile(r"^([a-z_]+):([A-Z]{1,8}):(\d{4}-\d{2}-\d{2})$")
_DATED_SLASH_RE = re.compile(r"^([a-z_]+)/([A-Z]{1,8})/(\d{4}-\d{2}-\d{2})$")

_DATED_KIND_LABEL: dict[str, str] = {
    "fundamentals": "fundamentals",
    "news": "news",
    "sentiment": "sentiment",
    "technical": "technical",
    "indicators": "technical indicators",
    "options": "options data",
    "social": "social signals",
}


def friendly_source_label(source_id: str) -> str:
    """Map a raw source_id to a short, human-readable label.

    Returns the source_id verbatim if no pattern matches -- callers that
    need a hard guarantee of "no leak" can compare against the input and
    omit unmapped sources from the rendered list.

    Examples:
      indicators/NVDA              -> "NVDA technical indicators"
      fundamentals/NVDA/2026-05-29 -> "NVDA fundamentals (2026-05-29)"
      fundamentals:NVDA:2026-05-29 -> "NVDA fundamentals (2026-05-29)"
      fx/USD/NIS                   -> "FX USD/NIS"
      agent_report:12345           -> "agent report #12345"
    """
    if not source_id:
        return ""
    # Dated patterns -- accept colon-separated (legacy) + slash-separated
    # (real producer shape). Both forms produce the same output.
    for dated_re in (_DATED_SLASH_RE, _DATED_COLON_RE):
        m = dated_re.match(source_id)
        if m:
            prefix, ticker, dt = m.group(1), m.group(2), m.group(3)
            kind = _DATED_KIND_LABEL.get(prefix, prefix)
            return f"{ticker} {kind} ({dt})"
    for prefix, template in _SOURCE_PREFIX_LABELS:
        if source_id.startswith(prefix):
            rest = source_id[len(prefix):]
            rest = rest.replace("/", " ").replace(":", " ")
            return template.format(rest=rest)
    # Unrecognized -- return verbatim. Caller decides whether to render.
    return source_id


def friendly_source_labels(source_ids: list[str], *, max_count: int = 6) -> list[str]:
    """Apply ``friendly_source_label`` over a list, deduped + capped."""
    seen: set[str] = set()
    out: list[str] = []
    for sid in source_ids:
        label = friendly_source_label(sid)
        if label and label not in seen:
            seen.add(label)
            out.append(label)
            if len(out) >= max_count:
                break
    return out


__all__ = [
    "friendly_agent_role",
    "friendly_source_label",
    "friendly_source_labels",
]
