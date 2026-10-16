"""Unit tests for the R5 rule: what is safe to persist about a failure.

The rule exists because the indexing failure paths used to store ``str(error)``
verbatim — exception text from an LLM adapter can carry prompts, URLs, corpus text
or credentials. What is persisted instead is the exception class name, the length of
the message, and a **keyed** HMAC-SHA256 fingerprint of it.
"""

from __future__ import annotations

import pytest

from book_graph_rag.domain.checkpoint_models import redacted_error
from book_graph_rag.domain.mcp_security import QueryFingerprint

_RAW = "LLM returned 401 for api_key=secret-token-abc on https://example.invalid/v1"
_KEY_ID = "mcp-log-v1"
_SECRET = "a" * 64


def test_fingerprint_is_the_keyed_hmac_of_the_raw_message() -> None:
    """Recomputed independently: the fingerprint is the keyed HMAC, not a bare digest."""
    redacted = redacted_error(RuntimeError(_RAW), key_id=_KEY_ID, secret=_SECRET)

    assert redacted.error_type == "RuntimeError"
    assert redacted.error_length == len(_RAW)
    assert redacted.error_fingerprint == QueryFingerprint.from_canonical(
        _KEY_ID, _SECRET, {"error_message": _RAW}
    )


def test_the_raw_text_is_absent_from_every_persisted_form_of_the_value() -> None:
    """Whatever is serialized downstream, the message itself must not be in it."""
    redacted = redacted_error(ValueError(_RAW), key_id=_KEY_ID, secret=_SECRET)

    dumped = redacted.model_dump_json()
    assert _RAW not in dumped
    assert "secret-token-abc" not in dumped
    assert repr(redacted).find("secret-token-abc") == -1


def test_two_installations_never_produce_the_same_fingerprint_for_the_same_message() -> None:
    """The fingerprint is keyed, so a bare digest is never stored under the plain key id."""
    other = redacted_error(RuntimeError(_RAW), key_id=_KEY_ID, secret="b" * 64)

    assert other.error_fingerprint is not None
    assert other.error_fingerprint.fingerprint_hex != (
        QueryFingerprint.from_canonical(_KEY_ID, _SECRET, {"error_message": _RAW}).fingerprint_hex
    )


def test_without_a_key_there_is_no_fingerprint_and_the_text_is_still_dropped() -> None:
    """No key means no fingerprint — never a fallback to the message itself."""
    redacted = redacted_error(RuntimeError(_RAW), key_id=_KEY_ID, secret="")

    assert redacted.error_fingerprint is None
    assert redacted.error_length == len(_RAW)
    assert _RAW not in redacted.model_dump_json()


def test_none_as_secret_is_the_same_as_no_key() -> None:
    """An unset setting arrives as ``None`` in some callers; it must not raise."""
    redacted = redacted_error(RuntimeError(_RAW), key_id=_KEY_ID, secret=None)

    assert redacted.error_fingerprint is None
    assert redacted.error_type == "RuntimeError"


def test_an_empty_message_is_still_a_valid_redaction() -> None:
    """A driver error with no text must not raise: the length just reads zero."""
    redacted = redacted_error(RuntimeError(""), key_id=_KEY_ID, secret=_SECRET)

    assert redacted.error_length == 0
    assert redacted.error_fingerprint is not None


def test_a_very_long_message_is_only_measured() -> None:
    """A prompt-length message is not truncated and stored: only its length is."""
    raw = "x" * 50_000
    redacted = redacted_error(RuntimeError(raw), key_id=_KEY_ID, secret=_SECRET)

    assert redacted.error_length == 50_000
    assert raw not in redacted.model_dump_json()


# The same shape rule the token, the log fingerprint and the Neo4j read credential use:
# spaces are guessable and invisible non-whitespace characters survive ``str.strip``.
@pytest.mark.parametrize(
    "weak_secret",
    [
        "a" * 31,
        "\u200b" * 40,
        "\ufeff" * 40,
        "\u200d" * 40,
        "   ",
        "",
    ],
    ids=["too-short", "zero-width-space", "bom", "zero-width-joiner", "spaces", "empty"],
)
def test_a_weak_key_never_produces_a_decorative_fingerprint(weak_secret: str) -> None:
    """A fingerprint over guessable text with a guessable key protects nothing."""
    redacted = redacted_error(RuntimeError(_RAW), key_id=_KEY_ID, secret=weak_secret)

    assert redacted.error_fingerprint is None
    assert _RAW not in redacted.model_dump_json()


def test_a_missing_key_id_also_yields_no_fingerprint() -> None:
    """A digest whose key cannot be named again is not worth storing."""
    redacted = redacted_error(RuntimeError(_RAW), key_id="", secret=_SECRET)

    assert redacted.error_fingerprint is None
    assert _RAW not in redacted.model_dump_json()
