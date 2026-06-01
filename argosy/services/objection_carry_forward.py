"""Carry-forward matcher — bridge FM objections across drafts.

Wave 7 Piece B. When a new synthesis draft commits, the matcher
attempts to identify each new-draft FM objection as a *continuation*
of a prior-draft objection (the user already AGREED or DISAGREED).
A successful match means the prior stance + counter-position can be
threaded into the new draft so the FM cannot silently re-raise a
concern Ariel already answered.

Deterministic matching stack (per ``docs/superpowers/plans/
2026-06-01-wave-7-convergence-and-scoping.md`` rev 2):

  1. **Exact ``topic_hash`` match** — same SHA-256 of topic+detail
     as a prior-draft row. Confidence 1.0. No embedding involved.
  2. **Embedding fallback** — cosine similarity over the local
     ``all-MiniLM-L6-v2`` embedding of ``topic + "\\n" + detail``.
     Match accepted only when ``score >= EMBEDDING_THRESHOLD`` AND
     the top1-top2 margin is ``>= AMBIGUITY_MARGIN``. If two prior
     objections score close together, the matcher abstains so the
     user re-disambiguates rather than risk carrying the wrong
     stance forward.

DEFER stances are always skipped: the user explicitly said "skip
this round," not "carry this resolution forward."

The function is pure (no DB writes). Callers persist the
``CarryForwardMatch`` results onto the new draft's
``fm_objection_user_state`` rows via the audit fields added by
migration 0060.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import numpy as np
from pydantic import BaseModel, Field

from argosy.api.routes.plan_objection_state import _hash_objection_topic
from argosy.services.embeddings import (
    MODEL_ID as EMBEDDING_MODEL_ID,
    cosine_similarity_matrix,
    encode_batch,
    model_version as embedding_model_version,
)

log = logging.getLogger(__name__)


# Tuned starting values per the wave-7 plan rev 2; calibration will
# adjust after the first 50 drafts of audit data.
EMBEDDING_THRESHOLD: float = 0.85
AMBIGUITY_MARGIN: float = 0.05


class _ObjectionLike(Protocol):
    """Structural type for new-draft objections — just topic + detail."""

    topic: str
    detail: str


class _PriorStateLike(Protocol):
    """Structural type for prior-draft user-state rows."""

    plan_version_id: int
    objection_index: int
    topic_hash: str
    topic: str
    detail: str
    stance: str
    counter_position: str | None


class CarryForwardMatch(BaseModel):
    """One successful carry-forward decision."""

    matched_from_plan_version_id: int
    matched_objection_index: int
    match_kind: str  # "exact_hash" | "embedding"
    score: float
    top2_score: float | None = None
    prior_stance: str  # "AGREE" | "DISAGREE" (DEFER never carried)
    prior_counter_position: str | None = None
    embedding_model: str | None = None
    embedding_model_version: str | None = None


def match_carry_forward(
    new_objections: list[_ObjectionLike] | list[Any],
    prior_state: list[_PriorStateLike] | list[Any],
) -> dict[int, CarryForwardMatch]:
    """Return ``{new_objection_index: CarryForwardMatch}`` for the
    subset of new-draft objections that match a prior-draft row.

    Objections without a match are absent from the dict. ``DEFER``
    prior rows never produce a carry-forward — they're filtered
    upfront.

    Empty inputs (no new objections or no prior rows) return ``{}``
    without touching the embedding model.
    """
    if not new_objections or not prior_state:
        return {}

    # DEFER means "user said skip" — never carry as a stance.
    carryable_prior = [p for p in prior_state if p.stance != "DEFER"]
    if not carryable_prior:
        return {}

    out: dict[int, CarryForwardMatch] = {}
    needs_embedding: list[tuple[int, _ObjectionLike]] = []

    # Pass 1 — exact topic_hash match. Highest confidence; no model load.
    by_hash: dict[str, _PriorStateLike] = {p.topic_hash: p for p in carryable_prior}
    for i, obj in enumerate(new_objections):
        h = _hash_objection_topic(obj.topic, obj.detail)
        if h in by_hash:
            p = by_hash[h]
            out[i] = CarryForwardMatch(
                matched_from_plan_version_id=p.plan_version_id,
                matched_objection_index=p.objection_index,
                match_kind="exact_hash",
                score=1.0,
                top2_score=None,
                prior_stance=p.stance,
                prior_counter_position=p.counter_position,
                embedding_model=None,
                embedding_model_version=None,
            )
        else:
            needs_embedding.append((i, obj))

    # Pass 2 — embedding fallback for the unmatched. Skip the model
    # entirely when there's nothing left to do.
    if not needs_embedding:
        return out

    # Encode in two batches so caller mocks can substitute ``encode_batch``
    # without needing to interleave the same call twice.
    new_texts = [f"{obj.topic}\n{obj.detail}" for _, obj in needs_embedding]
    prior_texts = [f"{p.topic}\n{p.detail}" for p in carryable_prior]
    new_vecs = encode_batch(new_texts)
    prior_vecs = encode_batch(prior_texts)
    sims = cosine_similarity_matrix(new_vecs, prior_vecs)
    # sims shape: (len(needs_embedding), len(carryable_prior))

    model_id = EMBEDDING_MODEL_ID
    model_ver = embedding_model_version()

    for row_idx, (new_idx, _) in enumerate(needs_embedding):
        row = sims[row_idx]
        if row.size == 0:
            continue
        order = np.argsort(row)[::-1]  # descending
        top1_col = int(order[0])
        top1 = float(row[top1_col])
        top2 = float(row[order[1]]) if row.size >= 2 else None

        if top1 < EMBEDDING_THRESHOLD:
            # Below threshold — too risky to carry forward.
            log.info(
                "carry_forward.below_threshold",
                extra={
                    "new_index": new_idx,
                    "top1": top1,
                    "threshold": EMBEDDING_THRESHOLD,
                },
            )
            continue
        if top2 is not None and (top1 - top2) < AMBIGUITY_MARGIN:
            # Ambiguous — two priors look like equally good candidates.
            # Abstain rather than risk carrying the wrong stance.
            log.info(
                "carry_forward.ambiguous_abstain",
                extra={
                    "new_index": new_idx,
                    "top1": top1,
                    "top2": top2,
                    "margin": top1 - top2,
                    "required_margin": AMBIGUITY_MARGIN,
                },
            )
            continue

        p = carryable_prior[top1_col]
        out[new_idx] = CarryForwardMatch(
            matched_from_plan_version_id=p.plan_version_id,
            matched_objection_index=p.objection_index,
            match_kind="embedding",
            score=top1,
            top2_score=top2,
            prior_stance=p.stance,
            prior_counter_position=p.counter_position,
            embedding_model=model_id,
            embedding_model_version=model_ver,
        )

    return out


__all__ = [
    "AMBIGUITY_MARGIN",
    "CarryForwardMatch",
    "EMBEDDING_THRESHOLD",
    "match_carry_forward",
]
