"""Carry-forward matcher (wave 7 Piece B).

The matcher takes a list of NEW-draft FM objections + the prior-
draft ``fm_objection_user_state`` rows and decides which prior
stances should carry forward into the new draft. Two matching legs:

  1. Exact ``topic_hash`` match — same SHA-256 of topic+detail.
     Confidence 1.0.
  2. Embedding fallback — cosine similarity over the local
     ``all-MiniLM-L6-v2`` embedding of ``topic + "\\n" + detail``.
     Match accepted only when score >= 0.85 AND the top1-top2
     margin >= 0.05 (ambiguity guard).

These tests pin every branch of the matcher. The embedding model
itself is mocked at module scope so tests don't pay the model-load
cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import numpy as np
import pytest


@dataclass
class _FakeObjection:
    """Stand-in for the live FMObjection pydantic model — the matcher
    only reads .topic + .detail + .severity, so a dataclass keeps
    test setup terse."""

    topic: str
    detail: str
    severity: str = "AMBER"


@dataclass
class _FakePriorState:
    """Stand-in for the prior-draft fm_objection_user_state row."""

    plan_version_id: int
    objection_index: int
    topic_hash: str
    topic: str
    detail: str
    stance: str
    counter_position: str | None = None


# ----------------------------------------------------------------------
# Leg 1: exact topic_hash match
# ----------------------------------------------------------------------


def test_exact_hash_match_returns_score_1():
    """When a new-draft objection's topic+detail hashes to the same
    SHA-256 as a prior-draft row, the matcher emits an exact_hash
    match with score=1.0 and no embedding involved."""
    from argosy.services.objection_carry_forward import (
        CarryForwardMatch,
        match_carry_forward,
    )
    from argosy.api.routes.plan_objection_state import _hash_objection_topic

    topic = "NVDA concentration breach"
    detail = "Position at 64.9%, cap is 55%."
    h = _hash_objection_topic(topic, detail)

    new_objs = [_FakeObjection(topic=topic, detail=detail)]
    prior = [
        _FakePriorState(
            plan_version_id=42,
            objection_index=3,
            topic_hash=h,
            topic=topic,
            detail=detail,
            stance="AGREE",
            counter_position="Push tranche to 2026-06-17.",
        ),
    ]

    matches = match_carry_forward(new_objs, prior)

    assert 0 in matches
    m: CarryForwardMatch = matches[0]
    assert m.match_kind == "exact_hash"
    assert m.score == pytest.approx(1.0)
    assert m.top2_score is None
    assert m.matched_from_plan_version_id == 42
    assert m.matched_objection_index == 3
    assert m.prior_stance == "AGREE"
    assert m.prior_counter_position == "Push tranche to 2026-06-17."
    assert m.embedding_model is None
    assert m.embedding_model_version is None


def test_no_match_when_prior_empty():
    """No prior-draft rows → empty map, no embedding call."""
    from argosy.services.objection_carry_forward import match_carry_forward

    new_objs = [_FakeObjection(topic="anything", detail="anything")]
    assert match_carry_forward(new_objs, []) == {}


def test_no_match_when_new_empty():
    """No new-draft objections → empty map (defensive)."""
    from argosy.services.objection_carry_forward import match_carry_forward

    prior = [
        _FakePriorState(
            plan_version_id=1,
            objection_index=0,
            topic_hash="abc",
            topic="x",
            detail="y",
            stance="AGREE",
        ),
    ]
    assert match_carry_forward([], prior) == {}


def test_defer_stance_is_skipped():
    """DEFER means 'user explicitly said skip this round' — the matcher
    must NOT carry it forward as a stance. Behaves as if the prior row
    didn't exist for that objection."""
    from argosy.services.objection_carry_forward import match_carry_forward
    from argosy.api.routes.plan_objection_state import _hash_objection_topic

    topic, detail = "topic x", "detail y"
    h = _hash_objection_topic(topic, detail)

    new_objs = [_FakeObjection(topic=topic, detail=detail)]
    prior = [
        _FakePriorState(
            plan_version_id=1,
            objection_index=0,
            topic_hash=h,
            topic=topic,
            detail=detail,
            stance="DEFER",
        ),
    ]
    assert match_carry_forward(new_objs, prior) == {}


# ----------------------------------------------------------------------
# Leg 2: embedding fallback
# ----------------------------------------------------------------------


def _mock_embeddings(text_to_vec: dict[str, np.ndarray]):
    """Return an encode_batch substitute that looks up vectors by text."""

    def _encode(texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 384), dtype=np.float32)
        return np.vstack([text_to_vec[t] for t in texts]).astype(np.float32)

    return _encode


def test_embedding_match_above_threshold_returns_match():
    """Reworded objection matches via embedding fallback when
    cosine >= 0.85 AND top1-top2 margin >= 0.05."""
    from argosy.services.objection_carry_forward import match_carry_forward

    # Three orthogonal direction vectors → cosine{a-vs-a}=1.0,
    # any-vs-different=0. We then nudge the prior vector so cos is ~0.92.
    new_topic = "FM-A topic"
    new_detail = "FM-A detail body"
    prior_text_match = "FM-A topic\nFM-A detail body reworded slightly"
    prior_text_other = "completely unrelated tax substrate\nconcern"
    new_text = new_topic + "\n" + new_detail

    v_new = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    # 0.92 cosine: same direction with small perpendicular component.
    v_prior_match = np.array([0.92, 0.39, 0.0], dtype=np.float32)
    v_prior_match /= np.linalg.norm(v_prior_match)
    # Orthogonal to new — cosine ~0.
    v_prior_other = np.array([0.0, 1.0, 0.0], dtype=np.float32)

    text_to_vec = {
        new_text: v_new,
        prior_text_match: v_prior_match,
        prior_text_other: v_prior_other,
    }

    new_objs = [_FakeObjection(topic=new_topic, detail=new_detail)]
    prior = [
        _FakePriorState(
            plan_version_id=10,
            objection_index=0,
            topic_hash="prior-hash-different",
            topic="FM-A topic",
            detail="FM-A detail body reworded slightly",
            stance="AGREE",
            counter_position="prior resolution note",
        ),
        _FakePriorState(
            plan_version_id=10,
            objection_index=1,
            topic_hash="prior-hash-other",
            topic="completely unrelated tax substrate",
            detail="concern",
            stance="DISAGREE",
            counter_position="prior counter",
        ),
    ]

    with patch(
        "argosy.services.objection_carry_forward.encode_batch",
        side_effect=_mock_embeddings(text_to_vec),
    ):
        matches = match_carry_forward(new_objs, prior)

    assert 0 in matches
    m = matches[0]
    assert m.match_kind == "embedding"
    assert m.score > 0.85
    assert m.top2_score is not None
    assert m.top2_score < 0.5  # The "other" prior was orthogonal
    assert m.prior_stance == "AGREE"
    assert m.prior_counter_position == "prior resolution note"
    assert m.embedding_model is not None
    assert m.embedding_model_version is not None


def test_embedding_score_below_threshold_no_match():
    """Cosine < 0.85 → no carry-forward. The objection is treated
    as fresh."""
    from argosy.services.objection_carry_forward import match_carry_forward

    new_topic, new_detail = "topic A", "detail A"
    prior_text = "topic B\ndetail B unrelated"
    new_text = new_topic + "\n" + new_detail

    # Cosine 0.6 — above zero but well below 0.85 threshold.
    v_new = np.array([1.0, 0.0], dtype=np.float32)
    v_prior = np.array([0.6, 0.8], dtype=np.float32)  # cos with v_new = 0.6
    text_to_vec = {new_text: v_new, prior_text: v_prior}

    new_objs = [_FakeObjection(topic=new_topic, detail=new_detail)]
    prior = [
        _FakePriorState(
            plan_version_id=5,
            objection_index=0,
            topic_hash="different",
            topic="topic B",
            detail="detail B unrelated",
            stance="AGREE",
        ),
    ]

    with patch(
        "argosy.services.objection_carry_forward.encode_batch",
        side_effect=_mock_embeddings(text_to_vec),
    ):
        matches = match_carry_forward(new_objs, prior)

    assert matches == {}


def test_ambiguity_guard_abstains_when_top1_top2_too_close():
    """Two prior objections both score above threshold and within
    0.05 of each other → matcher must abstain (no carry-forward).
    The new objection raises fresh; user re-disambiguates."""
    from argosy.services.objection_carry_forward import match_carry_forward

    new_topic, new_detail = "ambiguous topic", "ambiguous detail"
    prior_a_text = "prior A topic\nprior A detail"
    prior_b_text = "prior B topic\nprior B detail"
    new_text = new_topic + "\n" + new_detail

    # Both priors score ~0.90 vs new; difference 0.02 (below 0.05 margin).
    v_new = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    v_a = np.array([0.90, 0.435, 0.0], dtype=np.float32)
    v_a /= np.linalg.norm(v_a)
    v_b = np.array([0.92, 0.39, 0.0], dtype=np.float32)
    v_b /= np.linalg.norm(v_b)
    text_to_vec = {new_text: v_new, prior_a_text: v_a, prior_b_text: v_b}

    new_objs = [_FakeObjection(topic=new_topic, detail=new_detail)]
    prior = [
        _FakePriorState(
            plan_version_id=7,
            objection_index=0,
            topic_hash="a",
            topic="prior A topic",
            detail="prior A detail",
            stance="AGREE",
        ),
        _FakePriorState(
            plan_version_id=7,
            objection_index=1,
            topic_hash="b",
            topic="prior B topic",
            detail="prior B detail",
            stance="DISAGREE",
            counter_position="alt",
        ),
    ]

    with patch(
        "argosy.services.objection_carry_forward.encode_batch",
        side_effect=_mock_embeddings(text_to_vec),
    ):
        matches = match_carry_forward(new_objs, prior)

    # Both candidates similar enough that we abstain rather than risk
    # carrying the wrong prior stance forward.
    assert matches == {}


def test_exact_hash_takes_precedence_over_embedding():
    """When a new objection has an exact-hash match to one prior row
    AND a high-similarity embedding match to a DIFFERENT prior row,
    exact-hash wins. Embedding is the fallback, not a tiebreaker."""
    from argosy.services.objection_carry_forward import match_carry_forward
    from argosy.api.routes.plan_objection_state import _hash_objection_topic

    topic, detail = "shared text", "shared body"
    h = _hash_objection_topic(topic, detail)

    new_objs = [_FakeObjection(topic=topic, detail=detail)]
    prior = [
        _FakePriorState(
            plan_version_id=3,
            objection_index=0,
            topic_hash=h,                      # exact hash match
            topic=topic,
            detail=detail,
            stance="AGREE",
            counter_position="from exact match",
        ),
        _FakePriorState(
            plan_version_id=3,
            objection_index=1,
            topic_hash="different-hash",       # would also match via embedding
            topic="reworded shared text",
            detail="reworded shared body",
            stance="DISAGREE",
            counter_position="from embedding",
        ),
    ]

    # No mock — the embedding leg should NEVER be reached because
    # exact-hash hit on index 0.
    matches = match_carry_forward(new_objs, prior)
    assert matches[0].match_kind == "exact_hash"
    assert matches[0].prior_counter_position == "from exact match"
    assert matches[0].matched_objection_index == 0


# ----------------------------------------------------------------------
# Mixed scenarios
# ----------------------------------------------------------------------


def test_multiple_new_objections_match_independently():
    """Each new-draft objection is matched against the prior pool
    independently. Some match (exact + embedding), some don't.
    Order of returns must use new-draft index as the key."""
    from argosy.services.objection_carry_forward import match_carry_forward
    from argosy.api.routes.plan_objection_state import _hash_objection_topic

    # Objection 0 — exact hash match.
    # Objection 1 — no prior at all (no match).
    # Objection 2 — DEFER prior (skip).
    topic_0, detail_0 = "concern 0", "body 0"
    h_0 = _hash_objection_topic(topic_0, detail_0)

    new_objs = [
        _FakeObjection(topic=topic_0, detail=detail_0),
        _FakeObjection(topic="brand new", detail="never seen"),
        _FakeObjection(topic="prior-deferred", detail="ignored"),
    ]
    h_2 = _hash_objection_topic("prior-deferred", "ignored")
    prior = [
        _FakePriorState(
            plan_version_id=9,
            objection_index=0,
            topic_hash=h_0,
            topic=topic_0,
            detail=detail_0,
            stance="AGREE",
        ),
        _FakePriorState(
            plan_version_id=9,
            objection_index=5,
            topic_hash=h_2,
            topic="prior-deferred",
            detail="ignored",
            stance="DEFER",
        ),
    ]

    # Mock embedding so the "brand new" path doesn't actually
    # call the heavy model. Each unique text gets an orthogonal-ish
    # vector keyed by a stable content hash (NOT batch index — the
    # matcher encodes new + prior in separate batches, so index-keyed
    # mocking accidentally puts unrelated texts at the same position
    # and causes spurious cosine 1.0 matches).
    def _encode(texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 384), dtype=np.float32)
        out = np.zeros((len(texts), 384), dtype=np.float32)
        for i, t in enumerate(texts):
            slot = hash(t) % 384
            out[i, slot] = 1.0
        return out

    with patch(
        "argosy.services.objection_carry_forward.encode_batch",
        side_effect=_encode,
    ):
        matches = match_carry_forward(new_objs, prior)

    assert set(matches.keys()) == {0}
    assert matches[0].match_kind == "exact_hash"
    assert matches[0].prior_stance == "AGREE"
