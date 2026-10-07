"""Read-only probe: current state of the knowledge:graphrag-agentic namespace.

Safe to run at any time; never mutates the graph.
"""

import json
import os

from neo4j import GraphDatabase

PROJECT = os.path.expanduser("~/Gonzalo_codigo/Mcp_libro/MCP_neo4j_orangpi")
NS = "knowledge:graphrag-agentic"

env = {}
with open(os.path.join(PROJECT, ".env")) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip("\"'")

uri = env.get("NEO4J_URI", "bolt://localhost:7687")
user = env.get("NEO4J_USER", "neo4j")
pwd = env.get("NEO4J_PASSWORD", "")
db = env.get("NEO4J_DATABASE", "neo4j")

driver = GraphDatabase.driver(uri, auth=(user, pwd))
out: dict[str, object] = {}
with driver.session(database=db) as s:
    out["checkpoints"] = [
        dict(r)
        for r in s.run(
            "MATCH (c:Checkpoint) WHERE c.source_id CONTAINS $ns "
            "RETURN c.status AS status, count(*) AS n ORDER BY status",
            ns=NS,
        )
    ]
    chunks_rec = s.run("MATCH (c:Chunk) WHERE c.book_id = $ns RETURN count(*) AS n", ns=NS).single()
    assert chunks_rec is not None
    out["chunks_ns"] = chunks_rec["n"]
    entities_rec = s.run(
        "MATCH (e:Entity) WHERE e.id STARTS WITH $ns RETURN count(*) AS n", ns=NS
    ).single()
    assert entities_rec is not None
    out["entities_ns"] = entities_rec["n"]
    rels_rec = s.run(
        "MATCH ()-[r:RELATED]->() WHERE r.source_id STARTS WITH $ns RETURN count(*) AS n",
        ns=NS,
    ).single()
    assert rels_rec is not None
    out["rels_ns"] = rels_rec["n"]
    out["book"] = [
        dict(r)
        for r in s.run(
            "MATCH (b:Book) WHERE b.id = $ns RETURN b.id AS id, b.title AS title",
            ns=NS,
        )
    ]
    out["chapters"] = [
        dict(r)
        for r in s.run(
            "MATCH (b:Book {id:$ns})-[:HAS_CHAPTER]->(c:Chapter) "
            "RETURN c.chapter_number AS n, c.title AS title "
            "ORDER BY c.chapter_number",
            ns=NS,
        )
    ]
driver.close()
print(json.dumps(out, indent=2, default=str))
