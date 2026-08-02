# Fase 07.x — GraphRAG Production Hardening

> **Estado:** SPEC — pendiente de ciclo SDD (explore → propose → design → tasks → apply → verify → archive)
> **Auditoría base:** [graphrag-architect skill] — 2026-08-01
> **Sistema auditado:** `agentic-patterns` MCP Server (Neo4j 5.23 + FastMCP SSE)

## Contexto

El MCP `agentic-patterns` expone un knowledge graph con 2479 entidades, 4915
relaciones y 1074 chunks del libro *"Agentic Architectural Patterns"* via 6
tools MCP (find_entity, traverse_relationships, search_chunks, list_entities,
count_entities, search_rag).

Una auditoría con la lente **GraphRAG Architect** identificó 5 gaps que separan
el sistema actual de un GraphRAG production-grade según el pipeline estándar
(chunk → extract → import → communities → query → evaluate → monitor).

**Este spec NO reemplaza el roadmap híbrido actual** (07 → 07.1 → 07.2 → fork
A/B). Las mejoras son ortogonales — mejoran la calidad del sistema existente sin
cambiar la dirección del roadmap.

---

## Hallazgos de la auditoría (priorizados)

| # | Finding | Severidad | Impacto |
|---|---|---|---|
| F1 | Sin community summaries (Leiden + C0-C3) | **CRITICAL** | Preguntas globales imposibles |
| F2 | Sin self-reflection en extracción | **HIGH** | ~15-20% recall perdido en entidades |
| F3 | Sin dataset de evaluación cuantitativa | **HIGH** | Imposible medir mejoras/regresiones |
| F4 | Sin Text2Cypher fallback | **MEDIUM** | Queries complejas sin respuesta |
| F5 | Sin monitoreo de drift (TruLens Triad) | **MEDIUM** | Degradación silenciosa con el uso |

---

## Scope

### IN scope

| Mejora | Qué cambia | Archivos afectados |
|---|---|---|
| **REQ-GR.1** Community summaries | Leiden clustering + summaries C0-C3 en Neo4j | Nuevo adapter `community_adapter.py`, script `run_communities.py`, domain models |
| **REQ-GR.2** Self-reflection extraction | Extracción en 2-3 pasos ("¿qué me faltó?") | Modificar `llm_adapter.py` |
| **REQ-GR.3** Evaluation dataset + RAGAS | Personas+Tareas+Preguntas + métricas cuantitativas | Nuevo script `evaluate_rag.py`, nuevo dir `tests/evaluation/` |
| **REQ-GR.4** Text2Cypher fallback | Tool MCP que genera Cypher dinámico con EXPLAIN+self-heal | Modificar `mcp_server_adapter.py`, nuevo `text2cypher.py` |
| **REQ-GR.5** Monitoring con TruLens Triad | Sampleo de queries → scoring → drift detection | Nuevo `monitor.py`, dashboard o reportes |

### OUT of scope

- Cambiar el roadmap híbrido (07.2 observación → fork sigue vigente)
- Re-indexar el libro desde cero (las mejoras se aplican sobre el grafo existente)
- Vector embeddings (Fase 06b — gateado por evidencia de 07.2)
- Reemplazar Neo4j o FastMCP

---

## REQ-GR.1 — Community Summaries (CRITICAL)

### Problema

Hoy una pregunta global como *"¿de qué trata el capítulo 9?"* solo puede
responderse con `search_chunks` que devuelve fragmentos sueltos. No hay una
visión panorámica estructurada del grafo.

### Requerimiento

Implementar **detección de comunidades jerárquicas** (Leiden) sobre el grafo
existente y generar **summaries por nivel** (C0 = raíz global, C3 = hojas
detalladas) que permitan responder preguntas globales con map-reduce.

### Especificaciones técnicas

- **Algoritmo:** `graspologic.partition.hierarchical_leiden(graph, max_cluster_size=...)`
- **Niveles:** C0 (root, 1 summary) → C3 (leaf, ~50 summaries)
- **Nodos:** `CommunitySummary {level: int, summary: str, entity_ids: list[str]}`
- **Persistencia:** Neo4j (mismos constraints que entidades existentes)
- **Map-reduce para queries globales:**
  1. Seleccionar nivel C0-C3 según scope de la pregunta
  2. Map: partir summaries en batches → respuestas parciales con score 0-100
  3. Reduce: ordenar por score, llenar contexto, generar respuesta global
- **Cost constraint:** C0 ~1 llamada LLM, C3 ~50 llamadas. Usar modelo barato para summaries.

### Escenarios

**Escenario GR.1.1 — Comunidad global (C0):**
- DADO el grafo con 2479 entidades y 4915 relaciones
- CUANDO se ejecuta Leiden clustering y se genera summary C0
- ENTONCES se produce 1 nodo `CommunitySummary` con `level=0` y un resumen de 500-1000 tokens que describe los temas principales del libro

**Escenario GR.1.2 — Pregunta global respondida con map-reduce (C1):**
- DADO summaries C1 existentes (~10 summaries de nivel intermedio)
- CUANDO un agente pregunta "¿qué patrones de coordinación existen en el libro?"
- ENTONCES el sistema hace map-reduce sobre los summaries C1 y devuelve una respuesta estructurada que cubre los patrones de coordinación con sus descripciones

**Escenario GR.1.3 — Pregunta global con nivel detallado (C3):**
- DADO summaries C3 existentes (~50 summaries de nivel hoja)
- CUANDO un agente pregunta "¿cómo se relacionan LangGraph, CrewAI y AutoGen?"
- ENTONCES el sistema selecciona summaries C3 relevantes y produce una respuesta que describe las relaciones entre los frameworks

**Escenario GR.1.4 — Leiden sin re-indexar:**
- DADO el grafo existente (no se re-indexa el libro)
- CUANDO se ejecuta el script `run_communities.py`
- ENTONCES las comunidades se detectan sobre las entidades ya existentes sin modificar los chunks originales

### ACs

- **AC-GR.1.1:** Script `run_communities.py` ejecuta Leiden y persiste summaries en Neo4j
- **AC-GR.1.2:** Nueva tool MCP `ask_global` o extensión de `search_rag` que usa summaries para preguntas globales
- **AC-GR.1.3:** Map-reduce devuelve respuestas con fuente citada (`[Data: CommunitySummary(id)]`)
- **AC-GR.1.4:** El grafo existente no se modifica (solo se agregan nodos `CommunitySummary`)
- **AC-GR.1.5:** Gates pasan (ruff, mypy src, validate_architecture)

---

## REQ-GR.2 — Self-Reflection Extraction (HIGH)

### Problema

La extracción actual de entidades es **single-pass**: el LLM procesa cada chunk
una vez. Estudios muestran que un loop de self-reflection ("¿qué me faltó?")
mejora recall entre 15-20%.

### Requerimiento

Modificar `llm_adapter.py` para que la extracción de entidades use un loop de
**2-3 iteraciones** de self-reflection antes de devolver el resultado final.

### Especificaciones técnicas

- **Pass 1:** Extraer entidades + relaciones con `description` y `strength` (0-10) como ya se hace
- **Self-reflection:** "Here are the entities extracted so far: {entities}. What entities or relationships might have been missed? Answer Yes/No, then list any missing ones."
- **Pass 2 (si applica):** Extraer las entidades faltantes identificadas en la reflexión
- **Merge:** Unificar descripciones de entidades duplicadas con una llamada LLM extra
- **Max 3 iteraciones** (Pass 1 + 2 reflection loops máximo)
- **Early exit:** Si la reflexión responde "No, nothing missed", salir después del Pass 1
- **Pydantic models:** `frozen=True, strict=True`, `description` en cada `Field`

### Escenarios

**Escenario GR.2.1 — Self-reflection encuentra entidades faltantes:**
- DADO un chunk de 600 tokens sobre "Agent Orchestration patterns"
- CUANDO el Pass 1 extrae 5 entidades
- Y la reflexión identifica 2 entidades adicionales ("Fallback Pattern", "Supervisor Agent")
- ENTONCES el Pass 2 extrae las 2 entidades faltantes y se mergean en el resultado final (7 entidades total)

**Escenario GR.2.2 — Early exit cuando no falta nada:**
- DADO un chunk simple con pocas entidades
- CUANDO el Pass 1 extrae todas las entidades
- Y la reflexión responde "No, nothing missed"
- ENTONCES no se ejecuta Pass 2 y se devuelve el resultado del Pass 1

**Escenario GR.2.3 — Merge de duplicados:**
- DADO el Pass 1 extrae "Agent Orchestration" con descripción corta
- Y el Pass 2 extrae "Agent Orchestration" con descripción complementaria
- ENTONCES una llamada LLM mergea las descripciones en un solo resultado sin duplicados

### ACs

- **AC-GR.2.1:** El loop de self-reflection existe en `llm_adapter.py` con min 2, max 3 iteraciones
- **AC-GR.2.2:** Early exit funciona cuando la reflexión responde "No"
- **AC-GR.2.3:** Merge de entidades duplicadas produce descripciones unificadas
- **AC-GR.2.4:** Los modelos Pydantic usan `frozen=True, strict=True` con `description` en cada `Field`
- **AC-GR.2.5:** Tests unitarios cubren los 3 escenarios (missing, early exit, merge)
- **AC-GR.2.6:** Gates pasan

---

## REQ-GR.3 — Evaluation Dataset & RAGAS (HIGH)

### Problema

No hay forma de medir si el RAG es bueno o malo. Sin métricas cuantitativas, cada
cambio es un tiro al aire — no sabés si mejoraste o empeoraste.

### Requerimiento

Crear un **dataset de evaluación** (Personas+Tareas+Preguntas) y ejecutar
**RAGAS** (Faithfulness, AnswerRelevancy, ContextPrecision) para establecer una
línea de base.

### Especificaciones técnicas

- **Dataset generation:**
  1. 5 personas que usarían el sistema (ej: Arquitecto de Agentes, Developer, Tech Lead, Investigador, Product Manager)
  2. 5 tareas por persona
  3. 5 preguntas globales por tarea → 125 preguntas total
  4. Ejecutar contra el MCP actual para generar `(question, answer, contexts)` tuples
- **RAGAS evaluation:**
  ```python
  from ragas.metrics.collections import Faithfulness, AnswerRelevancy, ContextPrecision
  scorer = Faithfulness(llm=llm)
  result = await scorer.ascore(user_input=q, response=a, retrieved_contexts=c)
  ```
- **Métricas separadas:** Retrieval (ContextPrecision) vs Generation (Faithfulness, AnswerRelevancy)
- **Reporte:** tabla con métricas por persona/tarea + heatmap de gaps
- **Línea de base:** Guardar resultados como baseline para comparar después de GR.1 y GR.2

### Escenarios

**Escenario GR.3.1 — Dataset generation:**
- DADO el MCP agentic-patterns corriendo
- CUANDO se ejecuta el dataset generator con 5 personas × 5 tareas × 5 preguntas
- ENTONCES se produce un archivo `evaluation_dataset.jsonl` con 125 tuples `(persona, tarea, pregunta, respuesta, contexts)`

**Escenario GR.3.2 — RAGAS baseline:**
- DADO el dataset de 125 preguntas
- CUANDO se ejecuta `evaluate_rag.py --baseline`
- ENTONCES se produce un reporte con Faithfulness, AnswerRelevancy y ContextPrecision promediados y se guarda como `baseline_results.json`

**Escenario GR.3.3 — Comparación antes/después:**
- DADO baseline_results.json de la versión actual
- CUANDO se implementa GR.1 (community summaries) y se re-evalúa
- ENTONCES se produce un diff report mostrando qué métricas mejoraron/empeoraron

### ACs

- **AC-GR.3.1:** Script `evaluate_rag.py` genera dataset de 125 preguntas
- **AC-GR.3.2:** RAGAS evaluation produce métricas separadas (retrieval vs generation)
- **AC-GR.3.3:** Baseline se persiste para comparación futura
- **AC-GR.3.4:** Reporte incluye tabla de métricas + recomendaciones de mejora
- **AC-GR.3.5:** No se reporta un solo número combinado (regla #6 del skill)

---

## REQ-GR.4 — Text2Cypher Fallback (MEDIUM)

### Problema

Las 6 tools MCP son rígidas — cubren ~80% de queries comunes pero no queries
complejas como *"¿qué entidades de tipo 'pattern' están relacionadas con
'entidades de tipo 'risk' que mencionan 'security'?"*.

### Requerimiento

Agregar una 7ma tool MCP `query_cypher` que acepte lenguaje natural, genere
Cypher dinámico con self-healing (EXPLAIN + retry), y devuelva resultados.

### Especificaciones técnicas

- **Tool signature:** `query_cypher(question: str) -> dict`
- **Router:** Solo se activa cuando las 6 tools rígidas no matchean (last resort)
- **Self-healing loop:**
  1. Infer schema con `apoc.meta.data()` (pruned a 3-5 labels/relations relevantes)
  2. Generar Cypher con LLM
  3. Ejecutar `EXPLAIN <query>` — nunca ejecutar sin validar
  4. Si `EXPLAIN` falla: re-generar con schema pruneado + query fallida + error exacto
  5. Si `EXPLAIN` pasa: ejecutar query real
- **Timeout:** 10s máximo (más que las tools rígidas que tienen 3s)
- **Logging:** QueryLogEntry con tool_name="query_cypher", query_type="text2cypher"
- **Seguridad:** Solo queries de lectura (MATCH-only). Si el LLM genera CREATE/DELETE/SET, rechazar.

### Escenarios

**Escenario GR.4.1 — Query compleja que las tools rígidas no cubren:**
- DADO un agente pregunta "¿qué patrones mitigan riesgos de seguridad?"
- CUANDO las 6 tools rígidas no matchean (no hay tool que junte pattern + risk + security)
- ENTONCES el router activa `query_cypher` que genera: `MATCH (p:Entity {type:'pattern'})-[r]-(k:Entity {type:'risk'}) WHERE k.name CONTAINS 'security' RETURN p, r, k`
- Y devuelve resultados estructurados

**Escenario GR.4.2 — Self-healing de Cypher inválido:**
- DADO el LLM genera `MATCH (e:Entitty)` (typo en label)
- CUANDO `EXPLAIN` falla con error de sintaxis
- ENTONCES el sistema re-genera con el error exacto y produce `MATCH (e:Entity)` correcto

**Escenario GR.4.3 — Rechazo de query peligrosa:**
- DADO el LLM genera `MATCH (e) DETACH DELETE e`
- CUANDO el sistema detecta una operación de escritura
- ENTONCES rechaza la query y devuelve error sin ejecutarla

### ACs

- **AC-GR.4.1:** Tool `query_cypher` registrada como 7ma tool MCP
- **AC-GR.4.2:** Self-healing loop (EXPLAIN → error → re-generate → EXPLAIN) funciona
- **AC-GR.4.3:** Solo queries MATCH-only (rechaza CREATE/DELETE/SET/MERGE/DETACH)
- **AC-GR.4.4:** Timeout 10s, logging con QueryLogEntry
- **AC-GR.4.5:** Tests con fakes cubren happy path, Cypher inválido, y query peligrosa
- **AC-GR.4.6:** Gates pasan

---

## REQ-GR.5 — Monitoreo con TruLens Triad (MEDIUM)

### Problema

Hoy `QueryLogEntry` captura queries individuales (buena base para 07.2) pero no
hay scoring automático de calidad ni detección de drift. Si el uso del MCP cambia
con el tiempo, no te enterás hasta que los agentes fallan.

### Requerimiento

Implementar un **monitor de producción** que samplee queries reales, las evalúe
con el **RAG Triad** (Groundedness, Context Relevance, Answer Relevance) y detecte
**drift** (cambios en distribución de preguntas, chunks, o scores a lo largo del
tiempo).

### Especificaciones técnicas

- **Sampleo:** Tomar 5% de queries de `QueryLogEntry` (ya existe la infraestructura de logging)
- **Scoring:** `TruLens` RAG Triad sobre las queries sampleadas:
  ```python
  from trulens.providers.openai import OpenAI as TruOpenAI
  provider = TruOpenAI(model_engine="gpt-4.1-mini")
  groundedness = Groundedness(provider=provider)
  context_relevance = ContextRelevance(provider=provider)
  answer_relevance = AnswerRelevance(provider=provider)
  ```
- **Drift detection:** `Evidently` sobre distribuciones de scores, tipos de queries, y términos
- **Alerting:** Si un score cae >20% del baseline de GR.3 durante 3 días consecutivos
- **Remediation actions:** Si se detecta drift, sugerir: re-indexar chunks, re-ejecutar extracción, o re-generar summaries

### Escenarios

**Escenario GR.5.1 — Sampleo y scoring automático:**
- DADO 1000 queries en QueryLogEntry en una semana
- CUANDO se ejecuta el monitor diario
- ENTONCES se samplean 50 queries, se scorean con RAG Triad, y se persisten los scores

**Escenario GR.5.2 — Detección de drift:**
- DADO baseline del RAG Triad establecido en GR.3
- CUANDO durante 3 días consecutivos ContextRelevance cae de 0.85 a 0.60
- ENTONCES se genera una alerta con la métrica degradada y la acción sugerida (re-indexar o re-extraer)

**Escenario GR.5.3 — Sin falsos positivos:**
- DADO un solo día con score bajo (outlier puntual)
- CUANDO el monitor corre
- ENTONCES NO se genera alerta (requiere 3 días consecutivos de degradación)

### ACs

- **AC-GR.5.1:** Script `monitor_rag.py` samplea queries de QueryLogEntry
- **AC-GR.5.2:** RAG Triad scoring (Groundedness, Context Relevance, Answer Relevance)
- **AC-GR.5.3:** Drift detection con Evidently sobre distribuciones de scores
- **AC-GR.5.4:** Alerta solo después de 3 días consecutivos de degradación >20%
- **AC-GR.5.5:** Cada alerta incluye acción de remediation sugerida
- **AC-GR.5.6:** Tests unitarios para sampleo, scoring, y lógica de alerta

---

## Dependencias entre requisitos

```
GR.2 (self-reflection) ──┐
                          ├──> GR.3 (evaluation baseline)
GR.1 (communities) ──────┘         │
                                   ├──> GR.4 (Text2Cypher)
                                   │
GR.3 (baseline establecido) ──────> GR.5 (monitoring)
```

- **GR.1 y GR.2** son independientes (pueden implementarse en paralelo)
- **GR.3** requiere GR.1 y GR.2 completos para medir el impacto
- **GR.4** es independiente (se puede hacer en cualquier momento)
- **GR.5** requiere GR.3 (necesita baseline para detectar drift)

---

## Plan de batches sugerido

| Batch | Requisitos | Esfuerzo | Impacto |
|---|---|---|---|
| **B1** | GR.1 Community summaries | 2-3 semanas | Transformacional |
| **B2** | GR.2 Self-reflection extraction | 1 semana | Alto |
| **B3** | GR.3 Evaluation dataset + RAGAS | 1-2 semanas | Alto |
| **B4** | GR.4 Text2Cypher fallback | 1 semana | Medio |
| **B5** | GR.5 TruLens monitoring | 1-2 semanas | Medio |

---

## Criterios de éxito

Este spec se considera exitoso cuando:

1. Las preguntas globales ("¿de qué trata el capítulo X?") producen respuestas
   estructuradas basadas en community summaries, no fragmentos sueltos de
   full-text search.
2. RAGAS evaluation muestra una mejora cuantificable en Faithfulness y
   ContextPrecision comparado con el baseline pre-mejoras.
3. El monitor de drift detecta degradación antes de que los usuarios la reporten.

---

*Spec creado el 2026-08-01 tras auditoría GraphRAG del MCP agentic-patterns.
Pendiente de ciclo SDD completo.*
