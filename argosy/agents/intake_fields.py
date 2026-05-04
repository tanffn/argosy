"""Canonical field map for intake stages.

Drives:

- "Already answered" / "Still needed" computation in the /turn route,
  passed verbatim to the IntakeAgent prompt so it asks only about
  missing fields.
- Auto-advance: when stage_status(stage).missing is empty, the route
  forces current_stage forward regardless of what the agent claimed.
  This stops the loop where Haiku keeps re-asking answered questions
  because it can't reliably traverse free-form YAML.

Field paths use dotted notation. The first segment is the user_context
section (identity / goals / constraints); the rest is the path inside
that section. The lookup tolerates BOTH nested keys (spouse.citizenship)
AND flattened keys (spouse_citizenship), since past intake turns wrote
flat keys and we don't want to re-ask just because the shape differs.

Phase 2 (CFP expansion): the canonical field list is now defined in
`argosy.agents.gap_tracker` (`STAGE_FIELDS`) — this module re-exports
`STAGE_REQUIRED_FIELDS` from there as the dotted-path projection so all
existing call sites keep working unchanged. See gap_tracker.py for the
full CFP-aligned catalog (10 stages, ~55 fields).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    pass


def _has_value(node: Any) -> bool:
    """True iff `node` represents a real answered value."""
    if node is None:
        return False
    if isinstance(node, str) and not node.strip():
        return False
    if isinstance(node, (list, dict)) and len(node) == 0:
        return False
    return True


def _lookup(section_dict: dict, dotted_path: str) -> Any:
    """Try nested AND flattened key lookup for a single field.

    `dotted_path` example: 'identity.spouse_tax_residency' →
    we strip the leading section name (already chosen by caller) →
    try nested keys ['spouse_tax_residency'] then flattened
    'spouse_tax_residency'. For deeper paths (e.g. 'identity.spouse.citizenship')
    we also try the flattened form 'spouse_citizenship'.
    """
    parts = dotted_path.split(".")
    if parts and parts[0] in ("identity", "goals", "constraints"):
        parts = parts[1:]
    if not parts:
        return None

    # Nested lookup.
    current: Any = section_dict
    for p in parts:
        if isinstance(current, dict) and p in current:
            current = current[p]
        else:
            current = None
            break
    if _has_value(current):
        return current

    # Flattened lookup (join with underscore).
    flat = "_".join(parts)
    if flat in section_dict and _has_value(section_dict[flat]):
        return section_dict[flat]

    return None


def _safe_yaml_load(s: str) -> dict:
    if not s or not s.strip():
        return {}
    try:
        v = yaml.safe_load(s)
    except yaml.YAMLError:
        return {}
    return v if isinstance(v, dict) else {}


def __getattr__(name: str) -> Any:
    """Lazy re-export of `STAGE_REQUIRED_FIELDS` from gap_tracker.

    The canonical field catalog moved to `argosy.agents.gap_tracker` in
    Phase 2 (CFP expansion). gap_tracker imports the helpers in this
    module, so we cannot import gap_tracker at top level — instead we
    resolve `STAGE_REQUIRED_FIELDS` lazily via PEP 562 module __getattr__.
    """
    if name == "STAGE_REQUIRED_FIELDS":
        from argosy.agents.gap_tracker import STAGE_REQUIRED_FIELDS as _SRF

        return _SRF
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def stage_status(
    *,
    identity_yaml: str,
    goals_yaml: str,
    constraints_yaml: str,
    stage: str,
) -> dict[str, list[str]]:
    """Return {'answered': [...], 'missing': [...]} for the given stage.

    `answered` and `missing` are lists of dotted field paths. Their union
    equals `STAGE_REQUIRED_FIELDS[stage]`.
    """
    # Lazy import to avoid the circular dep — gap_tracker imports this
    # module's helpers, so we can't import it at top level.
    from argosy.agents.gap_tracker import STAGE_REQUIRED_FIELDS

    required = STAGE_REQUIRED_FIELDS.get(stage, [])
    by_section = {
        "identity": _safe_yaml_load(identity_yaml),
        "goals": _safe_yaml_load(goals_yaml),
        "constraints": _safe_yaml_load(constraints_yaml),
    }

    # Special-case: if marital_status is "single" (or "unmarried"), the
    # spouse fields are auto-answered as N/A — don't keep asking.
    marital = by_section["identity"].get("marital_status") or by_section["identity"].get(
        "marital", ""
    )
    spouse_skipped = isinstance(marital, str) and marital.lower() in (
        "single",
        "unmarried",
        "divorced",
        "widowed",
    )

    answered: list[str] = []
    missing: list[str] = []
    for field in required:
        if spouse_skipped and field.startswith("identity.spouse_"):
            answered.append(field)
            continue
        section = field.split(".", 1)[0]
        section_dict = by_section.get(section, {})
        value = _lookup(section_dict, field)
        if value is not None:
            answered.append(field)
        else:
            missing.append(field)
    return {"answered": answered, "missing": missing}


def all_required_complete(
    *,
    identity_yaml: str,
    goals_yaml: str,
    constraints_yaml: str,
    stage: str,
) -> bool:
    """True iff `stage_status(...).missing` is empty for the stage."""
    s = stage_status(
        identity_yaml=identity_yaml,
        goals_yaml=goals_yaml,
        constraints_yaml=constraints_yaml,
        stage=stage,
    )
    return len(s["missing"]) == 0


__all__ = [
    "STAGE_REQUIRED_FIELDS",
    "all_required_complete",
    "stage_status",
]
