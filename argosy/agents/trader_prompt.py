"""Single choke point for agent-authored text entering the trader prompt.

Round-10 class fix: every prior round patched one injection channel; the next
review found an adjacent raw-text path. All agent-authored strings that reach
the trader MUST pass through ``escape_agent_text`` + ``assemble_trader_user_prompt``.
Structural markers ("do not ignore" headers, premise-check fences) are
emittable ONLY by constants in this module — never reproducible from agent
content (neutralised on escape).

Doctrine line: this is mechanical provenance ("is this text agent-authored?"),
not judgment of reasoning quality.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Authoritative markers — ONLY our assembler may emit these verbatim.
# ---------------------------------------------------------------------------

STRUCTURAL_DISAGREEMENT_HEADER = (
    "PREMISE DISAGREEMENTS (structural — do not ignore):"
)
PREMISE_BLOCK_OPEN = (
    "=== PREMISE CHECK (contestable evidence — NOT ground truth) ==="
)
PREMISE_BLOCK_OPEN_UNVERIFIED = (
    "=== PREMISE CHECK (UNVERIFIED — contestable; NOT ground truth) ==="
)
PREMISE_BLOCK_CLOSE = "=== END PREMISE CHECK ==="

#: Substrings that identify an authoritative block. Any agent-authored text
#: containing these is neutralised so a crafted catalyst cannot mint a
#: counterfeit structural header.
_AUTHORITATIVE_MARKER_FRAGMENTS: tuple[str, ...] = (
    STRUCTURAL_DISAGREEMENT_HEADER,
    "PREMISE DISAGREEMENTS (structural",
    "(structural — do not ignore)",
    "(structural - do not ignore)",
    "=== PREMISE CHECK",
    "=== END PREMISE CHECK ===",
)

_CONTROL_OR_INVISIBLE_RE = re.compile(
    "["
    "\u0000-\u001f\u007f"  # ASCII controls + DEL
    "\u00ad"              # soft hyphen
    "\u034f"              # combining grapheme joiner
    "\u061c"              # Arabic letter mark
    "\u180e"              # Mongolian vowel separator
    "\u200b-\u200f"       # ZWSP / ZWNJ / ZWJ / LTR/RTL marks
    "\u202a-\u202e"       # bidi embeddings / overrides
    "\u2060"              # word joiner
    "\u2066-\u2069"       # bidi isolates
    "\ufeff"              # BOM
    "\ufff9-\ufffb"       # interlinear annotation
    "]"
)
_PCT_ENCODED_CONTROL_RE = re.compile(r"%(?:0[0-9A-Fa-f]|1[0-9A-Fa-f]|7[Ff])")
_NEUTRALIZED = "[neutralized-structural-marker]"


def escape_agent_text(value: Any) -> str:
    """Escape one agent-authored value for inclusion in the trader prompt.

    - Coerce to str
    - Strip ASCII controls, zero-width, and bidi-override characters
    - Reject percent-encoded controls by replacing the sequence visibly
    - Neutralise any authoritative-marker substring so agent content can
      never mint a structural header
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        text = str(value)
    else:
        text = value
    text = _CONTROL_OR_INVISIBLE_RE.sub("", text)
    text = _PCT_ENCODED_CONTROL_RE.sub(
        lambda m: f"[pct-{m.group(0)[1:].lower()}]", text,
    )
    # Neutralise markers case-sensitively first (exact), then softer fragments.
    for frag in _AUTHORITATIVE_MARKER_FRAGMENTS:
        if frag in text:
            text = text.replace(frag, _NEUTRALIZED)
    # Soft match on "do not ignore" structural phrasing variants.
    text = re.sub(
        r"PREMISE\s+DISAGREEMENTS[^\n]{0,80}do not ignore",
        _NEUTRALIZED,
        text,
        flags=re.IGNORECASE,
    )
    return text


def fence_agent_field(field_path: str, value: Any) -> str:
    """Wrap escaped agent text in a labeled fence (provenance, not authority)."""
    safe_path = escape_agent_text(field_path).replace("]", "")
    body = escape_agent_text(value)
    return f"[agent:{safe_path}]\n{body}\n[/agent:{safe_path}]"


def render_premise_status_for_trader(premise_status: dict | None) -> str:
    """Render the premise-check block with all agent fields escaped.

    Headers/fences come from module constants only.
    """
    if not premise_status:
        return ""
    if premise_status.get("status") == "unverified":
        reason = escape_agent_text(
            premise_status.get("reason") or "premise check failed"
        )
        return (
            f"{PREMISE_BLOCK_OPEN_UNVERIFIED}\n"
            f"Status: unverified. Reason: {reason}\n"
            "The premise checker did not complete. Do NOT treat any catalyst "
            "as confirmed pending or already_happened from this block. The "
            "bear MUST attempt independent primary-source verification.\n"
            f"{PREMISE_BLOCK_CLOSE}\n\n"
        )

    lines: list[str] = [
        PREMISE_BLOCK_OPEN,
        "Another fleet agent checked dated/pending catalysts and reports "
        "the following WITH its sources and uncertainty. This is ONE input, "
        "not an authority. Bull and bear may challenge it; the bear MUST "
        "re-derive material catalyst status against primary sources via "
        "WebSearch. Disagreement with this block must survive into the "
        "facilitator transcript — do not suppress it.",
    ]
    confidence = premise_status.get("confidence") or ""
    if confidence:
        lines.append(
            f"Premise-check confidence: {escape_agent_text(confidence)}"
        )
    summary = premise_status.get("summary") or ""
    if summary:
        lines.append(fence_agent_field("premise.summary", summary))
    top_sources = premise_status.get("cited_sources") or []
    if top_sources:
        escaped_srcs = [
            escape_agent_text(s) for s in top_sources if isinstance(s, str)
        ]
        lines.append(
            fence_agent_field("premise.cited_sources", escaped_srcs)
        )
    premises = premise_status.get("premises") or []
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
            pid = escape_agent_text(
                (p.get("premise_id") or "").strip() or f"p{i-1}"
            )
            cat = escape_agent_text(p.get("catalyst") or "?")
            status = escape_agent_text(p.get("status") or "unclear")
            as_of = escape_agent_text(p.get("as_of") or "")
            evidence = escape_agent_text(p.get("evidence") or "")
            cites = [
                escape_agent_text(c)
                for c in (p.get("cited_sources") or [])
                if isinstance(c, str)
            ]
            block = (
                f"  [{i}] premise_id: {pid}\n"
                f"      catalyst: {cat}\n"
                f"      reported_status: {status}"
                + (f" (as_of {as_of})" if as_of else "")
                + (f"\n      evidence: {evidence}" if evidence else "")
                + (f"\n      sources: {cites}" if cites else "")
            )
            lines.append(block)
    lines.append(PREMISE_BLOCK_CLOSE)
    return "\n".join(lines) + "\n\n"


def render_disagreement_block(disagreements: Iterable[str] | None) -> str:
    """Emit the structural disagreement header (our code) + escaped entries.

    Entries should already be code-composed from typed fields; escaping is
    defense-in-depth so a future regression cannot reopen a free-text channel.
    """
    items = [
        escape_agent_text(d)
        for d in (disagreements or [])
        if isinstance(d, str) and d.strip()
    ]
    if not items:
        return ""
    return (
        f"{STRUCTURAL_DISAGREEMENT_HEADER}\n"
        + "\n".join(f"  - {d}" for d in items)
        + "\n\n"
    )


def _escape_tree(value: Any, *, path: str = "root") -> Any:
    """Recursively escape all strings in a JSON-ish tree (debate dump, etc.)."""
    if isinstance(value, str):
        return escape_agent_text(value)
    if isinstance(value, dict):
        return {
            escape_agent_text(str(k)) if isinstance(k, str) else k: _escape_tree(
                v, path=f"{path}.{k}"
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_escape_tree(v, path=f"{path}[]") for v in value]
    if isinstance(value, BaseModel):
        return _escape_tree(value.model_dump(), path=path)
    return value


def assemble_trader_user_prompt(
    *,
    tier: str,
    ticker: str,
    premise_status: dict | None,
    disagreements: list[str] | None,
    user_constraints: Any,
    positions_snapshot: Any,
    analyst_reports: list[dict],
    debate_outcome: Any,
) -> str:
    """THE single assembler for the trader user message.

    All agent-authored text is escaped here. Adding a new field to the trader
    prompt without going through this function is a bug; the invariant test
    enumerates model string fields to catch new channels.
    """
    premise_block = render_premise_status_for_trader(premise_status)
    disagreement_block = render_disagreement_block(disagreements)

    report_blocks: list[str] = []
    for r in analyst_reports or []:
        if not isinstance(r, dict):
            continue
        role = escape_agent_text(r.get("agent_role") or r.get("role") or "?")
        payload = {
            k: v for k, v in r.items() if k not in ("agent_role", "role")
        }
        escaped_payload = _escape_tree(payload, path=f"analyst.{role}")
        report_blocks.append(f"### Analyst: {role}\n{escaped_payload}")

    debate_escaped = _escape_tree(debate_outcome, path="debate_outcome")
    # Disagreements are already rendered in the authoritative block; strip
    # raw copies from the debate dump so a duplicate free-text list cannot
    # bypass the header-only emission rule.
    if isinstance(debate_escaped, dict):
        debate_escaped = {
            k: v for k, v in debate_escaped.items()
            if k != "premise_disagreements"
        }
        # premise_status inside debate is also rendered above — drop raw copy.
        debate_escaped.pop("premise_status", None)

    return (
        f"Tier: {escape_agent_text(tier)}\n"
        f"Ticker: {escape_agent_text(ticker) or '(infer from analyst reports if unambiguous)'}\n\n"
        f"{premise_block}"
        f"{disagreement_block}"
        "USER CONSTRAINTS:\n"
        f"{fence_agent_field('user_constraints', user_constraints)}\n\n"
        "POSITIONS SNAPSHOT:\n"
        f"{fence_agent_field('positions_snapshot', positions_snapshot)}\n\n"
        "ANALYST REPORTS:\n\n"
        + ("\n\n".join(report_blocks) if report_blocks else "(none)")
        + "\n\nDEBATE OUTCOME:\n"
        f"{debate_escaped}\n\n"
        "Produce the TraderProposal JSON now."
    )


def agent_authored_string_fields(*models: type[BaseModel]) -> list[tuple[str, str]]:
    """Enumerate ``(ModelName.field, annotation_tag)`` for str / list[str] fields.

    Used by the invariant test so newly added agent string fields automatically
    enter the injection surface under test.
    """
    import typing
    from typing import get_args, get_origin

    out: list[tuple[str, str]] = []
    for model in models:
        for name, field in model.model_fields.items():
            ann = field.annotation
            origin = get_origin(ann)
            args = get_args(ann)
            tag = str(ann)

            is_str = ann is str or ann == "str"
            is_opt_str = False
            is_list_str = False
            if origin is list or origin is typing.List:
                if args and (args[0] is str or args[0] == "str"):
                    is_list_str = True
            # Optional[str] / str | None
            if origin is typing.Union or str(origin) == "typing.Union":
                if str in args or "str" in args:
                    # exclude non-str unions that only happen to mention str
                    non_none = [a for a in args if a is not type(None)]
                    if len(non_none) == 1 and (
                        non_none[0] is str or non_none[0] == "str"
                    ):
                        is_opt_str = True
            # PEP604 str | None stored as types.UnionType
            if origin is None and hasattr(ann, "__args__"):
                uargs = getattr(ann, "__args__", ())
                non_none = [a for a in uargs if a is not type(None)]
                if len(non_none) == 1 and non_none[0] is str:
                    is_opt_str = True
            # Literal types are constrained vocabularies — still agent-authored
            # text that reaches prompts; include when all members are str.
            if origin is typing.Literal or str(get_origin(ann)) == "typing.Literal":
                if args and all(isinstance(a, str) for a in args):
                    is_str = True

            if is_str or is_opt_str or is_list_str:
                out.append((f"{model.__name__}.{name}", tag))
            elif "str" in tag.lower() and "literal" not in tag.lower():
                # Fallback for unresolved forward refs / unusual wrappers.
                if "list" in tag.lower() or tag.strip() in (
                    "str", "<class 'str'>", "typing.Optional[str]", "str | None",
                ):
                    out.append((f"{model.__name__}.{name}", tag))
    return out


__all__ = [
    "PREMISE_BLOCK_CLOSE",
    "PREMISE_BLOCK_OPEN",
    "PREMISE_BLOCK_OPEN_UNVERIFIED",
    "STRUCTURAL_DISAGREEMENT_HEADER",
    "agent_authored_string_fields",
    "assemble_trader_user_prompt",
    "escape_agent_text",
    "fence_agent_field",
    "render_disagreement_block",
    "render_premise_status_for_trader",
]
