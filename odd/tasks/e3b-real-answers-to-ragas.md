# E3b — el scorer secundario de RAGAS debe auditar respuestas reales, no un dataset fantasma

**Dueño**: parent. **Rama**: `feat/e3b-real-answers-to-ragas`. **Risk**: item 3 — el puerto `RAGASRunnerPort` lo consumen cinco módulos y seis fakes ⇒ verificación independiente obligatoria.

Hoy el runner recibe `(question_id, "", ())` por pregunta, y el script **re-responde** cada pregunta desde el grafo con una llamada LLM nueva. Consecuencia: **los números de RAGAS que venimos registrando no miden las respuestas del pipeline** — el `0.7143` salió de preguntas re-respondidas.

## Specs

**S1.** "transformar el runner secundario de RAGAS en un auditor pasivo y determinista sobre las respuestas reales de la capa de generación".

**S2.** Contrato del JSONL de entrada (en `tmp_path`): `question` = "Texto real de la consulta del benchmark (no IDs/slugs)"; `contexts` = "Chunks reales recuperados del grafo que alimentaron la síntesis"; `answer` = "La respuesta textual generada por el LLM en la pasada evaluada (no vacía, no sintética)"; `ground_truth` = "Referencia canónica si existe".

**S3.** "Invariante E3a: Si falta `answer` o `contexts`, se mantiene `skip-when-empty` sin warning ruidoso."

**S4.** "Sin re-generación en el runner: Ragas opera en modo scoring puro, evaluando la respuesta que el pipeline ya generó."

**S5.** Pines exigidos: "Pin que verifique que el JSONL intermedio contenga textualmente la `answer` producida." · "Pin que verifique que la métrica compute sobre esa respuesta." · "Preservar la alarma ruidosa con redacción R5 ante fallos genuinos de subproceso."

**S6.** Decisión del operador (2026-10-18): la referencia "debe viajar en el payload JSONL para trazabilidad humana y auditoría, pero **NO** alimentar el scorer"; se mantiene `LLMContextPrecisionWithoutReference` "bajo el pin estricto (Pin #3): preservar la semántica de la métrica y la calibración histórica del gate en 0.80".

**S7.** Aprobado: `RagasSample` en `domain/`; que "`mypy --strict` sea el compilador que obligue a adaptar los 5 consumidores y los fakes".

**S8.** Aprobado: "Erradicación completa de la re-respuesta en el script"; si faltan contextos o respuesta, entra "la regla de skip documentada en rationale".

**S9.** Pin adicional aprobado: "la invariante de fallo fuerte ante dataset totalmente vacío" — un skip total no puede parecer una medición.

**S10.** Se conservan **verbatim las aserciones** de: los tests de redacción R5 del **runner**, los pines de E3a en `tests/unit/test_evaluate_retrieval_layer.py`, y los tests que leen `AGENTS.md`. Para el test de redacción del **script** (`tests/unit/test_run_ragas_evaluation_script.py`), cuya falla se inyecta monkeypatcheando la función de re-respuesta que T4 elimina, rige la resolución de **L4**: se adapta el *target del monkeypatch* y el fixture compartido, y **cada aserción de redacción queda byte-idéntica** (ausencia del marcador, tipo y longitud redactados, fingerprint con clave, exit 0, pasaje de la pregunta). La propiedad que S5 exige —un fallo genuino por fila se persiste ruidoso y **redactado**— se conserva: cambia su disparador, no su garantía.

## Tasks

- **T1** [S7] `RagasSample` en `domain/evaluation_models.py` junto a `PairwiseJudgment`; `ports/ragas_runner_port.py` pasa de la tupla a `Sequence[RagasSample]`. → commit 1
- **T2** [S1,S2] `_PerQuestionResult` retiene `question`/`answer`/`contexts` (ya son variables locales en `_evaluate_one`: **cero consultas nuevas al grafo**) y el runner los escribe en el JSONL. → commit 1
- **T3** [S3,S9] Espejo del skip de E3a en la capa de generation (vacío ⇒ `None` + nota al `rationale`, nunca a `warnings`) + el filtro del script pasa de `is not None` a no-vacío, conservando el fallo fuerte ante dataset totalmente vacío. → commit 1
- **T4** [S4,S8] Fuera la re-respuesta: `_eval_all` y `_proxy_compose`; el script puntúa lo que recibe y no fabrica nada. → commit 1
- **T5** [S5] Los pins: fidelidad textual del `answer` en el JSONL · la métrica computa sobre esa respuesta · la métrica sigue siendo sin-referencia · E3a sin llamadas ni warning ruidoso · todos-vacíos ruidoso · R5 intacto. → commit 1
- **T6** [S6] La referencia viaja al JSONL como evidencia y **no** alimenta la métrica. → commit 1
- **T7** Consumidores mecánicos: capa de retrieval, `main.py`, `diagnose_generation_layer.py`, stub, fakes. → commit 1
- **T8** [S10] Docs: `odd/backlog.md` (E3 a cerrado), `docs/spec/08` (schema del JSONL como evidencia), spec 07 (el caveat del `0.7143`). → commit 2
- **T9** Verificación independiente por el Risk item 3. → **parent**

## Log

**L1** (2026-10-18, pedido del operador, verbatim): "El objetivo de diseño está claro: transformar el runner secundario de RAGAS en un auditor pasivo y determinista sobre las respuestas reales de la capa de generación. Lineamientos para la feature: 1. Contrato del JSONL de entrada (en tmp_path): `question`: Texto real de la consulta del benchmark (no IDs/slugs). `contexts`: Chunks reales recuperados del grafo que alimentaron la síntesis. `answer`: La respuesta textual generada por el LLM en la pasada evaluada (no vacía, no sintética). `ground_truth`: Referencia canónica si existe. *Invariante E3a:* Si falta `answer` o `contexts`, se mantiene `skip-when-empty` sin warning ruidoso. 2. Sin re-generación en el runner: Ragas opera en modo scoring puro, evaluando la respuesta que el pipeline ya generó. 3. Pines de regresión: Pin que verifique que el JSONL intermedio contenga textualmente la `answer` producida. Pin que verifique que la métrica compute sobre esa respuesta. Preservar la alarma ruidosa con redacción R5 ante fallos genuinos de subproceso. Siguiendo tu criterio: leé primero la spec y el código actual de `evaluate_generation_layer_use_case.py` y traé tu propuesta técnica y los puntos de intervención antes de escribir código."

**L2** (2026-10-18, aprobación, verbatim): "**Persistir únicamente como evidencia:** La referencia debe viajar en el payload JSONL para trazabilidad humana y auditoría, pero **NO** alimentar el scorer. Mantenemos `LLMContextPrecisionWithoutReference` bajo el pin estricto (Pin #3): preservar la semántica de la métrica y la calibración histórica del gate en 0.80." + "**Scoring pasivo y determinista:** Erradicación completa de la re-respuesta en el script" + "Procedé con la creación de la rama para U1, la implementación y la verificación independiente (`gentle-ai-verify`)."

**L3** (evidencia del mapeo previo, verificado por el explorer con `path:line`):
- El fantasma nace en `application/evaluate_generation_layer_use_case.py:344` (`tuple((r.question_id, "", ()) for r in per_question_results)`).
- La respuesta y los contextos reales existen **solo como variables locales** en `_evaluate_one` (`:280-292`); `_PerQuestionResult` (`:427`) los descarta.
- El script re-responde en `scripts/run_ragas_evaluation.py:472-485` (`_eval_all`) con `_proxy_compose:113`, y sobrescribe el `answer`/`contexts` recibidos.
- El filtro `metric_rows = [... if r.get("answer") is not None]` (`:343`) **deja pasar la cadena vacía**.
- Los contextos (E2) son 8 resúmenes por pregunta, no los 659 KiB de antes ⇒ la retención de `answer`+`contexts` por pregunta es acotada y aceptable.
- `ground_truth`: el dataset curado trae `reference_answer` (`:240`) usado **solo** por el juez pairwise; el script elige a propósito la métrica **sin referencia** — alimentarla cambiaría qué se mide e invalidaría la calibración de 0.80.
- **Ningún test fija el payload fantasma** (el `_FakeRagasPort` no registra llamadas) ⇒ no hay contrato defectuoso que respetar.
- Los pines de E3a viven en la capa de **retrieval** (`evaluate_retrieval_layer_use_case.py:190-235` + `test_evaluate_retrieval_layer.py:469-498`); la de generation **no tiene skip** ⇒ hay que espejarlo.
- R5: el seam de redacción vive en `infrastructure/subprocess_ragas_runner.py:203-211`.

**L4** (2026-10-18, hallazgo del worker + resolución del parent): **conflicto real S8 vs S10.** `tests/unit/test_run_ragas_evaluation_script.py:221-281` inyecta su falla marcadora monkeypatcheando `_evaluate_local` (`:241`), la función de re-respuesta que T4 elimina; sin ella la fila pasa a ser un *skip* (S8) y la aserción `:264` (`assert "_ScriptProviderFailureError" in persisted`) no puede sostenerse. Además el fixture compartido `patched_module` (`:108-111`) monkeypatchea atributos de módulo (`Neo4jQueryAdapter`/`Neo4jCommunityAdapter`/`LLMAdapter`/`GlobalQueryUseCase`) que T4 también elimina, así que rompe los cuatro tests que lo usan.
**Resuelto con la Opción A**: erradicación completa de la re-respuesta (S8 intacto) + un **seam de preparación/validación por fila envuelto en redacción**, que preserva el contrato de artefactos actual (`evaluation_results.jsonl` con filas de error, `after.failed_questions`) + adaptación mínima del test (target del monkeypatch y fixture), con **todas las aserciones byte-idénticas**. Se rechaza la Opción B (resucitar la re-respuesta para filas sin respuesta: viola S8 y reabre el defecto que E3b cierra) y la Opción C (borrar el test: debilita una aserción, prohibido).
Nota de alcance: la redacción de S5 no era "los errores de re-respuesta se redactan", era "un fallo genuino por fila se persiste ruidoso y redactado"; al desaparecer la re-respuesta el disparador cambia —fila malformada, tipo inesperado, fallo del scoring— pero la garantía no.
Menor, sin decisión: el test R5 del runner pasa un payload tupla que `mypy --strict` nombrará tras el cambio de puerto ⇒ S7 lo vuelve parte del cambio (adaptación del literal, aserciones verbatim).

**L5** (2026-10-18, implementación, T1-T8 completados):
- **T1/T2/T6**: `RagasSample` (`domain/evaluation_models.py`, junto a `PairwiseJudgment`); el puerto pasa a `Sequence[RagasSample]`; `_PerQuestionResult` retiene `question`/`answer`/`contexts`/`ground_truth` (cero consultas nuevas al grafo) y `_write_generation_jsonl` escribe la pregunta real (nunca el id), las respuestas y el `ground_truth` como evidencia.
- **T3**: espejo de E3a en la capa de generation (`_run_ragas` devuelve `(metrics, skip_note)`: vacío ⇒ sin llamada al scorer, sin warning, nota en `rationale`, `ragas_secondary=None`); el filtro del script pasa a no-vacío (`answer` AND `contexts`) y un dataset totalmente vacío lanza `ClickException` antes de cualquier import de ragas.
- **T4**: fuera la re-respuesta completa en `scripts/run_ragas_evaluation.py` (`_eval_all`, `_proxy_compose`, `_evaluate_global`, `_evaluate_local`, composition root, `asyncio`); el script valida cada fila con `_prepare_row` envuelto en `format_redacted_error` (R5: fila malformada ⇒ fila de error ruidosa y redactada, contrato de artefactos intacto) y puntúa lo que recibe, sin fabricar nada.
- **T5 (los seis pins, RED observado antes de cada fix)**: (1) fidelidad textual del `answer` en el JSONL — `test_the_input_jsonl_carries_the_real_answer_textually` (RED: `assert 'gen-001' == 'What is the ReAct pattern?'`); (2) la métrica computa sobre esa respuesta — `test_ragas_receives_the_real_answer_it_must_score` (RED: `assert '' == 'what is MCP?'`, el payload fantasma); (3) la métrica sigue sin-referencia — `test_the_scorer_stays_reference_free_even_though_ground_truth_travels`; (4) E3a sin llamadas ni warning ruidoso — `test_empty_answers_skip_the_scorer_without_a_loud_warning` (RED: el scorer fue invocado con el fantasma); (5) todos-vacíos ruidoso — `test_an_all_skipped_dataset_fails_loudly_instead_of_looking_clean` (RED: el código viejo construía clientes y no lanzaba); (6) R5 intacto — los tests de redacción del runner preservados byte a byte y el del script adaptado solo en su target de inyección (`_prepare_row`), con todas las aserciones byte-idénticas (RED del adaptado: `AttributeError: module has no attribute '_prepare_row'`).
- **T7**: consumidores mecánicos (capa de retrieval, `main.py` sin cambios, `diagnose_generation_layer.py`, stub) y los seis fakes de test adaptados por la enumeración de `mypy --strict`.
- **T8**: `odd/backlog.md` (E3 cerrado en sus dos mitades), `docs/spec/08` (schema del JSONL como evidencia de lo medido), `docs/spec/07` (el caveat del 0.7143 queda con su resolución).
- **Verificación**: `uv run ruff check .` ✔ · `uv run ruff format .` (sin cambios pendientes) ✔ · `uv run mypy .` ✔ (474 archivos, sin errores) · `uv run --no-sync pytest tests/ -q --ignore=tests/integration --deselect tests/property/interruption_fuzz_test.py` ⇒ **2206 passed, 3 skipped, 1 error** — el único error es ambiental (`tests/property/backfill_dry_run_subset_test.py` necesita el daemon Docker, que no está corriendo en esta máquina) y ajeno a esta feature.
- **Commits**: `bfc05ef` (T1-T7; 15 archivos, +434/−264, pre-commit verde con ruff, mypy, validación de arquitectura) y el commit de docs que contiene esta línea (T8). El hash del segundo se registra en el PR: escribirlo acá obligaría a un commit que se referencie a sí mismo. El push y el PR son operación del padre.
