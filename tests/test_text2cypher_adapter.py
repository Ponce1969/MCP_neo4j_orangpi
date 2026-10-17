"""Tests for Text2Cypher port, adapters, and MCP wiring (REQ-GR.4)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from book_graph_rag.config import Settings
from book_graph_rag.domain.mcp_security import (
    ScopeContext,
    ScopeProof,
    StructuralPolicyViolationError,
)
from book_graph_rag.domain.models import (
    CypherGenerationError,
    Text2CypherTimeoutError,
    UnsafeCypherQueryError,
)
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.infrastructure.structural_cypher_policy import StructuralCypherPolicy
from book_graph_rag.infrastructure.text2cypher_adapter import (
    Text2CypherAdapter,
    _build_scope_parameter_map,
)
from book_graph_rag.ports.cypher_generator_port import (
    CypherFailureContext,
    CypherGeneratorPort,
)
from book_graph_rag.ports.mcp_security_port import (
    StructuralCypherValidator,
    StructuralValidationResult,
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

    async def generate_and_run(
        self, question: str, *, scope: ScopeContext | None = None
    ) -> Text2CypherResult:
        return Text2CypherResult(
            question=question,
            cypher="MATCH (n) RETURN n LIMIT 100",
            rows=[{"n": {}}],
            schema_source="manifest",
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
    assert result.schema_source == "manifest"
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
        neo4j_password=SecretStr("secret"),
    )
    assert settings.text2cypher_timeout == 10


def test_settings_schema_manifest_path_defaults_to_data_schema_manifest() -> None:
    settings = Settings(
        neo4j_uri="bolt://localhost",
        neo4j_user="neo4j",
        neo4j_password=SecretStr("secret"),
    )

    assert settings.schema_manifest_path == Path("data/schema/graph_schema.json")


def test_settings_text2cypher_timeout_must_be_within_range() -> None:
    with pytest.raises(ValueError, match="text2cypher_timeout"):
        Settings(
            neo4j_uri="bolt://localhost",
            neo4j_user="neo4j",
            neo4j_password=SecretStr("secret"),
            text2cypher_timeout=0,
        )

    with pytest.raises(ValueError, match="text2cypher_timeout"):
        Settings(
            neo4j_uri="bolt://localhost",
            neo4j_user="neo4j",
            neo4j_password=SecretStr("secret"),
            text2cypher_timeout=61,
        )


# ── Text2CypherAdapter fakes ─────────────────────────────────────────────────


class _FakeCypherGenerator(CypherGeneratorPort):
    """Records calls and returns configurable Cypher strings."""

    def __init__(self, cyphers: list[str] | None = None) -> None:
        self._cyphers = list(
            cyphers or ["MATCH (c:Chunk) WHERE c.book_id = $book_id RETURN c LIMIT 100"]
        )
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
        self.explain_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.execute_read_calls: list[tuple[str, dict[str, Any] | None]] = []

    async def explain(self, cypher: str, parameters: dict[str, Any] | None = None) -> None:
        self.explain_calls.append((cypher, parameters))
        if self.explain_should_fail:
            raise self.explain_should_fail.pop(0)

    async def execute_read(
        self, cypher: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.execute_read_calls.append((cypher, parameters))
        if "SHOW PROCEDURES" in cypher:
            procedure = cypher.split("'")[1]
            if procedure == "apoc.meta.data" and not isinstance(self.apoc_result, Exception):
                return [{"name": procedure}]
            return []
        if cypher == "CALL apoc.meta.data()":
            if isinstance(self.apoc_result, Exception):
                raise self.apoc_result
            return self.apoc_result
        return self.rows


class _SpyValidator(StructuralCypherValidator):
    """Recording validator that delegates to the real structural policy."""

    def __init__(self) -> None:
        self._inner = StructuralCypherPolicy()
        self.validate_calls: list[tuple[str, bool]] = []
        self.require_explain_calls: list[tuple[str, bool]] = []

    def validate(
        self,
        query: str,
        *,
        require_scope_proof: bool = False,
    ) -> StructuralValidationResult:
        self.validate_calls.append((query, require_scope_proof))
        return self._inner.validate(query, require_scope_proof=require_scope_proof)

    def require_explain(self, query: str, *, explain_applied: bool) -> None:
        self.require_explain_calls.append((query, explain_applied))
        self._inner.require_explain(query, explain_applied=explain_applied)


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
    generator = _FakeCypherGenerator(["MATCH (e:Entity) WHERE e.id = $id RETURN e LIMIT 100"])
    executor = _FakeExecutor()
    executor.apoc_result = [
        {
            "label": "Entity",
            "other": None,
            "elementType": "node",
            "type": "STRING",
            "property": "name",
        },
        {
            "label": "RELATED",
            "other": "Entity",
            "elementType": "relationship",
            "type": "STRING",
            "property": "type",
        },
    ]
    executor.rows = [{"e": {"name": "MCP"}}]

    adapter = Text2CypherAdapter(executor, generator, settings)
    result = await adapter.generate_and_run("find entities")

    assert result.question == "find entities"
    assert result.cypher == "MATCH (e:Entity) WHERE e.id = $id RETURN e LIMIT 100"
    assert result.rows == [{"e": {"name": "MCP"}}]
    assert result.schema_source == "apoc"
    assert result.retries == 0


class _TieredExecutor:
    """Scripted schema probes used to prove each rung independently."""

    def __init__(self, responses: dict[str, list[dict[str, Any]] | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def explain(self, cypher: str, parameters: dict[str, Any] | None = None) -> None:
        return None

    async def execute_read(
        self, cypher: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(cypher)
        response = self.responses.get(cypher, [])
        if isinstance(response, Exception):
            raise response
        return response


def _proc(name: str, available: bool) -> list[dict[str, Any]]:
    return [{"name": name}] if available else []


async def test_schema_ladder_uses_apoc_and_includes_observed_properties(
    settings: Settings,
) -> None:
    generator = _FakeCypherGenerator()
    apoc_probe = "SHOW PROCEDURES YIELD name WHERE name = 'apoc.meta.data'"
    apoc_call = "CALL apoc.meta.data()"
    executor = _TieredExecutor(
        {
            apoc_probe: _proc("apoc.meta.data", True),
            apoc_call: [
                {
                    "label": "Document",
                    "other": None,
                    "elementType": "node",
                    "type": "STRING",
                    "property": "title",
                },
                {
                    "label": "CITES",
                    "other": "Document",
                    "elementType": "relationship",
                    "type": "STRING",
                    "property": "since",
                },
            ],
        }
    )

    schema = await Text2CypherAdapter(executor, generator, settings)._infer_schema()

    assert schema.source == "apoc"
    assert "Document {title}" in schema.description
    assert "CITES {since}" in schema.description
    assert executor.calls[:2] == [apoc_probe, apoc_call]


async def test_schema_ladder_uses_native_procedures_without_apoc(
    settings: Settings,
) -> None:
    generator = _FakeCypherGenerator()
    apoc_probe = "SHOW PROCEDURES YIELD name WHERE name = 'apoc.meta.data'"
    node_probe = "SHOW PROCEDURES YIELD name WHERE name = 'db.schema.nodeTypeProperties'"
    rel_probe = "SHOW PROCEDURES YIELD name WHERE name = 'db.schema.relTypeProperties'"
    node_call = "CALL db.schema.nodeTypeProperties()"
    rel_call = "CALL db.schema.relTypeProperties()"
    executor = _TieredExecutor(
        {
            apoc_probe: [],
            node_probe: _proc("db.schema.nodeTypeProperties", True),
            rel_probe: _proc("db.schema.relTypeProperties", True),
            node_call: [
                {
                    "nodeLabels": ["Document"],
                    "propertyName": "title",
                    "propertyTypes": ["not-a-schema-property"],
                }
            ],
            rel_call: [
                {
                    "relType": "CITES",
                    "propertyName": "since",
                    "propertyTypes": ["not-a-schema-property"],
                }
            ],
        }
    )

    schema = await Text2CypherAdapter(executor, generator, settings)._infer_schema()

    assert schema.source == "native"
    assert "Document {title}" in schema.description
    assert "CITES {since}" in schema.description
    assert "not-a-schema-property" not in schema.description


async def test_schema_ladder_samples_labels_when_native_procedures_are_absent(
    settings: Settings,
) -> None:
    generator = _FakeCypherGenerator()
    apoc_probe = "SHOW PROCEDURES YIELD name WHERE name = 'apoc.meta.data'"
    node_probe = "SHOW PROCEDURES YIELD name WHERE name = 'db.schema.nodeTypeProperties'"
    rel_probe = "SHOW PROCEDURES YIELD name WHERE name = 'db.schema.relTypeProperties'"
    node_sample = "MATCH (n:`Document`) WITH n LIMIT 1 RETURN keys(n) AS properties"
    rel_sample = "MATCH ()-[r:`CITES`]-() WITH r LIMIT 1 RETURN keys(r) AS properties"
    executor = _TieredExecutor(
        {
            apoc_probe: [],
            node_probe: [],
            rel_probe: [],
            "CALL db.labels()": [{"label": "Document"}],
            "CALL db.relationshipTypes()": [{"relationshipType": "CITES"}],
            node_sample: [{"properties": ["title"]}],
            rel_sample: [{"properties": ["since"]}],
        }
    )

    schema = await Text2CypherAdapter(executor, generator, settings)._infer_schema()

    assert schema.source == "native"
    assert "Document {title}" in schema.description
    assert "CITES {since}" in schema.description
    assert node_sample in executor.calls
    assert rel_sample in executor.calls


async def test_schema_ladder_preserves_an_empty_graph_as_native(
    settings: Settings,
) -> None:
    generator = _FakeCypherGenerator()
    apoc_probe = "SHOW PROCEDURES YIELD name WHERE name = 'apoc.meta.data'"
    node_probe = "SHOW PROCEDURES YIELD name WHERE name = 'db.schema.nodeTypeProperties'"
    rel_probe = "SHOW PROCEDURES YIELD name WHERE name = 'db.schema.relTypeProperties'"
    executor = _TieredExecutor(
        {
            apoc_probe: [],
            node_probe: _proc("db.schema.nodeTypeProperties", True),
            rel_probe: _proc("db.schema.relTypeProperties", True),
            "CALL db.schema.nodeTypeProperties()": [],
            "CALL db.schema.relTypeProperties()": [],
        }
    )

    schema = await Text2CypherAdapter(executor, generator, settings)._infer_schema()

    assert schema.source == "native"
    assert "(empty graph)" in schema.description


async def test_schema_ladder_fails_closed_without_leaking_driver_error(
    settings: Settings,
) -> None:
    generator = _FakeCypherGenerator()
    sensitive_error = RuntimeError("bolt://neo4j:private-password@db.internal")
    probes = (
        "SHOW PROCEDURES YIELD name WHERE name = 'apoc.meta.data'",
        "SHOW PROCEDURES YIELD name WHERE name = 'db.schema.nodeTypeProperties'",
        "SHOW PROCEDURES YIELD name WHERE name = 'db.schema.relTypeProperties'",
        "CALL db.labels()",
        "CALL db.relationshipTypes()",
    )
    executor = _TieredExecutor(dict.fromkeys(probes, sensitive_error))
    adapter = Text2CypherAdapter(executor, generator, settings)

    with pytest.raises(CypherGenerationError, match="Schema inference failed") as raised:
        await adapter.generate_and_run("find records")

    assert "apoc.meta.data" in str(raised.value)
    assert "db.labels" in str(raised.value)
    assert "private-password" not in str(raised.value)
    assert generator.calls == []


async def test_text2cypher_uses_manifest_when_live_schema_is_unavailable(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from book_graph_rag.domain.schema_manifest import SchemaManifest

    manifest = SchemaManifest(
        schema_version="1.0.0",
        committed_at=datetime(2026, 10, 17, tzinfo=UTC),
        code_commit="abc1234",
        graph_version="gv-snapshot",
        source="native",
        labels={"Document": ("title",)},
        relationship_types={"CITES": ("since",)},
    )
    original_read_text = Path.read_text

    def read_manifest(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == settings.schema_manifest_path:
            return manifest.model_dump_json()
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_manifest)
    failing_probes: dict[str, list[dict[str, Any]] | Exception] = {
        "SHOW PROCEDURES YIELD name WHERE name = 'apoc.meta.data'": RuntimeError("offline"),
        "SHOW PROCEDURES YIELD name WHERE name = 'db.schema.nodeTypeProperties'": RuntimeError(
            "offline"
        ),
        "SHOW PROCEDURES YIELD name WHERE name = 'db.schema.relTypeProperties'": RuntimeError(
            "offline"
        ),
        "CALL db.labels()": RuntimeError("offline"),
        "CALL db.relationshipTypes()": RuntimeError("offline"),
    }
    generator = _FakeCypherGenerator()
    executor = _TieredExecutor(failing_probes)
    result = await Text2CypherAdapter(executor, generator, settings).generate_and_run(
        "find records"
    )

    assert result.schema_source == "manifest"
    assert "Document {title}" in generator.calls[0][0]
    assert result.metadata["schema_drift"]["state"] == "unknown"
    assert result.metadata["schema_drift"]["reason"] == "live_schema_unavailable"


async def test_runtime_schema_drift_is_metadata_warning_and_does_not_block_query(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from datetime import UTC, datetime
    from pathlib import Path

    from book_graph_rag.domain.schema_manifest import SchemaManifest

    manifest = SchemaManifest(
        schema_version="1.0.0",
        committed_at=datetime(2026, 10, 17, tzinfo=UTC),
        code_commit="abc1234",
        graph_version="gv-old",
        source="native",
        labels={"Expected": ("name",)},
        relationship_types={},
    )
    original_read_text = Path.read_text

    def read_manifest(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == settings.schema_manifest_path:
            return manifest.model_dump_json()
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_manifest)
    generator = _FakeCypherGenerator()
    executor = _FakeExecutor()
    adapter = Text2CypherAdapter(executor, generator, settings)

    with caplog.at_level("WARNING"):
        result = await adapter.generate_and_run("find records")

    assert result.schema_source == "native"
    assert result.metadata["schema_drift"]["state"] == "drift"
    assert result.metadata["graph_version"].startswith("gv-")
    assert result.rows == []
    assert generator.calls
    assert "schema drift" in caplog.text.lower()


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
    assert "MATCH (n) DETACH DELETE n" not in [c for c, _ in executor.execute_read_calls]


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
    assert "CALL dbms.security.listUsers()" not in [c for c, _ in executor.execute_read_calls]


async def test_text2cypher_self_heals_on_explain_failure(
    settings: Settings,
) -> None:
    """First EXPLAIN fails, second succeeds; adapter retries once."""
    cypher = "MATCH (e:Entity) WHERE e.id = $id RETURN e LIMIT 100"
    generator = _FakeCypherGenerator([cypher, cypher])
    executor = _FakeExecutor()
    executor.explain_should_fail = [RuntimeError("label not found")]
    executor.rows = [{"e": {"name": "Agent"}}]

    adapter = Text2CypherAdapter(executor, generator, settings)
    result = await adapter.generate_and_run("find entities")

    assert result.cypher == cypher
    assert result.retries == 1
    assert len(executor.explain_calls) == 2
    assert executor.execute_read_calls[-1][0] == cypher
    assert generator.calls[1][2] == CypherFailureContext(
        failed_cypher=cypher,
        error_message="label not found",
    )


async def test_text2cypher_exhausted_retries_raises_generation_error(
    settings: Settings,
) -> None:
    """Two EXPLAIN failures in a row result in CypherGenerationError."""
    cypher = "MATCH (e:Entity) WHERE e.id = $id RETURN e LIMIT 100"
    generator = _FakeCypherGenerator([cypher, cypher, cypher])
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
    assert cypher not in [c for c, _ in executor.execute_read_calls]


async def test_text2cypher_timeout_raises_domain_error(
    settings: Settings,
) -> None:
    """A pipeline that exceeds the timeout raises Text2CypherTimeoutError."""
    generator = _FakeCypherGenerator()
    executor = _FakeExecutor()

    async def slow_explain(cypher: str, parameters: dict[str, Any] | None = None) -> None:
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
    cypher = "MATCH (e:Entity) WHERE e.id = $id RETURN e LIMIT 100"
    generator = _FakeCypherGenerator([cypher, cypher, cypher])
    executor = _FakeExecutor()
    executor.explain_should_fail = [
        RuntimeError("first"),
        RuntimeError("second"),
        RuntimeError("third"),
    ]

    adapter = Text2CypherAdapter(executor, generator, settings)

    with pytest.raises(CypherGenerationError) as exc_info:
        await adapter.generate_and_run("find entities")

    assert cypher in str(exc_info.value)


# ── Structural validation wiring (T-E.3 part 2) ──────────────────────────────


async def test_text2cypher_rejects_missing_scope_proof(
    settings: Settings,
) -> None:
    """Generated output without a WHERE scope predicate fails closed."""
    generator = _FakeCypherGenerator(["MATCH (c:Chunk) RETURN c LIMIT 100"])
    executor = _FakeExecutor()

    adapter = Text2CypherAdapter(executor, generator, settings)

    with pytest.raises(StructuralPolicyViolationError, match="scope"):
        await adapter.generate_and_run("find chunks")

    assert executor.explain_calls == []
    assert "MATCH (c:Chunk) RETURN c LIMIT 100" not in [c for c, _ in executor.execute_read_calls]


async def test_text2cypher_rejects_dynamic_label(
    settings: Settings,
) -> None:
    """Generated output with a dynamic label is rejected before EXPLAIN/execute."""
    generator = _FakeCypherGenerator(["MATCH (n:$label) RETURN n"])
    executor = _FakeExecutor()

    adapter = Text2CypherAdapter(executor, generator, settings)

    with pytest.raises(StructuralPolicyViolationError, match="dynamic label"):
        await adapter.generate_and_run("find anything")

    assert executor.explain_calls == []
    assert "MATCH (n:$label) RETURN n" not in [c for c, _ in executor.execute_read_calls]


async def test_text2cypher_wires_validator_before_explain_and_execute(
    settings: Settings,
) -> None:
    """Structural validation and the EXPLAIN gate run before execution."""
    cypher = "MATCH (c:Chunk) WHERE c.book_id = $book_id RETURN c LIMIT $limit"
    validator = _SpyValidator()
    generator = _FakeCypherGenerator([cypher])
    executor = _FakeExecutor()
    executor.rows = [{"c": {"book_id": "ns:book"}}]

    adapter = Text2CypherAdapter(executor, generator, settings, validator=validator)
    result = await adapter.generate_and_run("find chunks")

    assert validator.validate_calls == [(cypher, True)]
    assert validator.require_explain_calls == [(cypher, True)]
    assert [c for c, _ in executor.explain_calls] == [cypher]
    assert executor.execute_read_calls[-1][0] == cypher
    assert result.rows == [{"c": {"book_id": "ns:book"}}]


async def test_text2cypher_binds_scope_parameters_from_proofs(
    settings: Settings,
) -> None:
    """The adapter binds scope parameters derived strictly from scope_proofs."""
    cypher = "MATCH (c:Chunk) WHERE c.book_id = $book_id RETURN c LIMIT 100"
    generator = _FakeCypherGenerator([cypher])
    executor = _FakeExecutor()
    executor.rows = [{"c": {"book_id": "book:default"}}]
    scope = ScopeContext(source=SourceNamespace(corpus="book", source="default"))

    adapter = Text2CypherAdapter(executor, generator, settings)
    result = await adapter.generate_and_run("find chunks", scope=scope)

    assert executor.explain_calls[-1] == (cypher, {"book_id": "book:default"})
    assert executor.execute_read_calls[-1] == (cypher, {"book_id": "book:default"})
    assert result.rows == [{"c": {"book_id": "book:default"}}]


async def test_text2cypher_non_derivable_scope_raises_after_retries(
    settings: Settings,
) -> None:
    """An Entity.id proof cannot be bound from ScopeContext; typed failure after retries=2."""
    cypher = "MATCH (e:Entity) WHERE e.id = $id RETURN e LIMIT 100"
    generator = _FakeCypherGenerator([cypher, cypher, cypher])
    executor = _FakeExecutor()
    scope = ScopeContext(source=SourceNamespace(corpus="book", source="default"))

    adapter = Text2CypherAdapter(executor, generator, settings)

    with pytest.raises(CypherGenerationError, match="non-derivable") as exc_info:
        await adapter.generate_and_run("find entity by id", scope=scope)

    assert cypher in str(exc_info.value)
    assert len(generator.calls) == 3
    assert executor.explain_calls == []


async def test_text2cypher_binds_book_ids_in_from_proofs(
    settings: Settings,
) -> None:
    """A Chunk.book_id IN proof binds the scope's book_ids list, nothing extra."""
    cypher = "MATCH (c:Chunk) WHERE c.book_id IN $book_ids RETURN c LIMIT 100"
    generator = _FakeCypherGenerator([cypher])
    executor = _FakeExecutor()
    executor.rows = [{"c": {"book_id": "book:one"}}]
    scope = ScopeContext(
        source=SourceNamespace(corpus="book", source="default"),
        book_ids=("book:one", "book:two"),
    )

    adapter = Text2CypherAdapter(executor, generator, settings)
    await adapter.generate_and_run("find chunks", scope=scope)

    assert executor.execute_read_calls[-1] == (
        cypher,
        {"book_ids": ["book:one", "book:two"]},
    )


def test_build_scope_parameter_map_in_falls_back_to_source_id() -> None:
    """An empty book_ids list falls back to [source_id] for the IN binding."""
    proof = ScopeProof(
        variable="c",
        label="Chunk",
        property="book_id",
        parameter="$book_ids",
        operator="IN",
    )
    scope = ScopeContext(source=SourceNamespace(corpus="book", source="default"))
    assert _build_scope_parameter_map((proof,), scope) == {"book_ids": ["book:default"]}


def test_build_scope_parameter_map_returns_empty_when_unscoped() -> None:
    """scope=None yields an empty map even when a proof exists."""
    proof = ScopeProof(
        variable="e",
        label="Entity",
        property="id",
        parameter="$id",
        operator="=",
    )
    assert _build_scope_parameter_map((proof,), None) == {}
