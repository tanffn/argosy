"""Life-events service: CRUD + Pydantic enum validator (loud-error).

Sprint commit #8 of the plan/execute/monitor reorg (spec cbf6a07 §4)
laid down the original loud-error contract.  Spec D commit #4 extends
the service to model **cashflow phases** — a life event now describes
HOW THE USER'S CASHFLOW CHANGES, not when retirement is constrained.
See ``docs/superpowers/specs/2026-05-29-life-events-cashflow-redesign-design.md``
§§1, 2.0, 3, 4 for the canonical contract.

Five-value ``delta_kind`` discriminator (spec §1.1, schema landed in
migration 0054):

  * ``one_shot``                  — single spike on ``target_date``
                                    (which doubles as the one_shot
                                    date — migration 0054 reused the
                                    ``target_date`` column rather than
                                    introducing a separate
                                    ``one_shot_date``).
  * ``recurring_every_n_years``   — periodic spike, ``target_date``
                                    used as the anchor, every
                                    ``recurring_period_years`` years.
  * ``phase_change_start``        — step function starting at
                                    ``phase_start_date`` onward
                                    (open-ended; ``phase_end_date``
                                    must be NULL).
  * ``phase_change_end``          — step function bounded by
                                    ``phase_start_date`` and
                                    ``phase_end_date``.
  * ``none``                      — no cashflow effect; row is
                                    display-only (timeline marker + a
                                    replan-trigger source, but
                                    ``apply_life_event_deltas`` skips
                                    it).

**Loud-error contract (codex BLOCKER on spec #1 §4.1)** — preserved
verbatim: the form service refuses to silently best-effort parse
out-of-category or out-of-shape input.  Pydantic enum + cross-field
validation is the gate; the route returns 422 with a structured
``{error, ...}`` body the UI red-banner consumes.  Spec D adds a new
422 variant ``delta_shape_invalid`` for the discriminator validation
(spec §3.3).

Backwards compatibility — legacy reads:
The DTO (``LifeEventDTO``) serializes BOTH the new per-shape columns
AND the original legacy columns (``target_date`` / ``amount_usd`` /
``recurring_years``) so callers that haven't migrated to the new shape
keep working until commit #7 drops the legacy columns from the schema.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from argosy.state.models import LifeEvent


# ---------------------------------------------------------------------------
# Enum schemas — DB-level CHECK enforces `category`; Pydantic enforces
# `kind` per category. Keeping the two layers in sync is verified by a
# unit test that diffs the enum literals against the DB CHECK clause.
# ---------------------------------------------------------------------------


class LifeEventCategory(str, Enum):
    career = "career_event"
    family = "family_event"
    asset = "asset_event"
    expense = "expense_event"
    recurring = "recurring_expense"
    retirement = "retirement_milestone"


class CareerEventKind(str, Enum):
    job_change = "job_change"
    layoff = "layoff"
    retirement = "retirement"
    promotion = "promotion"


class FamilyEventKind(str, Enum):
    marriage = "marriage"
    divorce = "divorce"
    birth = "birth"
    dependent_leaves = "dependent_leaves"
    health_event = "health_event"


class AssetEventKind(str, Enum):
    home_purchase = "home_purchase"
    home_sale = "home_sale"
    inheritance = "inheritance"
    other_asset_acquired = "other_asset_acquired"


class ExpenseEventKind(str, Enum):
    college = "college"
    medical_major = "medical_major"
    one_time_large = "one_time_large"


class RecurringExpenseKind(str, Enum):
    new_car = "new_car"
    major_renovation = "major_renovation"
    family_travel = "family_travel"


class RetirementMilestoneKind(str, Enum):
    target_retire_year_change = "target_retire_year_change"
    sigma_calibration = "sigma_calibration"
    annuity_decision = "annuity_decision"
    withdrawal_policy_change = "withdrawal_policy_change"


# Map category → allowed kinds. Used by the request validator to
# refuse out-of-category kinds with a 422.
KIND_ENUM_BY_CATEGORY: dict[LifeEventCategory, type[Enum]] = {
    LifeEventCategory.career: CareerEventKind,
    LifeEventCategory.family: FamilyEventKind,
    LifeEventCategory.asset: AssetEventKind,
    LifeEventCategory.expense: ExpenseEventKind,
    LifeEventCategory.recurring: RecurringExpenseKind,
    LifeEventCategory.retirement: RetirementMilestoneKind,
}


def valid_kinds_for(category: LifeEventCategory) -> list[str]:
    """Used by the 422 error body to tell the UI which kinds it
    should have offered."""
    return [k.value for k in KIND_ENUM_BY_CATEGORY[category]]


# ---------------------------------------------------------------------------
# Delta-kind enum + per-shape field catalogs (Spec D commit #4)
# ---------------------------------------------------------------------------


class DeltaKind(str, Enum):
    """Five cashflow-shape values landed by migration 0054.

    Spec D §1.1 / Appendix A.  Order matches the migration's
    ``_VALID_DELTA_KINDS`` tuple.
    """

    one_shot = "one_shot"
    recurring_every_n_years = "recurring_every_n_years"
    phase_change_start = "phase_change_start"
    phase_change_end = "phase_change_end"
    none = "none"


# Per-delta_kind field rules — drives both server-side validation AND the
# server-driven UI catalog (spec §3.2, §4.2).  Each entry declares
# `required` and `forbidden` field name lists; any field not in either
# list is optional.  Note the date-column reuse: `one_shot` and
# `recurring_every_n_years` both use ``target_date`` (the migration
# deliberately did NOT add separate ``one_shot_date`` /
# ``recurring_anchor_date`` columns — see migration 0054 docstring).
REQUIRED_FIELDS_BY_DELTA_KIND: dict[DeltaKind, list[str]] = {
    DeltaKind.one_shot: ["target_date", "one_shot_amount_usd"],
    DeltaKind.recurring_every_n_years: [
        "target_date",
        "recurring_amount_usd",
        "recurring_period_years",
    ],
    DeltaKind.phase_change_start: [
        "phase_start_date",
        "monthly_delta_usd",
    ],
    DeltaKind.phase_change_end: [
        "phase_start_date",
        "phase_end_date",
        "monthly_delta_usd",
    ],
    DeltaKind.none: [],
}


# Fields that the schema KNOWS about as per-shape inputs.  Used to derive
# the forbidden-field set per delta_kind (anything in this universe that
# isn't required for the chosen shape is forbidden — keeps the wire
# contract crisp and surfaces "I sent monthly_delta_usd to a one_shot
# event" as a loud 422 instead of a silent ignore).
_ALL_PER_SHAPE_FIELDS: tuple[str, ...] = (
    "target_date",
    "one_shot_amount_usd",
    "recurring_amount_usd",
    "recurring_period_years",
    "monthly_delta_usd",
    "phase_start_date",
    "phase_end_date",
)


def forbidden_fields_for(delta_kind: DeltaKind) -> list[str]:
    """Per-shape fields the wire payload MUST NOT carry for this
    ``delta_kind`` (computed as universe minus required)."""
    required = set(REQUIRED_FIELDS_BY_DELTA_KIND[delta_kind])
    return [f for f in _ALL_PER_SHAPE_FIELDS if f not in required]


# Per-category × delta_kind interaction matrix from spec §1.4.  Drives
# the UI's dependent-dropdown behavior (when category changes, the
# available delta_kinds change) and is enforced server-side: a
# ``recurring_expense`` row with ``delta_kind=one_shot`` is refused with
# a structured 422 (``delta_kind_not_valid_for_category``).
DELTA_KIND_RULES_BY_CATEGORY: dict[LifeEventCategory, dict[str, Any]] = {
    LifeEventCategory.career: {
        "allowed_delta_kinds": [
            DeltaKind.phase_change_start.value,
            DeltaKind.phase_change_end.value,
            DeltaKind.one_shot.value,
            DeltaKind.none.value,
        ],
        "default_delta_kind": DeltaKind.none.value,
        "nudge": (
            "Did your income change? Pick phase_change_start. "
            "Otherwise leave as none."
        ),
    },
    LifeEventCategory.family: {
        "allowed_delta_kinds": [
            DeltaKind.one_shot.value,
            DeltaKind.phase_change_start.value,
            DeltaKind.phase_change_end.value,
            DeltaKind.none.value,
        ],
        "default_delta_kind": DeltaKind.none.value,
        "nudge": (
            "Big gift? one_shot. Lifestyle shift (kids leave home)? "
            "phase_change_start."
        ),
    },
    LifeEventCategory.asset: {
        "allowed_delta_kinds": [
            DeltaKind.one_shot.value,
            DeltaKind.none.value,
        ],
        "default_delta_kind": DeltaKind.one_shot.value,
        "nudge": (
            "Home purchase / RSU vest / inheritance — one_shot."
        ),
    },
    LifeEventCategory.expense: {
        "allowed_delta_kinds": [
            DeltaKind.one_shot.value,
            DeltaKind.phase_change_end.value,
        ],
        "default_delta_kind": DeltaKind.one_shot.value,
        "nudge": (
            "Major medical, college year — one_shot per year or "
            "phase_change_end with end date."
        ),
    },
    LifeEventCategory.recurring: {
        "allowed_delta_kinds": [
            DeltaKind.recurring_every_n_years.value,
        ],
        "default_delta_kind": DeltaKind.recurring_every_n_years.value,
        "nudge": (
            "New car / renovation / family travel — pick the period."
        ),
    },
    LifeEventCategory.retirement: {
        "allowed_delta_kinds": [
            DeltaKind.none.value,
            DeltaKind.phase_change_start.value,
        ],
        "default_delta_kind": DeltaKind.none.value,
        "nudge": (
            "Sigma / annuity / policy decisions — none. Target "
            "retire-year change — phase_change_start only if the "
            "date matters to other consumers."
        ),
    },
}


# Per codex IMPORTANT on commit #8 review: the route used to discriminate
# kind-vs-other Pydantic errors via substring match on the message
# string. That breaks if Pydantic ever changes its message format. A
# typed exception is the stable contract: service validator raises this
# specific class; route catches it explicitly.
class InvalidKindForCategoryError(ValueError):
    """Raised by LifeEventCreateRequest's per-category-kind validator.

    Carries the category + invalid kind + the list of kinds the form
    SHOULD have offered, so the route can serialize a structured 422
    body for the UI banner without re-deriving anything.
    """
    def __init__(self, *, category: str, kind: str, valid_kinds: list[str]):
        self.category = category
        self.kind = kind
        self.valid_kinds = valid_kinds
        super().__init__(
            f"kind={kind!r} is not valid for category={category!r}. "
            f"Valid kinds: {valid_kinds}"
        )


class InvalidDeltaShapeError(ValueError):
    """Raised when ``delta_kind`` is not consistent with the per-shape
    fields the payload carries.

    Two sub-cases share the class (the route disambiguates via the
    ``reason`` attribute):

      * ``reason='missing_required'``  — the wire payload omitted a
        field required by the chosen ``delta_kind`` (e.g.
        ``one_shot`` without ``one_shot_amount_usd``).
      * ``reason='forbidden_present'`` — the wire payload sent a field
        that this ``delta_kind`` forbids (e.g. ``one_shot`` with
        ``monthly_delta_usd``).

    The route maps this to a 422 with body::

        {"error": "delta_shape_invalid",
         "delta_kind": "one_shot",
         "reason": "missing_required",
         "missing_fields": ["one_shot_amount_usd"],
         "forbidden_fields": []}

    Spec §3.1 + §3.3.  Codex BLOCKER #3 from the design doc requires the
    validation be loud — missing or surplus shape fields cannot be
    silently coerced.
    """

    def __init__(
        self,
        *,
        delta_kind: str,
        reason: Literal["missing_required", "forbidden_present"],
        missing_fields: list[str] | None = None,
        forbidden_fields: list[str] | None = None,
    ) -> None:
        self.delta_kind = delta_kind
        self.reason = reason
        self.missing_fields = missing_fields or []
        self.forbidden_fields = forbidden_fields or []
        parts: list[str] = [
            f"delta_kind={delta_kind!r} payload is malformed",
            f"reason={reason}",
        ]
        if self.missing_fields:
            parts.append(f"missing={self.missing_fields}")
        if self.forbidden_fields:
            parts.append(f"forbidden={self.forbidden_fields}")
        super().__init__("; ".join(parts))


class InvalidDeltaKindForCategoryError(ValueError):
    """Raised when (category, delta_kind) pair violates the spec §1.4
    interaction matrix — e.g. a ``recurring_expense`` row arrives with
    ``delta_kind='one_shot'``.

    The route maps this to a 422 with body::

        {"error": "delta_kind_not_valid_for_category",
         "category": "recurring_expense",
         "delta_kind": "one_shot",
         "allowed_delta_kinds": ["recurring_every_n_years"]}
    """

    def __init__(
        self,
        *,
        category: str,
        delta_kind: str,
        allowed_delta_kinds: list[str],
    ) -> None:
        self.category = category
        self.delta_kind = delta_kind
        self.allowed_delta_kinds = allowed_delta_kinds
        super().__init__(
            f"delta_kind={delta_kind!r} is not valid for "
            f"category={category!r}. Allowed: {allowed_delta_kinds}"
        )


# Spec §4 calls out per-category field metadata so the UI can be
# server-driven (codex IMPORTANT on commit #8 review: hardcoded
# category-set checks in the UI break when a new category is added that
# also needs the amount field).  Preserved verbatim for backwards
# compatibility with the existing UI; Spec D commit #4 extends the
# catalog payload with the ``delta_kind_rules`` sub-catalog (see
# ``get_catalog`` below) without breaking these fields.
FIELD_RULES_BY_CATEGORY: dict[LifeEventCategory, dict[str, bool]] = {
    LifeEventCategory.career: {
        "requires_amount": False,
        "supports_recurring_years": False,
    },
    LifeEventCategory.family: {
        "requires_amount": False,
        "supports_recurring_years": False,
    },
    LifeEventCategory.asset: {
        "requires_amount": True,
        "supports_recurring_years": False,
    },
    LifeEventCategory.expense: {
        "requires_amount": True,
        "supports_recurring_years": False,
    },
    LifeEventCategory.recurring: {
        "requires_amount": True,
        "supports_recurring_years": True,
    },
    LifeEventCategory.retirement: {
        "requires_amount": False,
        "supports_recurring_years": False,
    },
}


# ---------------------------------------------------------------------------
# Cross-field validation helper (Spec D commit #4)
# ---------------------------------------------------------------------------


def _validate_delta_shape(
    *,
    delta_kind: DeltaKind,
    payload_fields_present: dict[str, bool],
) -> None:
    """Enforce the per-shape required / forbidden field rules from
    ``REQUIRED_FIELDS_BY_DELTA_KIND`` + ``forbidden_fields_for``.

    ``payload_fields_present`` is the truthy-presence map of the
    per-shape fields the wire payload provided.  A field with value
    ``None`` counts as ABSENT (matches FastAPI / Pydantic semantics —
    ``model_dump(exclude_none=True)`` would drop the key).  This is the
    intended contract: the wire convention is "omit the key OR send
    null"; both mean "field not provided".

    For ``phase_change_end`` we require BOTH ``phase_start_date`` AND
    ``phase_end_date`` per spec §2.2 ("self-contained row carrying
    both phases").  For ``phase_change_start`` we forbid
    ``phase_end_date`` so the open-ended semantic is unambiguous.
    Raises ``InvalidDeltaShapeError`` on either violation.
    """
    required = REQUIRED_FIELDS_BY_DELTA_KIND[delta_kind]
    forbidden = forbidden_fields_for(delta_kind)

    missing = [f for f in required if not payload_fields_present.get(f)]
    if missing:
        raise InvalidDeltaShapeError(
            delta_kind=delta_kind.value,
            reason="missing_required",
            missing_fields=missing,
        )
    present_but_forbidden = [
        f for f in forbidden if payload_fields_present.get(f)
    ]
    if present_but_forbidden:
        raise InvalidDeltaShapeError(
            delta_kind=delta_kind.value,
            reason="forbidden_present",
            forbidden_fields=present_but_forbidden,
        )


def _validate_delta_kind_for_category(
    *,
    category: LifeEventCategory,
    delta_kind: DeltaKind,
) -> None:
    """Enforce spec §1.4 interaction matrix.  Raises
    ``InvalidDeltaKindForCategoryError`` if the pair is invalid."""
    allowed = DELTA_KIND_RULES_BY_CATEGORY[category]["allowed_delta_kinds"]
    if delta_kind.value not in allowed:
        raise InvalidDeltaKindForCategoryError(
            category=category.value,
            delta_kind=delta_kind.value,
            allowed_delta_kinds=list(allowed),
        )


# ---------------------------------------------------------------------------
# Request / Response shapes
# ---------------------------------------------------------------------------


class LifeEventCreateRequest(BaseModel):
    """POST /api/life-events payload.

    Carries BOTH the legacy fields (``target_date`` / ``amount_usd`` /
    ``recurring_years``) and the new per-shape fields.  The
    ``delta_kind`` discriminator gates which per-shape fields are
    required / forbidden — see ``_validate_delta_shape``.

    Backwards-compat path: payloads that OMIT ``delta_kind`` (i.e. the
    legacy wire format from before Spec D) default to
    ``delta_kind='none'`` so existing callers continue to work — but the
    new payload SHOULD always supply ``delta_kind`` explicitly.  Legacy
    callers that send ``amount_usd`` without ``delta_kind`` will land
    the row as ``delta_kind='none'`` and the amount in the legacy
    ``amount_usd`` column; the cashflow engine treats them as no-ops
    (per ``apply_life_event_deltas``'s ``none`` handler).
    """

    user_id: str
    category: LifeEventCategory
    kind: str  # validated by model_validator against per-category enum
    description: str | None = None
    source_id: int | None = None

    # Legacy fields — preserved for backwards-compat reads / writes.
    # When ``delta_kind`` is set, the new per-shape fields take
    # precedence; the legacy fields can still ride along on the row
    # for display purposes but the cashflow engine reads ONLY the
    # per-shape columns (commit #2's apply_life_event_deltas).
    target_date: date | None = None
    amount_usd: Annotated[float, Field(gt=0)] | None = None
    recurring_years: Annotated[int, Field(gt=0)] | None = None

    # New per-shape fields (Spec D commit #4).  All optional at the
    # type level; the model_validator enforces required-vs-forbidden
    # per ``delta_kind``.
    delta_kind: DeltaKind = DeltaKind.none
    one_shot_amount_usd: float | None = None
    recurring_amount_usd: float | None = None
    recurring_period_years: Annotated[int, Field(gt=0)] | None = None
    monthly_delta_usd: float | None = None
    phase_start_date: date | None = None
    phase_end_date: date | None = None

    @model_validator(mode="after")
    def _validate_payload(self) -> "LifeEventCreateRequest":
        """Loud-error gate.  Three checks, in spec-defined order:

        1. ``kind`` ∈ the per-category enum.  Raises
           ``InvalidKindForCategoryError``.
        2. ``delta_kind`` ∈ the category's allowed list per the §1.4
           matrix.  Raises ``InvalidDeltaKindForCategoryError``.
        3. Per-shape required / forbidden fields consistent with
           ``delta_kind``.  Raises ``InvalidDeltaShapeError``.

        **Backwards-compat carve-out.**  Legacy callers (pre-Spec D)
        post payloads that OMIT ``delta_kind`` entirely and supply
        ``target_date`` / ``amount_usd`` / ``recurring_years`` in the
        legacy shape.  For those payloads the default ``delta_kind`` is
        ``none``, but we SKIP the per-shape required/forbidden check —
        the row lands with the legacy fields populated and the
        cashflow engine treats it as a no-op (``none`` handler skips
        it).  Only payloads that EXPLICITLY set ``delta_kind`` get the
        strict validation.  This preserves the existing test surface +
        UI behavior while giving new callers the loud-error contract.

        For ``phase_change_end`` we additionally enforce
        ``phase_end_date > phase_start_date`` (matches the DB CHECK in
        spec §1.1).  Raises ``InvalidDeltaShapeError`` with
        ``reason='forbidden_present'`` carrying the end-date field name
        in ``forbidden_fields`` so the UI can surface a coherent banner
        without inventing a new error variant.
        """
        # --- Check 1: kind ∈ per-category enum -----------------------
        valid = valid_kinds_for(self.category)
        if self.kind not in valid:
            raise InvalidKindForCategoryError(
                category=self.category.value,
                kind=self.kind,
                valid_kinds=valid,
            )

        # Backwards-compat carve-out: if delta_kind wasn't explicitly
        # sent, skip the strict per-shape validation and let the
        # legacy fields ride along on the row.  See docstring above.
        delta_kind_was_supplied = "delta_kind" in self.model_fields_set
        if not delta_kind_was_supplied:
            return self

        # --- Check 2: (category, delta_kind) is allowed -------------
        _validate_delta_kind_for_category(
            category=self.category,
            delta_kind=self.delta_kind,
        )

        # --- Check 3: per-shape required / forbidden fields ----------
        # Field-presence map: a field with value ``None`` is treated as
        # ABSENT (spec contract).  ``target_date`` is in the legacy
        # column set BUT also serves as the one_shot / recurring anchor
        # date, so its presence is included in the map.
        present = {
            "target_date": self.target_date is not None,
            "one_shot_amount_usd": self.one_shot_amount_usd is not None,
            "recurring_amount_usd": self.recurring_amount_usd is not None,
            "recurring_period_years": (
                self.recurring_period_years is not None
            ),
            "monthly_delta_usd": self.monthly_delta_usd is not None,
            "phase_start_date": self.phase_start_date is not None,
            "phase_end_date": self.phase_end_date is not None,
        }
        _validate_delta_shape(
            delta_kind=self.delta_kind,
            payload_fields_present=present,
        )

        # --- Check 3b: phase_change_end end > start ------------------
        if (
            self.delta_kind == DeltaKind.phase_change_end
            and self.phase_start_date is not None
            and self.phase_end_date is not None
            and self.phase_end_date <= self.phase_start_date
        ):
            # Surface as a shape error with a synthetic
            # ``forbidden_fields=["phase_end_date"]`` so the UI knows
            # which input to highlight; reason is forbidden_present
            # (the user sent an end date that doesn't satisfy the
            # ordering invariant — the *value* is forbidden, not the
            # field itself).
            raise InvalidDeltaShapeError(
                delta_kind=self.delta_kind.value,
                reason="forbidden_present",
                forbidden_fields=["phase_end_date"],
            )

        return self


class LifeEventUpdateRequest(BaseModel):
    """Update payload — only sends the user_id_owner check + the
    fields to change.  Re-validates kind/category if either is sent.

    Update validation contract:
      * If ``category`` and/or ``kind`` are sent, re-validate the pair
        (existing behavior).
      * If ``category`` is changed, re-validate the row's existing
        ``delta_kind`` against the new category's allowed list.
      * If ``delta_kind`` is sent, it MUST equal the row's existing
        ``delta_kind``.  Cross-shape transitions are rejected with
        ``delta_shape_invalid`` / ``forbidden_present`` /
        ``forbidden_fields=['delta_kind']``.  Spec §3.1 does not
        commit to atomic cross-shape PUT in v1 (would require either
        nulling out the old per-shape fields atomically or carrying
        every required field of the new shape — both are footguns).
        Callers wanting a shape transition should DELETE + POST.
      * Forbidden-field check applies to the wire payload against the
        row's existing (and unchangeable) ``delta_kind``.  Update is
        a partial-state edit; we cannot enforce "required" because
        the missing required field may already exist on the row.
      * ``phase_change_end`` end>start invariant is re-checked on PUT
        against the EFFECTIVE phase dates (payload override > row
        value).  An update that only edits one of the two dates still
        422s if the result would violate the ordering.

    Rationale for the in-place-edit contract: an UPDATE that only
    changes ``description`` should not have to re-send every required
    per-shape field.  But if the update sends ``one_shot_amount_usd``
    while the row's ``delta_kind`` is ``phase_change_start``, that's
    a forbidden field and must 422 — same loud-error contract as
    create.
    """

    user_id: str
    category: LifeEventCategory | None = None
    kind: str | None = None
    target_date: date | None = None
    amount_usd: Annotated[float, Field(gt=0)] | None = None
    recurring_years: Annotated[int, Field(gt=0)] | None = None
    description: str | None = None
    source_id: int | None = None

    # New per-shape fields — all optional on PUT.
    delta_kind: DeltaKind | None = None
    one_shot_amount_usd: float | None = None
    recurring_amount_usd: float | None = None
    recurring_period_years: Annotated[int, Field(gt=0)] | None = None
    monthly_delta_usd: float | None = None
    phase_start_date: date | None = None
    phase_end_date: date | None = None


class LifeEventDTO(BaseModel):
    """Response payload.  Serializes BOTH the new per-shape columns AND
    the legacy columns so backwards-compat consumers keep reading the
    fields they always read.

    Spec D commit #7 (deferred) will drop the legacy columns from the
    table; until then the DTO carries both.
    """

    id: int
    user_id: str
    category: str
    kind: str

    # Legacy columns — preserved.
    target_date: date | None
    amount_usd: float | None
    recurring_years: int | None

    description: str | None
    source_id: int | None
    created_at: datetime
    updated_at: datetime

    # New per-shape columns (Spec D commit #4).
    delta_kind: str
    one_shot_amount_usd: float | None
    recurring_amount_usd: float | None
    recurring_period_years: int | None
    monthly_delta_usd: float | None
    phase_start_date: date | None
    phase_end_date: date | None
    fx_at_event: float | None


class LifeEventsListResponse(BaseModel):
    events: list[LifeEventDTO]


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


def list_life_events(
    session: Session, user_id: str
) -> list[LifeEventDTO]:
    rows = (
        session.query(LifeEvent)
        .filter(LifeEvent.user_id == user_id)
        .order_by(LifeEvent.target_date.asc().nullslast(), LifeEvent.id.asc())
        .all()
    )
    return [_to_dto(r) for r in rows]


def create_life_event(
    session: Session, payload: LifeEventCreateRequest
) -> LifeEventDTO:
    """Persist a validated payload.

    Per-shape columns are populated from the payload's ``delta_kind``
    branch; legacy columns (``target_date`` / ``amount_usd`` /
    ``recurring_years``) ride along as-sent for backwards-compat reads.

    Implementation note on ``target_date`` reuse: the schema reuses
    ``target_date`` as the one_shot date AND the recurring anchor
    (migration 0054 deliberately did NOT add separate date columns).
    The writer therefore stores the payload's ``target_date`` as-is
    for ``one_shot`` and ``recurring_every_n_years`` shapes; for
    ``phase_change_*`` shapes ``target_date`` is left as whatever the
    payload sent (typically NULL).
    """
    row = LifeEvent(
        user_id=payload.user_id,
        category=payload.category.value,
        kind=payload.kind,
        target_date=payload.target_date,
        amount_usd=payload.amount_usd,
        recurring_years=payload.recurring_years,
        description=payload.description,
        source_id=payload.source_id,
        delta_kind=payload.delta_kind.value,
        one_shot_amount_usd=payload.one_shot_amount_usd,
        recurring_amount_usd=payload.recurring_amount_usd,
        recurring_period_years=payload.recurring_period_years,
        monthly_delta_usd=payload.monthly_delta_usd,
        phase_start_date=payload.phase_start_date,
        phase_end_date=payload.phase_end_date,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _to_dto(row)


def update_life_event(
    session: Session,
    event_id: int,
    payload: LifeEventUpdateRequest,
) -> LifeEventDTO | None:
    row = session.get(LifeEvent, event_id)
    if row is None or row.user_id != payload.user_id:
        return None

    # If both category and kind are sent, re-validate the pair.
    # If only kind is sent, validate against the existing category.
    # If only category is sent, validate the existing kind against it.
    new_category = (
        payload.category.value if payload.category is not None else row.category
    )
    new_kind = payload.kind if payload.kind is not None else row.kind
    if payload.category is not None or payload.kind is not None:
        cat_enum = LifeEventCategory(new_category)
        valid = valid_kinds_for(cat_enum)
        if new_kind not in valid:
            raise InvalidKindForCategoryError(
                category=new_category,
                kind=new_kind,
                valid_kinds=valid,
            )

    # delta_kind handling on PUT.  Two cases per codex review (Spec D
    # commit #4):
    #
    #   A. Payload omits delta_kind  → in-place edit of the existing
    #      shape.  Forbidden-field check applies to the wire payload
    #      against the row's existing delta_kind.  (We can't enforce
    #      required-field on a partial update — missing required
    #      fields are presumed to already exist on the row.)  The
    #      end>start invariant for ``phase_change_end`` is re-checked
    #      against the EFFECTIVE phase dates (payload value if sent,
    #      else row value) so an update that only changes one of the
    #      two dates still 422s when the invariant breaks.
    #
    #   B. Payload sends delta_kind  → REJECTED with a structured 422
    #      (``delta_shape_invalid`` / ``reason='forbidden_present'`` /
    #      ``forbidden_fields=['delta_kind']``).  The docstring on
    #      ``LifeEventUpdateRequest`` already notes that full cross-
    #      shape transitions are not committed in v1; per codex
    #      BLOCKER on this commit, we enforce that contract loudly
    #      instead of silently allowing partial transitions that
    #      leave stale per-shape fields on the row.  Callers wanting
    #      a shape transition should DELETE + POST.
    if payload.delta_kind is not None and (
        payload.delta_kind.value != row.delta_kind
    ):
        raise InvalidDeltaShapeError(
            delta_kind=payload.delta_kind.value,
            reason="forbidden_present",
            forbidden_fields=["delta_kind"],
        )

    # The effective delta_kind for the rest of this update is the
    # row's existing one (whether or not the payload re-sent it
    # matching).
    try:
        effective_delta_kind = DeltaKind(row.delta_kind)
    except ValueError:
        # Defensive: row carries a delta_kind not in our enum
        # (shouldn't happen — DB CHECK enforces).  Skip the
        # forbidden-field check rather than mask the underlying
        # corruption.
        effective_delta_kind = DeltaKind.none

    # If the row's category is changing via PUT, re-validate that the
    # row's existing delta_kind is still allowed under the new
    # category (spec §1.4 interaction matrix).
    if payload.category is not None:
        _validate_delta_kind_for_category(
            category=LifeEventCategory(new_category),
            delta_kind=effective_delta_kind,
        )

    # Forbidden-field check on the wire payload only (don't synthesize
    # from row state — the spec is about what the caller SENT).
    forbidden = forbidden_fields_for(effective_delta_kind)
    wire_present = {
        "target_date": payload.target_date is not None,
        "one_shot_amount_usd": payload.one_shot_amount_usd is not None,
        "recurring_amount_usd": payload.recurring_amount_usd is not None,
        "recurring_period_years": (
            payload.recurring_period_years is not None
        ),
        "monthly_delta_usd": payload.monthly_delta_usd is not None,
        "phase_start_date": payload.phase_start_date is not None,
        "phase_end_date": payload.phase_end_date is not None,
    }
    present_but_forbidden = [
        f for f in forbidden if wire_present.get(f)
    ]
    if present_but_forbidden:
        raise InvalidDeltaShapeError(
            delta_kind=effective_delta_kind.value,
            reason="forbidden_present",
            forbidden_fields=present_but_forbidden,
        )

    # End > start invariant for phase_change_end — enforced on PUT
    # against the EFFECTIVE phase dates (payload override > row
    # value), per codex BLOCKER on this commit.  Same surface as
    # create: ``delta_shape_invalid`` / ``forbidden_present`` /
    # ``forbidden_fields=['phase_end_date']``.
    if effective_delta_kind == DeltaKind.phase_change_end:
        effective_start = (
            payload.phase_start_date
            if payload.phase_start_date is not None
            else row.phase_start_date
        )
        effective_end = (
            payload.phase_end_date
            if payload.phase_end_date is not None
            else row.phase_end_date
        )
        if (
            effective_start is not None
            and effective_end is not None
            and effective_end <= effective_start
        ):
            raise InvalidDeltaShapeError(
                delta_kind=effective_delta_kind.value,
                reason="forbidden_present",
                forbidden_fields=["phase_end_date"],
            )

    row.category = new_category
    row.kind = new_kind
    if payload.target_date is not None:
        row.target_date = payload.target_date
    if payload.amount_usd is not None:
        row.amount_usd = payload.amount_usd
    if payload.recurring_years is not None:
        row.recurring_years = payload.recurring_years
    if payload.description is not None:
        row.description = payload.description
    if payload.source_id is not None:
        row.source_id = payload.source_id
    # payload.delta_kind is guaranteed == row.delta_kind here (the
    # cross-shape transition case raised above).  No-op write skipped.
    if payload.one_shot_amount_usd is not None:
        row.one_shot_amount_usd = payload.one_shot_amount_usd
    if payload.recurring_amount_usd is not None:
        row.recurring_amount_usd = payload.recurring_amount_usd
    if payload.recurring_period_years is not None:
        row.recurring_period_years = payload.recurring_period_years
    if payload.monthly_delta_usd is not None:
        row.monthly_delta_usd = payload.monthly_delta_usd
    if payload.phase_start_date is not None:
        row.phase_start_date = payload.phase_start_date
    if payload.phase_end_date is not None:
        row.phase_end_date = payload.phase_end_date
    # Bump updated_at explicitly — ORM onupdate fires on row attribute
    # change but only when SQLAlchemy detects a dirty flag, and our
    # conditional updates may set fields to identical values.
    row.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(row)
    return _to_dto(row)


def delete_life_event(
    session: Session,
    event_id: int,
    user_id: str,
) -> bool:
    row = session.get(LifeEvent, event_id)
    if row is None or row.user_id != user_id:
        return False
    session.delete(row)
    session.commit()
    return True


def _to_dto(row: LifeEvent) -> LifeEventDTO:
    return LifeEventDTO(
        id=row.id,
        user_id=row.user_id,
        category=row.category,
        kind=row.kind,
        target_date=row.target_date,
        amount_usd=float(row.amount_usd) if row.amount_usd is not None else None,
        recurring_years=row.recurring_years,
        description=row.description,
        source_id=row.source_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        delta_kind=row.delta_kind,
        one_shot_amount_usd=(
            float(row.one_shot_amount_usd)
            if row.one_shot_amount_usd is not None
            else None
        ),
        recurring_amount_usd=(
            float(row.recurring_amount_usd)
            if row.recurring_amount_usd is not None
            else None
        ),
        recurring_period_years=row.recurring_period_years,
        monthly_delta_usd=(
            float(row.monthly_delta_usd)
            if row.monthly_delta_usd is not None
            else None
        ),
        phase_start_date=row.phase_start_date,
        phase_end_date=row.phase_end_date,
        fx_at_event=(
            float(row.fx_at_event)
            if row.fx_at_event is not None
            else None
        ),
    )


# ---------------------------------------------------------------------------
# Catalog endpoint — UI uses this to populate the category + kind
# dropdowns AND to drive the per-shape form section visibility.  Spec D
# commit #4 extends the payload with a ``delta_kind_rules`` sub-catalog
# (allowed delta_kinds + default + nudge text per category) AND a
# top-level ``required_fields_by_delta_kind`` / ``forbidden_fields_by_
# delta_kind`` map so the UI can validate required-vs-forbidden before
# submit (avoids the round-trip on common errors).
# ---------------------------------------------------------------------------


class DeltaKindRules(BaseModel):
    """Per-category delta_kind sub-catalog entry."""

    allowed_delta_kinds: list[str]
    default_delta_kind: str
    nudge: str


class LifeEventsCatalogResponse(BaseModel):
    categories: list[str]
    kinds_by_category: dict[str, list[str]]
    # Per-category field-rule metadata — UI uses this to decide which
    # optional legacy fields to expose for each category.  Server-driven
    # so the UI doesn't silently miss rendering when a new category is
    # added (codex IMPORTANT #6 on commit #8 review).  Preserved
    # verbatim for backwards-compat — Spec D commit #4 layers the
    # delta_kind sub-catalog ALONGSIDE this field.
    field_rules_by_category: dict[str, dict[str, bool]]
    # Spec D commit #4 — per-category delta_kind interaction matrix
    # (spec §1.4).  UI consumes this to constrain the section a row
    # can be created from + the default shape pick per category.
    delta_kind_rules_by_category: dict[str, DeltaKindRules]
    # Spec D commit #4 — per-delta_kind required-field / forbidden-
    # field map (spec §3.2).  UI consumes for client-side pre-submit
    # validation so the user gets immediate feedback without a 422
    # round-trip.
    required_fields_by_delta_kind: dict[str, list[str]]
    forbidden_fields_by_delta_kind: dict[str, list[str]]


def get_catalog() -> LifeEventsCatalogResponse:
    return LifeEventsCatalogResponse(
        categories=[c.value for c in LifeEventCategory],
        kinds_by_category={
            cat.value: [k.value for k in enum_cls]
            for cat, enum_cls in KIND_ENUM_BY_CATEGORY.items()
        },
        field_rules_by_category={
            cat.value: dict(rules)
            for cat, rules in FIELD_RULES_BY_CATEGORY.items()
        },
        delta_kind_rules_by_category={
            cat.value: DeltaKindRules(
                allowed_delta_kinds=list(rules["allowed_delta_kinds"]),
                default_delta_kind=rules["default_delta_kind"],
                nudge=rules["nudge"],
            )
            for cat, rules in DELTA_KIND_RULES_BY_CATEGORY.items()
        },
        required_fields_by_delta_kind={
            dk.value: list(REQUIRED_FIELDS_BY_DELTA_KIND[dk])
            for dk in DeltaKind
        },
        forbidden_fields_by_delta_kind={
            dk.value: forbidden_fields_for(dk) for dk in DeltaKind
        },
    )
