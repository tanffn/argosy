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

Item I (2026-07-12): when the fact-placeholder protocol is ON, persisted plan
bodies INTENTIONALLY carry ``{{fact:key}}`` tokens (READ-time rendering fills
them for the client). ``{{fact:`` is therefore NOT a leak in stored bytes —
only ``EMIT AS`` and a leftover ``[derivation pending]`` remain leak classes
for the stored form. Client-facing assembled text is still scanned for all
three when placeholders are OFF (legacy bake-digits path).
"""
from __future__ import annotations

import os

# The three known leak classes. Each is a literal substring (not a regex) — a leak is
# unambiguous and we want zero false positives on ordinary prose.
#   "[derivation pending]" — a placeholder whose value was unresolved at render time.
#   "EMIT AS"              — the synthesizer copied the placeholder-emission INSTRUCTION
#                            into the body instead of emitting the token.
#   "{{fact:"             — an unrendered fact placeholder (substitution never ran)
#                            — ONLY a leak when the placeholder protocol is OFF
#                            (legacy path expected digits baked into the body).
LEAKAGE_PATTERNS: tuple[str, ...] = ("[derivation pending]", "EMIT AS", "{{fact:")
_ALWAYS_LEAK: tuple[str, ...] = ("[derivation pending]", "EMIT AS")


def _placeholders_on() -> bool:
    env = os.environ.get("ARGOSY_FACT_PLACEHOLDERS")
    if env is not None:
        return str(env).strip().lower() in {"1", "true", "yes", "on"}
    try:
        from argosy.config import get_settings
        return bool(get_settings().fact_placeholders)
    except Exception:  # noqa: BLE001
        return True


def scan_leakage(
    text: str, *, allow_fact_tokens: bool | None = None,
) -> list[str]:
    """Return one human-readable entry per DISTINCT leak pattern present in ``text``
    (with its occurrence count), or [] when the text is leak-clean. De-duplicated by
    pattern so the report stays compact even when a token recurs dozens of times.

    ``allow_fact_tokens``: when True (or when the placeholder protocol is ON and
    the arg is omitted), ``{{fact:`` is not treated as a leak — tokens are the
    intentional stored form under READ-time rendering.
    """
    body = text or ""
    allow = _placeholders_on() if allow_fact_tokens is None else allow_fact_tokens
    patterns = _ALWAYS_LEAK if allow else LEAKAGE_PATTERNS
    out: list[str] = []
    for pat in patterns:
        n = body.count(pat)
        if n:
            out.append(f"{pat!r} x{n}")
    return out


def is_leak_clean(text: str, *, allow_fact_tokens: bool | None = None) -> bool:
    """True when the artifact contains NONE of the known leak tokens."""
    return not scan_leakage(text, allow_fact_tokens=allow_fact_tokens)


__all__ = ["LEAKAGE_PATTERNS", "scan_leakage", "is_leak_clean"]
