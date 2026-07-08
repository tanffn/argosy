"""Estate / US-situs domain-knowledge injection for funnel stage-3 packets.

Verify-run finding (2026-07-08, funnel run 138 / SOFI): the stage-3 fleet
adjudicated a BUY of a US-domiciled instrument WITHOUT the estate/us-situs
domain_knowledge in its packet — the fund manager explicitly noted "no
domain_knowledge file authorizing a US-estate rule was supplied" and routed
the question forward instead of blocking. The plan-synthesis and critique
paths load ``domain_knowledge/tax/us/estate_tax_nonresidents.md`` into their
packets (see ``plan_synthesis.inputs._load_tax_domain_kb_files`` /
``cli.critique._load_relevant_kb_for_israeli_user``); the funnel's deep
decision passed an empty ``user_constraints``. This module closes that
INPUTS gap: it loads the estate KB file(s) and renders them into the
constraints block handed to the trader / risk team / fund manager.

The deterministic FLOOR for the same rule lives in
``argosy.quality.plan_risk_kernel.evaluate_us_situs`` (extended to funnel
buys in ``deep_decision``) — this module only fixes the fleet's inputs so
judgment can act on the rule; it never judges anything itself.
"""
from __future__ import annotations

from argosy.logging import get_logger

_log = get_logger("argosy.services.decision_funnel.estate_kb")

#: The estate/us-situs rule file(s), relative to ``domain_knowledge_dir``.
#: Same file the alternatives sourcer / plan critique cite for the rule.
ESTATE_KB_RELPATHS: tuple[str, ...] = ("tax/us/estate_tax_nonresidents.md",)

# Rule summary rendered even when the file read fails, so the packet is never
# silently rule-free. Mirrors the standing prose used across the deploy /
# allocation paths (see services/allocation_plan.py, agents/alternatives_sourcer.py).
_ESTATE_RULE_SUMMARY = (
    "ESTATE / US-SITUS RULE (binding, from "
    "domain_knowledge/tax/us/estate_tax_nonresidents.md): the client is a "
    "NON-US person; US-domiciled (US-situs) securities carry a US estate-tax "
    "tail (no meaningful exemption, up to 40%). Prefer Irish/London UCITS or "
    "otherwise non-US-situs instruments for any NEW buy. NVDA is the ONE "
    "sanctioned US-situs sleeve. A BUY of any other US-domiciled instrument "
    "must be justified against this rule explicitly — do not route the "
    "question forward; block or justify."
)


def load_estate_kb() -> dict[str, str]:
    """Load the estate/us-situs KB file(s), keyed by repo-relative path
    (``domain_knowledge/tax/us/estate_tax_nonresidents.md``). Missing /
    unreadable files are skipped with a warning (the rule summary still
    travels in the constraints block, and the deterministic floor still
    guards regardless)."""
    from argosy.config import get_settings

    root = get_settings().domain_knowledge_dir
    out: dict[str, str] = {}
    for rel in ESTATE_KB_RELPATHS:
        path = root / rel
        try:
            if path.is_file():
                out[f"domain_knowledge/{rel}"] = path.read_text(encoding="utf-8")
            else:
                _log.warning("estate_kb.file_missing", path=str(path))
        except OSError as exc:  # pragma: no cover - filesystem blip
            _log.warning("estate_kb.file_read_failed", path=str(path), error=str(exc))
    return out


def estate_constraints_block(user_constraints: str = "") -> str:
    """Compose the stage-3 ``user_constraints`` string: the caller's
    constraints (if any) + the binding estate rule + the full KB file
    content, each file titled with its repo-relative path so agents can
    cite it in ``cited_sources``."""
    parts: list[str] = []
    if (user_constraints or "").strip():
        parts.append(user_constraints.strip())
    parts.append(_ESTATE_RULE_SUMMARY)
    for path, content in sorted(load_estate_kb().items()):
        parts.append(f"=== DOMAIN KNOWLEDGE: {path} ===\n{content.strip()}")
    return "\n\n".join(parts)


__all__ = ["ESTATE_KB_RELPATHS", "estate_constraints_block", "load_estate_kb"]
