"""Shared testcontainers fixtures for Neo4j integration tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from neo4j import AsyncGraphDatabase
from testcontainers.community.neo4j import Neo4jContainer

from book_graph_rag.config import Settings


@pytest.fixture(scope="session")
def neo4j_image() -> str:
    """Default Neo4j community image used by testcontainers."""
    return "neo4j:5.23-community"


@pytest.fixture
def neo4j_container(neo4j_image: str) -> Any:
    """Spin up a throwaway Neo4j container for one test/function."""
    container = Neo4jContainer(neo4j_image)
    container.start()
    yield container
    container.stop()


@pytest.fixture
def neo4j_settings(neo4j_container: Any) -> Settings:
    """Build Settings pointing at the running testcontainer."""
    return Settings.model_validate(
        {
            "neo4j_uri": neo4j_container.get_connection_url(),
            "neo4j_user": neo4j_container.username,
            "neo4j_password": neo4j_container.password,
        }
    )


@pytest_asyncio.fixture(scope="function")
async def neo4j_driver(neo4j_settings: Settings) -> AsyncGenerator[Any, None]:
    """Async Neo4j driver connected to the testcontainer."""
    driver = AsyncGraphDatabase.driver(
        neo4j_settings.neo4j_uri,
        auth=(neo4j_settings.neo4j_user, neo4j_settings.neo4j_password.get_secret_value()),
    )
    yield driver
    await driver.close()
