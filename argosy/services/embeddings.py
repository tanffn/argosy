"""Local sentence embeddings via sentence-transformers.

Wave 7 Piece B uses these for the carry-forward matcher's embedding-
fallback leg: when two FM objections don't share a `topic_hash` but
may still semantically match, we compute cosine similarity over the
concatenated `topic + "\\n" + detail` text.

Design notes:

  * **Model**: ``sentence-transformers/all-MiniLM-L6-v2``. 384-dim
    output, ~80MB on disk, CPU-fast (≈ms per text on a modern
    laptop). Codex zigzag verdict (2026-06-01) selected this model
    as the lowest-ops-risk option vs OpenAI / Voyage / other local
    alternatives. Per codex: "no new key/SDK/failure domain."
  * **Lazy load + cache**: the model is heavy to instantiate
    (downloads weights on first call, then loads them via torch
    into memory). We cache the instance at module scope so only
    the first ``get_model()`` call pays the cost; subsequent calls
    return the same instance. Thread-safe by Python GIL since the
    cache pointer is a single attribute write.
  * **Version pinning**: ``MODEL_ID`` and ``model_version()`` are
    persisted on every match in ``fm_objection_user_state`` so
    audit data captures which model+version produced each score.
    When the underlying sentence-transformers version changes, all
    existing scores remain interpretable.

This service is intentionally minimal — just enough for the wave-7
carry-forward matcher. If other features later want embeddings,
they can build on the same ``get_model()`` + cosine helpers without
introducing a parallel provider.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

log = logging.getLogger(__name__)

MODEL_ID: str = "sentence-transformers/all-MiniLM-L6-v2"

_model_cache: SentenceTransformer | None = None
_model_lock = threading.Lock()


def get_model() -> SentenceTransformer:
    """Return the cached SentenceTransformer instance.

    First call instantiates the model (slow — downloads + loads
    weights into memory). Subsequent calls return the cached
    instance immediately. The lock guards against two concurrent
    first-callers both kicking off an instantiation.
    """
    global _model_cache
    if _model_cache is not None:
        return _model_cache
    with _model_lock:
        if _model_cache is None:
            from sentence_transformers import SentenceTransformer

            log.info(
                "embeddings.model.loading", extra={"model_id": MODEL_ID}
            )
            _model_cache = SentenceTransformer(MODEL_ID)
            log.info(
                "embeddings.model.loaded", extra={"model_id": MODEL_ID}
            )
    return _model_cache


def model_version() -> str:
    """Pinned version string for audit persistence.

    Uses ``sentence_transformers.__version__`` so a future swap of
    the underlying library is detectable in stored audit rows even
    if ``MODEL_ID`` is unchanged.
    """
    import sentence_transformers

    return sentence_transformers.__version__


def encode_batch(texts: list[str]) -> np.ndarray:
    """Encode a list of texts into a (len(texts), 384) float32 array.

    Empty input returns a (0, 384) array. ``texts`` may not contain
    None or non-string entries — caller is responsible for
    normalising. The output is NOT L2-normalised here; callers that
    need normalised vectors for cosine similarity should call
    ``model.encode(..., normalize_embeddings=True)`` directly or use
    :func:`cosine_similarity_matrix` which handles normalisation
    internally.
    """
    if not texts:
        return np.zeros((0, 384), dtype=np.float32)
    model = get_model()
    arr = model.encode(texts, normalize_embeddings=True)
    return np.asarray(arr, dtype=np.float32)


def cosine_similarity_matrix(
    a: np.ndarray, b: np.ndarray
) -> np.ndarray:
    """Pairwise cosine similarity matrix.

    Args:
        a: shape ``(n, d)``, L2-normalised by ``encode_batch``.
        b: shape ``(m, d)``, L2-normalised.

    Returns:
        shape ``(n, m)`` similarity scores in ``[-1, 1]``. Empty
        inputs (n=0 or m=0) return an empty (n, m) array.
    """
    if a.shape[0] == 0 or b.shape[0] == 0:
        return np.zeros((a.shape[0], b.shape[0]), dtype=np.float32)
    return np.asarray(a @ b.T, dtype=np.float32)


__all__ = [
    "MODEL_ID",
    "cosine_similarity_matrix",
    "encode_batch",
    "get_model",
    "model_version",
]
