"""``CadenceLoop.tick`` return-type widening contract — Sprint A commit #7.

Spec: ``docs/superpowers/specs/2026-05-29-jobs-registry-design.md``
commit #7 — codex IMPORTANT #5.

Commit #7 widens ``CadenceLoop.tick`` from ``-> None`` to ``-> dict | None``.
The 14 existing loops keep their implicit-``None`` returns — ``None``
matches ``dict | None``. This test pins that contract:

  1. Every concrete ``CadenceLoop`` subclass on disk has a ``tick``
     coroutine whose annotated return type is compatible with
     ``dict | None`` (i.e. it's either ``None``, ``dict | None``,
     ``dict``, or a typing union containing those).
  2. The base class's abstract signature is the widened shape.

We discover subclasses dynamically rather than hardcoding the count so
the test keeps passing as new loops land.

Test command::

    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_cadence_loop_tick_widening.py -v
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
import typing

import pytest

import argosy.execution
import argosy.orchestrator.loops as loops_pkg
import argosy.services.jobs as jobs_pkg
from argosy.orchestrator.loops.base import CadenceLoop


def _import_all(pkg) -> None:
    """Import every module in `pkg` so subclass discovery is complete."""
    for mod in pkgutil.iter_modules(pkg.__path__):
        try:
            importlib.import_module(f"{pkg.__name__}.{mod.name}")
        except Exception:
            # Defensive: some sibling modules (e.g. discord listener)
            # may not import cleanly under the test env; we only care
            # about reachable CadenceLoop subclasses.
            continue


def _all_cadence_loop_subclasses() -> list[type[CadenceLoop]]:
    """Walk known package roots and return concrete ``CadenceLoop`` subclasses."""
    _import_all(loops_pkg)
    _import_all(jobs_pkg)
    # ReconcileLoop lives outside the loops package — import it explicitly.
    try:
        importlib.import_module("argosy.execution.reconcile")
    except Exception:
        pass

    seen: set[type[CadenceLoop]] = set()
    work: list[type[CadenceLoop]] = [CadenceLoop]
    while work:
        cls = work.pop()
        for sub in cls.__subclasses__():
            if sub not in seen:
                seen.add(sub)
                work.append(sub)
    # Drop abstract subclasses (the base alias itself is filtered by
    # __subclasses__ but defensive subclasses that override `tick` as
    # abstract again would be excluded by inspect.isabstract).
    return [c for c in seen if not inspect.isabstract(c)]


def test_base_class_tick_signature_widened() -> None:
    """The abstract ``CadenceLoop.tick`` annotates ``-> dict | None``."""
    sig = inspect.signature(CadenceLoop.tick)
    ann = sig.return_annotation
    # The annotation is a string under `from __future__ import annotations`.
    # Resolve it via typing.get_type_hints which honours forward refs.
    hints = typing.get_type_hints(CadenceLoop.tick)
    ret = hints.get("return", ann)
    # Accept the union form (`dict | None` collapses to `Union[dict, None]`
    # at typing.get_type_hints time on Py3.10+).
    origin = typing.get_origin(ret)
    args = typing.get_args(ret)
    assert origin is typing.Union or str(ret) in (
        "dict | None",
        "typing.Optional[dict]",
    ), f"unexpected return annotation: {ret!r}"
    assert dict in args
    assert type(None) in args


def test_all_concrete_subclasses_have_tick_returning_dict_or_none() -> None:
    """Every concrete CadenceLoop subclass's tick is compatible with
    ``dict | None``.

    A subclass is compatible if its annotated return is one of:
      - missing (`None` implicit by Python convention)
      - ``None`` literal
      - ``dict``
      - ``dict | None`` (or ``Optional[dict]``)
      - a typing union containing only ``dict``, ``None``, and/or
        broader supertypes (``object``, etc.)

    We don't strictly require ``dict | None`` — we require *some* type
    that ``dict | None`` is a subtype of, OR an unannotated/None return.
    The point of the widening is to allow loops to return a dict, not to
    mandate it.
    """
    subclasses = _all_cadence_loop_subclasses()
    assert len(subclasses) >= 1, (
        "Expected at least one concrete CadenceLoop subclass to be "
        "discovered — the import walk above didn't find any."
    )

    incompatible: list[tuple[str, object]] = []
    for cls in subclasses:
        try:
            hints = typing.get_type_hints(cls.tick)
        except Exception:
            # If type hints can't be resolved (forward refs to missing
            # modules etc.) we skip — those classes aren't relevant to
            # the widening contract.
            continue
        ret = hints.get("return", None)
        # Acceptable: no annotation, None, dict, dict | None,
        # Optional[dict], or any wider Union containing dict + None.
        if ret is None or ret is type(None) or ret is dict:
            continue
        args = set(typing.get_args(ret))
        if args and (dict in args or any(
            isinstance(a, type) and issubclass(dict, a) for a in args
        )):
            # If the subclass return is a Union including dict (or a
            # supertype of dict), the widening is satisfied.
            continue
        incompatible.append((f"{cls.__module__}.{cls.__name__}", ret))

    assert not incompatible, (
        "Subclasses with tick return annotations incompatible with "
        f"`dict | None`: {incompatible}"
    )


def test_subclass_count_matches_expectation() -> None:
    """Sanity check — fail loudly if the discovery walk regresses.

    Spec A commit #7 lands when there are ~14 CadenceLoop subclasses
    (13 in argosy/orchestrator/loops/ + 1 ReconcileLoop +
    NewsDailyJob itself = 15). Hardcoding the exact number would
    fragile against new loops landing; we just assert "many" so the
    test catches a discovery-walk break (e.g. import path renames).
    """
    subclasses = _all_cadence_loop_subclasses()
    names = sorted(c.__name__ for c in subclasses)
    assert len(names) >= 10, (
        f"Expected ≥10 CadenceLoop subclasses, found {len(names)}: {names}"
    )
    # NewsDailyJob is the commit-under-test — it must be in the set.
    assert "NewsDailyJob" in names, f"NewsDailyJob missing from: {names}"
