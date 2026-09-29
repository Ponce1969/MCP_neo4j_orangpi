"""Behavior tests for the local JSON profile artifact store."""

from __future__ import annotations

from pathlib import Path

import pytest

from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.routing_models import NamespaceProfile
from book_graph_rag.infrastructure.json_namespace_profile_store import (
    JsonNamespaceProfileStore,
    NamespaceProfileStoreError,
)


def _profile(namespace_id: str) -> NamespaceProfile:
    corpus, source = namespace_id.split(":", maxsplit=1)
    return NamespaceProfile(
        namespace=SourceNamespace(corpus=corpus, source=source),
        centroid=(1.0, 0.0, 0.0),
        dimension=3,
        model_id="test-model",
        profile_version="1.0.0",
        catalog_version="1",
        graph_snapshot="snapshot-x",
    )


async def test_store_round_trip(tmp_path: Path) -> None:
    store = JsonNamespaceProfileStore(tmp_path / "profiles.json")
    profiles = (_profile("knowledge:book-a"), _profile("knowledge:book-b"))

    await store.save_all(profiles)
    loaded = await store.load_all()

    assert loaded == profiles
    assert (tmp_path / "profiles.json").is_file()


async def test_store_missing_file_loads_empty(tmp_path: Path) -> None:
    store = JsonNamespaceProfileStore(tmp_path / "missing.json")

    assert await store.load_all() == ()


async def test_store_corrupt_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "profiles.json"
    path.write_text("{not json", encoding="utf-8")
    store = JsonNamespaceProfileStore(path)

    with pytest.raises(NamespaceProfileStoreError, match="invalid profile artifact"):
        await store.load_all()


async def test_store_invalid_profile_raises(tmp_path: Path) -> None:
    path = tmp_path / "profiles.json"
    path.write_text('[{"namespace": {"corpus": "knowledge"}}]', encoding="utf-8")
    store = JsonNamespaceProfileStore(path)

    with pytest.raises(NamespaceProfileStoreError, match="failed validation"):
        await store.load_all()


async def test_store_write_is_atomic_no_leftover_temp(tmp_path: Path) -> None:
    store = JsonNamespaceProfileStore(tmp_path / "profiles.json")
    profiles = (_profile("knowledge:book-a"),)

    await store.save_all(profiles)

    assert [p.name for p in tmp_path.iterdir()] == ["profiles.json"]
