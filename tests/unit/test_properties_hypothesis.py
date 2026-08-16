"""Property-based tests using Hypothesis for sanitization and canonicalization.

Protects invariants across arbitrary Unicode inputs, ASCII control characters,
whitespace variations, and case edge cases.
"""

from __future__ import annotations

import json
import string
from typing import get_args

from hypothesis import given
from hypothesis import strategies as st

from book_graph_rag.domain.models import EntityType
from book_graph_rag.infrastructure.llm_adapter import (
    LLMAdapter,
    _escape_json_string_control_chars,
)

# ── Strategies ────────────────────────────────────────────────────────────────

_ASCII_CONTROL_CHARS = "".join(chr(c) for c in range(32))

_CONTROL_TEXT_STRATEGY = st.text(
    alphabet=st.characters(codec="utf-8"),
    min_size=0,
    max_size=200,
)

_ENTITY_TYPE_STRATEGY = st.sampled_from(get_args(EntityType))

_NAME_STRATEGY = st.text(
    alphabet=string.ascii_letters + string.digits + " _-.:/&",
    min_size=1,
    max_size=50,
)


# ── JSON Sanitization Properties ──────────────────────────────────────────────


@given(text=_CONTROL_TEXT_STRATEGY)
def test_escape_json_string_control_chars_is_idempotent(text: str) -> None:
    """Sanitizing an already-sanitized JSON payload must not alter the content."""
    # Wrap text in a mock JSON string container
    raw_json = f'{{"content": "{text}"}}'
    first_pass = _escape_json_string_control_chars(raw_json)
    second_pass = _escape_json_string_control_chars(first_pass)

    assert first_pass == second_pass


@given(
    key=st.text(alphabet=string.ascii_letters, min_size=1, max_size=10),
    control_char=st.sampled_from([chr(c) for c in range(32)]),
    value=st.text(alphabet=string.ascii_letters + string.digits + " ", min_size=0, max_size=50),
)
def test_escape_json_string_control_chars_leaves_valid_json_parseable(
    key: str, control_char: str, value: str
) -> None:
    """Any JSON payload with raw embedded control characters parses cleanly after sanitization."""
    # Inject an actual raw control character byte into the JSON string value
    dirty_json = f'{{"{key}": "prefix{control_char}_{value}"}}'

    sanitized = _escape_json_string_control_chars(dirty_json)
    parsed = json.loads(sanitized)

    assert key in parsed
    assert isinstance(parsed[key], str)


@given(
    text=st.text(
        alphabet=st.characters(codec="utf-8", blacklist_characters='"\\'),
        min_size=0,
        max_size=100,
    )
)
def test_escape_json_string_control_chars_removes_all_raw_control_chars_in_strings(
    text: str,
) -> None:
    """No raw byte in range 0x00..0x1F may survive inside double quotes."""
    dirty_payload = f'["{text}"]'
    sanitized = _escape_json_string_control_chars(dirty_payload)

    # Within the inner quotes, check that no raw control characters exist
    assert len(sanitized) >= 4
    inner = sanitized[2:-2]  # strip [" and "]
    assert not any(c in _ASCII_CONTROL_CHARS for c in inner)


# ── Slugify Properties ────────────────────────────────────────────────────────


@given(text=st.text(min_size=0, max_size=100))
def test_slugify_contains_only_lowercase_alphanumeric_and_hyphens(text: str) -> None:
    """Slugified strings must only contain [a-z0-9-]."""
    slug = LLMAdapter._slugify(text)

    assert all(c in string.ascii_lowercase or c in string.digits or c == "-" for c in slug)


@given(text=st.text(min_size=0, max_size=100))
def test_slugify_never_starts_or_ends_with_hyphens(text: str) -> None:
    """Slugified strings must be trimmed of leading and trailing hyphens."""
    slug = LLMAdapter._slugify(text)

    if slug:
        assert not slug.startswith("-")
        assert not slug.endswith("-")


@given(text=st.text(min_size=0, max_size=100))
def test_slugify_is_idempotent(text: str) -> None:
    """Slugifying a slugified string yields the identical slug."""
    slug1 = LLMAdapter._slugify(text)
    slug2 = LLMAdapter._slugify(slug1)

    assert slug1 == slug2


@given(text=st.text(min_size=0, max_size=100))
def test_slugify_equals_slugify_of_lowercased_text(text: str) -> None:
    """Slugify always operates on lowercased input and is invariant to prior lowercasing."""
    slug_raw = LLMAdapter._slugify(text)
    slug_lower = LLMAdapter._slugify(text.lower())

    assert slug_raw == slug_lower


# ── Entity ID Resolution & Canonicalization Properties ────────────────────────


@given(
    name=_NAME_STRATEGY,
    canonical_name=st.one_of(st.none(), _NAME_STRATEGY),
    aliases=st.lists(_NAME_STRATEGY, min_size=0, max_size=5),
    entity_type=_ENTITY_TYPE_STRATEGY,
)
def test_resolve_entity_id_always_ends_with_entity_type_suffix(
    name: str,
    canonical_name: str | None,
    aliases: list[str],
    entity_type: EntityType,
) -> None:
    """Every resolved entity id is guaranteed to have the entity type as suffix."""
    entity_id, _ = LLMAdapter._resolve_entity_id(
        name=name,
        canonical_name=canonical_name,
        aliases=aliases,
        entity_type=entity_type,
    )

    assert entity_id.endswith(f"-{entity_type}")


@given(
    name=_NAME_STRATEGY,
    aliases=st.lists(st.text(min_size=1, max_size=20), min_size=1, max_size=10),
    stoplist=st.lists(st.text(min_size=1, max_size=20), min_size=1, max_size=5),
    entity_type=_ENTITY_TYPE_STRATEGY,
)
def test_resolve_entity_id_strictly_filters_stoplist_case_insensitively(
    name: str,
    aliases: list[str],
    stoplist: list[str],
    entity_type: EntityType,
) -> None:
    """No alias whose lower-case form is in the stoplist may appear in the filtered aliases."""
    stoplist_lower = {s.lower() for s in stoplist}
    _, filtered_aliases = LLMAdapter._resolve_entity_id(
        name=name,
        canonical_name=None,
        aliases=aliases,
        entity_type=entity_type,
        stoplist=stoplist,
    )

    for alias in filtered_aliases:
        assert alias.lower() not in stoplist_lower


@given(
    name=_NAME_STRATEGY,
    aliases=st.lists(_NAME_STRATEGY, min_size=0, max_size=10),
    entity_type=_ENTITY_TYPE_STRATEGY,
)
def test_resolve_entity_id_deduplicates_aliases_case_insensitively(
    name: str,
    aliases: list[str],
    entity_type: EntityType,
) -> None:
    """Returned aliases must be unique when compared case-insensitively."""
    _, filtered_aliases = LLMAdapter._resolve_entity_id(
        name=name,
        canonical_name=None,
        aliases=aliases,
        entity_type=entity_type,
    )

    seen: set[str] = set()
    for alias in filtered_aliases:
        lower = alias.lower()
        assert lower not in seen
        seen.add(lower)


@given(
    name=_NAME_STRATEGY,
    entity_type=_ENTITY_TYPE_STRATEGY,
)
def test_resolve_entity_id_identical_when_name_equals_canonical(
    name: str,
    entity_type: EntityType,
) -> None:
    """Passing a canonical_name that matches the name produces the exact same id."""
    id_without_canonical, _ = LLMAdapter._resolve_entity_id(
        name=name,
        canonical_name=None,
        aliases=(),
        entity_type=entity_type,
    )
    id_with_canonical, _ = LLMAdapter._resolve_entity_id(
        name=name,
        canonical_name=name,
        aliases=(),
        entity_type=entity_type,
    )

    assert id_without_canonical == id_with_canonical
