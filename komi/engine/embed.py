"""komi-learn — the embedding layer for semantic (meaning-based) recall.

Default recall is now semantic: a learning about "test suites" can surface when
you searched "unit tests", which keyword matching misses. This uses a LOCAL
sentence-transformers model (offline, no API key, no per-use cost) once installed
via the ``smart`` extra.

Zero-dependency safety is preserved by design: the import is guarded. If
sentence-transformers (or numpy) isn't installed, :func:`get_embedder` returns
None and the store/recall fall back to keyword FTS — nothing breaks, recall is
just less semantic until the model is present.

Vectors are L2-normalized at encode time, so cosine similarity is a plain dot
product (fast, no per-query normalization).
"""

from __future__ import annotations

import math
from typing import Optional, Protocol


# Small, fast, good-enough model. Override with KOMI_EMBED_MODEL.
import os
_DEFAULT_MODEL = os.environ.get("KOMI_EMBED_MODEL", "all-MiniLM-L6-v2")

# The embedding format version. Bump if the model/normalization changes so stale
# cached vectors are recomputed rather than mixed with incompatible ones.
EMBED_VERSION = "minilm-l6-v2/1"


class Embedder(Protocol):
    version: str
    dim: int
    def encode(self, text: str) -> list[float]: ...


class _SentenceTransformerEmbedder:
    """Local model embedder. Loaded lazily — the model only loads on first use,
    so importing this module stays cheap and the keyword path pays nothing."""

    def __init__(self, model_name: str = _DEFAULT_MODEL):
        self.model_name = model_name
        self.version = EMBED_VERSION
        self._model = None
        self._dim = 0

    def _ensure(self) -> bool:
        if self._model is not None:
            return True
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
            # method was renamed across versions; support both.
            getdim = (getattr(self._model, "get_embedding_dimension", None)
                      or self._model.get_sentence_embedding_dimension)
            self._dim = int(getdim())
            return True
        except Exception:
            self._model = None
            return False

    @property
    def dim(self) -> int:
        self._ensure()
        return self._dim

    def encode(self, text: str) -> list[float]:
        if not self._ensure():
            return []
        vec = self._model.encode(text or "", normalize_embeddings=True)
        return [float(x) for x in vec]


_cached: Optional[object] = None
_resolved = False


def get_embedder() -> Optional[Embedder]:
    """Return a working embedder, or None if the model backend isn't available.

    Result is cached per process. None is the signal to fall back to keyword
    search — callers must handle it (the whole point of the zero-dep guarantee)."""
    global _cached, _resolved
    if _resolved:
        return _cached  # type: ignore[return-value]
    _resolved = True
    try:
        emb = _SentenceTransformerEmbedder()
        # probe: actually load + encode something tiny. If the wheel/model is
        # missing or load fails, we get [] and treat the embedder as unavailable.
        if emb.encode("probe"):
            _cached = emb
        else:
            _cached = None
    except Exception:
        _cached = None
    return _cached  # type: ignore[return-value]


def available() -> bool:
    """True if semantic search is usable right now (model present + loadable)."""
    return get_embedder() is not None


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Vectors from this module are already normalized, so this
    is a dot product, but we normalize defensively in case of mixed sources."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _reset_cache_for_tests() -> None:
    """Test hook: clear the resolved embedder so a mock can be injected."""
    global _cached, _resolved
    _cached, _resolved = None, False


__all__ = ["Embedder", "get_embedder", "available", "cosine", "EMBED_VERSION"]
