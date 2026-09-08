"""Allowlisted, read-only Neo4j audit adapter."""
from __future__ import annotations

# Static Cypher is intentionally one statement per named operation.
# ruff: noqa: E501
from typing import Any, cast

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings
from book_graph_rag.domain.audit_models import (
    RULE_CATALOG,
    RULE_CATEGORY,
    AuditFinding,
    AuditQueryExecution,
    AuditScope,
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
    severity_for_category,
)
from book_graph_rag.ports.graph_audit_port import GraphIntegrityAuditPort

_NODES = ("Book", "Chapter", "Section", "Chunk", "Entity")
_RELS = ("CONTAINS", "HAS_SECTION", "HAS_SUBSECTION", "HAS_CHUNK", "MENTIONS", "RELATED")
_CATEGORY = RULE_CATEGORY

_QUERY_PLAN = (
    ("runtime_metadata", "CALL dbms.components() YIELD versions, edition RETURN versions[0] AS version, edition, $sample_limit AS sample_limit"),
    ("inventory_nodes", "UNWIND ['Book','Chapter','Section','Chunk','Entity'] AS label MATCH (n) WHERE label IN labels(n) RETURN label, count(n) AS total, $sample_limit AS sample_limit"),
    ("inventory_relationships", "UNWIND ['CONTAINS','HAS_SECTION','HAS_SUBSECTION','HAS_CHUNK','MENTIONS','RELATED'] AS rel_type MATCH ()-[r]->() WHERE type(r)=rel_type RETURN rel_type, count(r) AS total, $sample_limit AS sample_limit"),
    ("hierarchy_chapter_book_parent", "MATCH (n:Chapter) WHERE size([(b:Book)-[:CONTAINS]->(n) | b]) <> 1 WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("hierarchy_section_parent", "MATCH (n:Section) WHERE NOT (n)<-[:HAS_SECTION|HAS_SUBSECTION]-(:Chapter|Section) WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("hierarchy_chunk_parent_required", "MATCH (n:Chunk) WHERE NOT EXISTS {MATCH (:Book)-[:CONTAINS]->(:Chapter)-[:HAS_CHUNK]->(n)} AND NOT EXISTS {MATCH (:Book)-[:CONTAINS]->(:Chapter)-[:HAS_SECTION|HAS_SUBSECTION*1..]->(:Section)-[:HAS_CHUNK]->(n)} WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("hierarchy_chunk_multiple_parent", "MATCH (n:Chunk) WHERE size([(p)-[:HAS_CHUNK]->(n) | p]) > 1 WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("hierarchy_level_contradiction", "MATCH (n:Section) WHERE n.level IS NOT NULL AND n.level < 0 WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("endpoints_related", "MATCH (a)-[r:RELATED]->(b) WHERE NOT a:Entity OR NOT b:Entity OR a.id IS NULL OR b.id IS NULL WITH a,r,b ORDER BY coalesce(a.id,''),coalesce(r.type,''),coalesce(b.id,'') RETURN count(r) AS total, collect({key:coalesce(a.id,'')+'|'+coalesce(r.type,'')+'|'+coalesce(b.id,''),subject_ids:[coalesce(a.id,''),coalesce(b.id,'')],properties:properties(r)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("endpoints_mentions", "MATCH (a)-[r:MENTIONS]->(b) WHERE NOT a:Chunk OR NOT b:Entity OR a.book_id IS NULL OR a.chunk_index IS NULL OR b.id IS NULL WITH a,r,b ORDER BY coalesce(toString(a.book_id),'')+'|'+coalesce(toString(a.chunk_index),'')+'|'+coalesce(b.id,'') RETURN count(r) AS total, collect({key:coalesce(toString(a.book_id),'')+'|'+coalesce(toString(a.chunk_index),'')+'|'+coalesce(b.id,''),subject_ids:[coalesce(toString(a.book_id),'')+'|'+coalesce(toString(a.chunk_index),''),coalesce(b.id,'')],properties:properties(r)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("endpoints_hierarchy", "MATCH (a)-[r]->(b) WHERE type(r) IN ['CONTAINS','HAS_SECTION','HAS_SUBSECTION','HAS_CHUNK'] AND NOT ((type(r) = 'CONTAINS' AND a:Book AND a.id IS NOT NULL AND b:Chapter AND b.number IS NOT NULL AND b.title IS NOT NULL) OR (type(r) = 'HAS_SECTION' AND a:Chapter AND a.number IS NOT NULL AND a.title IS NOT NULL AND b:Section AND b.chapter_number IS NOT NULL AND b.title IS NOT NULL) OR (type(r) = 'HAS_SUBSECTION' AND a:Section AND a.chapter_number IS NOT NULL AND a.title IS NOT NULL AND b:Section AND b.chapter_number IS NOT NULL AND b.title IS NOT NULL) OR (type(r) = 'HAS_CHUNK' AND (a:Chapter AND a.number IS NOT NULL AND a.title IS NOT NULL OR a:Section AND a.chapter_number IS NOT NULL AND a.title IS NOT NULL) AND b:Chunk AND b.book_id IS NOT NULL AND b.chunk_index IS NOT NULL)) WITH a,r,b,CASE WHEN a:Book THEN coalesce(a.id,'') WHEN a:Chapter THEN coalesce(toString(a.number),'')+'|'+coalesce(a.title,'') WHEN a:Section THEN coalesce(toString(a.chapter_number),'')+'|'+coalesce(a.title,'') WHEN a:Chunk THEN coalesce(toString(a.book_id),'')+'|'+coalesce(toString(a.chunk_index),'') WHEN a:Entity THEN coalesce(a.id,'') ELSE '' END AS source_key,CASE WHEN b:Book THEN coalesce(b.id,'') WHEN b:Chapter THEN coalesce(toString(b.number),'')+'|'+coalesce(b.title,'') WHEN b:Section THEN coalesce(toString(b.chapter_number),'')+'|'+coalesce(b.title,'') WHEN b:Chunk THEN coalesce(toString(b.book_id),'')+'|'+coalesce(toString(b.chunk_index),'') WHEN b:Entity THEN coalesce(b.id,'') ELSE '' END AS target_key ORDER BY source_key,type(r),target_key RETURN count(r) AS total, collect({key:source_key+'|'+type(r)+'|'+target_key,subject_ids:[source_key,target_key],properties:properties(r)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("entity_unmentioned", "MATCH (n:Entity) WHERE (n.merged_into IS NULL OR n.merged_into = '') AND NOT (n)<-[:MENTIONS]-(:Chunk) WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("entity_isolated_related", "MATCH (n:Entity) WHERE (n.merged_into IS NULL OR n.merged_into = '') AND NOT (n)-[:RELATED]-() WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("provenance_entity", "MATCH (n:Entity) WHERE (n.merged_into IS NULL OR n.merged_into = '') AND n.source_page IS NULL AND NOT (n)<-[:MENTIONS]-(:Chunk) WITH n ORDER BY coalesce(n.id,'') RETURN count(n) AS total, collect({key:coalesce(n.id,''),subject_ids:[coalesce(n.id,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("provenance_relationship", "MATCH (a)-[r:RELATED]->(b) WHERE r.source_page IS NULL OR r.chunk_index IS NULL WITH a,r,b ORDER BY coalesce(a.id,''),coalesce(r.type,''),coalesce(b.id,'') RETURN count(r) AS total, collect({key:coalesce(a.id,'')+'|'+coalesce(r.type,'')+'|'+coalesce(b.id,''),subject_ids:[coalesce(a.id,''),coalesce(b.id,'')],properties:properties(r)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("provenance_mentions", "MATCH (a)-[r:MENTIONS]->(b) WHERE r.source_page IS NULL AND r.chunk_index IS NULL WITH a,r,b ORDER BY coalesce(toString(a.book_id),'')+'|'+coalesce(toString(a.chunk_index),'')+'|'+coalesce(b.id,'') RETURN count(r) AS total, collect({key:coalesce(toString(a.book_id),'')+'|'+coalesce(toString(a.chunk_index),'')+'|'+coalesce(b.id,''),subject_ids:[coalesce(toString(a.book_id),'')+'|'+coalesce(toString(a.chunk_index),''),coalesce(b.id,'')],properties:properties(r)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("provenance_chunk", "MATCH (n:Chunk) WHERE n.book_id IS NULL OR n.chunk_index IS NULL WITH n ORDER BY coalesce(toString(n.book_id),'')+'|'+coalesce(toString(n.chunk_index),'') RETURN count(n) AS total, collect({key:coalesce(toString(n.book_id),'')+'|'+coalesce(toString(n.chunk_index),''),subject_ids:[coalesce(toString(n.book_id),'')+'|'+coalesce(toString(n.chunk_index),'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("duplicates_entity", "MATCH (n:Entity) WHERE (n.merged_into IS NULL OR n.merged_into = '') AND n.id IS NOT NULL WITH n, split(n.id, ':') AS parts WHERE size(parts) >= 2 WITH n, parts ORDER BY coalesce(n.id, '') WITH n, parts[0] + ':' + parts[1] AS namespace, n.name AS name, n.type AS kind WITH namespace, name, kind, collect(n) AS members WHERE size(members) > 1 WITH namespace, name, kind, members ORDER BY namespace, coalesce(name, ''), coalesce(kind, '') RETURN count(members) AS total, collect({key: namespace + '|' + coalesce(name, '') + '|' + coalesce(kind, ''), namespace: namespace, name: coalesce(name, ''), kind: coalesce(kind, ''), subject_ids: [x IN members | coalesce(x.id, '')], properties: properties(members[0])})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("duplicates_relationship", "MATCH (a)-[r:RELATED]->(b) WITH a,r,b ORDER BY coalesce(a.id,''),coalesce(r.type,''),coalesce(b.id,'') WITH coalesce(a.id,'') AS source,coalesce(r.type,'') AS kind,coalesce(b.id,'') AS target,collect(r) AS members WHERE size(members)>1 WITH source,kind,target,members ORDER BY source,kind,target RETURN count(members) AS total, collect({key:source+'|'+kind+'|'+target,source:source,kind:kind,target:target,subject_ids:[source,target],native_edge_count:size(members),properties:properties(members[0])})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("pages_chunk", "MATCH (n:Chunk) OPTIONAL MATCH (b:Book {id:n.book_id}) WITH n,b WHERE n.page_start IS NULL OR n.page_end IS NULL OR n.page_start<1 OR n.page_end<n.page_start OR (b.page_count IS NOT NULL AND n.page_end>b.page_count) WITH n ORDER BY coalesce(toString(n.book_id),'')+'|'+coalesce(toString(n.chunk_index),'') RETURN count(n) AS total, collect({key:coalesce(toString(n.book_id),'')+'|'+coalesce(toString(n.chunk_index),''),subject_ids:[coalesce(toString(n.book_id),'')+'|'+coalesce(toString(n.chunk_index),'' )],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("pages_chapter", "MATCH (n:Chapter) OPTIONAL MATCH (b:Book)-[:CONTAINS]->(n) WITH n,b WHERE n.page_start IS NULL OR n.page_start<1 OR (b.page_count IS NOT NULL AND n.page_start>b.page_count) WITH n ORDER BY coalesce(toString(n.number),'')+'|'+coalesce(n.title,'') RETURN count(n) AS total, collect({key:coalesce(toString(n.number),'')+'|'+coalesce(n.title,''),subject_ids:[coalesce(toString(n.number),'')+'|'+coalesce(n.title,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
    ("pages_section", "MATCH (n:Section) OPTIONAL MATCH (b:Book)-[:CONTAINS]->(:Chapter)-[:HAS_SECTION|HAS_SUBSECTION*1..]->(n) WITH n,b WHERE n.page_start IS NULL OR n.page_start<1 OR (b.page_count IS NOT NULL AND n.page_start>b.page_count) WITH n ORDER BY coalesce(toString(n.chapter_number),'')+'|'+coalesce(n.title,'') RETURN count(n) AS total, collect({key:coalesce(toString(n.chapter_number),'')+'|'+coalesce(n.title,''),subject_ids:[coalesce(toString(n.chapter_number),'')+'|'+coalesce(n.title,'')],properties:properties(n)})[..$sample_limit] AS samples, $sample_limit AS sample_limit"),
)
QUERY_PLAN = _QUERY_PLAN
_RULE_QUERY = dict(_QUERY_PLAN)
_RULE_NAME = {"HIERARCHY_CHAPTER_BOOK_PARENT":"hierarchy_chapter_book_parent", "HIERARCHY_SECTION_PARENT":"hierarchy_section_parent", "HIERARCHY_CHUNK_PARENT_REQUIRED":"hierarchy_chunk_parent_required", "HIERARCHY_CHUNK_MULTIPLE_PARENT":"hierarchy_chunk_multiple_parent", "HIERARCHY_LEVEL_CONTRADICTION":"hierarchy_level_contradiction", "ENDPOINT_RELATED_INVALID":"endpoints_related", "ENDPOINT_MENTIONS_INVALID":"endpoints_mentions", "ENDPOINT_HIERARCHY_INVALID":"endpoints_hierarchy", "ENTITY_UNMENTIONED":"entity_unmentioned", "ENTITY_ISOLATED_RELATED":"entity_isolated_related", "PROVENANCE_ENTITY_MISSING":"provenance_entity", "PROVENANCE_RELATIONSHIP_MISSING":"provenance_relationship", "PROVENANCE_MENTIONS_MISSING":"provenance_mentions", "PROVENANCE_CHUNK_MISSING":"provenance_chunk", "DUPLICATE_ENTITY_LOGICAL":"duplicates_entity", "DUPLICATE_RELATIONSHIP_LOGICAL":"duplicates_relationship", "PAGE_CHUNK_INVALID_RANGE":"pages_chunk", "PAGE_CHAPTER_INVALID_START":"pages_chapter", "PAGE_SECTION_INVALID_START":"pages_section"}
_RULES = tuple((rule, _RULE_NAME[rule], _CATEGORY[rule]) for rule in RULE_CATALOG)


_ENTITY_RULES = {"entity_unmentioned", "entity_isolated_related", "provenance_entity", "duplicates_entity"}
_RELATED_RULES = {"endpoints_related", "provenance_relationship", "duplicates_relationship"}
_CHUNK_RULES = {"provenance_chunk", "pages_chunk"}
_MENTIONS_RULES = {"endpoints_mentions", "provenance_mentions"}
_CHUNK_HIERARCHY_RULES = {"hierarchy_chunk_parent_required", "hierarchy_chunk_multiple_parent"}
_BOOK_HIERARCHY_RULES = {"hierarchy_chapter_book_parent", "hierarchy_section_parent", "hierarchy_level_contradiction"}


def _scope_predicate(query_name: str) -> str | None:
    """Return the Cypher predicate to inject for a scoped query, or None."""
    if query_name in _ENTITY_RULES:
        return "n.id STARTS WITH $scope_prefix"
    if query_name in _RELATED_RULES:
        return "a.id STARTS WITH $scope_prefix AND b.id STARTS WITH $scope_prefix"
    if query_name in _CHUNK_RULES or query_name in _CHUNK_HIERARCHY_RULES:
        return "coalesce(toString(n.book_id), '') = $scope_source_id"
    if query_name in _MENTIONS_RULES:
        return "coalesce(toString(a.book_id), '') = $scope_source_id"
    if query_name == "endpoints_hierarchy":
        return (
            "(a:Book AND a.id = $scope_source_id) OR "
            "coalesce(toString(a.book_id), '') = $scope_source_id OR "
            "coalesce(toString(b.book_id), '') = $scope_source_id OR "
            "EXISTS { MATCH (b:Book {id: $scope_source_id})-[:CONTAINS|HAS_SECTION|HAS_SUBSECTION|HAS_CHUNK*1..]->(a) }"
        )
    if query_name in _BOOK_HIERARCHY_RULES:
        return "EXISTS { MATCH (b:Book {id: $scope_source_id})-[:CONTAINS|HAS_SECTION|HAS_SUBSECTION*1..]->(n) }"
    if query_name == "pages_chapter":
        return "EXISTS { MATCH (b:Book {id: $scope_source_id})-[:CONTAINS]->(n) }"
    if query_name == "pages_section":
        return "EXISTS { MATCH (b:Book {id: $scope_source_id})-[:CONTAINS]->(:Chapter)-[:HAS_SECTION|HAS_SUBSECTION*1..]->(n) }"
    return None


def _inject_scope(query: str, predicate: str) -> str:
    """Inject a scope predicate into a Cypher statement.

    Prefer insertion after the first WHERE clause when that WHERE appears
    before any WITH/RETURN projection.  Otherwise insert a WHERE before the
    first WITH so variables referenced by the predicate are still in scope.
    """
    where_marker = " WHERE "
    with_marker = " WITH "
    where_idx = query.find(where_marker)
    with_idx = query.find(with_marker)
    if where_idx != -1 and (with_idx == -1 or where_idx < with_idx):
        insert_at = where_idx + len(where_marker)
        return query[:insert_at] + f"({predicate}) AND " + query[insert_at:]
    if with_idx != -1:
        return query[:with_idx] + f" WHERE {predicate}" + query[with_idx:]
    return query


def _scoped_inventory_nodes() -> str:
    return (
        "UNWIND ['Book','Chapter','Section','Chunk','Entity'] AS label "
        "MATCH (n) WHERE label IN labels(n) AND "
        "((label = 'Entity' AND n.id STARTS WITH $scope_prefix) OR "
        "(label = 'Chunk' AND coalesce(toString(n.book_id), '') = $scope_source_id) OR "
        "(label = 'Book' AND n.id = $scope_source_id) OR "
        "(label = 'Chapter' AND EXISTS { MATCH (b:Book {id: $scope_source_id})-[:CONTAINS]->(n) }) OR "
        "(label = 'Section' AND EXISTS { MATCH (b:Book {id: $scope_source_id})-[:CONTAINS]->(:Chapter)-[:HAS_SECTION|HAS_SUBSECTION*1..]->(n) })) "
        "RETURN label, count(n) AS total, $sample_limit AS sample_limit"
    )


def _scoped_inventory_relationships() -> str:
    return (
        "UNWIND ['CONTAINS','HAS_SECTION','HAS_SUBSECTION','HAS_CHUNK','MENTIONS','RELATED'] AS rel_type "
        "MATCH (a)-[r]->(b) WHERE type(r)=rel_type AND "
        "((rel_type IN ['CONTAINS','HAS_SECTION','HAS_SUBSECTION','HAS_CHUNK'] AND "
        "  ((a:Book AND a.id = $scope_source_id) OR coalesce(toString(a.book_id), '') = $scope_source_id OR coalesce(toString(b.book_id), '') = $scope_source_id)) OR "
        "(rel_type = 'MENTIONS' AND coalesce(toString(a.book_id), '') = $scope_source_id) OR "
        "(rel_type = 'RELATED' AND a.id STARTS WITH $scope_prefix AND b.id STARTS WITH $scope_prefix)) "
        "RETURN rel_type, count(r) AS total, $sample_limit AS sample_limit"
    )


def _scoped_query(query_name: str, base_query: str, scope: AuditScope) -> str:
    """Return a scope-bounded variant of a named query."""
    if query_name == "runtime_metadata":
        return base_query
    if query_name == "inventory_nodes":
        return _scoped_inventory_nodes()
    if query_name == "inventory_relationships":
        return _scoped_inventory_relationships()
    predicate = _scope_predicate(query_name)
    if predicate is None:
        return base_query
    return _inject_scope(base_query, predicate)


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

    async def collect_snapshot(self, target: AuditTarget, sample_limit: int, scope: AuditScope | None = None) -> AuditSnapshot:
        if sample_limit < 0 or target.selector != "bookgraph-neo4j" or not target.database:
            raise ValueError("invalid audit target or sample_limit")
        params: dict[str, Any] = {"sample_limit": sample_limit}
        if scope is not None:
            params["scope_prefix"] = scope.entity_prefix
            params["scope_source_id"] = scope.source_id
        rule_query = {name: (_scoped_query(name, query, scope) if scope is not None else query) for name, query in _QUERY_PLAN}
        calls: list[AuditQueryExecution] = []
        inventory: dict[str, InventoryMetric] = {}
        findings: list[AuditFinding] = []
        runtime = RuntimeMetadata()
        try:
            async with self._driver.session(database=target.database) as session:
                try:
                    rows = await self._read(session, rule_query["runtime_metadata"], params)
                    if rows:
                        runtime = RuntimeMetadata(neo4j_version=rows[0].get("version"), edition=rows[0].get("edition"))
                    calls.append(AuditQueryExecution(name="runtime_metadata", state=QueryState.EVALUATED))
                except Exception:
                    calls.append(AuditQueryExecution(name="runtime_metadata", state=QueryState.UNSUPPORTED, error="unavailable"))
                for name, field, keys in (("inventory_nodes", "label", _NODES), ("inventory_relationships", "rel_type", _RELS)):
                    rows = await self._read(session, rule_query[name], params)
                    for row in rows:
                        if row.get(field) in keys:
                            inventory[str(row[field])] = InventoryMetric(value=int(row.get("total", 0)), state="evaluated")
                    for key in keys:
                        inventory.setdefault(key, InventoryMetric(value=0, state="evaluated"))
                    calls.append(AuditQueryExecution(name=name, state=QueryState.EVALUATED))
                for rule, query_name, category in _RULES:
                    rows = await self._read(session, rule_query[query_name], params)
                    row = rows[0] if rows else {"total": 0, "samples": []}
                    total = int(row.get("total", 0) or 0)
                    namespace = "entity" if rule == "DUPLICATE_ENTITY_LOGICAL" else "relationship" if rule == "DUPLICATE_RELATIONSHIP_LOGICAL" else None
                    samples = tuple(self._sample(x, namespace) for x in (row.get("samples") or [])[:sample_limit])
                    severity = severity_for_category(category)
                    findings.append(AuditFinding(rule_id=rule, category=category, severity=severity, total=total, samples=samples, sample_limit=sample_limit))
                    calls.append(AuditQueryExecution(name=rule, state=QueryState.EVALUATED))
            return AuditSnapshot(inventory=inventory, findings=tuple(findings), queries=tuple(calls), runtime=runtime, provenance_incomplete=any(f.severity == Severity.INCOMPLETE and f.total for f in findings))
        except Exception as error:
            import traceback
            traceback.print_exc()
            name = type(error).__name__.lower()
            state = OverallState.UNREACHABLE if any(x in name for x in ("connection", "timeout", "auth", "network", "unavailable")) else OverallState.FAILED
            query_state = QueryState.UNREACHABLE if state == OverallState.UNREACHABLE else QueryState.FAILED
            return AuditSnapshot(queries=tuple(calls) + (AuditQueryExecution(name="audit", state=query_state, error="failed"),), failure_state=state)
