"""Cheap single-fact prose corrector for ``llm_prose`` render sites.

A deterministic re-render cannot fix authored free text (HorizonSection.rationale
/ posture, Action.detail/rationale). This editor is handed ONLY the fact, its
canonical value, and the offending snippet, and returns a MINIMAL corrected
snippet — the smallest edit that makes the prose state the canonical value. It
does not see (or get to rewrite) the rest of the plan.

``editor`` is injectable: tests pass a stub. The default dispatch is NOT yet
wired to a live backend — live surgical prose-editing is gated on completing the
run-106 invariant coverage graph (the whole surgical pre-pass defaults OFF; see
the Slice-3 plan). Until then the default raises, and the fail-safe below returns
the original text unchanged so nothing breaks. When activating, replace
``_default_editor`` with a thin BaseAgent text dispatch (see argosy/agents/base.py
``_call_via_claude_code_inner``).
"""
from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)

_PROMPT = """You are correcting ONE factual value in a snippet of an existing financial plan.

Canonical fact: {fact_id}
Correct value: {value}

Offending snippet (it states a WRONG or stale value for this fact):
\"\"\"{snippet}\"\"\"

Return ONLY the corrected snippet — the SAME wording, with just the value fixed
to the canonical value above. Do not add commentary, caveats, or new sentences.
"""


def _default_editor(prompt: str) -> str:
    """Live dispatch is intentionally not wired in this slice (gated on full
    run-106 invariant coverage). The fail-safe in ``correct_prose_site`` catches
    this and returns the original text — so an accidental live call is a no-op,
    never a crash or a silent wrong edit."""
    raise NotImplementedError(
        "prose_editor default dispatch is not wired — inject an `editor` "
        "callable, or wire a BaseAgent text dispatch before enabling live "
        "surgical prose correction (ARGOSY_SURGICAL_CORRECTION)."
    )


def correct_prose_site(
    *,
    fact_id: str,
    canonical_value: object,
    offending_text: str,
    editor: Callable[[str], str] | None = None,
) -> str:
    """Return a minimal corrected snippet for an llm_prose site. Fail-safe:
    returns ``offending_text`` unchanged on any editor error."""
    editor = editor or _default_editor
    prompt = _PROMPT.format(fact_id=fact_id, value=canonical_value, snippet=offending_text)
    try:
        out = (editor(prompt) or "").strip()
        return out or offending_text
    except Exception as exc:  # noqa: BLE001 — fail-safe; re-verify is the backstop
        log.warning("prose_editor.failed fact=%s err=%s", fact_id, exc)
        return offending_text
