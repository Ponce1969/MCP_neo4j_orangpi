"""Read-only probe #2: relationship types + hierarchy for the GA namespace."""
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

driver = GraphDatabase.driver(
    env.get("NEO4J_URI", "bolt://localhost:7687"),
    auth=(env.get("NEO4J_USER", "neo4j"), env.get("NEO4J_PASSWORD", "")),
)
out: dict[str, object] = {}
with driver.session(database=env.get("NEO4J_DATABASE", "neo4j")) as s:
    out["rel_types_by_source_id"] = [
        dict(r)
        for r in s.run(
            "MATCH ()-[r]->() WHERE r.source_id STARTS WITH $ns "
            "RETURN type(r) AS rel, count(*) AS n ORDER BY n DESC",
            ns=NS,
        )
    ]
    out["rel_types_on_chunks"] = [
        dict(r)
        for r in s.run(
            "MATCH (c:Chunk {book_id:$ns})<-[r]-() "
            "RETURN type(r) AS rel, count(*) AS n ORDER BY n DESC LIMIT 10",
            ns=NS,
        )
    ]
    out["chapters"] = [
        dict(r)
        for r in s.run(
            "MATCH (c:Chapter) WHERE c.book_id = $ns "
            "RETURN c.chapter_number AS n, c.title AS title ORDER BY c.chapter_number",
            ns=NS,
        )
    ]
    sections_rec = s.run(
        "MATCH (s:Section) WHERE s.book_id = $ns RETURN count(*) AS n", ns=NS
    ).single()
    assert sections_rec is not None
    out["sections_ns"] = sections_rec["n"]
    out["book_rels"] = [
        dict(r)
        for r in s.run(
            "MATCH (b:Book {id:$ns})-[r]->(x) "
            "RETURN type(r) AS rel, labels(x) AS target, count(*) AS n LIMIT 15",
            ns=NS,
        )
    ]
    chunks_rec = s.run("MATCH (c:Chunk) RETURN count(*) AS n").single()
    entities_rec = s.run("MATCH (e:Entity) RETURN count(*) AS n").single()
    books_rec = s.run("MATCH (b:Book) RETURN count(*) AS n").single()
    assert chunks_rec is not None
    assert entities_rec is not None
    assert books_rec is not None
    out["global_counts"] = {
        "chunks": chunks_rec["n"],
        "entities": entities_rec["n"],
        "books": books_rec["n"],
    }
    out["sample_entity"] = [
        dict(r)
        for r in s.run(
            "MATCH (e:Entity) WHERE e.id STARTS WITH $ns "
            "RETURN e.id AS id, e.name AS name LIMIT 3",
            ns=NS,
        )
    ]
driver.close()
print(json.dumps(out, indent=2, default=str))
