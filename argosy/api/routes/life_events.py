"""REST routes for /api/life-events.

Sprint commit #8 of the plan/execute/monitor reorg (spec cbf6a07 §4).
Wraps the service layer at argosy/services/life_events.py with FastAPI
+ surfaces the loud-error 422 contract: bad category or bad kind is a
structured 422 body the UI can render as a red banner inline.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.services.life_events import (
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


@router.get("/catalog", response_model=LifeEventsCatalogResponse)
def get_catalog_route() -> LifeEventsCatalogResponse:
    """Return the full category + per-category-kind dictionary so the
    UI dropdowns are server-driven (no hardcoded enums client-side)."""
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
    """Create a life event. Two-stage validation:

    1. Category must be one of the 6 enum values (structured 422 if not).
    2. Kind must be valid for the chosen category (structured 422 if not).

    Both errors surface as `{error, input, valid_*}` so the UI can
    render the right red-banner content inline. The kind-validator
    raises a typed `InvalidKindForCategoryError` so this route's
    discriminator doesn't string-match on Pydantic's message format
    (codex IMPORTANT #2 on commit #8 review).
    """
    raw_category = payload.get("category", "")
    if raw_category not in {c.value for c in LifeEventCategory}:
        _raise_category_error(str(raw_category))

    try:
        req = LifeEventCreateRequest.model_validate(payload)
    except ValidationError as e:
        # Walk the underlying causes; if any chained exception is our
        # typed InvalidKindForCategoryError, render the structured kind
        # error. Otherwise surface the raw Pydantic 422.
        kind_err = _find_chained_kind_error(e)
        if kind_err is not None:
            _raise_kind_error_from_exc(kind_err)
        raise HTTPException(status_code=422, detail=e.errors())

    return create_life_event(db, req)


def _find_chained_kind_error(
    exc: ValidationError,
) -> InvalidKindForCategoryError | None:
    """Walk the ValidationError's underlying causes for our typed
    InvalidKindForCategoryError.

    Pydantic v2 wraps validator errors in ValidationError; the original
    exception is reachable via __cause__ on individual error contexts,
    or via the `ctx` dict in `.errors()`. We try both since the exact
    surface depends on Pydantic version.
    """
    # Check each entry's ctx for the original exception.
    for err in exc.errors():
        ctx = err.get("ctx")
        if isinstance(ctx, dict):
            inner = ctx.get("error")
            if isinstance(inner, InvalidKindForCategoryError):
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
    except ValueError as e:  # noqa: F841  — other ValueErrors stay generic 422
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
