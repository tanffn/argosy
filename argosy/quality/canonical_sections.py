"""Canonical 18-section retirement plan registry + distillate binding map.

Derived from docs/plans/argosy-comprehensive-plan-integration.md §3.2
Check 3 and §7 distillate_section_binding. The 18 section_ids match
the spec doc §1 ordering (Cover / Goals / Net Worth / ... / Action
Items).
"""
from __future__ import annotations

CANONICAL_SECTION_IDS: dict[str, str] = {
    "cover_assumptions":    "Cover, Scope, Assumptions Register",
    "client_goals":         "Client Circumstances + Goals",
    "net_worth":            "Net Worth Statement",
    "cashflow":             "Cash Flow + Savings Rate",
    "capital_sufficiency":  "Capital Sufficiency / Goal Funding",
    "ips":                  "Investment Policy Statement",
    "concentration":        "Concentration & Single-Stock Risk",
    "withdrawal":           "Retirement Income / Withdrawal Strategy",
    "monte_carlo":          "Monte Carlo / Sensitivity",
    "tax_plan":             "Tax Plan",
    "insurance":            "Insurance + Risk Management",
    "healthcare":           "Healthcare Cost Plan",
    "estate":               "Estate + Document Inventory",
    "cross_border":         "Cross-Border / Multi-Jurisdictional",
    "equity_comp":          "Equity Compensation Per-Grant",
    "fi_bridge":            "FI Bridge (pre-statutory-age)",
    "life_events":          "Life-Event Phasing",
    "action_items":         "Action Items + Owner + Due Date",
}
"""Map of canonical section_id -> human-readable title.

Synth output is required to emit Section.section_id values that are
keys in this dict. Coverage check counts how many distinct
section_ids appear across the three horizons.
"""

MVP_COVERAGE_THRESHOLD: int = 12
"""End-of-Phase-3 ship target: 12 of 18 sections present somewhere
across the three horizons. Below this, the section_coverage check
fails."""

FULL_SHIP_COVERAGE_THRESHOLD: int = 18
"""End-of-Phase-4 ship target: all 18 sections present."""


# ---------------------------------------------------------------------------
# distillate-field → bound section_id
# ---------------------------------------------------------------------------

DISTILLATE_FIELD_TO_SECTION_ID: dict[str, str | None] = {
    # Bound: non-empty field => section_id MUST appear AND carry a
    # citation with source_locator starting with "distillate.<field>".
    "plan_assumptions":      "cover_assumptions",
    "goals":                 "client_goals",
    "cashflow_phases":       "cashflow",
    "capital_sufficiency":   "capital_sufficiency",
    "ips":                   "ips",
    "withdrawal_schedule":   "withdrawal",
    "monte_carlo_grid":      "monte_carlo",
    "tax_schedule":          "tax_plan",
    "insurance_matrix":      "insurance",
    "healthcare_cost_plan":  "healthcare",
    "estate_documents":      "estate",
    "cross_border":          "cross_border",
    "equity_comp_grants":    "equity_comp",
    "fi_bridge":             "fi_bridge",
    "life_events":           "life_events",
    "priority_matrix":       "action_items",
    "real_estate_plan":      "net_worth",        # rolls into NW
    "fx_strategy":           "cashflow",         # savings-rate optimization
    "etf_reference":         "ips",              # IPS execution detail
    "securities_lending":    "ips",              # IPS execution detail
    "charitable_giving":     "tax_plan",         # tax-plan lever
    # Explicitly ungated — synthesis-wide meta, not per-section.
    "unmapped_sections":     None,
    "stress_tolerance":      None,
    "risk_priorities":       None,
    "decision_rules":        None,
    "constraints":           None,
    "principles":            None,
    "targets":               None,
}
"""Per-distillate-field section binding.

For every non-empty PlanDistillate field whose value here is a string,
the gate's binding check requires both:
  (a) a Section with that section_id to appear in PlanSynthesisOutput;
  (b) at least one Citation in that section whose source_locator
      starts with `distillate.<field_name>` — proving USE, not just
      structural presence.

Fields mapped to None are intentionally ungated (synthesis-wide
or meta-only).
"""


def is_canonical_section(section_id: str) -> bool:
    """True if `section_id` is one of the 18 canonical keys."""
    return section_id in CANONICAL_SECTION_IDS


def bound_section_for(distillate_field: str) -> str | None:
    """Look up the bound section_id for a distillate field, or None
    if the field is intentionally ungated (or unknown)."""
    return DISTILLATE_FIELD_TO_SECTION_ID.get(distillate_field)
