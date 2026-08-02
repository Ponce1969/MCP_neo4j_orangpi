"""Tests for Text2Cypher port, adapters, and MCP wiring (REQ-GR.4)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import (
    CypherGenerationError,
    Text2CypherTimeoutError,
    UnsafeCypherQueryError,
)
from book_graph_rag.infrastructure.text2cypher_adapter import Text2CypherAdapter
from book_graph_rag.ports.cypher_generator_port import (
    CypherFailureContext,
    CypherGeneratorPort,
)
from book_graph_rag.ports.text2cypher_port import (
    Text2CypherPort,
    Text2CypherResult,
)


class _FakeCypherGeneratorPort(CypherGeneratorPort):
    """Minimal implementation for ABC instantiation smoke test."""

    async def generate_cypher(
        self, schema: str, question: str, failure: CypherFailureContext | None
    ) -> str:
        return "MATCH (n) RETURN n LIMIT 100"


class _FakeText2CypherPort(Text2CypherPort):
    """Minimal implementation for ABC instantiation smoke test."""

    async def generate_and_run(self, question: str) -> Text2CypherResult:
        return Text2CypherResult(
            question=question,
            cypher="MATCH (n) RETURN n LIMIT 100",
            rows=[{"n": {}}],
            schema_source="hardcoded",
            retries=0,
        )


# ── Ports ────────────────────────────────────────────────────────────────────


def test_cypher_generator_port_abstract_cannot_instantiate() -> None:
    with pytest.raises(TypeError, match="abstract"):
        CypherGeneratorPort()  # type: ignore[abstract]


async def test_cypher_generator_port_fake_can_generate_cypher() -> None:
    port = _FakeCypherGeneratorPort()
    cypher = await port.generate_cypher("schema", "question", None)
    assert cypher == "MATCH (n) RETURN n LIMIT 100"


async def test_cypher_generator_port_fake_uses_failure_context() -> None:
    port = _FakeCypherGeneratorPort()
    failure = CypherFailureContext(failed_cypher="BAD", error_message="oops")
    cypher = await port.generate_cypher("schema", "question", failure)
    assert cypher == "MATCH (n) RETURN n LIMIT 100"


def test_text2cypher_port_abstract_cannot_instantiate() -> None:
    with pytest.raises(TypeError, match="abstract"):
        Text2CypherPort()  # type: ignore[abstract]


async def test_text2cypher_port_fake_returns_result() -> None:
    port = _FakeText2CypherPort()
    result = await port.generate_and_run("what is graph rag?")
    assert result.question == "what is graph rag?"
    assert result.cypher == "MATCH (n) RETURN n LIMIT 100"
    assert result.rows == [{"n": {}}]
    assert result.schema_source == "hardcoded"
    assert result.retries == 0


# ── Domain exceptions ────────────────────────────────────────────────────────


def test_unsafe_cypher_query_error_is_exception() -> None:
    exc = UnsafeCypherQueryError("CREATE found")
    assert str(exc) == "CREATE found"
    assert isinstance(exc, Exception)


def test_text2cypher_timeout_error_is_exception() -> None:
    exc = Text2CypherTimeoutError("timeout")
    assert str(exc) == "timeout"
    assert isinstance(exc, Exception)


# ── Config ───────────────────────────────────────────────────────────────────


def test_settings_text2cypher_timeout_default_is_10() -> None:
    settings = Settings(
        neo4j_uri="bolt://localhost",
        neo4j_user="neo4j",
        neo4j_password="secret",
    )
    assert settings.text2cypher_timeout == 10


def test_settings_text2cypher_timeout_must_be_within_range() -> None:
    with pytest.raises(ValueError, match="text2cypher_timeout"):
        Settings(
            neo4j_uri="bolt://localhost",
            neo4j_user="neo4j",
            neo4j_password="secret",
            text2cypher_timeout=0,
        )

    with pytest.raises(ValueError, match="text2cypher_timeout"):
        Settings(
            neo4j_uri="bolt://localhost",
            neo4j_user="neo4j",
            neo4j_password="secret",
            text2cypher_timeout=61,
        )


# ── Text2CypherAdapter fakes ─────────────────────────────────────────────────


class _FakeCypherGenerator(CypherGeneratorPort):
    """Records calls and returns configurable Cypher strings."""

    def __init__(self, cyphers: list[str] | None = None) -> None:
        self._cyphers = list(cyphers or ["MATCH (n) RETURN n LIMIT 100"])
        self.calls: list[tuple[str, str, CypherFailureContext | None]] = []

    async def generate_cypher(
        self, schema: str, question: str, failure: CypherFailureContext | None
    ) -> str:
        self.calls.append((schema, question, failure))
        return self._cyphers.pop(0)


class _FakeExecutor:
    """Stand-in for Neo4jQueryAdapter exposing only text2cypher methods."""

    def __init__(self) -> None:
        self.apoc_result: list[dict[str, Any]] | Exception = []
        self.explain_should_fail: list[Exception] = []
        self.rows: list[dict[str, Any]] = []
        self.explain_calls: list[str] = []
        self.execute_read_calls: list[str] = []

    async def explain(self, cypher: str) -> None:
        self.explain_calls.append(cypher)
        if self.explain_should_fail:
            raise self.explain_should_fail.pop(0)

    async def execute_read(self, cypher: str) -> list[dict[str, Any]]:
        self.execute_read_calls.append(cypher)
        if "apoc.meta.data" in cypher:
            if isinstance(self.apoc_result, Exception):
                raise self.apoc_result
            return self.apoc_result
        return self.rows


@pytest.fixture
def settings() -> Settings:
    return Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "text2cypher_timeout": 10,
        }
    )


# ── Text2CypherAdapter ───────────────────────────────────────────────────────


async def test_text2cypher_happy_path_returns_rows_and_apoc_schema(
    settings: Settings,
) -> None:
    """APOC schema succeeds, cypher passes EXPLAIN, executes, returns rows."""
    generator = _FakeCypherGenerator(["MATCH (e:Entity) RETURN e LIMIT 100"])
    executor = _FakeExecutor()
    executor.apoc_result = [
        {"label": "Entity", "relationships": ["RELATED"], "property": "name"}
    ]
    executor.rows = [{"e": {"name": "MCP"}}]

    adapter = Text2CypherAdapter(executor, generator, settings)
    result = await adapter.generate_and_run("find entities")

    assert result.question == "find entities"
    assert result.cypher == "MATCH (e:Entity) RETURN e LIMIT 100"
    assert result.rows == [{"e": {"name": "MCP"}}]
    assert result.schema_source == "apoc"
    assert result.retries == 0


async def test_text2cypher_apoc_failure_falls_back_to_hardcoded_schema(
    settings: Settings,
) -> None:
    """When APOC raises, the adapter uses the hardcoded schema."""
    generator = _FakeCypherGenerator(["MATCH (c:Chunk) RETURN c LIMIT 100"])
    executor = _FakeExecutor()
    executor.apoc_result = RuntimeError("APOC unavailable")
    executor.rows = [{"c": {"text": "chunk"}}]

    adapter = Text2CypherAdapter(executor, generator, settings)
    result = await adapter.generate_and_run("find chunks")

    assert result.schema_source == "hardcoded"
    assert result.rows == [{"c": {"text": "chunk"}}]


async def test_text2cypher_rejects_write_query_before_explain(
    settings: Settings,
) -> None:
    """A generated write query is rejected before EXPLAIN or execute."""
    generator = _FakeCypherGenerator(["MATCH (n) DETACH DELETE n"])
    executor = _FakeExecutor()

    adapter = Text2CypherAdapter(executor, generator, settings)

    with pytest.raises(UnsafeCypherQueryError):
        await adapter.generate_and_run("delete everything")

    assert executor.explain_calls == []
    assert "MATCH (n) DETACH DELETE n" not in executor.execute_read_calls


async def test_text2cypher_rejects_call_dbms_before_explain(
    settings: Settings,
) -> None:
    """A CALL dbms.* query is rejected before EXPLAIN or execute."""
    generator = _FakeCypherGenerator(["CALL dbms.security.listUsers()"])
    executor = _FakeExecutor()

    adapter = Text2CypherAdapter(executor, generator, settings)

    with pytest.raises(UnsafeCypherQueryError):
        await adapter.generate_and_run("list users")

    assert executor.explain_calls == []
    assert "CALL dbms.security.listUsers()" not in executor.execute_read_calls


async def test_text2cypher_self_heals_on_explain_failure(
    settings: Settings,
) -> None:
    """First EXPLAIN fails, second succeeds; adapter retries once."""
    generator = _FakeCypherGenerator(
        [
            "MATCH (e:Entitty) RETURN e LIMIT 100",
            "MATCH (e:Entity) RETURN e LIMIT 100",
        ]
    )
    executor = _FakeExecutor()
    executor.explain_should_fail = [RuntimeError("label not found")]
    executor.rows = [{"e": {"name": "Agent"}}]

    adapter = Text2CypherAdapter(executor, generator, settings)
    result = await adapter.generate_and_run("find entities")

    assert result.cypher == "MATCH (e:Entity) RETURN e LIMIT 100"
    assert result.retries == 1
    assert len(executor.explain_calls) == 2
    assert executor.execute_read_calls[-1] == "MATCH (e:Entity) RETURN e LIMIT 100"
    assert generator.calls[1][2] == CypherFailureContext(
        failed_cypher="MATCH (e:Entitty) RETURN e LIMIT 100",
        error_message="label not found",
    )


async def test_text2cypher_exhausted_retries_raises_generation_error(
    settings: Settings,
) -> None:
    """Two EXPLAIN failures in a row result in CypherGenerationError."""
    generator = _FakeCypherGenerator(
        [
            "MATCH (e:Entitty) RETURN e LIMIT 100",
            "MATCH (e:Entitty) RETURN e LIMIT 100",
            "MATCH (e:Entitty) RETURN e LIMIT 100",
        ]
    )
    executor = _FakeExecutor()
    executor.explain_should_fail = [
        RuntimeError("label not found"),
        RuntimeError("label not found"),
        RuntimeError("label not found"),
    ]

    adapter = Text2CypherAdapter(executor, generator, settings)

    with pytest.raises(CypherGenerationError, match="label not found"):
        await adapter.generate_and_run("find entities")

    assert len(executor.explain_calls) == 3
    assert "MATCH (e:Entitty) RETURN e LIMIT 100" not in executor.execute_read_calls


async def test_text2cypher_timeout_raises_domain_error(
    settings: Settings,
) -> None:
    """A pipeline that exceeds the timeout raises Text2CypherTimeoutError."""
    generator = _FakeCypherGenerator()
    executor = _FakeExecutor()

    async def slow_explain(cypher: str) -> None:
        await asyncio.sleep(2)

    executor.explain = slow_explain  # type: ignore[method-assign]

    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "text2cypher_timeout": 1,
        }
    )

    adapter = Text2CypherAdapter(executor, generator, settings)

    with pytest.raises(Text2CypherTimeoutError):
        await adapter.generate_and_run("find entities")


async def test_text2cypher_result_returns_last_cypher_on_exhausted_retries(
    settings: Settings,
) -> None:
    """On exhaustion the result carries the last generated cypher for debugging."""
    generator = _FakeCypherGenerator(
        [
            "MATCH (e:Entitty) RETURN e LIMIT 100",
            "MATCH (e:Entitty) RETURN e LIMIT 100",
            "MATCH (e:Entitty) RETURN e LIMIT 100",
        ]
    )
    executor = _FakeExecutor()
    executor.explain_should_fail = [
        RuntimeError("first"),
        RuntimeError("second"),
        RuntimeError("third"),
    ]

    adapter = Text2CypherAdapter(executor, generator, settings)

    with pytest.raises(CypherGenerationError) as exc_info:
        await adapter.generate_and_run("find entities")

    assert "MATCH (e:Entitty) RETURN e LIMIT 100" in str(exc_info.value)
