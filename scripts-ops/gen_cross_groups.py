"""Cross-namespace duplicate detection: same (name, kind) in 2+ knowledge namespaces.

Read-only over the graph. Active entities only (merged_into NULL/empty).
Group key = (name, kind); a group is kept when its member ids span >= 2
namespaces. Canonical = shortest id (global), candidates = the rest.
Writes the plan to /tmp/cross_groups.json for the reviewer.

Usage (OrangePi, project venv):
    uv run python gen_cross_groups.py
"""

from __future__ import annotations

import json
from collections import Counter

from book_graph_rag.config import Settings

OUT = "/tmp/cross_groups.json"

QUERY = """
MATCH (n:Entity)
WHERE (n.merged_into IS NULL OR n.merged_into = '') AND n.id STARTS WITH 'knowledge:'
WITH n, split(n.id, ':') AS parts WHERE size(parts) >= 2 AND size(parts) <= 3
WITH n, parts[0] + ':' + parts[1] AS ns, coalesce(n.name, '') AS name, n.type AS kind
WITH name, kind, collect(DISTINCT ns) AS nss, collect(n.id) AS ids
WHERE size(nss) >= 2
RETURN name, kind, nss, ids
"""


def main() -> None:
    settings = Settings.model_validate({})
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        with driver.session() as s:
            rows = list(s.run(QUERY))
    finally:
        driver.close()

    groups = []
    for r in rows:
        ids = sorted(r["ids"], key=len)
        groups.append(
            {
                "name": r["name"],
                "kind": r["kind"],
                "namespaces": sorted(r["nss"]),
                "canonical": ids[0],
                "candidates": ids[1:],
            }
        )

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(groups, f, indent=2, ensure_ascii=False)

    total_dups = sum(len(g["candidates"]) for g in groups)
    print(f"GROUPS {len(groups)} -> {OUT}")
    print(f"TOTAL_DUPS {total_dups}")
    print("KINDS:", dict(Counter(g["kind"] for g in groups)))
    print("NS_COMBOS:", dict(Counter(tuple(g["namespaces"]) for g in groups)))
    for g in groups:
        print(
            f"  [{g['kind']}] {g['name']!r} {g['namespaces']} | "
            f"canon={g['canonical']} | dups={len(g['candidates'])}"
        )


if __name__ == "__main__":
    main()