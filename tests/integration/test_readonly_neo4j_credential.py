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


@pytest.mark.parametrize(
    ("roles", "expected"),
    [(None, False), (["reader"], True)],
)
def test_rbac_detection_uses_reported_roles(roles: list[str] | None, expected: bool) -> None:
    rows = [{"user": "bookgraph_reader", "roles": roles}]

    assert _server_has_rbac_roles(rows, "bookgraph_reader") is expected


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


async def test_readonly_credential_can_read() -> None:
    """The dedicated credential can still execute the MCP's managed read path."""
    settings = _configured_read_settings()
    query_adapter = Neo4jQueryAdapter(settings)
    try:
        read_result = await query_adapter.execute_read("RETURN 1 AS value")
        assert read_result == [{"value": 1}]
    finally:
        await query_adapter.close()


async def test_readonly_credential_rejects_write_when_rbac_is_available() -> None:
    """Require denial where roles exist; explain Community's application-only boundary.

    If a raw write succeeds, SHOW USERS is inspected for this credential. A non-NULL
    roles field means the server has role-based privileges and accepting the write is
    a failure. Community Edition reports roles: NULL because it has no DBMS RBAC, so
    only the write-denial half is skipped there; the separate read test still runs.
    """
    settings = _configured_read_settings()
    probe_id = uuid4().hex
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

        if write_error is None:
            try:
                async with driver.session(database="system") as session:
                    result = await session.run(
                        "SHOW USERS YIELD user, roles WHERE user = $user RETURN user, roles",
                        user=settings.neo4j_user,
                    )
                    user_rows = await result.data()
                has_rbac_roles = _server_has_rbac_roles(user_rows, settings.neo4j_user)
            except Neo4jError as exc:
                pytest.fail(
                    "Neo4j accepted a raw write, but SHOW USERS could not establish "
                    f"whether RBAC is available (error code {exc.code}); refusing to "
                    "assume this is Community Edition.",
                    pytrace=False,
                )
            if not has_rbac_roles:
                pytest.skip(
                    "SHOW USERS reports roles: NULL for this credential. Neo4j "
                    "Community Edition has no DBMS RBAC, so this credential cannot "
                    "enforce the write boundary; enforcement remains application-level "
                    "in Neo4jQueryAdapter (READ_ACCESS + managed execute_read)."
                )
    finally:
        await driver.close()

    assert write_error is not None, (
        "Neo4j accepted a write under a credential with reported RBAC roles"
    )
    assert write_error.code in {
        "Neo.ClientError.Security.Forbidden",
        "Neo.ClientError.Statement.AccessMode",
        "Neo.ClientError.General.ForbiddenOnReadOnlyDatabase",
    }


def _server_has_rbac_roles(rows: list[dict[str, Any]], username: str) -> bool:
    matching_rows = [row for row in rows if row.get("user") == username]
    if len(matching_rows) != 1:
        raise AssertionError(f"SHOW USERS did not return exactly one row for user {username!r}")
    return matching_rows[0].get("roles") is not None
