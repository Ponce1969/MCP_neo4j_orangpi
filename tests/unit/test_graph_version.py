"""The graph version digest: stable, order-independent and structural."""

from book_graph_rag.domain.graph_version import graph_version_from

_STATS = {
    "knowledge:ai-engineering-huyen": {"chunks": 1241, "entities": 5900},
    "knowledge:essential-graphrag": {"chunks": 6899, "entities": 6526},
}


class TestStability:
    """The same census always names the same version."""

    def test_same_census_same_version(self) -> None:
        assert graph_version_from("c1", _STATS) == graph_version_from("c1", _STATS)

    def test_source_order_does_not_matter(self) -> None:
        reversed_stats = dict(reversed(list(_STATS.items())))

        assert graph_version_from("c1", reversed_stats) == graph_version_from("c1", _STATS)

    def test_shape_is_prefixed_and_short(self) -> None:
        version = graph_version_from("c1", _STATS)

        assert version.startswith("gv-")
        assert len(version) == len("gv-") + 12


class TestSensitivity:
    """Anything that changes the graph's shape changes the version."""

    def test_a_changed_chunk_count_changes_the_version(self) -> None:
        changed = {**_STATS, "knowledge:essential-graphrag": {"chunks": 6900, "entities": 6526}}

        assert graph_version_from("c1", changed) != graph_version_from("c1", _STATS)

    def test_a_changed_entity_count_changes_the_version(self) -> None:
        changed = {**_STATS, "knowledge:essential-graphrag": {"chunks": 6899, "entities": 6527}}

        assert graph_version_from("c1", changed) != graph_version_from("c1", _STATS)

    def test_a_new_source_changes_the_version(self) -> None:
        changed = {**_STATS, "knowledge:new-book": {"chunks": 10, "entities": 20}}

        assert graph_version_from("c1", changed) != graph_version_from("c1", _STATS)

    def test_a_changed_catalog_version_changes_the_version(self) -> None:
        assert graph_version_from("c2", _STATS) != graph_version_from("c1", _STATS)

    def test_an_empty_census_still_names_a_version(self) -> None:
        empty = graph_version_from("c1", {})

        assert empty.startswith("gv-")
        assert empty != graph_version_from("c1", _STATS)
