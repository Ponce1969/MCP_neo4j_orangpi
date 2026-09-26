"""Pre-resolución: (1) backup fresco vía _backup del pipeline, (2) grupos
DUPLICATE_ENTITY_LOGICAL COMPLETOS (sin truncar) del namespace GA.

Read-only sobre el grafo: solo lee y escribe el backup a ~/backups_neo4j/.
Nada se aplica. Correr en el OrangePi con el venv del proyecto.
"""
import asyncio
import importlib.util
import json
import os
from typing import Any

from neo4j import GraphDatabase

from book_graph_rag.config import Settings

_REPO = os.path.expanduser("~/Gonzalo_codigo/Mcp_libro/MCP_neo4j_orangpi")
_spec = importlib.util.spec_from_file_location(
    "run_full_pipeline", os.path.join(_REPO, "scripts", "run_full_pipeline.py")
)
assert _spec is not None
assert _spec.loader is not None
_run_full = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_run_full)
_backup = _run_full._backup
_make_driver = _run_full._make_driver

NS_PREFIX = "knowledge:graphrag-agentic:"
OUT = "/tmp/resolve_redo_groups.json"

QUERY = """
MATCH (n:Entity)
WHERE (n.merged_into IS NULL OR n.merged_into = '') AND n.id STARTS WITH $prefix
WITH n, split(n.id, ':') AS parts WHERE size(parts) >= 2
WITH n, parts[0] + ':' + parts[1] AS namespace, n.name AS name, n.type AS kind
WITH namespace, name, kind, collect(n) AS members WHERE size(members) > 1
RETURN namespace, name, kind,
       [x IN members | x.id] AS ids
ORDER BY namespace, coalesce(name, ''), coalesce(kind, '')
"""


async def backup() -> None:
    settings = Settings.model_validate({})
    driver = _make_driver(settings)
    try:
        path = await _backup(driver)
        print(f"BACKUP_OK {path}")
    finally:
        await driver.close()


def groups() -> list[dict[str, Any]]:
    settings = Settings.model_validate({})
    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value())
    )
    try:
        with driver.session() as s:
            rows = list(s.run(QUERY, prefix=NS_PREFIX))
        out = []
        for row in rows:
            ids = sorted(row["ids"], key=len)
            canonical, candidates = ids[0], ids[1:]
            out.append(
                {
                    "name": row["name"],
                    "kind": row["kind"],
                    "canonical": canonical,
                    "candidates": candidates,
                }
            )
        return out
    finally:
        driver.close()


def main() -> None:
    asyncio.run(backup())
    groups_list = groups()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(groups_list, f, indent=2)
    print(f"GROUPS {len(groups_list)} -> {OUT}")
    for g in groups_list:
        print(f"  [{g['kind']}] {g['name']!r}: {g['canonical']} <- {g['candidates']}")


if __name__ == "__main__":
    main()