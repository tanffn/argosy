"""Section-level freeze/merge for medium plan amendments.

The doctrine ("amend, never full-regenerate") claims a medium amendment
"freezes untouched sections". Before this module existed that was false:
``_medium_worker`` re-invoked the synthesizer for the whole horizon body
every time, so guidance naming two sections could (and did, measured on
plan 106 -> 107) rewrite every section, rename headings, and drop
sections outright (``cover_assumptions``, ``fi_bridge``, ``monte_carlo``
vanished from the long horizon in one real run).

This module makes the doctrine true: given the PRIOR horizon markdown and
the freshly re-synthesized NEW horizon markdown, ``merge_frozen_sections``
restores every section not explicitly named in ``allow`` to its prior
text, verbatim, and never lets a requested section disappear.

Sections are matched by SLUG, not heading text, because headings get
renamed across a re-synth run (`` Withdrawal & Spend Basis`` ->
``Withdrawal Strategy``) while the backticked slug embedded in the
heading (`` `withdrawal` ``) stays stable. Headings with no backticked
slug (``## Targets``, ``## Themes``, ``# Long horizon``) fall back to a
normalized-heading key so they can still be matched and frozen.

The document is split into sections at EVERY heading line, regardless of
level (H1..H6). This is deliberate: it means a parent heading with no
slug (e.g. ``## Appendix -- Section-by-section evidence``) and its
slugged child headings (e.g. ``### Tax Plan -- `tax_plan` (medium)``)
are independent, separately-matchable sections — which is exactly the
granularity the real regression needs (the appendix parent is unslugged
prose; the child sections are the individually-named, individually
freezable evidence blocks).
"""

from __future__ import annotations

import re

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$", re.MULTILINE)
_SLUG_RE = re.compile(r"`([A-Za-z0-9_\-]+)`")

# Sentinel key for the preamble (any text before the first heading).
PREAMBLE_KEY = "__preamble__"


def _normalize_heading(text: str) -> str:
    """Fallback key for headings with no backticked slug.

    Lowercase, collapse anything non-alphanumeric to a single
    underscore, strip leading/trailing underscores. Distinct enough for
    real headings (``## Targets`` -> ``targets``) while being immune to
    punctuation-only rewrites.
    """
    key = text.strip().lower()
    key = re.sub(r"[^a-z0-9]+", "_", key)
    return key.strip("_")


def _section_key(heading_text: str) -> str:
    m = _SLUG_RE.search(heading_text)
    if m:
        return m.group(1)
    return _normalize_heading(heading_text)


def _split_sections(md: str) -> tuple[str, list[tuple[str, str]]]:
    """Split ``md`` into ``(preamble, [(key, section_text), ...])``.

    ``section_text`` includes its own heading line through to (but not
    including) the next heading line, in document order.
    """
    if not md:
        return "", []

    matches = list(_HEADING_RE.finditer(md))
    if not matches:
        return md, []

    preamble = md[: matches[0].start()]
    sections: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        text = md[start:end]
        key = _section_key(m.group(2))
        sections.append((key, text))
    return preamble, sections


def merge_frozen_sections(
    prior_md: str, new_md: str, *, allow: set[str],
) -> tuple[str, list[str]]:
    """Merge ``new_md`` into ``prior_md``, freezing everything not in ``allow``.

    Returns ``(merged_markdown, notes)`` where ``notes`` is a list of
    human-readable strings describing what was frozen / restored /
    dropped, suitable for ``log.info``.

    Rules (iterate in PRIOR order, preserving it):
      - key in ``allow`` -> take the NEW section; if the model dropped
        it entirely, fall back to the PRIOR section (never lose a
        requested section either).
      - key NOT in ``allow`` -> take the PRIOR section verbatim (the
        freeze).
      - key present in NEW but absent in PRIOR and not in ``allow`` ->
        DROP it (record a note) — an unrequested new section is scope
        creep.
      - key present in NEW but absent in PRIOR and IS in ``allow`` ->
        keep it, appended after the prior-ordered sections (a
        requested new addition).
      - preamble (text before the first heading) is preserved from
        PRIOR unless ``PREAMBLE_KEY`` is in ``allow``.
    """
    notes: list[str] = []
    prior_preamble, prior_sections = _split_sections(prior_md)
    new_preamble, new_sections = _split_sections(new_md)

    # First-occurrence wins if a key repeats (shouldn't happen in
    # practice; defensive rather than silently dropping data).
    new_by_key: dict[str, str] = {}
    for key, text in new_sections:
        new_by_key.setdefault(key, text)

    out_parts: list[str] = []

    if PREAMBLE_KEY in allow:
        out_parts.append(new_preamble)
        if new_preamble != prior_preamble:
            notes.append(f"{PREAMBLE_KEY}: replaced with new text (allowed)")
    else:
        out_parts.append(prior_preamble)
        if new_preamble and new_preamble != prior_preamble:
            notes.append(f"{PREAMBLE_KEY}: frozen (new text discarded)")

    prior_keys = {key for key, _ in prior_sections}

    for key, prior_text in prior_sections:
        if key in allow:
            new_text = new_by_key.get(key)
            if new_text is not None:
                out_parts.append(new_text)
                if new_text != prior_text:
                    notes.append(f"{key}: updated (allowed)")
            else:
                out_parts.append(prior_text)
                notes.append(f"{key}: allowed but dropped by model — restored prior")
        else:
            out_parts.append(prior_text)
            new_text = new_by_key.get(key)
            if new_text is None:
                notes.append(f"{key}: frozen — restored (absent from new)")
            elif new_text != prior_text:
                notes.append(f"{key}: frozen (new text discarded)")

    # Sections that only exist in NEW.
    seen_appended: set[str] = set()
    for key, new_text in new_sections:
        if key in prior_keys or key in seen_appended:
            continue
        seen_appended.add(key)
        if key in allow:
            if out_parts and out_parts[-1] and not out_parts[-1].endswith("\n\n"):
                out_parts.append("\n" if out_parts[-1].endswith("\n") else "\n\n")
            out_parts.append(new_text)
            notes.append(f"{key}: new section added (allowed)")
        else:
            notes.append(f"{key}: unrequested new section dropped")

    merged = "".join(out_parts)
    return merged, notes


__all__ = ["merge_frozen_sections", "PREAMBLE_KEY"]
