"""Allowlisted, read-only Neo4j audit adapter."""
from __future__ import annotations

# Static Cypher is intentionally one statement per named operation.
# ruff: noqa: E501
from typing import Any, Literal, cast

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings
from book_graph_rag.domain.audit_models import (
    RULE_CATALOG,
    AuditFinding,
    AuditQueryExecution,
    AuditSnapshot,
    AuditTarget,
    FindingSample,
    InventoryMetric,
    OverallState,
    QueryState,
    RuntimeMetadata,
    Severity,
    duplicate_group_id,
    normalize_key,
)
from book_graph_rag.ports.graph_audit_port import GraphIntegrityAuditPort

_NODES = ("Book", "Chapter", "Section", "Chunk", "Entity")
_RELS = ("CONTAINS", "HAS_SECTION", "HAS_SUBSECTION", "HAS_CHUNK", "MENTIONS", "RELATED")
_CATEGORY: dict[str, Literal["hierarchy", "endpoints", "provenance", "duplicates", "pages", "coverage"]] = {**{r: "hierarchy" for r in RULE_CATALOG if r.startswith("HIERARCHY_")}, **{r: "endpoints" for r in RULE_CATALOG if r.startswith("ENDPOINT_")}, **{r: "pages" for r in RULE_CATALOG if r.startswith("PAGE_")}, **{r: "duplicates" for r in RULE_CATALOG if r.startswith("DUPLICATE_")}, **{r: "provenance" for r in RULE_CATALOG if r.startswith(("PROVENANCE_", "ENTITY_"))}}

_QUERY_PLAN = (
    ("runtime_metadata", "CALL dbms.components() YIELD versions, edition RETURN versions[0] AS version, edition, $sample_limit AS sample_limit"),
    ("inventory_nodes", "UNWIND ['Book','Chapter','Section','Chunk','Entity'] AS label MATCH (n) WHERE label IN labels(n) RETURN label, count(n) AS total, $sample_limit AS sample_limit"),
    ("inventory_relationships", "UNWIND ['CONTAINS','HAS_SECTION','HAS_SUBSECTION','HAS_CHUNK','MENTIONS','RELATED'] AS rel_type MATCH ()-[r]->() WHERE type(r)=rel_type RETURN rel_type, count(r) AS total, $sample_limit AS sample_limit"),
    ("hierarchy_chapter_book_parent", "MATCH (n:Chapter) WHERE size([(b:Book)-[:CONTAINS]->(n) | b]) <> 1 WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("hierarchy_section_parent", "MATCH (n:Section) WHERE NOT (n)<-[:HAS_SECTION|HAS_SUBSECTION]-(:Chapter|Section) WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("hierarchy_chunk_parent_required", "MATCH (n:Chunk) WHERE NOT EXISTS {MATCH (:Book)-[:CONTAINS]->(:Chapter)-[:HAS_CHUNK]->(n)} AND NOT EXISTS {MATCH (:Book)-[:CONTAINS]->(:Chapter)-[:HAS_SECTION|HAS_SUBSECTION*1..]->(:Section)-[:HAS_CHUNK]->(n)} WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("hierarchy_chunk_multiple_parent", "MATCH (n:Chunk) WHERE size([(p)-[:HAS_CHUNK]->(n) | p]) > 1 WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("hierarchy_level_contradiction", "MATCH (n:Section) WHERE n.level IS NOT NULL AND n.level < 0 WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("endpoints_related", "MATCH (a)-[r:RELATED]->(b) WHERE NOT b:Entity OR a.id IS NULL OR b.id IS NULL WITH a,r,b ORDER BY coalesce(a.id,''),coalesce(r.type,''),coalesce(b.id,'') RETURN count(r) AS total, collect({key:coalesce(a.id,'')+'|'+coalesce(r.type,'')+'|'+coalesce(b.id,''),subject_ids:[coalesce(a.id,''),coalesce(b.id,'')],properties:properties(r)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("endpoints_mentions", "MATCH (a)-[r:MENTIONS]->(b) WHERE NOT a:Chunk OR NOT b:Entity OR a.id IS NULL OR b.id IS NULL WITH a,r,b ORDER BY coalesce(a.id,''),coalesce(b.id,'') RETURN count(r) AS total, collect({key:coalesce(a.id,'')+'|'+coalesce(b.id,''),subject_ids:[coalesce(a.id,''),coalesce(b.id,'')],properties:properties(r)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("endpoints_hierarchy", "MATCH (a)-[r]->(b) WHERE type(r) IN ['CONTAINS','HAS_SECTION','HAS_SUBSECTION','HAS_CHUNK'] AND (a.id IS NULL OR b.id IS NULL) WITH a,r,b ORDER BY coalesce(a.id,''),type(r),coalesce(b.id,'') RETURN count(r) AS total, collect({key:coalesce(a.id,''),subject_ids:[coalesce(a.id,''),coalesce(b.id,'')],properties:properties(r)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("entity_unmentioned", "MATCH (n:Entity) WHERE NOT (n)<-[:MENTIONS]-(:Chunk) WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("entity_isolated_related", "MATCH (n:Entity) WHERE NOT (n)-[:RELATED]-() WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("provenance_entity", "MATCH (n:Entity) WHERE n.source_page IS NULL AND NOT (n)<-[:MENTIONS]-(:Chunk) WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("provenance_relationship", "MATCH (a)-[r:RELATED]->(b) WHERE r.source_page IS NULL OR r.chunk_index IS NULL WITH a,r,b ORDER BY coalesce(a.id,''),coalesce(r.type,''),coalesce(b.id,'') RETURN count(r) AS total, collect({key:coalesce(a.id,'')+'|'+coalesce(r.type,'')+'|'+coalesce(b.id,''),subject_ids:[coalesce(a.id,''),coalesce(b.id,'')],properties:properties(r)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("provenance_mentions", "MATCH (a)-[r:MENTIONS]->(b) WHERE r.source_page IS NULL AND r.chunk_index IS NULL WITH a,r,b ORDER BY coalesce(a.id,''),coalesce(b.id,'') RETURN count(r) AS total, collect({key:coalesce(a.id,'')+'|'+coalesce(b.id,''),subject_ids:[coalesce(a.id,''),coalesce(b.id,'')],properties:properties(r)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("provenance_chunk", "MATCH (n:Chunk) WHERE n.book_id IS NULL OR n.id IS NULL WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("duplicates_entity", "MATCH (n:Entity) WITH n ORDER BY coalesce(n.id,'') WITH n.name AS name,n.type AS kind,collect(n) AS members WHERE size(members)>1 WITH name,kind,members ORDER BY coalesce(name,''),coalesce(kind,'') RETURN count(members) AS total, collect({key:coalesce(name,'')+'|'+coalesce(kind,''),name:coalesce(name,''),kind:coalesce(kind,''),subject_ids:[x IN members | coalesce(x.id,'')],properties:properties(members[0])})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("duplicates_relationship", "MATCH (a)-[r:RELATED]->(b) WITH a,r,b ORDER BY coalesce(a.id,''),coalesce(r.type,''),coalesce(b.id,'') WITH coalesce(a.id,'') AS source,coalesce(r.type,'') AS kind,coalesce(b.id,'') AS target,collect(r) AS members WHERE size(members)>1 WITH source,kind,target,members ORDER BY source,kind,target RETURN count(members) AS total, collect({key:source+'|'+kind+'|'+target,source:source,kind:kind,target:target,subject_ids:[source,target],native_edge_count:size(members),properties:properties(members[0])})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("pages_chunk", "MATCH (n:Chunk) OPTIONAL MATCH (b:Book {id:n.book_id}) WHERE n.page_start IS NULL OR n.page_end IS NULL OR n.page_start<1 OR n.page_end<n.page_start OR (b.page_count IS NOT NULL AND n.page_end>b.page_count) WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("pages_chapter", "MATCH (n:Chapter) OPTIONAL MATCH (b:Book)-[:CONTAINS]->(n) WHERE n.page_start IS NULL OR n.page_start<1 OR (b.page_count IS NOT NULL AND n.page_start>b.page_count) WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("pages_section", "MATCH (n:Section) OPTIONAL MATCH (b:Book)-[:CONTAINS]->(:Chapter)-[:HAS_SECTION|HAS_SUBSECTION*1..]->(n) WHERE n.page_start IS NULL OR n.page_start<1 OR (b.page_count IS NOT NULL AND n.page_start>b.page_count) WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
)
QUERY_PLAN = _QUERY_PLAN
_RULE_QUERY = dict(_QUERY_PLAN)
_RULE_NAME = {"HIERARCHY_CHAPTER_BOOK_PARENT":"hierarchy_chapter_book_parent", "HIERARCHY_SECTION_PARENT":"hierarchy_section_parent", "HIERARCHY_CHUNK_PARENT_REQUIRED":"hierarchy_chunk_parent_required", "HIERARCHY_CHUNK_MULTIPLE_PARENT":"hierarchy_chunk_multiple_parent", "HIERARCHY_LEVEL_CONTRADICTION":"hierarchy_level_contradiction", "ENDPOINT_RELATED_INVALID":"endpoints_related", "ENDPOINT_MENTIONS_INVALID":"endpoints_mentions", "ENDPOINT_HIERARCHY_INVALID":"endpoints_hierarchy", "ENTITY_UNMENTIONED":"entity_unmentioned", "ENTITY_ISOLATED_RELATED":"entity_isolated_related", "PROVENANCE_ENTITY_MISSING":"provenance_entity", "PROVENANCE_RELATIONSHIP_MISSING":"provenance_relationship", "PROVENANCE_MENTIONS_MISSING":"provenance_mentions", "PROVENANCE_CHUNK_MISSING":"provenance_chunk", "DUPLICATE_ENTITY_LOGICAL":"duplicates_entity", "DUPLICATE_RELATIONSHIP_LOGICAL":"duplicates_relationship", "PAGE_CHUNK_INVALID_RANGE":"pages_chunk", "PAGE_CHAPTER_INVALID_START":"pages_chapter", "PAGE_SECTION_INVALID_START":"pages_section"}
_RULES = tuple((rule, _RULE_NAME[rule], _CATEGORY[rule]) for rule in RULE_CATALOG)


class Neo4jAuditAdapter(GraphIntegrityAuditPort):
    """Collect a typed snapshot through one configured, read-only session."""
    def __init__(self, settings: Settings) -> None:
        self._driver: Any = AsyncGraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()))

    async def close(self) -> None:
        await self._driver.close()

    @staticmethod
    async def _transaction(tx: Any, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        result = await tx.run(query, params)
        rows: list[dict[str, Any]] = []
        async for row in result:
            rows.append(row.data() if hasattr(row, "data") else dict(row))
        return rows

    @classmethod
    async def _read(cls, session: Any, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], await session.execute_read(cls._transaction, query, params))

    @staticmethod
    def _sample(value: Any, namespace: str | None = None) -> FindingSample:
        if not isinstance(value, dict):
            return FindingSample(key=str(value), subject_ids=(str(value),))
        data = dict(value)
        if namespace == "entity":
            name, kind = data.pop("name", None), data.pop("kind", None)
            key = f"{normalize_key(name)}|{normalize_key(kind)}"
            data["key"] = key
            data["group_id"] = duplicate_group_id(namespace, key)
        elif namespace == "relationship":
            parts = tuple(data.pop(part, None) for part in ("source", "kind", "target"))
            key = "|".join(normalize_key(part) for part in parts)
            data["key"] = key
            data["group_id"] = duplicate_group_id(namespace, key)
        data["subject_ids"] = tuple(sorted(str(x) for x in data.get("subject_ids", ())))
        return FindingSample(**data)

    async def collect_snapshot(self, target: AuditTarget, sample_limit: int) -> AuditSnapshot:
        if sample_limit < 0 or target.selector != "bookgraph-neo4j" or not target.database:
            raise ValueError("invalid audit target or sample_limit")
        params = {"sample_limit": sample_limit}
        calls: list[AuditQueryExecution] = []
        inventory: dict[str, InventoryMetric] = {}
        findings: list[AuditFinding] = []
        runtime = RuntimeMetadata()
        try:
            async with self._driver.session(database=target.database) as session:
                try:
                    rows = await self._read(session, _RULE_QUERY["runtime_metadata"], params)
                    if rows:
                        runtime = RuntimeMetadata(neo4j_version=rows[0].get("version"), edition=rows[0].get("edition"))
                    calls.append(AuditQueryExecution(name="runtime_metadata", state=QueryState.EVALUATED))
                except Exception:
                    calls.append(AuditQueryExecution(name="runtime_metadata", state=QueryState.UNSUPPORTED, error="unavailable"))
                for name, field, keys in (("inventory_nodes", "label", _NODES), ("inventory_relationships", "rel_type", _RELS)):
                    rows = await self._read(session, _RULE_QUERY[name], params)
                    for row in rows:
                        if row.get(field) in keys:
                            inventory[str(row[field])] = InventoryMetric(value=int(row.get("total", 0)), state="evaluated")
                    for key in keys:
                        inventory.setdefault(key, InventoryMetric(value=0, state="evaluated"))
                    calls.append(AuditQueryExecution(name=name, state=QueryState.EVALUATED))
                for rule, query_name, category in _RULES:
                    rows = await self._read(session, _RULE_QUERY[query_name], params)
                    row = rows[0] if rows else {"total": 0, "samples": []}
                    total = int(row.get("total", 0) or 0)
                    namespace = "entity" if rule == "DUPLICATE_ENTITY_LOGICAL" else "relationship" if rule == "DUPLICATE_RELATIONSHIP_LOGICAL" else None
                    samples = tuple(self._sample(x, namespace) for x in (row.get("samples") or [])[:sample_limit])
                    severity = Severity.INCOMPLETE if category == "provenance" else Severity.WARNING if category == "duplicates" or rule == "ENTITY_ISOLATED_RELATED" else Severity.BLOCKING
                    findings.append(AuditFinding(rule_id=rule, category=category, severity=severity, total=total, samples=samples, sample_limit=sample_limit))
                    calls.append(AuditQueryExecution(name=rule, state=QueryState.EVALUATED))
            return AuditSnapshot(inventory=inventory, findings=tuple(findings), queries=tuple(calls), runtime=runtime, provenance_incomplete=any(f.severity == Severity.INCOMPLETE and f.total for f in findings))
        except Exception as error:
            name = type(error).__name__.lower()
            state = OverallState.UNREACHABLE if any(x in name for x in ("connection", "timeout", "auth", "network", "unavailable")) else OverallState.FAILED
            query_state = QueryState.UNREACHABLE if state == OverallState.UNREACHABLE else QueryState.FAILED
            return AuditSnapshot(queries=tuple(calls) + (AuditQueryExecution(name="audit", state=query_state, error="failed"),), failure_state=state)
