"""Text2Cypher adapter: natural language to validated, read-only Cypher."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Protocol

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import (
    CypherGenerationError,
    Text2CypherTimeoutError,
    UnsafeCypherQueryError,
)
from book_graph_rag.ports.cypher_generator_port import (
    CypherFailureContext,
    CypherGeneratorPort,
)
from book_graph_rag.ports.text2cypher_port import Text2CypherPort, Text2CypherResult

_HARDCODED_SCHEMA = """\
Node labels:
- Entity: {id, name, type, description, source_page}
- Chunk: {id, text, page_start, page_end}

Relationship types:
- (:Entity)-[:RELATED {type, description, source_page}]->(:Entity)
"""

# Read-only guard: reject any write/admin keyword before EXPLAIN/execute.
_WRITE_KEYWORDS_RE = re.compile(
    r"\b(CREATE|DELETE|SET|MERGE|DETACH|REMOVE|DROP)\b|\bCALL\s+dbms\b",
    re.IGNORECASE | re.MULTILINE,
)

_MAX_RETRIES = 2


class _CypherExecutor(Protocol):
    """Minimal surface the adapter needs from a Cypher runner."""

    async def explain(self, cypher: str) -> None: ...
    async def execute_read(self, cypher: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class _SchemaInfo:
    """Internal schema description with its provenance."""

    description: str
    source: str


class Text2CypherAdapter(Text2CypherPort):
    """End-to-end text-to-Cypher pipeline with schema inference and safety guards.

    Pipeline:
        1. Infer schema via APOC or fall back to a hardcoded schema.
        2. Ask the ``CypherGeneratorPort`` to generate a query.
        3. Reject queries that contain write/admin keywords.
        4. Run ``EXPLAIN``; retry up to 2 times on failure.
        5. Execute the validated query and return structured rows.

    The whole pipeline is wrapped in ``asyncio.wait_for`` using the configured
    ``text2cypher_timeout``.
    """

    def __init__(
        self,
        executor: _CypherExecutor,
        generator: CypherGeneratorPort,
        settings: Settings,
    ) -> None:
        self._executor = executor
        self._generator = generator
        self._timeout = settings.text2cypher_timeout

    async def generate_and_run(self, question: str) -> Text2CypherResult:
        """Run the full pipeline and return the result."""
        try:
            return await asyncio.wait_for(
                self._generate_and_run(question), timeout=self._timeout
            )
        except TimeoutError as exc:
            raise Text2CypherTimeoutError() from exc

    async def _generate_and_run(self, question: str) -> Text2CypherResult:
        schema_info = await self._infer_schema()
        cypher, retries = await self._generate_and_validate(
            schema_info.description, question
        )
        rows = await self._executor.execute_read(cypher)
        return Text2CypherResult(
            question=question,
            cypher=cypher,
            rows=rows,
            schema_source=schema_info.source,  # type: ignore[arg-type]
            retries=retries,
        )

    async def _infer_schema(self) -> _SchemaInfo:
        """Try APOC schema inference; fall back to hardcoded schema on any error."""
        try:
            records = await self._executor.execute_read("CALL apoc.meta.data()")
        except Exception:  # noqa: BLE001 - APOC absence is expected fallback
            return _SchemaInfo(_HARDCODED_SCHEMA, "hardcoded")

        if not records:
            return _SchemaInfo(_HARDCODED_SCHEMA, "hardcoded")

        description = self._format_schema(records)
        return _SchemaInfo(description, "apoc")

    @staticmethod
    def _format_schema(records: list[dict[str, Any]]) -> str:
        """Build a concise schema string from APOC meta.data records."""
        labels: set[str] = set()
        rels: set[str] = set()
        for record in records:
            if record.get("type") == "node":
                label = record.get("label")
                if label:
                    labels.add(str(label))
            elif record.get("type") == "relationship":
                rel_type = record.get("name")
                if rel_type:
                    rels.add(str(rel_type))

        lines = ["Node labels:"]
        lines.extend(f"- {label}" for label in sorted(labels))
        lines.append("Relationship types:")
        lines.extend(f"- ()-[:{rel}]->()" for rel in sorted(rels))
        return "\n".join(lines) if labels or rels else _HARDCODED_SCHEMA

    async def _generate_and_validate(
        self, schema: str, question: str
    ) -> tuple[str, int]:
        """Generate, validate, and EXPLAIN a Cypher query; retry up to 2 times."""
        retries = 0
        failure: CypherFailureContext | None = None

        while retries <= _MAX_RETRIES:
            cypher = await self._generator.generate_cypher(schema, question, failure)
            self._ensure_read_only(cypher)

            try:
                await self._executor.explain(cypher)
            except Exception as exc:  # noqa: BLE001 - EXPLAIN errors drive self-heal
                if retries == _MAX_RETRIES:
                    raise CypherGenerationError(
                        f"Cypher generation failed after {_MAX_RETRIES} retries. "
                        f"Last query: {cypher}. Error: {exc}"
                    ) from exc
                failure = CypherFailureContext(
                    failed_cypher=cypher,
                    error_message=str(exc),
                )
                retries += 1
                continue

            return cypher, retries

        # Defensive: loop should always return or raise above.
        raise CypherGenerationError("Cypher generation failed after max retries")

    @staticmethod
    def _ensure_read_only(cypher: str) -> None:
        """Reject any query containing write or DB-admin keywords."""
        if _WRITE_KEYWORDS_RE.search(cypher) is not None:
            raise UnsafeCypherQueryError(
                "Generated Cypher query contains disallowed write/admin operations"
            )
