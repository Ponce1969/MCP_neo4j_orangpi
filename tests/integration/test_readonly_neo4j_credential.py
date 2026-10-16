"""Server-side read-only credential verification against an explicitly selected test graph."""

from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

import pytest
from neo4j import WRITE_ACCESS, AsyncGraphDatabase
from neo4j.exceptions import Neo4jError

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter
from book_graph_rag.infrastructure.neo4j_read_credentials import (
    has_readonly_credentials,
    readonly_settings,
)

pytestmark = pytest.mark.neo4j_integration


def _configured_read_settings() -> Settings:
    """Use only the dedicated test URI and skip unless the read credential is complete."""
    test_uri = os.environ.get("NEO4J_READONLY_TEST_URI", "").strip()
    readonly_user = os.environ.get("NEO4J_READONLY_USER", "")
    readonly_password = os.environ.get("NEO4J_READONLY_PASSWORD", "")
    settings = Settings.model_validate(
        {
            "neo4j_uri": test_uri,
            "neo4j_user": readonly_user,
            "neo4j_password": readonly_password,
            "neo4j_readonly_user": readonly_user,
            "neo4j_readonly_password": readonly_password,
        }
    )
    if not test_uri:
        pytest.skip("set NEO4J_READONLY_TEST_URI to an isolated Neo4j test server")
    if not has_readonly_credentials(settings):
        pytest.skip("NEO4J_READONLY_USER and NEO4J_READONLY_PASSWORD are not configured")
    return readonly_settings(settings)


async def test_readonly_credential_reads_but_server_rejects_write() -> None:
    """A real read-only login can read, and a raw write transaction is denied by Neo4j."""
    settings = _configured_read_settings()
    probe_id = uuid4().hex

    query_adapter = Neo4jQueryAdapter(settings)
    try:
        read_result = await query_adapter.execute_read("RETURN 1 AS value")
        assert read_result == [{"value": 1}]
    finally:
        await query_adapter.close()

    driver: Any = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    write_error: Neo4jError | None = None
    try:
        async with driver.session(
            database=settings.neo4j_read_database,
            default_access_mode=WRITE_ACCESS,
        ) as session:
            transaction = await session.begin_transaction()
            try:
                result = await transaction.run(
                    "CREATE (n:ReadonlyCredentialProbe {id: $probe_id}) RETURN n.id AS id",
                    probe_id=probe_id,
                )
                await result.consume()
            except Neo4jError as exc:
                write_error = exc
            finally:
                await transaction.rollback()
    finally:
        await driver.close()

    assert write_error is not None, "Neo4j accepted a write under the read-only credential"
    assert write_error.code in {
        "Neo.ClientError.Security.Forbidden",
        "Neo.ClientError.Statement.AccessMode",
        "Neo.ClientError.General.ForbiddenOnReadOnlyDatabase",
    }
