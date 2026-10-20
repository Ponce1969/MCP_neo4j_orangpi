"""Recrear ``chunk_text_index`` con el analyzer declarado (operación DDL gateada).

Por qué existe (S5 de ``odd/tasks/retrieval-phase2-lexical.md``): el analyzer
``standard-no-stop-words`` era un copo de nieve — ningún código del repo lo
declaraba, así que una reconstrucción futura podía revertir silenciosamente
cualquier cambio. Ahora ``ensure_indexes`` lo declara en el DDL (T1), y este
script permite recrear el índice con **cualquier** analyzer elegido siguiendo el
patrón de operación gateada del proyecto (dry-run por defecto → aprobación →
fingerprint del plan revisado), el mismo de ``scripts-ops/resolve_intra_ns.py``.

El índice fulltext se repuebla **solo** a partir de los nodos ``:Chunk`` al
crearse: DROP + CREATE no escribe datos ni vectores, solo DDL. La recreación es
idempotente: re-correrla produce siempre el mismo estado final (índice ONLINE al
100 % con el analyzer declarado).

Fases:
  * dry-run (default, READ-ONLY): imprime estado actual del índice, censo de
    chunks, el plan exacto (DROP → CREATE → ``db.awaitIndexes``) y su
    fingerprint sha256. No escribe nada.
  * ``--apply``: exige ``--approval <archivo>`` cuyo contenido incluya la
    palabra ``approve`` y ``--expect-fingerprint <hex>`` con el hash del
    dry-run revisado; aborta ante cualquier diferencia. Después de aplicar,
    verifica estado ONLINE, población 100 % y que el censo de chunks no cambió.

Uso:
    uv run python scripts-ops/recreate_chunk_text_index.py                  # dry-run
    uv run python scripts-ops/recreate_chunk_text_index.py --json           # plan + fingerprint
    uv run python scripts-ops/recreate_chunk_text_index.py \
        --analyzer simple                                                   # otro analyzer
    uv run python scripts-ops/recreate_chunk_text_index.py \
        --apply --approval /tmp/aprove_recreate.txt --expect-fingerprint <hex>
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter

INDEX_NAME = "chunk_text_index"
#: El analyzer vigente en producción (spec 09 §1, SHOW INDEXES) — el que T1
#: declara en el DDL de ``ensure_indexes``. Cambiarlo acá sin cambiarlo ahí (o
#: viceversa) rompe el pin cruzado de los tests.
DECLARED_ANALYZER = "standard-no-stop-words"
APPROVAL_WORD = "approve"
#: El analyzer cae dentro de texto DDL: solo identificadores Lucene conocidos.
_ANALYZER_PATTERN = re.compile(r"^[a-z0-9-]+$")

_INDEX_STATE_CYPHER = (
    "SHOW INDEXES YIELD name, type, state, populationPercent "
    "WHERE name = $name RETURN type, state, populationPercent"
)
_CHUNK_COUNT_CYPHER = "MATCH (c:Chunk) RETURN count(c) AS chunks"
_DROP_STATEMENT = f"DROP INDEX {INDEX_NAME} IF EXISTS"
_AWAIT_STATEMENT = "CALL db.awaitIndexes(300)"


def create_statement(analyzer: str) -> str:
    """The DDL that declares the analyzer — byte-identical to ``ensure_indexes``.

    Pinned by ``tests/unit/test_recreate_chunk_text_index.py`` against the
    literal ``tests/test_neo4j_query_adapter.py`` pins: the script and the
    adapter must issue the same statement or one of the two tests fails.
    """
    if not _ANALYZER_PATTERN.match(analyzer):
        raise ValueError(f"analyzer {analyzer!r} is not a plain [a-z0-9-] identifier")
    return (
        "CREATE FULLTEXT INDEX chunk_text_index IF NOT EXISTS FOR (n:Chunk) "
        "ON EACH [n.text] OPTIONS "
        f"{{indexConfig: {{'fulltext.analyzer': '{analyzer}'}}}}"
    )


def build_plan(*, analyzer: str, chunk_count: int, index_state: str) -> dict[str, Any]:
    """The reviewed unit of work: statements + census + current index state."""
    return {
        "index": INDEX_NAME,
        "analyzer": analyzer,
        "chunk_count": chunk_count,
        "current_index_state": index_state,
        "statements": [_DROP_STATEMENT, create_statement(analyzer), _AWAIT_STATEMENT],
    }


def plan_fingerprint(plan: dict[str, Any]) -> str:
    """sha256 determinista del plan canónico: reviewed == applied (T10-C contract).

    Cubre analyzer, censo y statements: si el grafo o el plan cambian entre el
    dry-run revisado y el ``--apply``, el hash difiere y la operación aborta
    antes de la primera escritura.
    """
    canonical_json = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--analyzer",
        default=DECLARED_ANALYZER,
        help=f"analyzer del nuevo índice (default: {DECLARED_ANALYZER})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="aplica DROP + CREATE (muta solo DDL; requiere --approval y --expect-fingerprint)",
    )
    parser.add_argument("--approval", type=Path, help="archivo con la palabra 'approve'")
    parser.add_argument(
        "--expect-fingerprint",
        default=None,
        help="sha256 del dry-run revisado (obligatorio con --apply; aborta si difiere)",
    )
    parser.add_argument("--json", action="store_true", help="salida JSON del plan")
    return parser.parse_args(argv)


def validate_apply_gates(args: argparse.Namespace, *, computed: str) -> None:
    """Gates de ``--apply``: aprobación humana + fingerprint del plan revisado.

    Mismo contrato que ``resolve_intra_ns._validate_*``: se **rechaza** ante la
    ausencia, no se advierte — sin el hash revisado la mutación no queda atada
    a la revisión, que es todo el propósito del fingerprint.
    """
    if args.approval is None or not args.approval.exists():
        sys.exit("APPLY abortado: falta --approval <archivo existente>")
    content = args.approval.read_text(encoding="utf-8").strip().lower()
    if APPROVAL_WORD not in content:
        sys.exit(f"APPLY abortado: {args.approval} debe contener la palabra '{APPROVAL_WORD}'")

    if args.expect_fingerprint is None:
        sys.exit(
            "APPLY abortado: falta --expect-fingerprint. Corre primero el dry-run, "
            f"revisa el plan y repite con --expect-fingerprint {computed}"
        )
    if args.expect_fingerprint != computed:
        sys.exit(
            f"FINGERPRINT abortado: reviewed {args.expect_fingerprint}, recomputed {computed} "
            "(el plan cambio desde la revision: analyzer, censo o estado del indice)"
        )


async def _read_state(adapter: Neo4jQueryAdapter) -> tuple[dict[str, Any], int]:
    """Current index row (or ``absent``) + chunk census. Read-only."""
    rows = await adapter.execute_read(_INDEX_STATE_CYPHER, {"name": INDEX_NAME})
    if rows:
        state = str(rows[0].get("state", "unknown"))
        population = rows[0].get("populationPercent")
        index_state = f"{state} (population={population}%)"
    else:
        index_state = "absent"
    count_rows = await adapter.execute_read(_CHUNK_COUNT_CYPHER)
    chunk_count = int(count_rows[0]["chunks"]) if count_rows else 0
    return {"index_state": index_state}, chunk_count


async def _apply(plan: dict[str, Any]) -> None:
    """DROP → CREATE → await. Solo DDL: la repoblación es automática (S5)."""
    settings = Settings.model_validate({})
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        async with driver.session() as session:
            for statement in plan["statements"]:
                print(f"  > {statement}")
                await session.run(statement)
    finally:
        await driver.close()


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    analyzer = str(args.analyzer)

    # Gate de aprobación ANTES de conectar: sin ella ni siquiera se abre sesión.
    if args.apply:
        # El fingerprint se valida recién con el censo vivo; la palabra
        # "approve" se valida acá para fallar rápido y sin conexión.
        if args.approval is None or not args.approval.exists():
            sys.exit("APPLY abortado: falta --approval <archivo existente>")
        if APPROVAL_WORD not in args.approval.read_text(encoding="utf-8").strip().lower():
            sys.exit(f"APPLY abortado: {args.approval} debe contener la palabra '{APPROVAL_WORD}'")

    settings = Settings.model_validate({})
    adapter = Neo4jQueryAdapter(settings)
    try:
        state, chunk_count = await _read_state(adapter)
        plan = build_plan(
            analyzer=analyzer,
            chunk_count=chunk_count,
            index_state=str(state["index_state"]),
        )
        fingerprint = plan_fingerprint(plan)
        if args.json:
            print(json.dumps({"plan": plan, "fingerprint": fingerprint}, indent=2))
        else:
            print(f"índice actual : {state['index_state']}")
            print(f"censo chunks  : {chunk_count}")
            print(f"analyzer nuevo: {analyzer}")
            print("plan (solo DDL; la repoblación del índice fulltext es automática):")
            for statement in plan["statements"]:
                print(f"  > {statement}")
            print(f"fingerprint   : {fingerprint}")
            print()
            print(
                "nota: SHOW INDEXES no expone el analyzer configurado de un índice existente; "
                "la única forma de garantizarlo es la recreación que este script aprueba."
            )
        if not args.apply:
            print("dry-run: ninguna escritura realizada.")
            return 0

        validate_apply_gates(args, computed=fingerprint)
        print("APLICANDO…")
        await _apply(plan)

        verify_state, verify_count = await _read_state(adapter)
        ok_state = str(verify_state["index_state"]).startswith("ONLINE (population=100.0%")
        ok_count = verify_count == chunk_count
        print(f"post-condiciones: estado={verify_state['index_state']} censo={verify_count}")
        if not (ok_state and ok_count):
            print(
                "POST-CONDICIONES FALLIDAS: "
                + ("estado no ONLINE/100%; " if not ok_state else "")
                + ("el censo de chunks cambio; " if not ok_count else "")
                + "revisar antes de continuar (el indice puede seguir poblando)."
            )
            return 1
        print("listo: índice recreado, ONLINE al 100 %, censo intacto.")
        return 0
    finally:
        await adapter.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
