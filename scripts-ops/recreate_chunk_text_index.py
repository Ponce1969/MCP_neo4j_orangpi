"""Recrear ``chunk_text_index`` con el analyzer declarado (operación DDL gateada).

Por qué existe (S5 de ``odd/tasks/retrieval-phase2-lexical.md``): el analyzer
``standard-no-stop-words`` era un copo de nieve — ningún código del repo lo
declaraba, así que una reconstrucción futura podía revertir silenciosamente
cualquier cambio. Ahora ``ensure_indexes`` lo declara en el DDL (T1) con **un
solo origen**: este script deriva su CREATE de ``chunk_text_index_ddl`` del
adapter, no guarda una copia. Y este script permite recrear el índice con
**cualquier** analyzer elegido siguiendo el patrón de operación gateada del
proyecto (dry-run por defecto → aprobación → fingerprint del plan revisado), el
mismo de ``scripts-ops/resolve_intra_ns.py``.

El índice fulltext se repuebla **solo** a partir de los nodos ``:Chunk`` al
crearse: DROP + CREATE no escribe datos ni vectores, solo DDL. La recreación es
idempotente: re-correrla produce siempre el mismo estado final (índice ONLINE al
100 % con el analyzer declarado, **verificado** después vía ``SHOW INDEXES``
``options.indexConfig['fulltext.analyzer']`` — incluye el agujero no-op:
``DROP … IF EXISTS`` + ``CREATE … IF NOT EXISTS`` no puede pasar la verificación
sin que el analyzer pedido esté realmente aplicado).

Ventana de indisponibilidad (declarada, inherente al plan):
  * El plan es DROP → CREATE: entre ambas sentencias ``chunk_text_index`` NO
    existe y la pata de chunks de retrieval queda sin fuente. El nombre está
    hardcodeado en el camino de lectura
    (``src/book_graph_rag/infrastructure/neo4j_query_adapter.py`` líneas 536 y
    554): no hay shadow-swap posible; la ventana es inherente al plan, este
    script la declara y no puede eliminarla.
  * Después de la DDL una corrida puede leer ``precision@k = 0.0000`` porque el
    índice está ausente o repoblando, **NO** porque el analyzer destruya la
    retrieval: el path de evaluación es silencioso ante el índice ausente
    (``src/book_graph_rag/infrastructure/neo4j_retrieval_adapter.py`` líneas
    92-95 usan ``return_exceptions=True`` y solo proceden ``if
    isinstance(chunks, list)``: la pata de chunks colapsa a ``[]`` y la
    precisión lee 0.0).
  * NO correr la evaluación (``scripts/eval_fulltext_recall.py``) con la
    ventana abierta.

Rollback (protocolo, ejecutable): el default de ``--analyzer`` es
``DECLARED_ANALYZER`` — el analyzer que ``ensure_indexes`` declara en el DDL —
así que restaurar el analyzer original es **este mismo script sin
``--analyzer``**, con la MISMA puerta (dry-run fresco → aprobación → fingerprint
nuevo):

    uv run python scripts-ops/recreate_chunk_text_index.py --apply \
        --approval <archivo con 'approve'> --expect-fingerprint <hex>

Verificación posterior al rollback: re-correr
``scripts/eval_fulltext_recall.py --shape-ab`` y leer la fila de control — con
el analyzer original restaurado debe volver a reproducir la línea base
commiteada. Trampa (encontrada por el verificador): **después de la DDL la fila
de control declara ``RUN VOID``**, porque corre contra el analyzer nuevo y ya no
reproduce la línea base commiteada; eso es esperado, NO drift de medición. La
comparación post-DDL es **por pregunta contra el registro pre-DDL**, nunca
contra la línea base commiteada.

Fases:
  * dry-run (default, READ-ONLY): imprime estado actual del índice (estado,
    población y analyzer observado), censo de chunks, el plan exacto (DROP →
    CREATE → ``db.awaitIndexes``), su fingerprint sha256, la ventana de
    indisponibilidad y el protocolo de rollback. No escribe nada.
  * ``--apply``: exige ``--approval <archivo>`` cuyo contenido incluya la
    palabra ``approve`` y ``--expect-fingerprint <hex>`` con el hash del
    dry-run revisado; aborta ante cualquier diferencia. Después de aplicar,
    verifica estado ONLINE, población 100 % (acepta ``100.0`` y ``100``), que
    el analyzer aplicado coincida con el leído de ``SHOW INDEXES`` y que el
    censo de chunks no cambió. Si un statement falla tras el DROP, imprime el
    estado observado (p. ej. ``absent``), dice que el índice debe recrearse y
    da la recuperación exacta: dry-run nuevo + fingerprint nuevo, porque el
    plan incluye ``current_index_state`` y ahora lee ``absent``.

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
import sys
from pathlib import Path
from typing import Any

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.neo4j_query_adapter import (
    DECLARED_CHUNK_ANALYZER,
    Neo4jQueryAdapter,
    chunk_text_index_ddl,
)

INDEX_NAME = "chunk_text_index"
#: El analyzer vigente en producción (spec 09 §1, SHOW INDEXES) — el que T1
#: declara en el DDL de ``ensure_indexes``. **Único origen**: la constante vive
#: en el adapter y este script la importa; cambiarlo acá o ahí en solitario
#: rompe los pins cruzados de los tests.
DECLARED_ANALYZER = DECLARED_CHUNK_ANALYZER
APPROVAL_WORD = "approve"

_INDEX_STATE_CYPHER = (
    "SHOW INDEXES YIELD name, type, state, populationPercent, options "
    "WHERE name = $name RETURN type, state, populationPercent, options"
)
_CHUNK_COUNT_CYPHER = "MATCH (c:Chunk) RETURN count(c) AS chunks"
_DROP_STATEMENT = f"DROP INDEX {INDEX_NAME} IF EXISTS"
_AWAIT_STATEMENT = "CALL db.awaitIndexes(300)"

#: Ventana de indisponibilidad: la declaración obligatoria que el dry-run y el
#: ``--apply`` imprimen (defecto 1 de la verificación independiente).
_WINDOW_WARNING = (
    "VENTANA DE INDISPONIBILIDAD (inherente al plan, no negociable):\n"
    "  * DROP antes de CREATE: entre ambas sentencias chunk_text_index NO existe y la\n"
    "    pata de chunks de retrieval queda sin fuente. El nombre está hardcodeado en el\n"
    "    camino de lectura (src/book_graph_rag/infrastructure/neo4j_query_adapter.py:536\n"
    "    y :554): no hay shadow-swap posible; la ventana es inherente al plan.\n"
    "  * Después de la DDL una corrida puede leer precision@k = 0.0000 porque el índice\n"
    "    está ausente o repoblando, NO porque el analyzer destruya la retrieval: el path\n"
    "    de evaluación es silencioso ante el índice ausente\n"
    "    (src/book_graph_rag/infrastructure/neo4j_retrieval_adapter.py:92-95 usa\n"
    "    return_exceptions=True y solo procede si isinstance(chunks, list): la pata de\n"
    "    chunks colapsa a [] y la precisión lee 0.0).\n"
    "  * NO correr la evaluación (scripts/eval_fulltext_recall.py) con la ventana abierta."
)

#: El rollback como protocolo (defecto 4 de la verificación independiente):
#: ejecutable porque el default de ``--analyzer`` reproduce DECLARED_ANALYZER.
_ROLLBACK_PROTOCOL = (
    "rollback = recrear con el analyzer original: este mismo script SIN --analyzer "
    f"(default = {DECLARED_ANALYZER}, el analyzer que declara ensure_indexes) con la MISMA "
    "puerta (dry-run fresco -> aprobación -> fingerprint nuevo). Verificación posterior: "
    "re-correr scripts/eval_fulltext_recall.py --shape-ab y leer la fila de control. "
    "Trampa esperada: tras la DDL la fila declara RUN VOID (corre contra el analyzer nuevo); "
    "la comparación post-DDL es por pregunta contra el registro pre-DDL, nunca contra la "
    "línea base commiteada."
)


def create_statement(analyzer: str) -> str:
    """The DDL of the plan — derived from the adapter, never copied (S5a).

    Delegates to ``chunk_text_index_ddl``, the single implementation shared
    with ``ensure_indexes`` (the analyzer identifier guard lives there too).
    Pinned by the delegation and byte tests in
    ``tests/unit/test_recreate_chunk_text_index.py`` plus the executed-bytes
    cross-pin in ``tests/test_neo4j_query_adapter.py``.
    """
    return chunk_text_index_ddl(analyzer)


def build_plan(
    *,
    analyzer: str,
    chunk_count: int,
    index_state: str,
    neo4j_uri: str,
) -> dict[str, Any]:
    """The reviewed unit of work: statements + census + current state + target graph.

    ``neo4j_uri`` rides inside the plan so a fingerprint minted against dev can
    never authorize an apply against production (or the reverse): changing the
    target graph changes the hash.
    """
    return {
        "index": INDEX_NAME,
        "analyzer": analyzer,
        "chunk_count": chunk_count,
        "current_index_state": index_state,
        "neo4j_uri": neo4j_uri,
        "statements": [_DROP_STATEMENT, create_statement(analyzer), _AWAIT_STATEMENT],
    }


def plan_fingerprint(plan: dict[str, Any]) -> str:
    """sha256 determinista del plan canónico: reviewed == applied (T10-C contract).

    Cubre analyzer, censo, estado actual, neo4j_uri y statements: si el grafo,
    el target o el plan cambian entre el dry-run revisado y el ``--apply``, el
    hash difiere y la operación aborta antes de la primera escritura.
    """
    canonical_json = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _population_at_100(value: Any) -> bool:
    """True only for Neo4j's two 100 % spellings (``100.0`` and ``100``).

    The previous ``startswith("ONLINE (population=100.0%")`` false-alarmed on
    the integer spelling; a non-numeric population never passes.
    """
    try:
        return float(value) == 100.0
    except (TypeError, ValueError):
        return False


def _analyzer_from_options(options: Any) -> str | None:
    """Read ``options.indexConfig['fulltext.analyzer']`` from a ``SHOW INDEXES`` row.

    Operator-verified on Neo4j 5.23: the analyzer **is** exposed there. (The
    old note claiming SHOW INDEXES could not show it was wrong and is deleted;
    the post-condition now proves the analyzer instead of implying it.)
    """
    if not isinstance(options, dict):
        return None
    index_config = options.get("indexConfig")
    if not isinstance(index_config, dict):
        return None
    analyzer = index_config.get("fulltext.analyzer")
    return str(analyzer) if analyzer is not None else None


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
            "(el plan cambio desde la revision: analyzer, censo, estado del indice "
            "o neo4j_uri)"
        )


async def _read_state(adapter: Neo4jQueryAdapter) -> tuple[dict[str, Any], int]:
    """Current index row (or ``absent``) + chunk census. Read-only.

    The row is structured (``state``/``population``/``analyzer``) for the
    post-conditions, plus the display string that feeds ``current_index_state``
    in the reviewed plan.
    """
    rows = await adapter.execute_read(_INDEX_STATE_CYPHER, {"name": INDEX_NAME})
    if rows:
        state = str(rows[0].get("state", "unknown"))
        population = rows[0].get("populationPercent")
        analyzer = _analyzer_from_options(rows[0].get("options"))
        info: dict[str, Any] = {
            "index_state": f"{state} (population={population}%, analyzer={analyzer})",
            "state": state,
            "population": population,
            "analyzer": analyzer,
        }
    else:
        info = {"index_state": "absent", "state": None, "population": None, "analyzer": None}
    count_rows = await adapter.execute_read(_CHUNK_COUNT_CYPHER)
    chunk_count = int(count_rows[0]["chunks"]) if count_rows else 0
    return info, chunk_count


async def _apply(settings: Settings, plan: dict[str, Any]) -> None:
    """DROP → CREATE → await. Solo DDL: la repoblación es automática (S5).

    Los errores **no** se tragan acá: ``main`` imprime el estado observado, la
    recuperación exacta (dry-run nuevo → fingerprint nuevo) y devuelve 2.
    """
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


async def _report_apply_failure(adapter: Neo4jQueryAdapter, exc: Exception) -> int:
    """Observed state + exact recovery after a failed statement. Never a raw traceback."""
    print(f"APPLY FALLÓ: {type(exc).__name__}: {exc}")
    observed = "desconocido (la relectura del estado también falló)"
    try:
        failed_state, _ = await _read_state(adapter)
        observed = str(failed_state["index_state"])
    except Exception as read_exc:
        observed = f"{observed}: {type(read_exc).__name__}: {read_exc}"
    print(f"estado observado tras el fallo: {observed}")
    print(
        "La recreación quedó INCOMPLETA: el DROP puede haberse aplicado sin que el CREATE "
        "llegara a ejecutarse."
    )
    if observed.startswith("absent"):
        print(
            "ÍNDICE AUSENTE: chunk_text_index no existe; search_chunks fallará en duro hasta "
            "recrearlo. Recuperación con el gate completo (no hay atajo):\n"
            "  1. dry-run NUEVO: el plan incluye current_index_state y ahora lee 'absent', "
            "así que el fingerprint del plan revisado ya no valida;\n"
            "  2. revisá el plan nuevo y guardá su fingerprint;\n"
            "  3. repetí --apply con --approval (palabra 'approve') y "
            "--expect-fingerprint <hex del dry-run nuevo>."
        )
    else:
        print(
            "El estado observado no es 'absent': corré el dry-run de verificación (el plan y "
            "su fingerprint también cambiaron) antes de reintentar."
        )
    return 2


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
            neo4j_uri=settings.neo4j_uri,
        )
        fingerprint = plan_fingerprint(plan)
        if args.json:
            print(
                json.dumps(
                    {
                        "plan": plan,
                        "fingerprint": fingerprint,
                        "unavailability_window": _WINDOW_WARNING,
                        "rollback_protocol": _ROLLBACK_PROTOCOL,
                    },
                    indent=2,
                )
            )
        else:
            print(f"índice actual : {state['index_state']}")
            print(f"censo chunks  : {chunk_count}")
            print(f"analyzer nuevo: {analyzer}")
            print("plan (solo DDL; la repoblación del índice fulltext es automática):")
            for statement in plan["statements"]:
                print(f"  > {statement}")
            print(f"fingerprint   : {fingerprint}")
            print()
            print(_WINDOW_WARNING)
            print()
            print(f"rollback: {_ROLLBACK_PROTOCOL}")
        if not args.apply:
            print("dry-run: ninguna escritura realizada.")
            return 0

        validate_apply_gates(args, computed=fingerprint)
        print("APLICANDO…")
        try:
            await _apply(settings, plan)
        except Exception as exc:
            return await _report_apply_failure(adapter, exc)

        verify_state, verify_count = await _read_state(adapter)
        ok_state = str(verify_state.get("state")) == "ONLINE" and _population_at_100(
            verify_state.get("population")
        )
        ok_analyzer = verify_state.get("analyzer") == analyzer
        ok_count = verify_count == chunk_count
        print(f"post-condiciones: estado={verify_state['index_state']} censo={verify_count}")
        if not (ok_state and ok_analyzer and ok_count):
            problems: list[str] = []
            if not ok_state:
                problems.append(f"estado no ONLINE/100% (observado: {verify_state['index_state']})")
            if not ok_analyzer:
                problems.append(
                    f"analyzer observado {verify_state.get('analyzer')!r} != {analyzer!r} "
                    "(leído de SHOW INDEXES options.indexConfig['fulltext.analyzer'])"
                )
            if not ok_count:
                problems.append("el censo de chunks cambió")
            print(
                "POST-CONDICIONES FALLIDAS: "
                + "; ".join(problems)
                + ". Revisar antes de continuar (el índice puede seguir poblando o sin crear)."
            )
            return 1
        print(
            f"listo: índice recreado con analyzer '{analyzer}' verificado vía SHOW INDEXES "
            "(options.indexConfig['fulltext.analyzer']), ONLINE al 100 %, censo intacto."
        )
        return 0
    finally:
        await adapter.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
