"""Behavior tests for the local JSON profile artifact store."""

from __future__ import annotations

import json
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
        profile_version="2.0.0",
        prototypes=((1.0, 0.0, 0.0),),
        k=1,
        prototype_sources=("chapter:0",),
        catalog_version="1",
        graph_snapshot="snapshot-x",
    )


def _v1_entry() -> dict[str, object]:
    """A complete v1-shape artifact entry: no ``prototypes`` field at all."""
    return {
        "namespace": {"corpus": "knowledge", "source": "book-a"},
        "centroid": [1.0, 0.0, 0.0],
        "dimension": 3,
        "model_id": "test-model",
        "profile_version": "2.0.0",
        "catalog_version": "1",
        "graph_snapshot": "snapshot-x",
    }


def _profile_with_central() -> NamespaceProfile:
    """T6 profile carrying the optional L9 ``central_centroid`` field."""
    return NamespaceProfile(
        namespace=SourceNamespace(corpus="knowledge", source="book-a"),
        centroid=(1.0, 0.0, 0.0),
        central_centroid=(0.0, 1.0, 0.0),
        dimension=3,
        model_id="test-model",
        profile_version="2.0.0",
        prototypes=((1.0, 0.0, 0.0),),
        k=1,
        prototype_sources=("chapter:0",),
        catalog_version="1",
        graph_snapshot="snapshot-x",
    )


def _entry_without_central() -> dict[str, object]:
    """A v2 entry predating ``central_centroid`` — the shipped artifact's shape."""
    payload: dict[str, object] = dict(_profile("knowledge:book-a").model_dump(mode="json"))
    payload.pop("central_centroid")
    return payload


async def test_store_round_trip(tmp_path: Path) -> None:
    store = JsonNamespaceProfileStore(tmp_path / "profiles.json")
    profiles = (_profile("knowledge:book-a"), _profile("knowledge:book-b"))

    await store.save_all(profiles)
    loaded = await store.load_all()

    assert loaded == profiles
    assert (tmp_path / "profiles.json").is_file()


async def test_store_round_trip_preserves_prototype_fields(tmp_path: Path) -> None:
    store = JsonNamespaceProfileStore(tmp_path / "profiles.json")
    profile = _profile("knowledge:book-a")

    await store.save_all((profile,))
    loaded = await store.load_all()

    assert loaded == (profile,)
    assert loaded[0].prototypes == profile.prototypes
    assert loaded[0].prototype_sources == profile.prototype_sources
    assert loaded[0].k == profile.k


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


# ── v2 shape gate (S4) ─────────────────────────────────────────────────────────


async def test_store_rejects_v1_artifact_without_prototypes_field(tmp_path: Path) -> None:
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps([_v1_entry()]), encoding="utf-8")
    store = JsonNamespaceProfileStore(path)

    with pytest.raises(NamespaceProfileStoreError, match="'prototypes'"):
        await store.load_all()


async def test_store_rejects_profile_version_expectation_mismatch(tmp_path: Path) -> None:
    payload = _profile("knowledge:book-a").model_dump(mode="json")
    payload["profile_version"] = "1.0.0"
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps([payload]), encoding="utf-8")
    store = JsonNamespaceProfileStore(path, expected_profile_version="2.0.0")

    with pytest.raises(NamespaceProfileStoreError, match="profile_version mismatch"):
        await store.load_all()


async def test_store_rejects_model_id_expectation_mismatch(tmp_path: Path) -> None:
    payload = _profile("knowledge:book-a").model_dump(mode="json")
    payload["model_id"] = "other-model"
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps([payload]), encoding="utf-8")
    store = JsonNamespaceProfileStore(path, expected_model_id="test-model")

    with pytest.raises(NamespaceProfileStoreError, match="model_id mismatch"):
        await store.load_all()


async def test_store_rejects_empty_prototypes_field(tmp_path: Path) -> None:
    payload = _profile("knowledge:book-a").model_dump(mode="json")
    payload["prototypes"] = []
    payload["k"] = 0
    payload["prototype_sources"] = []
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps([payload]), encoding="utf-8")
    store = JsonNamespaceProfileStore(path)

    with pytest.raises(NamespaceProfileStoreError, match="prototypes must not be empty"):
        await store.load_all()


async def test_store_rejects_k_below_one_field(tmp_path: Path) -> None:
    payload = _profile("knowledge:book-a").model_dump(mode="json")
    payload["k"] = 0
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps([payload]), encoding="utf-8")
    store = JsonNamespaceProfileStore(path)

    with pytest.raises(NamespaceProfileStoreError, match="k must be >= 1"):
        await store.load_all()


async def test_store_refuses_to_write_profile_without_prototypes(tmp_path: Path) -> None:
    legacy = NamespaceProfile(
        namespace=SourceNamespace(corpus="knowledge", source="book-a"),
        centroid=(1.0, 0.0, 0.0),
        dimension=3,
        model_id="test-model",
        profile_version="2.0.0",
    )
    store = JsonNamespaceProfileStore(tmp_path / "profiles.json")

    with pytest.raises(NamespaceProfileStoreError, match="prototypes must be non-empty"):
        await store.save_all((legacy,))
    assert not (tmp_path / "profiles.json").exists()


# ── T6 (L10): central_centroid — optional in the artifact, fail-closed when scored ──


async def test_store_round_trip_preserves_central_centroid(tmp_path: Path) -> None:
    store = JsonNamespaceProfileStore(tmp_path / "profiles.json")
    profile = _profile_with_central()

    await store.save_all((profile,))
    loaded = await store.load_all()

    assert loaded == (profile,)
    assert loaded[0].central_centroid == (0.0, 1.0, 0.0)


async def test_store_loads_artifact_without_central_centroid_by_default(tmp_path: Path) -> None:
    """T6/L10: the pre-T6 production artifact keeps loading unchanged —
    absent field is valid while nobody scores it."""
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps([_entry_without_central()]), encoding="utf-8")
    store = JsonNamespaceProfileStore(path)

    loaded = await store.load_all()

    assert len(loaded) == 1
    assert loaded[0].central_centroid is None
    assert loaded[0].prototypes  # still a healthy v2 profile


async def test_store_fails_closed_without_central_centroid_when_required(tmp_path: Path) -> None:
    """ROUTER_SCORING=central + pre-T6 artifact: rejected at load with a
    message naming the field and the rebuild — never a silent fallback."""
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps([_entry_without_central()]), encoding="utf-8")
    store = JsonNamespaceProfileStore(path, require_central_centroid=True)

    with pytest.raises(NamespaceProfileStoreError, match="central_centroid") as excinfo:
        await store.load_all()

    message = str(excinfo.value)
    assert "central_centroid" in message
    assert "scripts/build_namespace_profiles.py" in message


async def test_store_with_central_required_accepts_artifact_having_the_field(
    tmp_path: Path,
) -> None:
    """Positive control: the gate rejects a missing field, not the gate."""
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps([_profile_with_central().model_dump(mode="json")]), encoding="utf-8")
    store = JsonNamespaceProfileStore(path, require_central_centroid=True)

    loaded = await store.load_all()

    assert loaded[0].central_centroid == (0.0, 1.0, 0.0)
