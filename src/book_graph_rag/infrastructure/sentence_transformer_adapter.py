"""Local sentence-transformers implementation of ``EmbeddingProviderPort``.

The heavy ``sentence_transformers`` import is deferred until a model actually
needs to be loaded, keeping module-import time fast and unit tests without the
dependency runnable. Production callers use the default model loader; tests can
inject a fake ``_encode_sync`` and ``_model_loader`` for deterministic vectors.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from book_graph_rag.config import Settings
from book_graph_rag.ports.embedding_provider_port import (
    EmbeddingBatch,
    EmbeddingProviderPort,
    EmbeddingRequest,
    EmbeddingVector,
)

logger = logging.getLogger(__name__)

# Fallback window when a backend does not expose ``max_seq_length`` (fake or
# minimal models). 128 is the token window of
# ``paraphrase-multilingual-MiniLM-L12-v2``, this project's default embedding
# model, so the fallback reproduces the historical truncation point instead of
# inventing a new one.
_FALLBACK_MAX_TOKENS = 128


class _WindowTokenizer(Protocol):
    """Minimal tokenizer shape required to window texts; HF tokenizers fit."""

    def encode(self, text: str, add_special_tokens: bool = ...) -> list[int]: ...

    def decode(self, ids: list[int]) -> str: ...


def window_texts(
    texts: Sequence[str],
    tokenizer: _WindowTokenizer,
    max_tokens: int,
) -> list[list[str]]:
    """Split each text into consecutive token-bounded windows.

    Windows do not overlap on purpose: the caller averages the window
    representations into one vector per text, and overlapping windows would
    double-count the shared tokens in that average without adding information.
    An empty text yields an empty window list (never a bogus empty window), so
    no text is invented or dropped.
    """
    if max_tokens < 1:
        raise ValueError(f"max_tokens must be >= 1, got {max_tokens}")
    windows_per_text: list[list[str]] = []
    for text in texts:
        token_ids = tokenizer.encode(text, add_special_tokens=False)
        windows_per_text.append(
            [
                tokenizer.decode(token_ids[start : start + max_tokens])
                for start in range(0, len(token_ids), max_tokens)
            ]
        )
    return windows_per_text


def _window_budget(model: Any, tokenizer: _WindowTokenizer) -> int:
    """Token budget per window, read from the model rather than configured.

    ``SentenceTransformer.max_seq_length`` is the model's own window; the
    documented fallback above applies only when the attribute is missing.
    Special tokens that ``model.encode`` re-adds are subtracted when the
    tokenizer can report them, so a full window is never re-truncated at embed
    time.
    """
    seq_length = getattr(model, "max_seq_length", None)
    budget = int(seq_length) if seq_length else _FALLBACK_MAX_TOKENS
    specials = getattr(tokenizer, "num_special_tokens_to_add", None)
    if callable(specials):
        budget -= int(specials(pair=False))
    if budget < 1:
        raise ValueError(f"window budget too small: {budget}")
    return budget


def _mean_renormalized(vectors: Sequence[tuple[float, ...]]) -> tuple[float, ...]:
    """Plain mean of unit window vectors, re-normalized to unit norm.

    ``normalize_embeddings=True`` already gives every window vector unit norm,
    so the plain arithmetic mean is the natural per-text aggregate; it is then
    re-normalized so the returned vector keeps the unit norm every other caller
    of this adapter observes. A mean that cancels out to the zero vector is
    returned unchanged rather than dividing by zero.
    """
    if not vectors:
        raise ValueError("cannot average zero vectors")
    dimension = len(vectors[0])
    mean = tuple(
        sum(vector[index] for vector in vectors) / len(vectors) for index in range(dimension)
    )
    norm = math.sqrt(sum(value * value for value in mean))
    if norm == 0.0:
        return mean
    return tuple(value / norm for value in mean)


class SentenceTransformerAdapter(EmbeddingProviderPort):
    """Embeds texts using a local ``sentence-transformers`` model.

    - Loads from ``settings.embedding_local_path / model_id`` when present,
      otherwise falls back to the Hugging Face hub identifier.
    - Caches loaded models per process.
    - Runs the synchronous ``encode`` call off the asyncio event loop.
    - ``model_dim`` prefers ``get_embedding_dimension()`` and falls back to
      ``get_sentence_embedding_dimension()`` for compatibility.
    - ``window_long_texts`` (default OFF) embeds every token window of each
      text and averages the window vectors into one vector per text. It is
      opt-in because the same adapter class serves routing questions,
      entity-resolution anchors and evaluation runs whose measured numbers
      must stay comparable; OFF keeps the historical call path untouched.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        encode_sync: Any | None = None,
        model_loader: Any | None = None,
        window_long_texts: bool = False,
    ) -> None:
        self._settings = settings
        self._models: dict[str, Any] = {}
        self._encode_sync = encode_sync if encode_sync is not None else self._default_encode_sync
        self._model_loader = (
            model_loader if model_loader is not None else self._default_model_loader
        )
        self._window_long_texts = window_long_texts

    async def embed(self, request: EmbeddingRequest) -> EmbeddingBatch:
        """Embed ``request.texts`` using the requested model.

        With ``window_long_texts`` OFF (the default) the texts reach
        ``encode_sync`` unchanged — the historical call path, byte-identical.
        With it ON, every text is windowed, all windows are embedded, and their
        vectors are averaged into exactly one vector per input text, in input
        order.
        """
        model = self._load_model(request.model_id)
        if self._window_long_texts:
            vectors = await asyncio.to_thread(self._encode_windowed, model, request.texts)
        else:
            vectors = await asyncio.to_thread(self._encode_sync, model, request.texts)
        return EmbeddingBatch(
            model_id=request.model_id,
            vectors=[EmbeddingVector(values=tuple(v), model_id=request.model_id) for v in vectors],
        )

    def model_dim(self, model_id: str) -> int:
        """Return the embedding dimension reported by ``model_id``."""
        model = self._load_model(model_id)
        dim_fn: Any = getattr(model, "get_embedding_dimension", None)
        if dim_fn is None:
            dim_fn = model.get_sentence_embedding_dimension
        return int(dim_fn())

    def _load_model(self, model_id: str) -> Any:
        """Return a cached model, loading it on first use."""
        if model_id in self._models:
            return self._models[model_id]
        model = self._model_loader(model_id)
        self._models[model_id] = model
        return model

    def _default_model_loader(self, model_id: str) -> Any:
        """Load a SentenceTransformer model from local path or HF hub."""
        from sentence_transformers import SentenceTransformer

        local_path = self._settings.embedding_local_path
        if local_path:
            path = Path(local_path) / model_id
            if path.exists():
                logger.debug("Loading model %s from local path %s", model_id, path)
                return SentenceTransformer(str(path))
        logger.debug("Loading model %s from Hugging Face hub", model_id)
        return SentenceTransformer(model_id)

    def _encode_windowed(self, model: Any, texts: tuple[str, ...]) -> list[tuple[float, ...]]:
        """Embed every window of every text; average to one vector per text."""
        tokenizer: _WindowTokenizer = model.tokenizer
        budget = _window_budget(model, tokenizer)
        encoded: list[tuple[float, ...]] = []
        for text in texts:
            windows = window_texts((text,), tokenizer, budget)[0]
            if not windows:
                # Empty text: there are no tokens to window, but the batch must
                # still yield exactly one vector per input — embed the original
                # string directly instead of inventing a bogus window.
                windows = [text]
            rows = [
                tuple(float(value) for value in row)
                for row in self._encode_sync(model, tuple(windows))
            ]
            # One window is already the text's vector (unit norm); only a
            # multi-window mean needs re-normalization.
            encoded.append(rows[0] if len(rows) == 1 else _mean_renormalized(rows))
        return encoded

    @staticmethod
    def _default_encode_sync(model: Any, texts: tuple[str, ...]) -> list[tuple[float, ...]]:
        """Default synchronous encode: numpy array → tuple rows."""
        arr = model.encode(list(texts), convert_to_numpy=True, normalize_embeddings=True)
        return [tuple(float(x) for x in row) for row in arr]
