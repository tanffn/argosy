"""Life-events service: CRUD + Pydantic enum validator (loud-error).

Sprint commit #8 of the plan/execute/monitor reorg (spec cbf6a07 §4).
Backs the `/life-events` structured-intake page where the user records
career / family / asset / expense / recurring / retirement-milestone
events that feed:

  - cashflow_projection.effective_retire_ready_age() — clamps retire-
    ready age by retirement_milestone:target_retire_year_change +
    blocking expense_event entries (the clamp hook is stubbed in commit
    #9; this commit makes it real).
  - <HolisticTimelineCard> on /retirement (commit #10) — renders all
    life events as timeline markers.
  - Monitor agent — reads as context for drift/MC interpretation
    (NOT trigger — life events feed context not red flags, per Ariel's
    Q2 answer on the design phase).

**Loud-error contract (codex BLOCKER on spec #1 §4.1):** the form
service refuses to silently best-effort parse out-of-category input.
Pydantic enum validation is the gate; the route returns 422 with
`{error: "category_not_recognized", input: "<raw>", valid_categories:
[...]}` so the UI can render a red banner inline (asserted by the UI
test in `ui/__tests__/life-events-form.spec.tsx`).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Annotated, Literal

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


# Spec §4 calls out per-category field metadata so the UI can be
# server-driven (codex IMPORTANT on commit #8 review: hardcoded
# category-set checks in the UI break when a new category is added that
# also needs the amount field). Each category declares which optional
# fields the form should expose.
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
# Request / Response shapes
# ---------------------------------------------------------------------------


class LifeEventCreateRequest(BaseModel):
    user_id: str
    category: LifeEventCategory
    kind: str  # validated by model_validator against per-category enum
    target_date: date | None = None
    amount_usd: Annotated[float, Field(gt=0)] | None = None
    recurring_years: Annotated[int, Field(gt=0)] | None = None
    description: str | None = None
    source_id: int | None = None

    @model_validator(mode="after")
    def validate_kind_for_category(self) -> "LifeEventCreateRequest":
        """Refuse silently best-effort parsing — the loud-error gate.

        If `kind` isn't a valid value for the given `category`, raise
        `InvalidKindForCategoryError` (a typed ValueError subclass).
        The route catches it explicitly and returns a structured 422
        without string-matching on Pydantic's message format (codex
        IMPORTANT #2 on commit #8 review).
        """
        valid = valid_kinds_for(self.category)
        if self.kind not in valid:
            raise InvalidKindForCategoryError(
                category=self.category.value,
                kind=self.kind,
                valid_kinds=valid,
            )
        return self


class LifeEventUpdateRequest(BaseModel):
    """Update payload — only sends the user_id_owner check + the
    fields to change. Re-validates kind/category if either is sent."""

    user_id: str
    category: LifeEventCategory | None = None
    kind: str | None = None
    target_date: date | None = None
    amount_usd: Annotated[float, Field(gt=0)] | None = None
    recurring_years: Annotated[int, Field(gt=0)] | None = None
    description: str | None = None
    source_id: int | None = None


class LifeEventDTO(BaseModel):
    id: int
    user_id: str
    category: str
    kind: str
    target_date: date | None
    amount_usd: float | None
    recurring_years: int | None
    description: str | None
    source_id: int | None
    created_at: datetime
    updated_at: datetime


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
    row = LifeEvent(
        user_id=payload.user_id,
        category=payload.category.value,
        kind=payload.kind,
        target_date=payload.target_date,
        amount_usd=payload.amount_usd,
        recurring_years=payload.recurring_years,
        description=payload.description,
        source_id=payload.source_id,
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
    )


# ---------------------------------------------------------------------------
# Catalog endpoint — UI uses this to populate the category + kind
# dropdowns so the client never has to hardcode the enum values.
# ---------------------------------------------------------------------------


class LifeEventsCatalogResponse(BaseModel):
    categories: list[str]
    kinds_by_category: dict[str, list[str]]
    # Per-category field-rule metadata — UI uses this to decide which
    # optional fields to expose for each category. Server-driven so the
    # UI doesn't silently miss rendering when a new category is added
    # (codex IMPORTANT #6 on commit #8 review).
    field_rules_by_category: dict[str, dict[str, bool]]


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
    )
