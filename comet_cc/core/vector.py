"""Local embedder — BGE-M3 via sentence-transformers + numpy cosine search."""

from __future__ import annotations

import threading

import numpy as np
from loguru import logger

_MODEL_NAME = "BAAI/bge-m3"
_DIM = 1024

_model = None
_model_lock = threading.Lock()


def _load_model():
    """Lazy-load. First call downloads ~560MB into HF cache; subsequent calls reuse."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                logger.info(f"Loading embedder: {_MODEL_NAME}")
                _model = SentenceTransformer(_MODEL_NAME)
    return _model


def embed(text: str) -> np.ndarray:
    """Single-text embedding. Returns L2-normalized float32 vector of dim=1024."""
    model = _load_model()
    vec = model.encode(text, normalize_embeddings=True, show_progress_bar=False)
    return vec.astype(np.float32)


def embed_batch(texts: list[str]) -> np.ndarray:
    model = _load_model()
    vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False,
                        batch_size=16)
    return vecs.astype(np.float32)


def cosine_search(
    query: np.ndarray,
    candidates: list[tuple[str, np.ndarray]],
    top_k: int = 5,
    min_score: float = 0.30,
) -> list[tuple[str, float]]:
    """Cosine similarity search. Candidates assumed pre-normalized.

    Returns (id, score) pairs sorted desc, filtered by min_score.
    """
    if not candidates:
        return []
    ids = [c[0] for c in candidates]
    mat = np.stack([c[1] for c in candidates])
    scores = mat @ query  # both normalized → dot product = cosine
    ranked = sorted(zip(ids, scores.tolist()), key=lambda x: x[1], reverse=True)
    return [(i, s) for i, s in ranked[:top_k] if s >= min_score]


DIM = _DIM
