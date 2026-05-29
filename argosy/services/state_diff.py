"""State-diff service — pure-function snapshot comparator (Spec B commit #3).

Spec: ``docs/superpowers/specs/2026-05-29-state-observer-agent-design.md`` §2.

Computes structured diffs between two `current_state` snapshot dicts (the
six-section dict produced by `state_snapshot.collect_state_snapshot`). Two
comparison directions:

  * **vs plan-baseline**   — current state vs the `plan_inputs` baseline.
    Cross-section pairing via :data:`PLAN_BASELINE_COMPARATOR_MAP` (e.g.
    `macro.fx_usd_nis_spot` is paired with `plan_inputs.assumed_fx_usd_nis`).
    This is the FX-emergence gate — the diff row that lets the observer
    surface the 22% USD/NIS deviation a hand-rolled detector missed.
  * **vs prior snapshot**  — current state vs the immediately-prior
    snapshot for the same user. Same-section walk; no cross-section
    pairing.

Pure functions; no DB access. The sibling `state_snapshot.py` service
(commit #2) owns persistence + assembly; this module just consumes the
dicts they produce.

Public surface:

  - :class:`FieldDiff`                  — one (path, current, baseline, kind, magnitude) row.
  - :class:`FullDiff`                   — `vs_plan` + `vs_prior` lists, truncation marker.
  - :func:`compute_diff`                — generic numeric/categorical walk.
  - :func:`compute_full_diff`           — wraps both comparison directions.
  - :func:`compute_deviation_bucket`    — deterministic bucket from magnitude.
  - :data:`PLAN_BASELINE_COMPARATOR_MAP` — cross-section pairing for vs-plan.
  - :data:`_NO_PLAN_BASELINE_FIELDS`    — allowlist of numeric fields with
    no plan-baseline counterpart (CI-invariant gate).
  - :data:`SNAPSHOT_FIELD_PREFIXES`     — enumerable prefixes for the
    `inferred_kind` mapping in commit #6.
  - :data:`MAX_FIELDS_PER_DIFF`         — token-cap on diff payload size.

Bucket scheme (brief contract): ``"<5pct"`` / ``"5to15pct"`` /
``"15to30pct"`` / ``">30pct"`` — coarser than the spec's small/moderate/
large/extreme labels, but stable enough that a deviation jittering across
a band edge does not re-fire flag-level dedup. The brief took precedence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

#: Maximum number of FieldDiff rows in each side of FullDiff (§2.5).
#: Categorical/appeared/disappeared + allowlist rows are kept regardless;
#: numeric rows are sorted by |magnitude| and truncated to fit within 300.
MAX_FIELDS_PER_DIFF: int = 300


#: 2% absolute deviation_pct is the default "material" threshold for numeric
#: fields (§2.4). Below this and outside the always-include allowlist, the
#: row is dropped so the LLM's diff payload stays signal-dense.
DEFAULT_NUMERIC_THRESHOLD_PCT: float = 0.02


#: Magnitude floors by field-name suffix (§2.4). A field whose deviation_pct
#: looks large but absolute movement is tiny (e.g. 0.001 -> 0.002 = +100%)
#: gets filtered unless its absolute delta also exceeds the floor.
#: Suffix match is performed against the LAST component of the dotted path.
_MAGNITUDE_FLOORS_BY_SUFFIX: dict[str, float] = {
    # 0.5pp for percentage / rate / yield / ratio fields.
    "_pct":   0.005,
    "_rate":  0.005,
    "_yield": 0.005,
    "_ratio": 0.005,
    # $100 currency floor.
    "_usd":   100.0,
    "_nis":   100.0,
    "_k_usd": 100.0,
    "_k_nis": 100.0,
    # 0.5 index-points / value-points floor.
    "_index": 0.5,
    "_value": 0.5,
}


#: Cross-section comparator map for the vs-plan diff (§2.3). Keys are
#: current-state paths; values are the plan-baseline path the current
#: value should be compared against.
#:
#: The ``[]`` syntax means "for each row in this list, compare the named
#: sub-field." Resolved by :func:`_resolve_list_field`.
#:
#: **CRITICAL**: the first entry (`macro.fx_usd_nis_spot`) is the
#: FX-emergence gate. If you remove it you have broken the architecture's
#: empirical contract — re-read the spec before touching this map.
PLAN_BASELINE_COMPARATOR_MAP: dict[str, str] = {
    # FX — the spec's named example. 3.6 (plan) vs 2.8 (current) = -22%.
    "macro.fx_usd_nis_spot":                                "plan_inputs.assumed_fx_usd_nis",
    "macro.fx_usd_nis_30d_avg":                             "plan_inputs.assumed_fx_usd_nis",
    # Allocation drift — current_pct vs target_pct per allocation row.
    "portfolio.allocations[].current_pct":                  "portfolio.allocations[].target_pct",
    "portfolio.allocations[].current_k_usd":                "portfolio.allocations[].target_k_usd",
    # Realized vs assumed monthly cashflow.
    "cashflow_recent.last_3_months[].realized_expense_nis": "plan_inputs.assumed_monthly_expenses_nis",
    "cashflow_recent.last_3_months[].realized_income_nis":  "plan_inputs.assumed_monthly_income_nis",
    # Tax-bracket / effective-rate vs assumed marginal.
    "tax_assumptions.current_marginal_bracket_pct":         "plan_inputs.assumed_marginal_tax_rate",
    "tax_assumptions.effective_rate_prior_year_pct":        "plan_inputs.assumed_marginal_tax_rate",
}


#: Always-include allowlist — paths whose ANY change (even sub-threshold)
#: stays in the diff. Auto-derived from the comparator map per §2.4: a
#: tiny numeric change in a plan-anchored field can still be structurally
#: significant (e.g. a tax-bracket tier transition).
ALWAYS_INCLUDE_ALLOWLIST: frozenset[str] = frozenset(PLAN_BASELINE_COMPARATOR_MAP.keys())


#: Numeric fields in the §1.2 snapshot schema that have NO plan-baseline
#: comparator and that is intentional. Every such field must be listed
#: here OR appear as a key in :data:`PLAN_BASELINE_COMPARATOR_MAP` — the
#: CI invariant test (`test_state_diff.py::test_every_numeric_field_*`)
#: enforces this so a new schema field cannot silently slip past the
#: vs-plan diff.
#:
#: Each entry is a dotted path matching the leaf field as it appears in
#: the snapshot.state dict. List-elements are matched via the `[]` syntax.
_NO_PLAN_BASELINE_FIELDS: frozenset[str] = frozenset({
    # Portfolio totals — the plan doesn't anchor a specific total value;
    # it anchors allocation targets per category. Total_value drifts with
    # market movement and is not a "plan deviation" per se.
    "portfolio.total_value_usd",
    "portfolio.cash_balances_usd",
    "portfolio.unallocated_cash_usd",
    "portfolio.top_concentration_pct",  # derived statistic, no plan anchor
    # Portfolio position-level fields — positions list is dynamic; per-row
    # diff is via appeared/disappeared, not numeric vs plan.
    "portfolio.positions[].shares",
    "portfolio.positions[].value_usd",
    "portfolio.positions[].value_nis",
    # Macro indices / rates — the plan doesn't pre-commit to a specific
    # S&P level or Fed funds rate; these are "world state" the observer
    # reads against context (and uses for vs-prior movement detection).
    "macro.fed_funds_rate_pct",
    "macro.treasury_10y_yield_pct",
    "macro.sp500_index",
    "macro.sp500_30d_return_pct",
    "macro.nasdaq_index",
    "macro.nasdaq_30d_return_pct",
    "macro.vix",
    # Cashflow deviations are themselves derived statistics; the realized
    # vs assumed pairing is what's in the comparator map.
    "cashflow_recent.last_3_months[].projected_expense_nis",
    "cashflow_recent.last_3_months[].projected_income_nis",
    "cashflow_recent.last_3_months[].deviation_pct",
    "cashflow_recent.last_3_months[].income_deviation_pct",
    "cashflow_recent.cumulative_deviation_nis",
    # Tax — withholding cap is static-config, never deviates against plan.
    "tax_assumptions.assumed_marginal_rate_pct",
    "tax_assumptions.withholding_supplemental_cap_pct",
})


#: Enumerable snapshot-field prefixes the `inferred_kind` mapping in
#: commit #6 (§4.2) discriminates on. Listed here so the CI invariant test
#: can confirm every section of the snapshot schema is covered (no field
#: silently falls through to `other_observation` unless that's intentional).
#:
#: Each entry is a tuple (prefix, intended_kind_label). The label strings
#: match the §4.2 mapping table verbatim. Commit #6's flag-writer reads
#: this constant to build its kind-derivation table.
SNAPSHOT_FIELD_PREFIXES: tuple[tuple[str, str], ...] = (
    ("macro.fx_",                       "fx_observation"),
    ("macro.fed_funds_",                "rates_observation"),
    ("macro.treasury_",                 "rates_observation"),
    ("macro.sp500_",                    "equity_observation"),
    ("macro.nasdaq_",                   "equity_observation"),
    ("macro.vix",                       "volatility_observation"),
    ("macro.recent_",                   "news_observation"),
    ("portfolio.allocations",           "allocation_observation"),
    ("portfolio.positions",             "position_observation"),
    ("portfolio.top_concentration_",    "concentration_observation"),
    ("portfolio.unallocated_cash_",     "cash_observation"),
    ("portfolio.cash_balances_",        "cash_observation"),
    ("portfolio.total_value_",          "portfolio_observation"),
    ("cashflow_recent.",                "cashflow_observation"),
    ("tax_assumptions.",                "tax_observation"),
    ("plan_inputs.",                    "plan_assumption_observation"),
    ("metadata.",                       "metadata_observation"),
)


#: Fields the diff service unconditionally skips (§2.4). These are "noise"
#: in the sense that they change every snapshot run by construction
#: (timestamps, ids, hash-derived markers) and dilute the LLM's signal
#: without ever carrying information the observer needs.
_SKIP_PATH_PREFIXES: tuple[str, ...] = (
    # Metadata sub-fields that change every run by design.
    "metadata.snapshot_id",
    "metadata.snapshot_date",
    "metadata.user_id",        # constant per user; irrelevant in a diff
    "metadata.plan_draft_id",  # changes when /plan re-synthesizes (own signal)
    "metadata.source_versions",  # code SHAs + replay-gap list
    # Source version sub-fields anywhere in the tree.
    "metadata.fx_as_of",
    "macro.fx_as_of",
    "portfolio.snapshot_date",
)


#: Fields with a leading underscore in any segment are skipped — convention
#: for internal-only diagnostic fields that shouldn't reach the LLM.
def _is_internal_path(path: str) -> bool:
    return any(seg.startswith("_") for seg in path.split("."))


def _is_skipped_path(path: str) -> bool:
    """True if this path should be filtered out unconditionally (§2.4)."""
    if _is_internal_path(path):
        return True
    for prefix in _SKIP_PATH_PREFIXES:
        if path == prefix or path.startswith(prefix + "."):
            return True
    # Suffix-based noisy-timestamp skip: any *_at field is a creation /
    # modification timestamp that changes by construction.
    last = path.rsplit(".", 1)[-1]
    if last.endswith("_at"):
        return True
    return False


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

DeviationKind = Literal[
    "numeric_pct",
    "numeric_abs",
    "categorical_change",
    "appeared",
    "disappeared",
]


BaselineLabel = Literal["plan", "prior_snapshot"]


@dataclass(frozen=True)
class FieldDiff:
    """One field-level deviation between two snapshot dicts.

    Attributes:
      path:            dotted-path to the field (e.g. ``"macro.fx_usd_nis_spot"``).
                       List elements use ``[i]`` syntax (e.g.
                       ``"portfolio.positions[0].ticker"``).
      current_value:   value from the current snapshot.
      baseline_value:  value from the baseline (plan inputs or prior snapshot).
      deviation_kind:  one of the :data:`DeviationKind` literals.
      magnitude:       signed magnitude of the deviation.
                         - ``numeric_pct``: ``(current - baseline) / baseline``.
                           A 22% drop (3.6 -> 2.8) produces -0.222.
                         - ``numeric_abs``: ``current - baseline`` (used when
                           baseline is zero / near-zero).
                         - others: ``None``.
      baseline_label:  ``"plan"`` for vs-plan rows, ``"prior_snapshot"`` for
                       vs-prior rows. Lets a merged FullDiff round-trip.
    """

    path: str
    current_value: Any
    baseline_value: Any
    deviation_kind: DeviationKind
    magnitude: float | None
    baseline_label: BaselineLabel = "plan"


@dataclass(frozen=True)
class FullDiff:
    """Result of :func:`compute_full_diff` — both diff directions plus
    truncation markers.

    Attributes:
      vs_plan:           FieldDiff rows for the current-vs-plan comparison.
      vs_prior:          FieldDiff rows for the current-vs-prior comparison.
      vs_plan_truncated: True iff the vs_plan list was capped at MAX_FIELDS_PER_DIFF.
      vs_prior_truncated: True iff the vs_prior list was capped.
    """

    vs_plan: list[FieldDiff] = field(default_factory=list)
    vs_prior: list[FieldDiff] = field(default_factory=list)
    vs_plan_truncated: bool = False
    vs_prior_truncated: bool = False


# ---------------------------------------------------------------------------
# Helpers — dict walking + path resolution
# ---------------------------------------------------------------------------

_NUMERIC_TYPES = (int, float)


def _is_numeric(value: Any) -> bool:
    """True iff value is a numeric scalar (excludes bool — Python's bool
    is an int subclass but ``True == 1`` masquerading as a number is a
    common source of false positives)."""
    if isinstance(value, bool):
        return False
    return isinstance(value, _NUMERIC_TYPES)


def _is_near_zero(value: float, eps: float = 1e-9) -> bool:
    return abs(value) < eps


def _walk_dict(d: dict, prefix: str = "") -> list[tuple[str, Any]]:
    """Flatten a nested dict to a list of (path, leaf_value) tuples.

    Lists of dicts are expanded as ``prefix[i].subfield``; lists of
    scalars are kept as a single leaf (the whole list is the value).
    """
    out: list[tuple[str, Any]] = []
    if not isinstance(d, dict):
        out.append((prefix, d))
        return out

    for key, value in d.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.extend(_walk_dict(value, path))
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            # list of dicts -> expand each row.
            for idx, row in enumerate(value):
                if isinstance(row, dict):
                    out.extend(_walk_dict(row, f"{path}[{idx}]"))
                else:
                    out.append((f"{path}[{idx}]", row))
        else:
            out.append((path, value))
    return out


def _dict_lookup(state: dict, path: str) -> tuple[bool, Any]:
    """Look up ``path`` in ``state``. Returns ``(found, value)``.

    Supports ``[i]`` syntax for list indices and ``[]`` syntax meaning
    "match all rows" (returns a list of values across rows).
    """
    if "[]" in path:
        # Bracket-wildcard mode — caller wants per-row resolution; we
        # don't materialise that here, return found=False so the wildcard
        # is handled by the per-row walker.
        return (False, None)

    cur: Any = state
    # Split on dots but respect [i] bracket suffixes on segments.
    for raw_seg in path.split("."):
        # Segment may have form ``name`` or ``name[i]``.
        seg = raw_seg
        index: int | None = None
        if "[" in seg and seg.endswith("]"):
            name, idx_str = seg.split("[", 1)
            idx_str = idx_str[:-1]
            try:
                index = int(idx_str)
            except ValueError:
                return (False, None)
            seg = name
        if not isinstance(cur, dict) or seg not in cur:
            return (False, None)
        cur = cur[seg]
        if index is not None:
            if not isinstance(cur, list) or index >= len(cur):
                return (False, None)
            cur = cur[index]
    return (True, cur)


def _resolve_list_field(
    state: dict,
    template: str,
) -> list[tuple[str, Any]]:
    """Resolve a ``foo.bar[].baz`` template against state, expanding the
    bracket-wildcard into one (concrete_path, value) tuple per row.

    Used by the cross-section vs-plan comparator (§2.3) so a single
    map entry like ``portfolio.allocations[].current_pct`` produces one
    diff row per allocation entry.
    """
    if "[]" not in template:
        found, value = _dict_lookup(state, template)
        return [(template, value)] if found else []

    head, tail = template.split("[]", 1)
    # head ends without a trailing dot (e.g. "portfolio.allocations").
    found_head, list_val = _dict_lookup(state, head)
    if not found_head or not isinstance(list_val, list):
        return []

    out: list[tuple[str, Any]] = []
    # tail typically starts with `.` (e.g. ".current_pct").
    tail_path = tail.lstrip(".")
    for idx, row in enumerate(list_val):
        if not isinstance(row, dict):
            continue
        if tail_path:
            found_leaf, leaf = _dict_lookup(row, tail_path)
            if not found_leaf:
                continue
            concrete = f"{head}[{idx}].{tail_path}"
            out.append((concrete, leaf))
        else:
            out.append((f"{head}[{idx}]", row))
    return out


# ---------------------------------------------------------------------------
# Filter logic
# ---------------------------------------------------------------------------

def _suffix_magnitude_floor(path: str) -> float | None:
    """Return the absolute-deviation floor for ``path`` based on the last
    segment's suffix (e.g. ``*_pct`` -> 0.005). Returns None when no
    suffix matches."""
    last = path.rsplit(".", 1)[-1].rsplit("[", 1)[0]
    for suffix, floor in _MAGNITUDE_FLOORS_BY_SUFFIX.items():
        if last.endswith(suffix):
            return floor
    return None


def _passes_filter(
    diff: FieldDiff,
    threshold_pct: float = DEFAULT_NUMERIC_THRESHOLD_PCT,
) -> bool:
    """True iff this diff should survive the §2.4 filter.

    Rules:
      - categorical_change / appeared / disappeared: always keep
        (rare events, high signal).
      - allowlist (plan-anchored fields): always keep regardless of size.
      - numeric_pct: keep if |magnitude| >= threshold_pct OR absolute
        delta exceeds the per-suffix magnitude floor.
      - numeric_abs: keep if |magnitude| >= per-suffix floor (no pct
        meaningful when baseline is zero).
    """
    if diff.deviation_kind in ("categorical_change", "appeared", "disappeared"):
        return True

    if _is_in_allowlist(diff.path):
        return True

    if diff.magnitude is None:
        # Should not happen for numeric kinds, but be defensive.
        return False

    if diff.deviation_kind == "numeric_pct":
        if abs(diff.magnitude) >= threshold_pct:
            return True
        # Below the pct threshold — fall through to the floor check.
        floor = _suffix_magnitude_floor(diff.path)
        if floor is not None:
            try:
                cur = float(diff.current_value)
                base = float(diff.baseline_value)
            except (TypeError, ValueError):
                return False
            if abs(cur - base) >= floor:
                return True
        return False

    if diff.deviation_kind == "numeric_abs":
        floor = _suffix_magnitude_floor(diff.path)
        if floor is not None:
            return abs(diff.magnitude) >= floor
        # No floor for this suffix — keep numeric_abs rows by default,
        # since reaching numeric_abs already means "baseline was zero
        # and current is non-zero" which is usually material.
        return True

    return True


def _is_in_allowlist(path: str) -> bool:
    """True iff this path is plan-anchored per :data:`ALWAYS_INCLUDE_ALLOWLIST`.

    The allowlist contains templates like ``portfolio.allocations[].current_pct``;
    we match concrete paths like ``portfolio.allocations[2].current_pct`` by
    expanding the template prefix.
    """
    if path in ALWAYS_INCLUDE_ALLOWLIST:
        return True
    for tmpl in ALWAYS_INCLUDE_ALLOWLIST:
        if "[]" not in tmpl:
            continue
        head, tail = tmpl.split("[]", 1)
        if not path.startswith(head + "["):
            continue
        # path tail after the closing ] must match the template tail.
        try:
            close = path.index("]", len(head))
        except ValueError:
            continue
        path_tail = path[close + 1:]
        if path_tail == tail:
            return True
    return False


# ---------------------------------------------------------------------------
# Core diff
# ---------------------------------------------------------------------------

def compute_diff(
    current: dict,
    baseline: dict,
    *,
    baseline_label: BaselineLabel = "plan",
    threshold_pct: float = DEFAULT_NUMERIC_THRESHOLD_PCT,
) -> list[FieldDiff]:
    """Compute per-field deviations between ``current`` and ``baseline``.

    Same-section walk (no cross-section pairing — that's
    :func:`compute_full_diff`'s job via :data:`PLAN_BASELINE_COMPARATOR_MAP`).

    Returns FieldDiff rows that pass the §2.4 filter, ordered by:
      1. appeared / disappeared / categorical_change first (always-keep).
      2. allowlist plan-anchored rows next.
      3. remaining numeric rows by ``abs(magnitude)`` descending.

    Numeric semantics:
      - both numeric, baseline non-zero -> ``numeric_pct``, magnitude is
        signed ``(current - baseline) / baseline``.
      - both numeric, baseline near-zero -> ``numeric_abs``, magnitude is
        signed ``current - baseline``.
      - one numeric / one non-numeric -> ``categorical_change``.
      - non-numeric and unequal -> ``categorical_change``, magnitude None.
      - missing in current -> ``disappeared`` row, magnitude None.
      - missing in baseline -> ``appeared`` row, magnitude None.
      - equal values -> no row.
    """
    current_flat = dict(_walk_dict(current))
    baseline_flat = dict(_walk_dict(baseline))

    paths = set(current_flat) | set(baseline_flat)
    out: list[FieldDiff] = []

    for path in paths:
        if _is_skipped_path(path):
            continue
        in_current = path in current_flat
        in_baseline = path in baseline_flat

        if in_current and not in_baseline:
            out.append(FieldDiff(
                path=path,
                current_value=current_flat[path],
                baseline_value=None,
                deviation_kind="appeared",
                magnitude=None,
                baseline_label=baseline_label,
            ))
            continue
        if in_baseline and not in_current:
            out.append(FieldDiff(
                path=path,
                current_value=None,
                baseline_value=baseline_flat[path],
                deviation_kind="disappeared",
                magnitude=None,
                baseline_label=baseline_label,
            ))
            continue

        cur_v = current_flat[path]
        base_v = baseline_flat[path]
        if cur_v == base_v:
            continue

        # Numeric vs numeric.
        if _is_numeric(cur_v) and _is_numeric(base_v):
            base_f = float(base_v)
            cur_f = float(cur_v)
            if _is_near_zero(base_f):
                if _is_near_zero(cur_f):
                    continue  # both zero -> no diff
                out.append(FieldDiff(
                    path=path,
                    current_value=cur_v,
                    baseline_value=base_v,
                    deviation_kind="numeric_abs",
                    magnitude=cur_f - base_f,
                    baseline_label=baseline_label,
                ))
            else:
                out.append(FieldDiff(
                    path=path,
                    current_value=cur_v,
                    baseline_value=base_v,
                    deviation_kind="numeric_pct",
                    magnitude=(cur_f - base_f) / base_f,
                    baseline_label=baseline_label,
                ))
            continue

        # Type mismatch or non-numeric inequality -> categorical change.
        out.append(FieldDiff(
            path=path,
            current_value=cur_v,
            baseline_value=base_v,
            deviation_kind="categorical_change",
            magnitude=None,
            baseline_label=baseline_label,
        ))

    # Apply §2.4 filter.
    filtered = [d for d in out if _passes_filter(d, threshold_pct=threshold_pct)]
    return _sort_diffs(filtered)


def _sort_diffs(diffs: list[FieldDiff]) -> list[FieldDiff]:
    """Stable sort: always-keep (categorical/appeared/disappeared) first,
    then allowlist, then numeric rows by descending |magnitude|.

    Within each tier, order is by path (stable / deterministic across runs).
    """
    def tier(d: FieldDiff) -> int:
        if d.deviation_kind in ("appeared", "disappeared", "categorical_change"):
            return 0
        if _is_in_allowlist(d.path):
            return 1
        return 2

    def key(d: FieldDiff) -> tuple[int, float, str]:
        t = tier(d)
        mag = -abs(d.magnitude) if d.magnitude is not None else 0.0
        return (t, mag, d.path)

    return sorted(diffs, key=key)


# ---------------------------------------------------------------------------
# Full diff (cross-section comparator + truncation)
# ---------------------------------------------------------------------------

def _compute_vs_plan_diff(
    current: dict,
    plan_baseline: dict,
    *,
    threshold_pct: float,
) -> list[FieldDiff]:
    """Apply :data:`PLAN_BASELINE_COMPARATOR_MAP` to pair current-state
    fields against plan-input fields, then build FieldDiff rows.

    Cross-section pairing is the bit a naive recursive diff would miss —
    the FX live spot (``macro.fx_usd_nis_spot``) doesn't live next to
    ``plan_inputs.assumed_fx_usd_nis``, so a same-section walk never
    sees them as a pair. This routine resolves both sides through the
    map and synthesises a single FieldDiff row.
    """
    out: list[FieldDiff] = []
    for current_tmpl, baseline_tmpl in PLAN_BASELINE_COMPARATOR_MAP.items():
        current_rows = _resolve_list_field(current, current_tmpl)
        baseline_rows = _resolve_list_field(plan_baseline, baseline_tmpl)

        if "[]" not in current_tmpl:
            # Scalar-to-scalar pairing.
            if not current_rows or not baseline_rows:
                continue
            (cur_path, cur_v) = current_rows[0]
            (_, base_v) = baseline_rows[0]
            row = _make_pair_diff(cur_path, cur_v, base_v, "plan")
            if row is not None:
                out.append(row)
            continue

        # List-template pairing.
        if "[]" in baseline_tmpl:
            # Both sides are lists; pair by index.
            for idx, (cur_path, cur_v) in enumerate(current_rows):
                if idx >= len(baseline_rows):
                    out.append(FieldDiff(
                        path=cur_path,
                        current_value=cur_v,
                        baseline_value=None,
                        deviation_kind="appeared",
                        magnitude=None,
                        baseline_label="plan",
                    ))
                    continue
                (_, base_v) = baseline_rows[idx]
                row = _make_pair_diff(cur_path, cur_v, base_v, "plan")
                if row is not None:
                    out.append(row)
            # Surplus baseline rows -> disappeared.
            for idx in range(len(current_rows), len(baseline_rows)):
                (base_path, base_v) = baseline_rows[idx]
                out.append(FieldDiff(
                    path=base_path,
                    current_value=None,
                    baseline_value=base_v,
                    deviation_kind="disappeared",
                    magnitude=None,
                    baseline_label="plan",
                ))
        else:
            # Current is a list but baseline is a scalar — every current
            # row compares against the single baseline scalar.
            if not baseline_rows:
                continue
            (_, base_v) = baseline_rows[0]
            for cur_path, cur_v in current_rows:
                row = _make_pair_diff(cur_path, cur_v, base_v, "plan")
                if row is not None:
                    out.append(row)

    # Filter + sort.
    filtered = [d for d in out if _passes_filter(d, threshold_pct=threshold_pct)]
    return _sort_diffs(filtered)


def _make_pair_diff(
    path: str,
    current_value: Any,
    baseline_value: Any,
    baseline_label: BaselineLabel,
) -> FieldDiff | None:
    """Build a FieldDiff for a pair where the path is already known.

    Returns None for equal values (no deviation to report).
    """
    if current_value == baseline_value:
        return None

    if current_value is None and baseline_value is not None:
        return FieldDiff(
            path=path,
            current_value=None,
            baseline_value=baseline_value,
            deviation_kind="disappeared",
            magnitude=None,
            baseline_label=baseline_label,
        )
    if baseline_value is None and current_value is not None:
        return FieldDiff(
            path=path,
            current_value=current_value,
            baseline_value=None,
            deviation_kind="appeared",
            magnitude=None,
            baseline_label=baseline_label,
        )

    if _is_numeric(current_value) and _is_numeric(baseline_value):
        cur_f = float(current_value)
        base_f = float(baseline_value)
        if _is_near_zero(base_f):
            return FieldDiff(
                path=path,
                current_value=current_value,
                baseline_value=baseline_value,
                deviation_kind="numeric_abs",
                magnitude=cur_f - base_f,
                baseline_label=baseline_label,
            )
        return FieldDiff(
            path=path,
            current_value=current_value,
            baseline_value=baseline_value,
            deviation_kind="numeric_pct",
            magnitude=(cur_f - base_f) / base_f,
            baseline_label=baseline_label,
        )

    return FieldDiff(
        path=path,
        current_value=current_value,
        baseline_value=baseline_value,
        deviation_kind="categorical_change",
        magnitude=None,
        baseline_label=baseline_label,
    )


def _merge_and_truncate(
    diffs: list[FieldDiff],
    cap: int,
) -> tuple[list[FieldDiff], bool]:
    """Apply §2.5 truncation: keep all categorical/appeared/disappeared
    and allowlist rows; fill remaining slots with numeric rows sorted by
    descending |magnitude|. Returns (final_list, was_truncated)."""
    if len(diffs) <= cap:
        return (diffs, False)

    keep_unconditional: list[FieldDiff] = []
    numeric_candidates: list[FieldDiff] = []

    for d in diffs:
        if d.deviation_kind in ("appeared", "disappeared", "categorical_change"):
            keep_unconditional.append(d)
        elif _is_in_allowlist(d.path):
            keep_unconditional.append(d)
        else:
            numeric_candidates.append(d)

    # If the unconditional set already exceeds the cap, we keep ALL of
    # them (data integrity beats the cap; the cap is a soft budget for
    # the LLM's token window). Log via the truncated marker.
    if len(keep_unconditional) >= cap:
        return (_sort_diffs(keep_unconditional), True)

    remaining_slots = cap - len(keep_unconditional)
    # Numeric candidates sorted by |magnitude| desc (largest deviations win).
    numeric_sorted = sorted(
        numeric_candidates,
        key=lambda d: -abs(d.magnitude or 0.0),
    )
    kept_numeric = numeric_sorted[:remaining_slots]
    final = _sort_diffs(keep_unconditional + kept_numeric)
    return (final, True)


def compute_full_diff(
    current: dict,
    plan_baseline: dict | None,
    prior_snapshot: dict | None,
    *,
    threshold_pct: float = DEFAULT_NUMERIC_THRESHOLD_PCT,
    max_fields_per_side: int = MAX_FIELDS_PER_DIFF,
) -> FullDiff:
    """Compute both the vs-plan and vs-prior diffs in one shot.

    Args:
      current:        the current snapshot.state dict (six-section shape).
      plan_baseline:  the plan_inputs baseline as a six-section-shaped
                      dict (typically ``{"plan_inputs": ..., "portfolio":
                      {"allocations": [...]}}``). Pass None when no
                      active plan exists (vs_plan returned empty).
      prior_snapshot: the immediately-prior snapshot.state dict. Pass
                      None when no prior snapshot exists (vs_prior
                      returned empty — the observer sees the empty list
                      as "no movement signal", not as "nothing moved").
      threshold_pct:  numeric filter threshold (§2.4); defaults to 0.02.
      max_fields_per_side: §2.5 truncation cap. Defaults to
                      :data:`MAX_FIELDS_PER_DIFF` (300).
    """
    vs_plan: list[FieldDiff] = []
    if plan_baseline is not None:
        vs_plan = _compute_vs_plan_diff(
            current,
            plan_baseline,
            threshold_pct=threshold_pct,
        )

    vs_prior: list[FieldDiff] = []
    if prior_snapshot is not None:
        vs_prior = compute_diff(
            current,
            prior_snapshot,
            baseline_label="prior_snapshot",
            threshold_pct=threshold_pct,
        )

    vs_plan_final, vs_plan_trunc = _merge_and_truncate(vs_plan, max_fields_per_side)
    vs_prior_final, vs_prior_trunc = _merge_and_truncate(vs_prior, max_fields_per_side)

    return FullDiff(
        vs_plan=vs_plan_final,
        vs_prior=vs_prior_final,
        vs_plan_truncated=vs_plan_trunc,
        vs_prior_truncated=vs_prior_trunc,
    )


# ---------------------------------------------------------------------------
# Deviation bucket
# ---------------------------------------------------------------------------

def compute_deviation_bucket(magnitude: float | None, deviation_kind: str) -> str:
    """Deterministic bucket label from a numeric magnitude (the brief's
    contract for commit #6's dedup-key formula).

    Bucket scheme (brief):
      - ``<5pct``      : 0 <= |magnitude| < 0.05
      - ``5to15pct``   : 0.05 <= |magnitude| < 0.15
      - ``15to30pct``  : 0.15 <= |magnitude| < 0.30
      - ``>30pct``     : |magnitude| >= 0.30

    For non-numeric kinds (categorical_change / appeared / disappeared)
    the bucket is ``"categorical"`` — a stable partition that has its
    own dedup band (the flag either fires or it doesn't; there's no
    "smoothly graduates past a band threshold" failure mode here).

    Monotonicity contract (tested): m1 <= m2 implies the bucket index
    of m1 is <= bucket index of m2. The function never returns different
    buckets for the same magnitude (no randomness, no jitter).
    """
    if deviation_kind in ("categorical_change", "appeared", "disappeared"):
        return "categorical"

    if magnitude is None:
        # Defensive: numeric kind but no magnitude is a contract bug.
        return "categorical"

    abs_m = abs(magnitude)
    if abs_m < 0.05:
        return "<5pct"
    if abs_m < 0.15:
        return "5to15pct"
    if abs_m < 0.30:
        return "15to30pct"
    return ">30pct"


# Bucket ordering for monotonicity tests (commit #3 CI invariant + the
# flag-writer in commit #6 reads this to know which way the band steps).
DEVIATION_BUCKET_ORDER: tuple[str, ...] = (
    "<5pct",
    "5to15pct",
    "15to30pct",
    ">30pct",
)


__all__ = [
    "FieldDiff",
    "FullDiff",
    "DeviationKind",
    "BaselineLabel",
    "PLAN_BASELINE_COMPARATOR_MAP",
    "ALWAYS_INCLUDE_ALLOWLIST",
    "MAX_FIELDS_PER_DIFF",
    "DEFAULT_NUMERIC_THRESHOLD_PCT",
    "DEVIATION_BUCKET_ORDER",
    "SNAPSHOT_FIELD_PREFIXES",
    "compute_diff",
    "compute_full_diff",
    "compute_deviation_bucket",
]
