"""Tests for the community clustering backend and graph helpers."""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.domain.models import Entity, Relationship
from book_graph_rag.infrastructure.community_clustering import (
    CommunityDetectionError,
    build_entity_graph,
    run_leiden,
    select_leiden_backend,
)


@pytest.fixture
def entities() -> list[Entity]:
    return [
        Entity(id="a", name="A", type="agent"),
        Entity(id="b", name="B", type="agent"),
        Entity(id="c", name="C", type="concept"),
    ]


@pytest.fixture
def relationships() -> list[Relationship]:
    return [
        Relationship(source_entity_id="a", target_entity_id="b", type="requires"),
        Relationship(source_entity_id="b", target_entity_id="c", type="enables"),
    ]


def test_build_entity_graph_includes_nodes_and_edges(
    entities: list[Entity], relationships: list[Relationship]
) -> None:
    """The graph contains every entity and every valid relationship."""
    graph = build_entity_graph(entities, relationships)

    assert set(graph.nodes()) == {"a", "b", "c"}
    assert graph.has_edge("a", "b")
    assert graph.has_edge("b", "c")


def test_build_entity_graph_ignores_missing_endpoint(
    entities: list[Entity],
) -> None:
    """Relationships pointing to unknown entities are skipped."""
    dangling = Relationship(
        source_entity_id="a", target_entity_id="missing", type="requires"
    )

    graph = build_entity_graph(entities, [dangling])

    assert not graph.has_edge("a", "missing")


def test_select_backend_prefers_graspologic(monkeypatch: pytest.MonkeyPatch) -> None:
    """When graspologic is importable, it is selected."""

    def fake_find_spec(name: str, package: str | None = None) -> Any:
        return object() if name == "graspologic" else None

    monkeypatch.setattr("importlib.util.find_spec", fake_find_spec)

    assert select_leiden_backend() == "graspologic"


def test_select_backend_falls_back_to_leidenalg(monkeypatch: pytest.MonkeyPatch) -> None:
    """When graspologic is absent but leidenalg+igraph are present, select leidenalg."""

    def fake_find_spec(name: str, package: str | None = None) -> Any:
        return object() if name in {"leidenalg", "igraph"} else None

    monkeypatch.setattr("importlib.util.find_spec", fake_find_spec)

    assert select_leiden_backend() == "leidenalg"


def test_select_backend_raises_when_no_backend_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When neither backend is importable, raise CommunityDetectionError."""

    def fake_find_spec(name: str, package: str | None = None) -> Any:
        return None

    monkeypatch.setattr("importlib.util.find_spec", fake_find_spec)

    with pytest.raises(CommunityDetectionError):
        select_leiden_backend()


def test_run_leiden_with_graspologic_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """The graspologic backend is called and returns communities."""
    graph = build_entity_graph(
        [Entity(id="a", name="A", type="agent"), Entity(id="b", name="B", type="agent")],
        [Relationship(source_entity_id="a", target_entity_id="b", type="requires")],
    )

    def fake_leiden(
        g: Any, *, resolution: float, random_seed: int | None = None
    ) -> dict[str, int]:
        return {"a": 0, "b": 1}

    fake_partition_module: Any = type(
        "partition", (), {"leiden": staticmethod(fake_leiden)}
    )()
    fake_graspologic_module: Any = type(
        "graspologic", (), {"partition": fake_partition_module}
    )()
    monkeypatch.setitem(
        pytest.importorskip("sys").modules, "graspologic", fake_graspologic_module
    )
    monkeypatch.setitem(
        pytest.importorskip("sys").modules,
        "graspologic.partition",
        fake_partition_module,
    )

    result = run_leiden(graph, resolution=1.0, backend="graspologic")

    assert result == [["a"], ["b"]]


def test_run_leiden_with_leidenalg_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """The leidenalg backend is called and returns communities."""
    graph = build_entity_graph(
        [Entity(id="a", name="A", type="agent"), Entity(id="b", name="B", type="agent")],
        [Relationship(source_entity_id="a", target_entity_id="b", type="requires")],
    )

    class FakePartition:
        def __init__(self, n: int) -> None:
            self._membership = [[0], [1]]

        def __len__(self) -> int:
            return len(self._membership)

        def __iter__(self) -> Any:
            return iter(self._membership)

    def fake_find_partition(
        g: Any, partition_type: Any, **kwargs: Any
    ) -> FakePartition:
        return FakePartition(len(g.vs))

    fake_leidenalg: Any = type(
        "FakeLeidenalg",
        (),
        {
            "find_partition": staticmethod(fake_find_partition),
            "RBConfigurationVertexPartition": object,
        },
    )()

    def _fake_from_networkx(g: Any) -> Any:
        return type("G", (), {"vs": [{"name": n} for n in g.nodes()], "es": []})()

    fake_igraph: Any = type(
        "FakeIgraph",
        (),
        {"Graph": type("Graph", (), {"from_networkx": staticmethod(_fake_from_networkx)})},
    )()
    monkeypatch.setitem(
        pytest.importorskip("sys").modules, "leidenalg", fake_leidenalg
    )
    monkeypatch.setitem(pytest.importorskip("sys").modules, "igraph", fake_igraph)

    result = run_leiden(graph, resolution=1.0, backend="leidenalg")

    assert result == [["a"], ["b"]]


def test_run_leiden_unknown_backend_raises() -> None:
    """An unsupported backend string raises CommunityDetectionError."""
    graph = build_entity_graph([Entity(id="a", name="A", type="agent")], [])

    with pytest.raises(CommunityDetectionError):
        run_leiden(graph, resolution=1.0, backend="unknown")


def test_run_leiden_graspologic_on_real_graph() -> None:
    """Real graspologic backend returns a partition of the graph."""
    graph = build_entity_graph(
        [
            Entity(id="a", name="A", type="agent"),
            Entity(id="b", name="B", type="agent"),
            Entity(id="c", name="C", type="agent"),
        ],
        [
            Relationship(source_entity_id="a", target_entity_id="b", type="requires"),
            Relationship(source_entity_id="b", target_entity_id="c", type="requires"),
        ],
    )

    result = run_leiden(graph, resolution=0.01, backend="graspologic")

    assert len(result) >= 1
    assert set().union(*result) == {"a", "b", "c"}
