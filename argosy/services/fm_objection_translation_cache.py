"""Cache + eager-precompute helper for FM-objection plain-English translations.

Used by ``GET /api/plan/draft/objections`` to attach a ``translation``
block to each objection in the response. On the first call for a given
draft, the helper:

  1. Hashes each objection's ``(severity, topic, detail)`` triplet.
  2. Reads any existing rows in ``fm_objection_translations`` for the
     draft, indexed by ``objection_index``.
  3. For slots whose stored hash doesn't match the live objection text
     (or that have no row yet), fires the ``ObjectionTranslatorAgent``
     **in parallel** via ``asyncio.gather`` and persists the results.
  4. Returns a ``{objection_index: TranslationDTO}`` map the route can
     splice into its response.

Why eager precompute?
  The translator is a Sonnet call (~3-8 s per objection); doing it
  lazily on click means the user waits 3-8 s every time they hit the
  "Explain in plain English" button for a different objection. By
  blocking the *first* draft load by ~10-15 s (N≈6 objections in
  parallel) and persisting the results, every subsequent load —
  whether the user clicks "explain" zero times or N times — is
  instant.

Translator failures degrade gracefully: a slot whose translator call
raises returns ``None`` in the map and is NOT persisted. The route
omits the ``translation`` field on that objection and the existing UI
fallback (lazy on-demand POST to
``/api/plan/draft/objections/translate``) still works.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from argosy.state.models import FMObjectionTranslation

log = logging.getLogger(__name__)


@dataclass
class TranslationDTO:
    """Shape returned to the route; mirrors the on-demand translate endpoint."""

    headline: str
    plain_english: str
    recommended_actions: list[str]


def _hash_objection(severity: str, topic: str, detail: str) -> str:
    """sha256 over the objection text used to invalidate stale cache rows.

    A bare hash over the three FM-emitted strings is sufficient — the
    objection list is anchored on ``decision_run_id`` which is immutable
    per draft, so collisions across drafts don't matter (cache rows are
    scoped to ``plan_version_id``). The hash exists as defense in depth
    against an upstream re-evaluation slipping new text into an existing
    draft slot.
    """
    blob = f"{severity}\x1f{topic}\x1f{detail}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


async def _translate_one(
    *,
    user_id: str,
    severity: str,
    topic: str,
    detail: str,
    cited_sources: list[str] | None,
) -> TranslationDTO | None:
    """Run ``ObjectionTranslatorAgent`` once. Returns None on any failure.

    Returning None instead of raising keeps ``asyncio.gather`` from
    cancelling its sibling translations — one bad objection mustn't
    poison the batch.
    """
    # Lazy import to avoid pulling the agent stack into module import.
    from argosy.agents.errors import AgentRunError, MissingAPIKeyError
    from argosy.agents.objection_translator import ObjectionTranslatorAgent

    try:
        agent = ObjectionTranslatorAgent(user_id=user_id)
        report = await agent.run(
            topic=topic,
            detail=detail,
            severity=severity,
            cited_sources=cited_sources or None,
        )
    except (AgentRunError, MissingAPIKeyError) as exc:
        log.warning(
            "fm_objection_translator failed user_id=%s topic=%r err=%s",
            user_id,
            topic[:80],
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — best-effort cache fill
        log.warning(
            "fm_objection_translator unexpected error user_id=%s topic=%r err=%s",
            user_id,
            topic[:80],
            exc,
        )
        return None

    out = report.output
    return TranslationDTO(
        headline=out.headline,
        plain_english=out.plain_english,
        recommended_actions=list(out.recommended_actions or []),
    )


async def _gather_translations_async(
    *,
    user_id: str,
    misses: list[tuple[int, str, str, str, list[str] | None]],
) -> list[tuple[int, TranslationDTO | None]]:
    """Run all missing translations in parallel.

    ``misses`` is a list of ``(objection_index, severity, topic, detail,
    cited_sources)`` tuples. Returns a list of ``(objection_index,
    TranslationDTO | None)`` in the same order.
    """
    tasks = [
        _translate_one(
            user_id=user_id,
            severity=sev,
            topic=topic,
            detail=detail,
            cited_sources=cs,
        )
        for (_idx, sev, topic, detail, cs) in misses
    ]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    return [(idx, res) for (idx, _, _, _, _), res in zip(misses, results)]


def _run_async(coro):
    """Drive an async coroutine from a sync caller.

    The plan route handler is a sync ``def`` running in FastAPI's
    threadpool, so ``asyncio.get_running_loop()`` raises and a fresh
    ``asyncio.run`` is safe. If a future caller invokes this helper
    from an already-running loop we fall back to a dedicated thread so
    we don't blow up with "asyncio.run() cannot be called from a
    running event loop".
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop running — safe to use asyncio.run directly.
        return asyncio.run(coro)
    # A loop is already running on this thread (rare for our sync route,
    # but possible if a test wraps us). Off-thread the coroutine into a
    # fresh loop so we don't deadlock.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(asyncio.run, coro)
        return fut.result()


def get_or_compute_translations(
    db: Session,
    *,
    user_id: str,
    plan_version_id: int,
    objections: list[dict],
    cited_sources: list[str] | None = None,
) -> dict[int, TranslationDTO]:
    """Return a per-index map of cached/newly-computed translations.

    Args:
        db: sync SQLAlchemy session used for read + write of the cache table.
        user_id: passed to the translator agent for cost-tracking + override
            resolution.
        plan_version_id: the draft row the objections belong to.
        objections: a list of ``{severity, topic, detail}`` dicts in the
            sorted order the API will emit them. ``objection_index`` is
            the position in this list.
        cited_sources: optional list of FM-cited source IDs threaded
            through to every translator call so the agent can echo them.

    Returns:
        A ``{objection_index: TranslationDTO}`` map covering every
        objection that was successfully translated (cached OR freshly
        computed). Objections that failed translation are simply absent
        from the map — the caller treats absence as "no translation
        available" and the UI falls back to the on-demand button.
    """
    if not objections:
        return {}

    # 1. Load existing rows for this draft, keyed by objection_index.
    rows: list[FMObjectionTranslation] = list(
        db.execute(
            select(FMObjectionTranslation).where(
                FMObjectionTranslation.plan_version_id == plan_version_id
            )
        )
        .scalars()
        .all()
    )
    by_index: dict[int, FMObjectionTranslation] = {r.objection_index: r for r in rows}

    # 2. Walk the live objections; classify each as hit / stale / miss.
    out: dict[int, TranslationDTO] = {}
    misses: list[tuple[int, str, str, str, list[str] | None]] = []
    stale_indices: list[int] = []
    for idx, o in enumerate(objections):
        sev = str(o.get("severity") or "")
        topic = str(o.get("topic") or "")
        detail = str(o.get("detail") or "")
        h = _hash_objection(sev, topic, detail)
        existing = by_index.get(idx)
        if existing is not None and existing.topic_hash == h:
            # Cache hit — deserialize and use as-is.
            try:
                actions = json.loads(existing.recommended_actions_json or "[]")
            except json.JSONDecodeError:
                actions = []
            if not isinstance(actions, list):
                actions = []
            out[idx] = TranslationDTO(
                headline=existing.headline,
                plain_english=existing.plain_english,
                recommended_actions=[str(a) for a in actions],
            )
        else:
            if existing is not None:
                stale_indices.append(idx)
            misses.append((idx, sev, topic, detail, cited_sources))

    if not misses:
        return out

    # 3. Run all misses in parallel via asyncio.gather, then persist.
    results = _run_async(
        _gather_translations_async(user_id=user_id, misses=misses)
    )

    # Drop any stale rows we're about to re-fill so the UNIQUE constraint
    # on (plan_version_id, objection_index) doesn't fight the insert.
    if stale_indices:
        db.execute(
            delete(FMObjectionTranslation).where(
                FMObjectionTranslation.plan_version_id == plan_version_id,
                FMObjectionTranslation.objection_index.in_(stale_indices),
            )
        )

    persisted_any = False
    for (idx, dto), miss in zip(results, misses):
        if dto is None:
            # Translator failed for this slot; leave it out so the UI
            # falls back to the on-demand "Explain in plain English"
            # button. Don't persist a partial row.
            continue
        miss_idx, sev, topic, detail, _cs = miss
        h = _hash_objection(sev, topic, detail)
        db.add(
            FMObjectionTranslation(
                plan_version_id=plan_version_id,
                objection_index=miss_idx,
                topic_hash=h,
                headline=dto.headline,
                plain_english=dto.plain_english,
                recommended_actions_json=json.dumps(
                    dto.recommended_actions, ensure_ascii=False
                ),
            )
        )
        out[idx] = dto
        persisted_any = True

    if persisted_any:
        db.commit()

    return out


__all__ = [
    "TranslationDTO",
    "get_or_compute_translations",
]
