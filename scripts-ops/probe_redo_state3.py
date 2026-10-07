"""Read-only probe #3: relationship model comparison essential vs GA namespace."""

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
    for tag, ns in [("essential", NS_ESS), ("ga", NS_GA)]:
        out[f"{tag}_rel_types"] = [
            dict(r)
            for r in s.run(
                "MATCH ()-[r]->() WHERE r.source_id STARTS WITH $ns "
                "RETURN type(r) AS rel, count(*) AS n ORDER BY n DESC LIMIT 8",
                ns=ns,
            )
        ]
        mentions_rec = s.run(
            "MATCH (:Entity)-[r:MENTIONS]->(:Chunk) "
            "WHERE r.source_id STARTS WITH $ns OR r.chunk_id STARTS WITH $ns "
            "RETURN count(*) AS n",
            ns=ns,
        ).single()
        assert mentions_rec is not None
        out[f"{tag}_mentions_to_chunks"] = mentions_rec["n"]
        out[f"{tag}_ent_to_chunk_rels"] = [
            dict(r)
            for r in s.run(
                "MATCH (e:Entity)-[r]->(c:Chunk) WHERE c.book_id = $ns "
                "RETURN type(r) AS rel, count(*) AS n ORDER BY n DESC LIMIT 8",
                ns=ns,
            )
        ]
    # Any RELATED edge anywhere that mentions the GA namespace in any field.
    ga_related_rec = s.run(
        "MATCH ()-[r:RELATED]->() WHERE toString(r.source_id) CONTAINS $ns RETURN count(*) AS n",
        ns="graphrag-agentic",
    ).single()
    assert ga_related_rec is not None
    out["ga_related_edges"] = ga_related_rec["n"]
    # How many RELATED edges exist globally (sanity: model uses RELATED at all).
    global_related_rec = s.run("MATCH ()-[r:RELATED]->() RETURN count(*) AS n").single()
    assert global_related_rec is not None
    out["global_related_edges"] = global_related_rec["n"]
driver.close()
print(json.dumps(out, indent=2, default=str))
