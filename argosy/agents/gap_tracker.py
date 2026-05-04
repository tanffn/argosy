"""Gap tracker — extends intake_fields with per-field freshness policies.

A "gap" is any required field that is either MISSING (never answered)
or STALE (answered, but the freshness window has elapsed). The advisor
panel renders the full gap list as a color-coded sidebar; the cadence
loops emit `gap_due` events when items go stale.

Backwards-compat: the original `intake_fields.STAGE_REQUIRED_FIELDS`
shape (dict[str, list[str]]) is rebuilt as `STAGE_REQUIRED_FIELDS` here
by projecting `STAGE_FIELDS` to dotted-path lists, so the existing
/api/intake/turn auto-advance logic (which imports either symbol)
keeps working unchanged.

Field catalog scope: aligned with the CFP Board's "Core Financial
Planning Technologies Questionnaire" (https://www.cfp.net/) intake
categories — Personal/Family, Goals, Income, Cash Flow, Net Worth,
Insurance, Estate, Tax, Retirement, Education. Israeli-specific
pension fields (קרן השתלמות, קופת גמל, מס שבח) are preserved
alongside the US-centric CFP defaults — Argosy is bicultural.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import yaml
from sqlalchemy import select

from argosy.agents.intake_fields import _has_value, _lookup, _safe_yaml_load
from argosy.state import db as db_mod
from argosy.state.models import AgentReport as AgentReportRow

Section = Literal["identity", "goals", "constraints"]
Freshness = Literal["one_shot", "monthly", "quarterly", "annual"]


@dataclass(frozen=True)
class FieldSpec:
    """One required field plus its freshness policy.

    `path` is the dotted path: section + nested-or-flat key, e.g.
    `identity.spouse_citizenship`. `freshness` controls how long an
    answered value is considered "fresh" before the gap tracker flags
    it stale (see `_FRESHNESS_WINDOWS`). `priority` is the gap-driven
    ordering hint (1 = ask soonest).
    """

    path: str
    label: str
    section: Section
    freshness: Freshness
    priority: int


@dataclass
class GapStatus:
    """Classified-by-state field list for one user.

    `fresh`: answered AND within the freshness window.
    `missing`: never answered (no value present in the YAML).
    `stale`: answered, but the freshness window has elapsed; carries
             the last-updated timestamp so the UI can show "as of …".
    """

    fresh: list[FieldSpec]
    missing: list[FieldSpec]
    stale: list[tuple[FieldSpec, datetime]]


# ----------------------------------------------------------------------
# Field catalog
# ----------------------------------------------------------------------
#
# Defaults align with the reframe brief:
#   - Identity life-event fields → one_shot (don't change unless the
#     user reports a marriage / new child / move).
#   - Employment / asset values → annual (review yearly).
#   - Bank + brokerage balances → monthly (cadence loops nudge).
#   - Estate documents / insurance → annual (review yearly).
#   - RSU vest, real estate P/L → quarterly.
# Priority: 1 = must-have for the very first session;
#           2 = should-have once basics are in;
#           3 = nice-to-have / operational.
#
# Stage layout (10 stages total — Phase 2 adds estate/insurance/tax/education):
#   stage_1  identity & jurisdiction
#   stage_2  goals & timeline
#   stage_3  financial picture (income, balances, real estate, IL pensions, US retirement)
#   stage_4  brokerage connections
#   stage_5  plan import & critique
#   stage_6  operational preferences
#   stage_7  estate planning           (CFP)
#   stage_8  risk management/insurance (CFP)
#   stage_9  tax situation             (CFP)
#   stage_10 education funding         (CFP)

STAGE_FIELDS: dict[str, list[FieldSpec]] = {
    # --- Stage 1: Identity & jurisdiction (CFP Personal / Family) ----
    "stage_1": [
        FieldSpec(
            path="identity.tax_residency",
            label="Tax residency",
            section="identity",
            freshness="one_shot",
            priority=1,
        ),
        FieldSpec(
            path="identity.user_citizenship",
            label="Your citizenship",
            section="identity",
            freshness="one_shot",
            priority=1,
        ),
        FieldSpec(
            path="identity.marital_status",
            label="Marital status",
            section="identity",
            freshness="one_shot",
            priority=1,
        ),
        FieldSpec(
            path="identity.spouse_citizenship",
            label="Spouse citizenship",
            section="identity",
            freshness="one_shot",
            priority=2,
        ),
        FieldSpec(
            path="identity.spouse_tax_residency",
            label="Spouse tax residency",
            section="identity",
            freshness="one_shot",
            priority=2,
        ),
        FieldSpec(
            path="identity.children",
            label="Children",
            section="identity",
            freshness="one_shot",
            priority=2,
        ),
        FieldSpec(
            path="identity.user_date_of_birth",
            label="Your date of birth",
            section="identity",
            freshness="one_shot",
            priority=1,
        ),
        FieldSpec(
            path="identity.spouse_date_of_birth",
            label="Spouse date of birth",
            section="identity",
            freshness="one_shot",
            priority=2,
        ),
        FieldSpec(
            path="identity.dependents_count",
            label="Number of financial dependents",
            section="identity",
            freshness="one_shot",
            priority=1,
        ),
        FieldSpec(
            path="identity.primary_residence_country",
            label="Primary residence (country)",
            section="identity",
            freshness="one_shot",
            priority=2,
        ),
        FieldSpec(
            path="identity.employment_status",
            label="Employment status (employed / self-employed / retired)",
            section="identity",
            freshness="annual",
            priority=1,
        ),
    ],
    # --- Stage 2: Goals & timeline (CFP Goals & Aspirations) --------
    "stage_2": [
        FieldSpec(
            path="goals.retirement_target_year",
            label="Retirement target year",
            section="goals",
            freshness="annual",
            priority=1,
        ),
        FieldSpec(
            path="goals.target_annual_income",
            label="Target annual income (retirement)",
            section="goals",
            freshness="annual",
            priority=1,
        ),
        FieldSpec(
            path="goals.near_term_spending",
            label="Near-term spending events",
            section="goals",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="goals.risk_tolerance",
            label="Risk tolerance (conservative / moderate / aggressive)",
            section="goals",
            freshness="annual",
            priority=1,
        ),
        FieldSpec(
            path="goals.investment_time_horizon_years",
            label="Investment time horizon (years)",
            section="goals",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="goals.lifestyle_aspirations",
            label="Lifestyle aspirations (travel, second home, sabbatical)",
            section="goals",
            freshness="annual",
            priority=3,
        ),
        FieldSpec(
            path="goals.legacy_intent",
            label="Legacy / inheritance intent",
            section="goals",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="goals.charitable_intent",
            label="Charitable giving intent",
            section="goals",
            freshness="annual",
            priority=3,
        ),
    ],
    # --- Stage 3: Financial picture (CFP Income + Cash Flow + Net Worth) ----
    "stage_3": [
        FieldSpec(
            path="identity.user_employment_employer",
            label="Your employer",
            section="identity",
            freshness="annual",
            priority=1,
        ),
        FieldSpec(
            path="identity.user_employment_gross_annual",
            label="Your gross annual comp",
            section="identity",
            freshness="annual",
            priority=1,
        ),
        FieldSpec(
            path="identity.spouse_employment_gross_annual",
            label="Spouse gross annual comp",
            section="identity",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="identity.bank_accounts",
            label="Bank accounts (balances)",
            section="identity",
            freshness="monthly",
            priority=2,
        ),
        FieldSpec(
            path="identity.brokerage_accounts",
            label="Brokerage accounts (positions)",
            section="identity",
            freshness="monthly",
            priority=1,
        ),
        FieldSpec(
            path="identity.real_estate",
            label="Real estate holdings",
            section="identity",
            freshness="quarterly",
            priority=2,
        ),
        FieldSpec(
            path="identity.pensions",
            label="Pensions (קרן השתלמות / קופת גמל / פנסיה)",
            section="identity",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="identity.rsu_grants",
            label="RSU / equity grants & vest schedule",
            section="identity",
            freshness="quarterly",
            priority=1,
        ),
        FieldSpec(
            path="identity.bonus_history",
            label="Bonus history (last 3 years)",
            section="identity",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="identity.secondary_income",
            label="Secondary income (rental, consulting, royalties)",
            section="identity",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="identity.us_retirement_accounts",
            label="US retirement accounts (401k / IRA / Roth / HSA)",
            section="identity",
            freshness="quarterly",
            priority=1,
        ),
        FieldSpec(
            path="identity.monthly_expenses_total",
            label="Total monthly household expenses",
            section="identity",
            freshness="quarterly",
            priority=1,
        ),
        FieldSpec(
            path="identity.monthly_expenses_breakdown",
            label="Monthly expense breakdown (housing / food / transit / discretionary)",
            section="identity",
            freshness="quarterly",
            priority=2,
        ),
        FieldSpec(
            path="identity.emergency_fund_months",
            label="Emergency fund (months of expenses covered)",
            section="identity",
            freshness="quarterly",
            priority=1,
        ),
        FieldSpec(
            path="identity.mortgage_balance",
            label="Mortgage balance & rate",
            section="identity",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="identity.other_debts",
            label="Other debts (auto, student, credit card)",
            section="identity",
            freshness="quarterly",
            priority=2,
        ),
        FieldSpec(
            path="identity.business_interests",
            label="Business ownership interests",
            section="identity",
            freshness="annual",
            priority=3,
        ),
        FieldSpec(
            path="identity.foreign_assets",
            label="Foreign assets (FBAR / declaration scope)",
            section="identity",
            freshness="annual",
            priority=3,
        ),
    ],
    # --- Stage 4: Brokerage connections -----------------------------
    "stage_4": [
        FieldSpec(
            path="constraints.broker_credentials_acknowledged",
            label="Broker credentials acknowledged",
            section="constraints",
            freshness="annual",
            priority=3,
        ),
    ],
    # --- Stage 5: Plan import & critique -----------------------------
    "stage_5": [
        FieldSpec(
            path="constraints.plan_imported",
            label="Plan imported",
            section="constraints",
            freshness="annual",
            priority=3,
        ),
    ],
    # --- Stage 6: Operational preferences ----------------------------
    "stage_6": [
        FieldSpec(
            path="constraints.tier_override_mode",
            label="Tier override mode",
            section="constraints",
            freshness="annual",
            priority=3,
        ),
        FieldSpec(
            path="constraints.execution_mode_default",
            label="Execution mode default",
            section="constraints",
            freshness="annual",
            priority=3,
        ),
        FieldSpec(
            path="constraints.alert_email",
            label="Alert email",
            section="constraints",
            freshness="annual",
            priority=3,
        ),
    ],
    # --- Stage 7: Estate planning (CFP) -----------------------------
    "stage_7": [
        FieldSpec(
            path="constraints.will_exists",
            label="Will / last testament exists",
            section="constraints",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="constraints.will_last_review_year",
            label="Will last reviewed (year)",
            section="constraints",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="constraints.living_trust_exists",
            label="Living trust",
            section="constraints",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="constraints.power_of_attorney",
            label="Durable power of attorney",
            section="constraints",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="constraints.healthcare_directive",
            label="Healthcare directive / living will",
            section="constraints",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="constraints.beneficiary_designations_reviewed",
            label="Beneficiary designations reviewed (retirement / insurance)",
            section="constraints",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="constraints.guardianship_for_minors",
            label="Guardianship designation for minor children",
            section="constraints",
            freshness="annual",
            priority=2,
        ),
    ],
    # --- Stage 8: Risk management / Insurance (CFP) -----------------
    "stage_8": [
        FieldSpec(
            path="identity.life_insurance",
            label="Life insurance (carrier, face amount, type)",
            section="identity",
            freshness="annual",
            priority=1,
        ),
        FieldSpec(
            path="identity.disability_insurance",
            label="Disability insurance (short / long term)",
            section="identity",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="identity.health_insurance",
            label="Health insurance (carrier, deductible, HSA-eligible)",
            section="identity",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="identity.long_term_care_insurance",
            label="Long-term care insurance",
            section="identity",
            freshness="annual",
            priority=3,
        ),
        FieldSpec(
            path="identity.property_casualty_insurance",
            label="Property & casualty (home, auto, renters)",
            section="identity",
            freshness="annual",
            priority=3,
        ),
        FieldSpec(
            path="identity.umbrella_liability_insurance",
            label="Umbrella liability coverage",
            section="identity",
            freshness="annual",
            priority=3,
        ),
    ],
    # --- Stage 9: Tax situation (CFP) ------------------------------
    "stage_9": [
        FieldSpec(
            path="identity.tax_filing_status",
            label="Tax filing status (MFJ / MFS / single / HoH / IL individual)",
            section="identity",
            freshness="annual",
            priority=1,
        ),
        FieldSpec(
            path="identity.prior_year_agi",
            label="Prior-year adjusted gross income",
            section="identity",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="identity.prior_year_effective_rate",
            label="Prior-year effective tax rate",
            section="identity",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="identity.tax_carryforwards",
            label="Tax carryforwards (capital losses, AMT credit, foreign tax)",
            section="identity",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="constraints.tax_loss_harvesting_enabled",
            label="Tax-loss harvesting enabled",
            section="constraints",
            freshness="annual",
            priority=3,
        ),
        FieldSpec(
            path="constraints.planned_charitable_giving_annual",
            label="Planned annual charitable giving (USD/NIS)",
            section="constraints",
            freshness="annual",
            priority=3,
        ),
        FieldSpec(
            path="identity.estimated_quarterly_taxes",
            label="Estimated quarterly tax payments (self-employed / large variable)",
            section="identity",
            freshness="quarterly",
            priority=3,
        ),
    ],
    # --- Stage 10: Education funding (CFP) -------------------------
    "stage_10": [
        FieldSpec(
            path="goals.education_funding_targets",
            label="Education funding targets (per dependent: year, cost, currency)",
            section="goals",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="identity.education_savings_accounts",
            label="Education savings accounts (529 / Coverdell / חיסכון לכל ילד)",
            section="identity",
            freshness="annual",
            priority=2,
        ),
        FieldSpec(
            path="goals.education_funding_strategy",
            label="Education funding strategy (full / partial / loans expected)",
            section="goals",
            freshness="annual",
            priority=3,
        ),
    ],
}


# Stage progression — keep this in sync with `INTAKE_STAGES` and the
# `_persist_turn` next-stage default map. The Phase 2 expansion runs
# 1→2→3→4→5→6→7→8→9→10→complete.
STAGE_ORDER: tuple[str, ...] = (
    "stage_1",
    "stage_2",
    "stage_3",
    "stage_4",
    "stage_5",
    "stage_6",
    "stage_7",
    "stage_8",
    "stage_9",
    "stage_10",
)


# Backwards-compat shim. Keep `STAGE_REQUIRED_FIELDS` exported so the
# existing /api/intake/turn route + tests that import it from
# argosy.agents.intake_fields keep working — both paths must agree.
STAGE_REQUIRED_FIELDS: dict[str, list[str]] = {
    stage: [f.path for f in fields] for stage, fields in STAGE_FIELDS.items()
}


def all_fields() -> list[FieldSpec]:
    """Flatten STAGE_FIELDS into a single deduped list (ordered by stage)."""
    seen: set[str] = set()
    out: list[FieldSpec] = []
    for stage in STAGE_ORDER:
        for f in STAGE_FIELDS.get(stage, []):
            if f.path in seen:
                continue
            seen.add(f.path)
            out.append(f)
    return out


def field_by_path(path: str) -> FieldSpec | None:
    for f in all_fields():
        if f.path == path:
            return f
    return None


# ----------------------------------------------------------------------
# Freshness windows
# ----------------------------------------------------------------------
#
# Generous defaults — the gap tracker is a nudge, not a tripwire. A
# couple of grace days means a once-a-month reminder doesn't fire on
# day 30 sharp.

_FRESHNESS_WINDOWS: dict[Freshness, timedelta] = {
    "one_shot": timedelta(days=10_000),  # effectively "never stale"
    "monthly": timedelta(days=33),
    "quarterly": timedelta(days=95),
    "annual": timedelta(days=380),
}


def _is_stale(spec: FieldSpec, last_updated: datetime, now: datetime) -> bool:
    window = _FRESHNESS_WINDOWS[spec.freshness]
    return (now - last_updated) > window


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def gap_status(
    *,
    identity_yaml: str,
    goals_yaml: str,
    constraints_yaml: str,
    last_updated_per_field: dict[str, datetime] | None = None,
    now: datetime | None = None,
) -> GapStatus:
    """Classify every required field as fresh / missing / stale.

    `last_updated_per_field` maps each dotted field path to the
    datetime of the most-recent agent_reports row that recorded a
    context_update touching that field. When a field is answered but
    has no entry in the dict, we treat it as fresh (the field exists
    in YAML so the user told us at some point — we just can't pin
    the timestamp).
    """
    if now is None:
        now = datetime.now(UTC)
    last_updated_per_field = last_updated_per_field or {}

    by_section = {
        "identity": _safe_yaml_load(identity_yaml),
        "goals": _safe_yaml_load(goals_yaml),
        "constraints": _safe_yaml_load(constraints_yaml),
    }

    # Skip spouse fields when we can confidently infer the user is
    # unmarried — same rule as intake_fields.stage_status.
    marital = by_section["identity"].get("marital_status") or by_section["identity"].get(
        "marital", ""
    )
    spouse_skipped = isinstance(marital, str) and marital.lower() in (
        "single",
        "unmarried",
        "divorced",
        "widowed",
    )

    fresh: list[FieldSpec] = []
    missing: list[FieldSpec] = []
    stale: list[tuple[FieldSpec, datetime]] = []

    for spec in all_fields():
        if spouse_skipped and spec.path.startswith("identity.spouse_"):
            fresh.append(spec)
            continue

        section_dict = by_section.get(spec.section, {})
        value = _lookup(section_dict, spec.path)
        if value is None:
            missing.append(spec)
            continue

        ts = last_updated_per_field.get(spec.path)
        if ts is None:
            # Answered, no timestamp available → treat as fresh.
            fresh.append(spec)
            continue

        if _is_stale(spec, ts, now):
            stale.append((spec, ts))
        else:
            fresh.append(spec)

    return GapStatus(fresh=fresh, missing=missing, stale=stale)


def gaps_for_prompt(status: GapStatus) -> tuple[list[str], list[str]]:
    """Convert GapStatus → (answered_paths, still_needed_paths) for the prompt.

    Mirrors the historical `stage_status` shape so prompt-building
    code can keep its `answered_fields` / `missing_fields` parameters
    without a wider refactor. "Still needed" includes both missing AND
    stale, since the agent should re-confirm staled values.
    """
    answered = [f.path for f in status.fresh]
    still_needed = [f.path for f in status.missing] + [f.path for f, _ in status.stale]
    return answered, still_needed


def pick_gap_driven_target(status: GapStatus) -> FieldSpec | None:
    """Pick the highest-priority gap to ask about next.

    Prefers missing over stale (we'd rather get a never-answered
    field than re-confirm a stale one), then by `priority` (lower =
    earlier), then by stage order via the `all_fields()` traversal.
    Returns `None` if there are no gaps.
    """
    candidates: list[tuple[int, int, FieldSpec]] = []
    order = {f.path: i for i, f in enumerate(all_fields())}
    for f in status.missing:
        candidates.append((0, f.priority, f))  # missing first
    for f, _ts in status.stale:
        candidates.append((1, f.priority, f))
    if not candidates:
        return None
    candidates.sort(key=lambda t: (t[0], t[1], order.get(t[2].path, 999)))
    return candidates[0][2]


# ----------------------------------------------------------------------
# Audit-log timestamps
# ----------------------------------------------------------------------


async def compute_field_timestamps(user_id: str) -> dict[str, datetime]:
    """Walk agent_reports for the user, return field → last_updated.

    For each agent_reports row produced by the intake / advisor / intake
    extractor agents, we parse the `response_text` JSON and look for
    `context_updates` entries. Each entry's `yaml_patch` is parsed and
    flattened to dotted paths under its `target_section`; we record the
    row's `created_at` as the last-updated timestamp for those paths.
    Later (newer) rows clobber older ones — `agent_reports` is append-
    only, so iterating in ascending `created_at` order gives us the
    most-recent timestamp per path.

    Defensive on malformed JSON / YAML: on any parse error for a row,
    we just skip that row and keep going. The route falls back to
    "fresh-without-timestamp" for fields it can't pin a date on.
    """
    import json as _json  # local import — avoids stdlib pollution at module top

    out: dict[str, datetime] = {}
    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(AgentReportRow)
                .where(AgentReportRow.user_id == user_id)
                .where(
                    AgentReportRow.agent_role.in_(
                        ("intake", "advisor", "intake_extractor")
                    )
                )
                .order_by(AgentReportRow.created_at.asc())
            )
        ).scalars().all()

    for row in rows:
        text = row.response_text or ""
        if not text.strip():
            continue
        try:
            parsed = _json.loads(text)
        except (ValueError, _json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue

        # Two shapes to support:
        # 1) intake / advisor turn output: {"context_updates": [{target_section, yaml_patch, ...}, ...]}
        # 2) intake_extractor output: {"identity_yaml": "...", "goals_yaml": "...", "constraints_yaml": "..."}

        ts = row.created_at
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)

        if "context_updates" in parsed and isinstance(parsed["context_updates"], list):
            for upd in parsed["context_updates"]:
                if not isinstance(upd, dict):
                    continue
                section = upd.get("target_section")
                patch = upd.get("yaml_patch") or ""
                if section not in ("identity", "goals", "constraints"):
                    continue
                for path in _flatten_yaml_to_paths(section, patch):
                    out[path] = ts  # type: ignore[assignment]

        for section_key in ("identity_yaml", "goals_yaml", "constraints_yaml"):
            if section_key in parsed and isinstance(parsed[section_key], str):
                section = section_key.removesuffix("_yaml")
                for path in _flatten_yaml_to_paths(section, parsed[section_key]):
                    out[path] = ts  # type: ignore[assignment]

    return out


def _flatten_yaml_to_paths(section: str, yaml_text: str) -> list[str]:
    """Parse `yaml_text`, yield dotted paths matching FieldSpec.path keys.

    A field is considered "touched" by the patch when any FieldSpec
    whose `section` matches and whose tail (after the section prefix)
    appears at the top level of the parsed YAML — under either the
    nested or flat key shape. This is intentionally lenient: we'd
    rather mark a field as fresh on a near-match than miss it.
    """
    if not yaml_text or not yaml_text.strip():
        return []
    try:
        obj: Any = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return []
    if not isinstance(obj, dict):
        return []

    out: list[str] = []
    for spec in all_fields():
        if spec.section != section:
            continue
        # Strip the leading "<section>." prefix.
        tail = spec.path.split(".", 1)[1] if "." in spec.path else spec.path
        if _lookup(obj, tail) is not None or _has_value(obj.get(tail)):
            out.append(spec.path)
    return out


__all__ = [
    "FieldSpec",
    "GapStatus",
    "STAGE_FIELDS",
    "STAGE_ORDER",
    "STAGE_REQUIRED_FIELDS",
    "all_fields",
    "compute_field_timestamps",
    "field_by_path",
    "gap_status",
    "gaps_for_prompt",
    "pick_gap_driven_target",
]
