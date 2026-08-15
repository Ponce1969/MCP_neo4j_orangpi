"""Graph construction and Leiden community-detection helpers.

The module isolates the clustering logic from the offline CLI so it can be
tested without a real Neo4j or LLM.  It supports two backends:
``graspologic`` (preferred) and ``leidenalg`` + ``python-igraph`` (fallback).
"""

from __future__ import annotations

import importlib.util

import networkx as nx

from book_graph_rag.domain.models import Entity, Relationship, _community_summary_id

__all__ = [
    "CommunityDetectionError",
    "build_entity_graph",
    "select_leiden_backend",
    "run_leiden",
    "assign_parent_ids",
    "_community_summary_id",
]


class CommunityDetectionError(Exception):
    """Raised when no Leiden backend is available or clustering fails."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


def build_entity_graph(
    entities: list[Entity], relationships: list[Relationship]
) -> nx.Graph:
    """Build an undirected ``networkx`` graph from entities and relationships."""
    graph: nx.Graph = nx.Graph()
    valid_ids = {entity.id for entity in entities}
    for entity in entities:
        graph.add_node(
            entity.id,
            name=entity.name,
            type=entity.type,
            description=entity.description,
        )
    for relationship in relationships:
        if (
            relationship.source_entity_id in valid_ids
            and relationship.target_entity_id in valid_ids
        ):
            graph.add_edge(
                relationship.source_entity_id,
                relationship.target_entity_id,
                type=relationship.type,
            )
    return graph


def select_leiden_backend() -> str:
    """Return the first available Leiden backend."""
    if importlib.util.find_spec("graspologic") is not None:
        return "graspologic"
    if (
        importlib.util.find_spec("leidenalg") is not None
        and importlib.util.find_spec("igraph") is not None
    ):
        return "leidenalg"
    raise CommunityDetectionError(
        "No Leiden backend available. Install the 'community' extra: "
        "uv pip install -e '.[community]'"
    ) from None


def run_leiden(graph: nx.Graph, resolution: float, backend: str) -> list[list[str]]:
    """Run Leiden on ``graph`` at ``resolution`` using the chosen ``backend``."""
    if backend == "graspologic":
        from graspologic.partition import leiden

        # graspologic returns a Dict[node_id, community_id].
        membership = leiden(graph, resolution=resolution, random_seed=42)
        communities: dict[int, list[str]] = {}
        for node_id, community_id in membership.items():
            communities.setdefault(community_id, []).append(node_id)
        return list(communities.values())
    if backend == "leidenalg":
        import igraph as ig
        import leidenalg

        igraph_graph = ig.Graph.from_networkx(graph)
        partition = leidenalg.find_partition(
            igraph_graph,
            leidenalg.RBConfigurationVertexPartition,
            resolution_parameter=resolution,
            seed=42,
        )
        return [
            [igraph_graph.vs[index]["name"] for index in community]
            for community in partition
        ]
    raise CommunityDetectionError(f"Unknown Leiden backend: {backend}")


def assign_parent_ids(
    communities_by_level: dict[int, list[list[str]]]
) -> dict[int, list[tuple[list[str], str | None]]]:
    """Assign parent ids to each community based on maximum overlap at level-1.

    Level 0 communities have no parent.  For higher levels, the parent is the
    level-1 community with the largest intersection of entity ids.
    """
    result: dict[int, list[tuple[list[str], str | None]]] = {}
    parent_ids: dict[int, list[str]] = {}
    for level, communities in sorted(communities_by_level.items()):
        level_assignments: list[tuple[list[str], str | None]] = []
        for community in communities:
            parent_id: str | None = None
            if level > 0 and parent_ids:
                parent_communities = communities_by_level.get(level - 1, [])
                best_parent = max(
                    parent_communities,
                    key=lambda parent: len(set(community) & set(parent)),
                    default=[],
                )
                parent_id = _community_summary_id(level - 1, best_parent)
            level_assignments.append((community, parent_id))
        result[level] = level_assignments
        parent_ids[level] = [
            _community_summary_id(level, community) for community in communities
        ]
    return result
