"""Escalation bar — fatal FORKS only (Ariel's binding rule, 2026-07-09).

Two internal judges disagreeing on a VALUE or a WORDING is a DERIVATION
question — the fleet zigzags it (each side argues from raw sources, a
blind third re-derives, converge, record the rationale). The client gets
only structurally different PATHS where defensible derivations
irreconcilably diverge, or real-world facts only the client owns.

This module carries the doctrine in two forms:

* :data:`ESCALATION_BAR` — the prompt block every escalation-DECIDING
  agent embeds in its system prompt (CritiqueCloserAgent,
  FundManagerDialogueVerdictAgent, action_proposer). The AGENT judges.
* :func:`same_path_signature` — a light DETERMINISTIC transport check.
  Doctrine: plumbing may enforce SHAPE, never judgment. A
  needs-user-input question whose text compares two same-unit numbers
  of the same magnitude ("NVDA target 12.0% vs 13.0%") looks like a
  derivation disagreement, not a fork. The transport only LOGS a
  warning (the agent may have a good reason; the log feeds the weekly
  fleet self-review) — it NEVER blocks.
"""

from __future__ import annotations

import re

__all__ = ["ESCALATION_BAR", "same_path_signature"]


#: The escalation bar, embedded verbatim(-ish) in every prompt that can
#: choose "ask the user". Tests assert each touched agent's system
#: prompt contains this block.
ESCALATION_BAR = (
    "ESCALATION BAR (binding — fatal FORKS only).\n"
    "Before routing anything to the client, STATE which of these the "
    "question is:\n"
    "  (a) a DERIVATION / value / wording disagreement — two surfaces "
    "or two judges disagree on what a number or a sentence should be. "
    "This is NEVER a client question. Route it to reconciliation "
    "instead (the dispute / zigzag path): each side argues from RAW "
    "sources, a blind third re-derives, converge, record the "
    "rationale.\n"
    "  (b) a structurally different PATH — two defensible derivations "
    "that irreconcilably diverge into different courses of action — or "
    "a missing real-world fact ONLY the client owns (a goal, an "
    "external account, a life event). Only (b) may reach the client.\n"
    "Canonical NON-escalation: 'surface A says 12%, surface B says 13% "
    "— which is right?' is a derivation question; re-derive it from "
    "raw sources, never ask the client. Canonical escalation: 'sell "
    "the NVDA core to fund the sleeve vs keep the core and fund from "
    "cash' — a real fork only the client can choose."
)


# Two numbers compared with "vs"/"versus". Units captured so the guard
# only fires when BOTH sides carry the SAME unit shape (both %, or both
# bare) — "$40,000 vs 3 accounts" style prose must not trip it.
_NUM_VS_NUM = re.compile(
    r"(?P<a>\d[\d,]*(?:\.\d+)?)\s*(?P<ua>%|percent\b)?\s*"
    r"(?:vs\.?|versus)\s*"
    r"(?P<b>\d[\d,]*(?:\.\d+)?)\s*(?P<ub>%|percent\b)?",
    re.IGNORECASE,
)

#: "Same magnitude" = within one order of magnitude of each other. A
#: 12-vs-13 disagreement is a derivation smell; a 100-vs-2 comparison is
#: probably describing two different things.
_MAGNITUDE_RATIO_CAP = 10.0


def same_path_signature(question: str | None) -> bool:
    """True when the question text compares two same-unit numbers of the
    same magnitude ('12.0% vs 13.0%', '2.944 vs 3.00') — the signature
    of a derivation/value disagreement that per the escalation bar
    should have gone to reconciliation, not to the client.

    Deterministic transport SHAPE check only. Callers log a warning on
    True and let the payload through — never block on this.
    """
    if not question:
        return False
    for m in _NUM_VS_NUM.finditer(question):
        if bool(m.group("ua")) != bool(m.group("ub")):
            continue  # different units — not a same-path comparison
        a = float(m.group("a").replace(",", ""))
        b = float(m.group("b").replace(",", ""))
        if a == 0.0 or b == 0.0:
            if a == b:
                return True
            continue
        if max(a, b) / min(a, b) <= _MAGNITUDE_RATIO_CAP:
            return True
    return False
