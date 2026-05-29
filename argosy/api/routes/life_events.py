"""REST routes for /api/life-events.

Sprint commit #8 of the plan/execute/monitor reorg (spec cbf6a07 §4)
laid down the original loud-error contract.  Spec D commit #4 extends
the route to translate the new typed validation exceptions
(``InvalidDeltaShapeError`` / ``InvalidDeltaKindForCategoryError``)
into structured 422 banners per spec §3.3:

  * ``{"error": "category_not_recognized", ...}``           — preserved
  * ``{"error": "kind_not_valid_for_category", ...}``       — preserved
  * ``{"error": "delta_kind_not_valid_for_category", ...}`` — new
  * ``{"error": "delta_shape_invalid", ...}``               — new

The UI red-banner handler discriminates on the ``error`` string and
renders one of four message variants; the rest of the payload carries
the alternatives the form should have offered (valid kinds, allowed
delta_kinds, missing / forbidden field names).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.services.life_events import (
    InvalidDeltaKindForCategoryError,
    InvalidDeltaShapeError,
    InvalidKindForCategoryError,
    LifeEventCategory,
    LifeEventCreateRequest,
    LifeEventDTO,
    LifeEventUpdateRequest,
    LifeEventsCatalogResponse,
    LifeEventsListResponse,
    create_life_event,
    delete_life_event,
    get_catalog,
    list_life_events,
    update_life_event,
)


router = APIRouter(prefix="/life-events", tags=["life-events"])


class CategoryNotRecognizedError(BaseModel):
    """422 body when the category or kind doesn't pass the loud-error
    validator. UI renders this as a red banner above the form."""

    error: str
    input: str
    valid_categories: list[str] | None = None
    valid_kinds: list[str] | None = None


def _raise_category_error(input_value: str) -> None:
    """The category came back from the form as something not in our enum.
    Raise the structured 422 the UI expects."""
    raise HTTPException(
        status_code=422,
        detail={
            "error": "category_not_recognized",
            "input": input_value,
            "valid_categories": [c.value for c in LifeEventCategory],
        },
    )


def _raise_kind_error_from_exc(exc: InvalidKindForCategoryError) -> None:
    """The kind isn't valid for the chosen category. The typed exception
    carries the valid alternatives so we don't have to recompute.

    Replaces the prior implementation that re-derived `valid_kinds`
    from the category string — keeps the route's discriminator from
    being a string match on Pydantic's error message (codex
    IMPORTANT #2 on commit #8 review)."""
    raise HTTPException(
        status_code=422,
        detail={
            "error": "kind_not_valid_for_category",
            "input": exc.kind,
            "valid_kinds": exc.valid_kinds,
        },
    )


def _raise_delta_kind_for_category_error(
    exc: InvalidDeltaKindForCategoryError,
) -> None:
    """The (category, delta_kind) pair violates spec §1.4 matrix.

    Spec D commit #4 / §3.3 — structured 422 carries the allowed
    alternatives so the UI banner can tell the user which section to
    move the event into.
    """
    raise HTTPException(
        status_code=422,
        detail={
            "error": "delta_kind_not_valid_for_category",
            "category": exc.category,
            "delta_kind": exc.delta_kind,
            "allowed_delta_kinds": exc.allowed_delta_kinds,
        },
    )


def _raise_delta_shape_error(exc: InvalidDeltaShapeError) -> None:
    """The per-shape required/forbidden field rule was violated.

    Spec D commit #4 / §3.3 — structured 422 carries the missing /
    forbidden field names so the UI can highlight the affected inputs
    inline (in addition to the banner).  The ``reason`` discriminates
    the two sub-cases (``missing_required`` vs ``forbidden_present``).
    """
    raise HTTPException(
        status_code=422,
        detail={
            "error": "delta_shape_invalid",
            "delta_kind": exc.delta_kind,
            "reason": exc.reason,
            "missing_fields": exc.missing_fields,
            "forbidden_fields": exc.forbidden_fields,
        },
    )


@router.get("/catalog", response_model=LifeEventsCatalogResponse)
def get_catalog_route() -> LifeEventsCatalogResponse:
    """Return the full category + per-category-kind dictionary so the
    UI dropdowns are server-driven (no hardcoded enums client-side).

    Spec D commit #4 — payload now also carries:
      * ``delta_kind_rules_by_category`` — allowed delta_kinds + default
        + nudge text per category (spec §1.4 interaction matrix).
      * ``required_fields_by_delta_kind`` /
        ``forbidden_fields_by_delta_kind`` — per-shape field rules so
        the UI can pre-validate before submit.
    """
    return get_catalog()


@router.get("", response_model=LifeEventsListResponse)
def list_route(
    user_id: str,
    db: Session = Depends(get_db),
) -> LifeEventsListResponse:
    events = list_life_events(db, user_id)
    return LifeEventsListResponse(events=events)


@router.post("", response_model=LifeEventDTO, status_code=201)
def create_route(
    payload: dict,
    db: Session = Depends(get_db),
) -> LifeEventDTO:
    """Create a life event.  Validation pipeline:

    1. ``category`` must be one of the 6 enum values (structured 422 if
       not — ``category_not_recognized``).
    2. ``kind`` must be valid for the chosen category (structured 422 if
       not — ``kind_not_valid_for_category``).
    3. ``delta_kind`` must be in the category's allowed list per the
       §1.4 interaction matrix (structured 422 if not —
       ``delta_kind_not_valid_for_category``).
    4. Per-shape required + forbidden fields must be consistent with
       ``delta_kind`` (structured 422 if not — ``delta_shape_invalid``).

    All four error variants surface as ``{error, ...}`` so the UI can
    render the right red-banner content inline.  The validators raise
    typed exceptions (``InvalidKindForCategoryError`` etc.) so this
    route's discriminator doesn't string-match on Pydantic's message
    format (codex IMPORTANT #2 on commit #8 review).
    """
    raw_category = payload.get("category", "")
    if raw_category not in {c.value for c in LifeEventCategory}:
        _raise_category_error(str(raw_category))

    try:
        req = LifeEventCreateRequest.model_validate(payload)
    except ValidationError as e:
        # Walk the underlying causes; if any chained exception is one of
        # our typed validators, surface the structured error.  Order
        # matters: kind-not-valid-for-category is the most specific.
        kind_err = _find_chained_error(e, InvalidKindForCategoryError)
        if kind_err is not None:
            _raise_kind_error_from_exc(kind_err)
        dk_cat_err = _find_chained_error(
            e, InvalidDeltaKindForCategoryError
        )
        if dk_cat_err is not None:
            _raise_delta_kind_for_category_error(dk_cat_err)
        shape_err = _find_chained_error(e, InvalidDeltaShapeError)
        if shape_err is not None:
            _raise_delta_shape_error(shape_err)
        raise HTTPException(status_code=422, detail=e.errors())

    return create_life_event(db, req)


def _find_chained_error(
    exc: ValidationError,
    cls: type[Exception],
):
    """Walk a Pydantic ``ValidationError``'s underlying causes for an
    exception of class ``cls``.

    Pydantic v2 wraps model_validator errors in ``ValidationError``;
    the original exception is reachable via the ``ctx`` dict in
    ``.errors()`` under the ``error`` key.  Returns the typed exception
    if found, else None.
    """
    for err in exc.errors():
        ctx = err.get("ctx")
        if isinstance(ctx, dict):
            inner = ctx.get("error")
            if isinstance(inner, cls):
                return inner
    return None


@router.put("/{event_id}", response_model=LifeEventDTO)
def update_route(
    event_id: int,
    payload: LifeEventUpdateRequest,
    db: Session = Depends(get_db),
) -> LifeEventDTO:
    try:
        result = update_life_event(db, event_id, payload)
    except InvalidKindForCategoryError as e:
        _raise_kind_error_from_exc(e)
    except InvalidDeltaKindForCategoryError as e:
        _raise_delta_kind_for_category_error(e)
    except InvalidDeltaShapeError as e:
        _raise_delta_shape_error(e)
    except ValueError as e:  # other ValueErrors stay generic 422
        raise HTTPException(status_code=422, detail=str(e))

    if result is None:
        raise HTTPException(status_code=404, detail="life event not found")
    return result


@router.delete("/{event_id}", status_code=204)
def delete_route(
    event_id: int,
    user_id: str,
    db: Session = Depends(get_db),
) -> None:
    ok = delete_life_event(db, event_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="life event not found")


__all__ = ["router"]
