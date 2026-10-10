"""A reproducible identity for a graph state.

The version is derived from the same cheap census the ``bookgraph://catalog``
resource already reads, so it needs no state of its own and cannot drift from the
graph it names.

It is a **structural** digest: it moves when counts move (a merge, a new book, a
re-index that changes node counts) and it does not move for an edit that leaves
every count unchanged. That limit is deliberate — the alternative is a stateful
counter, which drifts the moment a write path forgets to bump it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

_SEPARATOR = "\x1f"
_PREFIX = "gv-"
_DIGEST_CHARS = 12


def graph_version_from(
    catalog_version: str | int,
    stats: Mapping[str, Mapping[str, int]],
) -> str:
    """Return a stable short digest of the census at ``catalog_version``.

    ``stats`` maps ``source_id`` to its ``chunks``/``entities`` counts, exactly as
    the catalog resource reads them. Source order never matters. ``catalog_version``
    is the catalog's own version, which the catalog declares as a number.
    """
    parts: list[str] = [f"catalog{_SEPARATOR}{catalog_version}"]
    for source_id in sorted(stats):
        counts = stats[source_id]
        chunks = int(counts.get("chunks", 0))
        entities = int(counts.get("entities", 0))
        parts.append(f"{source_id}{_SEPARATOR}{chunks}{_SEPARATOR}{entities}")
    digest = hashlib.sha256(_SEPARATOR.join(parts).encode("utf-8")).hexdigest()
    return f"{_PREFIX}{digest[:_DIGEST_CHARS]}"
