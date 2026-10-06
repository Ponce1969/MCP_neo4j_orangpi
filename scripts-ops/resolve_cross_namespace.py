"""DESHABILITADO por diseño: ya no es posible aplicar merges cross-namespace directamente.

Historial: este script forjaba ``band=EXACT, cross_namespace=True`` y llamaba a
``ApplyMergeUseCase`` directamente, saltándose la cuarentena obligatoria
(``docs/spec/03-semantic-entity-resolution.md`` §2.4: la namespace es frontera
dura; los merges cross-namespace son SIEMPRE cuarentena/revisión, policy R6.2).
Así se aplicaron 302 merges sin auditar.

El guardia hoy vive en ``ApplyMergeUseCase`` (design D-A2, task T7): un grupo
cuyo canonical y duplicados cruzan de namespace exige un ``MergeApproval``
emitido por el flujo de cuarentena. Por lo tanto, aplicar merges
cross-namespace directamente ya no es posible por diseño (spec 03 §2.4).

Flujo correcto (AGENTS.md §7.2):
  1. enqueue — ``book-graph-rag resolve-entities`` crea el registro de cuarentena
  2. render  — ``book-graph-rag quarantine render <seq>`` muestra la hoja de decisión
  3. approve — aprobación explícita por ``--seq`` con backup → dry-run → archivo
     de aprobación → apply → audit

Este script ya no ejecuta ningún modo: termina con error antes de abrir
conexión con la base de datos. No muta nada.
"""

from __future__ import annotations

import sys

FAIL_MESSAGE = (
    "ERROR: resolve_cross_namespace.py está deshabilitado por diseño.\n"
    "Aplicar merges cross-namespace directamente ya no es posible (spec 03 §2.4):\n"
    "ApplyMergeUseCase exige un MergeApproval del flujo de cuarentena.\n"
    "Flujo correcto: enqueue → render → approve (por --seq, gate AGENTS.md §7.2).\n"
    "Nada fue modificado: este script no abre conexión con la base de datos."
)


def main() -> int:
    """Imprime el motivo del bloqueo y termina con error; no hace nada más."""
    print(FAIL_MESSAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
