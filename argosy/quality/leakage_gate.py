"""Deterministic leakage gate for the assembled plan artifact.

The whole-artifact reader is an LLM COHERENCE critic — it hunts contradictions and
fragile claims, not unrendered template tokens. In practice a draft riddled with
``[derivation pending]`` / leaked ``EMIT AS`` placeholder-emission scaffolding /
unrendered ``{{fact:KEY}}`` tokens has read as APPROVE_WITH_CONDITIONS, i.e. a leaky
artifact falsely promotable. This gate is the engineered, deterministic, fail-closed
backstop: if the bytes the client will read contain any leak token, promotion BLOCKS,
no LLM judgement involved.

Pure (no DB, no LLM). Used as (a) a precheck in the whole-artifact reader (BLOCK before
the codex call) and (b) a first-class promotion authority in the /accept publish gate.
"""
from __future__ import annotations

# The three known leak classes. Each is a literal substring (not a regex) — a leak is
# unambiguous and we want zero false positives on ordinary prose.
#   "[derivation pending]" — a placeholder whose value was unresolved at render time.
#   "EMIT AS"              — the synthesizer copied the placeholder-emission INSTRUCTION
#                            into the body instead of emitting the token.
#   "{{fact:"             — an unrendered fact placeholder (substitution never ran).
LEAKAGE_PATTERNS: tuple[str, ...] = ("[derivation pending]", "EMIT AS", "{{fact:")


def scan_leakage(text: str) -> list[str]:
    """Return one human-readable entry per DISTINCT leak pattern present in ``text``
    (with its occurrence count), or [] when the text is leak-clean. De-duplicated by
    pattern so the report stays compact even when a token recurs dozens of times."""
    body = text or ""
    out: list[str] = []
    for pat in LEAKAGE_PATTERNS:
        n = body.count(pat)
        if n:
            out.append(f"{pat!r} x{n}")
    return out


def is_leak_clean(text: str) -> bool:
    """True when the artifact contains NONE of the known leak tokens."""
    return not scan_leakage(text)


__all__ = ["LEAKAGE_PATTERNS", "scan_leakage", "is_leak_clean"]
