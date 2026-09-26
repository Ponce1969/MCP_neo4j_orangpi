"""Read-only probe #4: real schema of entity relationships + counts per ns."""
import json
import os

from neo4j import GraphDatabase

PROJECT = os.path.expanduser("~/Gonzalo_codigo/Mcp_libro/MCP_neo4j_orangpi")
NS_GA = "knowledge:graphrag-agentic"
NS_ESS = "knowledge:essential-graphrag"

env = {}
with open(os.path.join(PROJECT, ".env")) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip("\"'")

driver = GraphDatabase.driver(
    env.get("NEO4J_URI", "bolt://localhost:7687"),
    auth=(env.get("NEO4J_USER", "neo4j"), env.get("NEO4J_PASSWORD", "")),
)
out: dict[str, object] = {}
with driver.session(database=env.get("NEO4J_DATABASE", "neo4j")) as s:
    # Sample one related-edge from the healthy essential book.
    out["sample_essential_rel"] = [
        dict(r)
        for r in s.run(
            "MATCH (e:Entity)-[r:RELATED]->(e2:Entity) "
            "WHERE e.id STARTS WITH $ns RETURN e.id AS src, type(r) AS rel, "
            "e2.id AS dst, keys(r) AS rkeys, r.source_id AS sid, "
            "r.source_page AS page, r.chunk_index AS ci LIMIT 3",
            ns=NS_ESS,
        )
    ]
    # Any top labels attached to one of those edges' endpoints.
    out["sample_dst_labels"] = [
        dict(r)
        for r in s.run(
            "MATCH (e:Entity)-[r:RELATED]->(x) WHERE e.id STARTS WITH $ns "
            "RETURN labels(x) AS labels LIMIT 3",
            ns=NS_ESS,
        )
    ]
    # Plain property-path independent counts for the GA namespace.
    for tag, ns in [("essential", NS_ESS), ("ga", NS_GA)]:
        related_rec = s.run(
            "MATCH (e:Entity)-[r:RELATED]->(e2:Entity) "
            "WHERE e.id STARTS WITH $ns RETURN count(*) AS n",
            ns=ns,
        ).single()
        assert related_rec is not None
        out[f"{tag}_related_between_entities"] = related_rec["n"]
        entity_total_rec = s.run(
            "MATCH (e:Entity) WHERE e.id STARTS WITH $ns RETURN count(*) AS n",
            ns=ns,
        ).single()
        assert entity_total_rec is not None
        out[f"{tag}_entity_total"] = entity_total_rec["n"]
        out[f"{tag}_entity_sample_flags"] = [
            dict(r)
            for r in s.run(
                "MATCH (e:Entity) WHERE e.id STARTS WITH $ns "
                "RETURN e.merged_into AS merged, count(*) AS n "
                "ORDER BY n DESC LIMIT 5",
                ns=ns,
            )
        ]
driver.close()
print(json.dumps(out, indent=2, default=str))
