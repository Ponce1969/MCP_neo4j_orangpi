"""Behavior tests for windowed profile embedding (the opt-in text windowing
in ``SentenceTransformerAdapter``).

The default path must stay byte-identical for every caller except
``scripts/build_namespace_profiles.py``, which opts into windowing so that long
``MATCH (k:Chunk)`` profile texts are not silently truncated at the model's
token window. Fakes only: no network, no real model weights.
"""

from __future__ import annotations

import math

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.sentence_transformer_adapter import (
    SentenceTransformerAdapter,
    window_texts,
)
from book_graph_rag.ports.embedding_provider_port import EmbeddingRequest

_WINDOW = 4


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
        }
    )


class _FakeTokenizer:
    """Whitespace tokenizer: one id per word with an exact ``decode`` round-trip."""

    def __init__(self) -> None:
        self._ids: dict[str, int] = {}
        self._words: list[str] = []

    def encode(self, text: str, add_special_tokens: bool = False) -> list[int]:
        ids: list[int] = []
        for word in text.split():
            if word not in self._ids:
                self._ids[word] = len(self._words)
                self._words.append(word)
            ids.append(self._ids[word])
        return ids

    def decode(self, ids: list[int]) -> str:
        return " ".join(self._words[index] for index in ids)

    def encode_for_model(self, text: str, *, window: int) -> list[int]:
        """Ids for model input, truncated like a real tokenizer asked to
        truncate: only the first ``window`` tokens survive, silently.

        ``encode`` itself never truncates (that is how ``window_texts`` can
        see every token); the loss happens only when a string is turned into
        model input — exactly where the real pipeline loses the tail.
        """
        return self.encode(text)[:window]


class _WindowedModel:
    """Fake model exposing the tokenizer and the ``max_seq_length`` window."""

    def __init__(self, tokenizer: _FakeTokenizer | None = None) -> None:
        self.tokenizer = tokenizer if tokenizer is not None else _FakeTokenizer()
        self.max_seq_length = _WINDOW

    def get_embedding_dimension(self) -> int:
        return 3

    def get_sentence_embedding_dimension(self) -> int:
        return 3


class _RecordingEncode:
    """Fake ``encode_sync``: records every string the model receives.

    When ``window`` is set it rejects any string past that many tokens — the
    stand-in for the real tokenizer's silent truncation. The vector is derived
    from the token count so tests can tell windows apart.
    """

    def __init__(self, window: int | None = None) -> None:
        self._window = window
        self.received: list[str] = []

    def __call__(self, _model: object, texts: tuple[str, ...]) -> list[tuple[float, ...]]:
        for text in texts:
            self.received.append(text)
            tokens = len(text.split())
            if self._window is not None and tokens > self._window:
                raise AssertionError(
                    f"text of {tokens} tokens exceeds the {self._window}-token window: {text!r}"
                )
        return [(1.0, float(len(text.split())), 0.0) for text in texts]


def _fingerprint(ids: list[int]) -> tuple[float, ...]:
    """Deterministic 3-dim vector derived **only** from the id sequence.

    L2-normalised, and every component is a function of the ids it receives
    (the first component is the surviving token count), so a truncated id
    sequence and a full one give provably different vectors. No text outside
    the ids can influence the result.
    """
    values = (
        float(len(ids)),
        float(sum((position + 1) * token_id for position, token_id in enumerate(ids))),
        float(sum(token_id * token_id for token_id in ids)) + 1.0,
    )
    norm = math.sqrt(sum(value * value for value in values))
    return tuple(value / norm for value in values)


class _TruncatingEncode:
    """Fake ``encode_sync`` reproducing the real model's silent truncation.

    Each string it receives is tokenized and truncated to the first
    ``window`` ids (via ``encode_for_model``), then embedded as the
    fingerprint of exactly those surviving ids. The vector therefore depends
    on nothing but what actually reached the model — a tail that never
    arrives can never show up in the result.
    """

    def __init__(self, tokenizer: _FakeTokenizer, window: int) -> None:
        self._tokenizer = tokenizer
        self._window = window
        self.received: list[str] = []

    def __call__(self, _model: object, texts: tuple[str, ...]) -> list[tuple[float, ...]]:
        rows: list[tuple[float, ...]] = []
        for text in texts:
            self.received.append(text)
            rows.append(_fingerprint(self._tokenizer.encode_for_model(text, window=self._window)))
        return rows


async def test_flag_on_makes_tail_tokens_contribute_to_the_vector() -> None:
    """The tail of a long text reaches the vector with ON; with OFF it cannot.

    Proves the actual defect by construction, not just the API gap: the fake
    tokenizer truncates to the first ``window`` tokens when producing model
    input, and the fake ``encode`` derives its vector solely from the ids it
    receives, so truncated and full inputs give provably different vectors.
    One tokenizer/model instance is shared by all three embeddings so the ids
    are comparable across calls.
    """
    tokenizer = _FakeTokenizer()
    model = _WindowedModel(tokenizer)
    encode = _TruncatingEncode(tokenizer, _WINDOW)
    off_adapter = SentenceTransformerAdapter(
        _settings(),
        encode_sync=encode,
        model_loader=lambda _model_id: model,
    )
    on_adapter = SentenceTransformerAdapter(
        _settings(),
        encode_sync=encode,
        model_loader=lambda _model_id: model,
        window_long_texts=True,
    )
    long_text = " ".join(f"t{i}" for i in range(12))  # 12 tokens = 3 windows of 4
    first_window = "t0 t1 t2 t3"

    on_batch = await on_adapter.embed(EmbeddingRequest(texts=(long_text,), model_id="fake-x"))
    off_batch = await off_adapter.embed(EmbeddingRequest(texts=(long_text,), model_id="fake-x"))
    first_batch = await off_adapter.embed(
        EmbeddingRequest(texts=(first_window,), model_id="fake-x")
    )
    on_full = on_batch.vectors[0].values
    off_full = off_batch.vectors[0].values
    first_alone = first_batch.vectors[0].values

    # Tail must contribute when ON: the windowed mean includes windows 2 and 3
    # (tokens t4..t11), so it cannot equal the vector of the first window
    # alone. Equality here would mean the tail contributes nothing — i.e. the
    # truncation bug is back.
    assert on_full != first_alone
    # Documents today's loss with the flag OFF (the regression guard, not an
    # endorsement): embedding the full text yields exactly the vector of its
    # first window, because the tokenizer silently drops the rest before the
    # model sees it — equality is the defect, byte-for-byte.
    assert off_full == first_alone


async def test_flag_off_passes_texts_through_unchanged() -> None:
    """Default flag (OFF): the model receives one entry per text, unchanged.

    This is the regression guard for every other caller: routing questions,
    entity-resolution anchors and evaluation runs embed through the same
    adapter and must keep byte-identical ``model.encode`` inputs.
    """
    encode = _RecordingEncode()
    adapter = SentenceTransformerAdapter(
        _settings(),
        encode_sync=encode,
        model_loader=lambda _model_id: _WindowedModel(),
    )
    long_text = " ".join(f"t{i}" for i in range(12))
    request = EmbeddingRequest(texts=(long_text, "short"), model_id="fake-x")

    batch = await adapter.embed(request)

    assert encode.received == [long_text, "short"]
    assert len(batch.vectors) == 2


async def test_flag_on_covers_every_token_and_returns_one_vector() -> None:
    """ON: every token of a long text reaches the model; one vector comes back."""
    encode = _RecordingEncode(window=_WINDOW)
    adapter = SentenceTransformerAdapter(
        _settings(),
        encode_sync=encode,
        model_loader=lambda _model_id: _WindowedModel(),
        window_long_texts=True,
    )
    text = " ".join(f"t{i}" for i in range(12))  # 12 tokens = 3 windows of 4

    batch = await adapter.embed(EmbeddingRequest(texts=(text,), model_id="fake-x"))

    # Windows are consecutive and non-overlapping: their tokens, concatenated
    # in order, reproduce the original text — nothing was dropped.
    received = [token for piece in encode.received for token in piece.split()]
    assert received == text.split()
    assert len(batch.vectors) == 1


async def test_flag_on_short_text_yields_single_window() -> None:
    """ON: a text inside the window is not split pointlessly."""
    encode = _RecordingEncode(window=_WINDOW)
    adapter = SentenceTransformerAdapter(
        _settings(),
        encode_sync=encode,
        model_loader=lambda _model_id: _WindowedModel(),
        window_long_texts=True,
    )
    text = "only two"

    batch = await adapter.embed(EmbeddingRequest(texts=(text,), model_id="fake-x"))

    assert encode.received == [text]  # exactly one window, the text itself
    assert len(batch.vectors) == 1
    assert batch.vectors[0].values == pytest.approx((1.0, 2.0, 0.0))


async def test_flag_on_returns_one_vector_per_text_in_order() -> None:
    """ON: several texts still yield exactly one vector each, in input order."""
    encode = _RecordingEncode(window=_WINDOW)
    adapter = SentenceTransformerAdapter(
        _settings(),
        encode_sync=encode,
        model_loader=lambda _model_id: _WindowedModel(),
        window_long_texts=True,
    )
    texts = ("alpha a1 a2 a3 a4 a5", "beta", "gamma g1 g2")

    batch = await adapter.embed(EmbeddingRequest(texts=texts, model_id="fake-x"))

    assert len(batch.vectors) == len(texts)
    # First text windows into 4+2 tokens → vectors (1,4,0) and (1,2,0) →
    # mean (1,3,0) → re-normalized to unit norm.
    assert batch.vectors[0].values == pytest.approx((1 / math.sqrt(10), 3 / math.sqrt(10), 0.0))
    # Single-window texts keep their raw (already unit-scale) vector.
    assert batch.vectors[1].values == pytest.approx((1.0, 1.0, 0.0))
    assert batch.vectors[2].values == pytest.approx((1.0, 3.0, 0.0))


async def test_flag_on_empty_text_yields_one_vector_without_bogus_window() -> None:
    """ON: an empty text does not crash and does not invent a window."""
    encode = _RecordingEncode(window=_WINDOW)
    adapter = SentenceTransformerAdapter(
        _settings(),
        encode_sync=encode,
        model_loader=lambda _model_id: _WindowedModel(),
        window_long_texts=True,
    )

    batch = await adapter.embed(EmbeddingRequest(texts=("", "second"), model_id="fake-x"))

    assert len(batch.vectors) == 2
    # The empty text is embedded as-is (no token window was manufactured).
    assert encode.received == ["", "second"]
    assert window_texts(("",), _FakeTokenizer(), _WINDOW) == [[]]


def test_window_texts_splits_ten_tokens_into_three_windows() -> None:
    """Pure helper: 10 tokens / window of 4 → 3 windows that reconstruct."""
    tokenizer = _FakeTokenizer()
    text = " ".join(f"w{i}" for i in range(10))

    windows = window_texts((text,), tokenizer, 4)

    assert windows == [["w0 w1 w2 w3", "w4 w5 w6 w7", "w8 w9"]]
    # Consecutive and non-overlapping: joined back, the text is unchanged.
    assert " ".join(windows[0]) == text
