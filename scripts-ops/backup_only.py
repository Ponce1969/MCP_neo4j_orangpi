"""Backup fresco del grafo via la lógica de _backup del pipeline.

Variant: json.dumps(default=str) para propiedades neo4j.DateTime (puestas por
el fold de aliases de los merges) que el dump del pipeline no serializa.
Read-only sobre el grafo; solo escribe el JSON de backup.
"""

import asyncio
import importlib.util
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings

_REPO = os.path.expanduser("~/Gonzalo_codigo/Mcp_libro/MCP_neo4j_orangpi")
_spec = importlib.util.spec_from_file_location(
    "run_full_pipeline", os.path.join(_REPO, "scripts", "run_full_pipeline.py")
)
assert _spec is not None
assert _spec.loader is not None
_run_full = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_run_full)

_BACKUP_DIR = Path.home() / "backups_neo4j"
_INDEX_NODE_LABELS = _run_full._INDEX_NODE_LABELS
_INDEX_EDGE_TYPES = _run_full._INDEX_EDGE_TYPES


async def backup(default_str: bool = True) -> Path:
    driver = AsyncGraphDatabase.driver(
        Settings.model_validate({}).neo4j_uri,
        auth=(
            Settings.model_validate({}).neo4j_user,
            Settings.model_validate({}).neo4j_password.get_secret_value(),
        ),
    )
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = _BACKUP_DIR / f"bookgraph_backup_{timestamp}.json"
    try:
        async with driver.session() as session:
            label_filter = " OR ".join(f"n:{label}" for label in _INDEX_NODE_LABELS)
            node_result = await session.run(
                f"MATCH (n) WHERE {label_filter} "
                "RETURN labels(n) AS labels, properties(n) AS properties"
            )
            nodes = [
                {"labels": rec["labels"], "properties": rec["properties"]}
                async for rec in node_result
            ]
            rel_result = await session.run(
                "MATCH (a)-[r]->(b) WHERE type(r) IN $types "
                "RETURN type(r) AS type, properties(r) AS properties, "
                "labels(a) AS start_labels, properties(a) AS start_props, "
                "labels(b) AS end_labels, properties(b) AS end_props",
                {"types": list(_INDEX_EDGE_TYPES)},
            )
            rels = [
                {
                    "type": rec["type"],
                    "properties": rec["properties"],
                    "start_labels": rec["start_labels"],
                    "start_props": rec["start_props"],
                    "end_labels": rec["end_labels"],
                    "end_props": rec["end_props"],
                }
                async for rec in rel_result
            ]
    finally:
        await driver.close()
    payload = {
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "nodes": nodes,
        "relationships": rels,
    }
    backup_path.write_text(
        json.dumps(payload, indent=2, default=str if default_str else None), encoding="utf-8"
    )
    return backup_path


if __name__ == "__main__":
    path = asyncio.run(backup())
    print(f"BACKUP_OK {path}")