# GraphRAG Evaluation Improvement Directive

## Objetivo

Mejorar la calidad, reproducibilidad y eficiencia del sistema GraphRAG
sobre Neo4j utilizado para indexar:

"Agentic Architectural Patterns for Building Multi-Agent Systems"

El sistema está implementado con arquitectura hexagonal y actualmente
dispone de:

- Neo4jCommandAdapter — write side
- Neo4jQueryAdapter — read side
- Entity retrieval
- Full-text chunk retrieval
- Relationship traversal
- Shortest-path retrieval
- Provenance mediante chunk/page
- Ragas evaluation
- Dataset de evaluación de aproximadamente 125 casos (201 en la visión a futuro)

NO realizar refactors generales.
NO cambiar la arquitectura hexagonal.
NO reemplazar Neo4j.
NO modificar el dominio sin evidencia proveniente de tests/evaluaciones.

El objetivo es mejorar la calidad del GraphRAG basándose en evidencia.

---

# 1. PRIMERA PRIORIDAD — arreglar la infraestructura de evaluación

Actualmente la evaluación muestra:

    BadRequestError:
    Invalid n value (currently only n = 1 is supported)

y:

    LLM returned 1 generations instead of requested 3.

Esto indica incompatibilidad entre la configuración de Ragas y el
LLM/provider utilizado para la evaluación.

Antes de interpretar cualquier métrica de calidad:

1. Identificar qué componente de Ragas solicita n=3.
2. Identificar qué wrapper/modelo/provider está utilizando Ragas.
3. Determinar si el provider soporta múltiples generaciones.
4. Configurar el evaluator para solicitar n=1 cuando el provider solamente
   soporta una generación.
5. Verificar si existe alguna configuración de retry/fallback que vuelva
   a solicitar n>1.
6. Ejecutar una prueba pequeña de 5-10 casos.
7. Confirmar que no aparecen:
      - Invalid n value
      - LLM returned 1 generations instead of requested 3
8. Solo después ejecutar el benchmark completo.

IMPORTANTE:

Una evaluación con errores del LLM evaluator NO debe utilizarse como
baseline confiable.

Registrar por separado:

- evaluation errors
- retries
- fallback generations
- successful evaluations
- final metric values

---

# 2. NO optimizar el grafo todavía

Antes de cambiar Cypher, índices, traversal depth o entidades:

Ejecutar un diagnóstico.

Separar los problemas en:

A. Graph construction
B. Entity retrieval
C. Relationship retrieval
D. Chunk retrieval
E. Context assembly
F. Answer generation
G. Evaluation infrastructure

No asumir que una métrica baja significa que Neo4j está mal.

---

# 3. Crear un diagnóstico por pregunta

Para cada pregunta del benchmark registrar:

- question_id
- question
- expected answer
- retrieved entities
- retrieved relationships
- retrieved chunks
- retrieved paths
- retrieval strategy
- traversal depth
- number of retrieved entities
- number of retrieved relationships
- number of retrieved chunks
- source pages
- retrieval latency
- generation latency
- evaluator latency
- evaluation errors

El objetivo es poder responder:

"¿Por qué falló esta pregunta?"

y no solamente:

"Ragas obtuvo 0.63."

---

# 4. Clasificar las preguntas

Clasificar el dataset por tipo:

- factual
- definition
- entity lookup
- relationship
- multi-hop
- comparison
- chapter/section
- cross-section
- difficult/ambiguous

Calcular métricas separadas por categoría.

NO utilizar únicamente una media global.

Ejemplo:

factual:
    0.94

definition:
    0.91

relationship:
    0.76

multi-hop:
    0.58

comparison:
    0.71

Si multi-hop es significativamente peor, investigar traversal y
context assembly antes de modificar otras partes.

---

# 5. Evaluar el retrieval independientemente del LLM

No utilizar únicamente Faithfulness o Answer Relevancy.

Crear métricas deterministas cuando exista ground truth.

Evaluar:

- Entity Recall
- Entity Precision
- Entity F1
- Relationship Recall
- Relationship Precision
- Relationship F1
- Graph Path Recall
- Source/Provenance Recall
- Chunk Recall

Ejemplo:

Expected entities:

    [Delegation, Coordination, Agent]

Retrieved:

    [Delegation, Agent, Planning]

Entonces:

Entity Recall = 2/3

Entity Precision = 2/3

No usar un LLM judge cuando la comparación puede hacerse mediante IDs
deterministas.

---

# 6. Mejorar el contrato del retrieval

Neo4jQueryAdapter debe proporcionar suficiente información para
diagnosticar el retrieval.

El resultado de retrieval debería permitir conocer:

- entity ID
- entity name
- entity type
- match strategy
- retrieval score
- chunk ID
- book ID
- chapter
- section
- page
- relationship type
- relationship source
- relationship target
- relationship provenance
- graph path

NO ocultar provenance durante el retrieval.

---

# 7. Mejorar find_entity()

Actualmente find_entity() utiliza tiers:

1. exact
2. case-insensitive
3. substring
4. fulltext

Mantener esta arquitectura, pero hacer explícita la estrategia utilizada.

NO tratar los siguientes scores como probabilidades comparables:

    1.0
    0.8
    0.6
    fulltext_score * 0.4

Separar:

    match_tier

de:

    retrieval_score

Ejemplo:

    match_tier = "exact"
    retrieval_score = 1.0

o:

    match_tier = "fulltext"
    retrieval_score = 7.31

No comparar directamente 1.0 contra 7.31 como si fueran probabilidades.

---

# 8. Mejorar search_chunks()

El resultado de search_chunks() debe conservar:

- chunk_index
- book_id
- text
- page_start
- page_end
- chapter
- section
- score

La evaluación debe poder identificar exactamente qué chunk fue recuperado.

NO devolver únicamente texto.

---

# 9. Mejorar provenance

Toda evidencia recuperada debe poder rastrearse:

Entity
    ↓
Chunk
    ↓
Section
    ↓
Chapter
    ↓
Book
    ↓
Page

Las relaciones deberían poder rastrearse:

Entity
    ↓
Relationship
    ↓
Source Chunk
    ↓
Page

Si una relación no puede rastrearse hasta el texto fuente, marcarla como
provenance incompleta.

---

# 10. Evaluar traversal depth experimentalmente

No asumir que depth=3 es óptimo.

Ejecutar el mismo benchmark con:

    depth=1
    depth=2
    depth=3

Comparar:

- Entity Recall
- Relationship Recall
- Context Recall
- Context Precision
- Faithfulness
- Answer Relevancy
- latency

Buscar el punto de equilibrio.

NO aumentar depth indefinidamente para aumentar recall.

Un traversal más profundo puede introducir ruido.

---

# 11. Evaluar top-K / limits

Experimentar con:

    chunks = 3
    chunks = 5
    chunks = 10

y:

    entities = 5
    entities = 10
    entities = 20

Comparar:

    Context Recall
    Context Precision
    Faithfulness
    latency

Objetivo:

maximizar información relevante,
no maximizar cantidad de contexto.

---

# 12. Investigar el problema de context pollution

Si Context Recall aumenta pero Context Precision disminuye:

NO asumir que el retrieval mejoró.

Puede significar que estamos recuperando demasiado contexto.

Ejemplo:

    Recall = 0.94
    Precision = 0.41

Interpretación:

"Encontramos la información correcta, pero mezclada con demasiado ruido."

En ese caso mejorar:

- ranking
- filtering
- entity disambiguation
- graph traversal constraints
- context deduplication

antes de aumentar K.

---

# 13. Evaluar deduplicación

El sistema debe evitar entregar múltiples chunks que contienen
prácticamente la misma información.

Evaluar:

- duplicate entities
- duplicate relationships
- duplicate chunks
- overlapping chunks
- repeated context

El contexto enviado al LLM debe maximizar información única.

---

# 14. Evaluar entity disambiguation

No asumir que una coincidencia de nombre es la entidad correcta.

Para cada entidad considerar:

- name
- canonical_name
- aliases
- type
- description
- source chunk
- surrounding relationships

Cuando exista ambigüedad, utilizar contexto estructural.

Ejemplo:

    Entity name match
          +
    Entity type
          +
    Neighbor entities
          +
    Source section

---

# 15. Evaluar relationship retrieval

No evaluar solamente si existen los dos nodos.

Evaluar la triple completa:

    source
    relationship type
    target

Ejemplo:

    (Negotiation, ENABLES, Coordination)

NO considerar correcto:

    (Negotiation, RELATED, Coordination)

si el tipo esperado es ENABLES.

---

# 16. Evaluar Graph Path

Para preguntas multi-hop:

Comparar:

    expected path

contra:

    retrieved path

Evaluar:

- node recall
- relationship recall
- path recall
- path precision
- path length
- provenance

No utilizar únicamente shortestPath como criterio semántico.

Un camino corto no necesariamente es el camino explicado por el libro.

---

# 17. No exponer execute_read() al MCP

execute_read() puede mantenerse para:

- debugging
- administración
- tests
- diagnóstico

Pero NO debe ser una herramienta MCP para agentes.

El agente debe utilizar herramientas tipadas:

- find_entity
- search_chunks
- traverse_relationships
- find_path

Nunca permitir Cypher arbitrario desde el agente.

---

# 18. Introducir una política de "evidence first"

El agente debe construir respuestas a partir de evidencia recuperada.

Pipeline:

Question
    ↓
Intent classification
    ↓
Entity/Chunk retrieval
    ↓
Graph traversal when required
    ↓
Evidence filtering
    ↓
Evidence ranking
    ↓
Answer generation

No realizar traversal para preguntas puramente textuales si el chunk
retrieval ya contiene suficiente evidencia.

No realizar chunk search indiscriminadamente si la pregunta requiere
principalmente una relación estructural.

---

# 19. Crear routing por tipo de pregunta

Investigar si diferentes preguntas requieren diferentes retrieval
strategies.

Ejemplo:

Factual:
    chunk search

Definition:
    chunk search + entity

Relationship:
    entity + relationship traversal

Multi-hop:
    entity + traversal + path

Chapter/section:
    structural retrieval

Comparison:
    entity retrieval + independent evidence retrieval

NO utilizar una única estrategia universal si los experimentos
demuestran que empeora las métricas.

---

# 20. Evaluar cada estrategia individualmente

Crear experimentos:

    chunk_only
    entity_only
    entity_plus_chunk
    entity_plus_traversal
    entity_plus_path
    hybrid

Comparar exactamente sobre el mismo dataset.

Ejemplo:

Strategy               Recall    Precision    Faithfulness

chunk_only              0.78       0.86          0.87
entity_only             0.61       0.91          0.81
entity_plus_chunk       0.87       0.84          0.91
hybrid                  0.92       0.79          0.94

Elegir la estrategia según evidencia.

---

# 21. Separar retrieval quality de generation quality

Nunca concluir:

"Graph retrieval is bad"

solamente porque:

    Faithfulness = 0.61

Primero verificar:

    retrieval recall
    retrieval precision
    source provenance

Si retrieval es correcto pero answer quality es baja:

investigar prompt/modelo/generation.

Si retrieval es incorrecto:

investigar Neo4jQueryAdapter.

---

# 22. Mantener un baseline

Antes de cada cambio guardar:

    git commit
    evaluation dataset
    configuration
    metrics
    timestamp

Cada modificación debe compararse contra baseline.

NO aceptar cambios basados únicamente en:

"parece mejor".

Debe existir evidencia.

---

# 23. Regla de regresión

Un cambio solamente se acepta si:

1. mejora la métrica objetivo;
2. no produce una regresión significativa en otras categorías;
3. no aumenta la latencia excesivamente;
4. no introduce errores de evaluación;
5. mantiene los tests existentes.

Ejemplo:

Si:

    multi-hop recall:
    0.72 → 0.84

pero:

    factual precision:
    0.91 → 0.62

NO aceptar automáticamente el cambio.

---

# 24. Prioridad de trabajo

Orden obligatorio:

P0
----
Resolver incompatibilidad Ragas/provider n=3 vs n=1.

P1
----
Crear baseline limpio y reproducible.

P2
----
Diagnóstico por categoría de preguntas.

P3
----
Entity / relationship / chunk retrieval metrics.

P4
----
Provenance.

P5
----
Traversal depth / K experiments.

P6
----
Hybrid retrieval.

P7
----
Prompt/generation optimization.

P8
----
MCP/agent evaluation.

NO saltar directamente a P7.

---

# 25. Restricciones

NO:

- reescribir toda la arquitectura;
- introducir otra base de datos;
- reemplazar Neo4j;
- agregar embeddings sin demostrar necesidad;
- aumentar arbitrariamente top-K;
- aumentar arbitrariamente traversal depth;
- modificar el dominio sin tests;
- optimizar únicamente para una métrica;
- considerar una ejecución Ragas con errores como baseline;
- ocultar errores del evaluator;
- eliminar provenance para simplificar código.

SÍ:

- medir;
- comparar;
- experimentar;
- registrar;
- crear tests;
- mantener reproducibilidad;
- realizar cambios pequeños;
- justificar cada modificación con métricas.

---

# 26. Resultado esperado

El agente NO debe entregar simplemente:

"Ragas mejoró de 0.71 a 0.78."

Debe entregar:

1. Problema identificado.
2. Evidencia.
3. Hipótesis.
4. Cambio realizado.
5. Tests ejecutados.
6. Benchmark utilizado.
7. Métricas antes/después.
8. Latencia antes/después.
9. Regresiones detectadas.
10. Decisión final.

Formato:

Problem:
    Multi-hop retrieval loses relevant intermediate entities.

Evidence:
    Relationship Recall = 0.61
    Path Recall = 0.48

Hypothesis:
    depth=1 is insufficient.

Experiment:
    depth=1 vs depth=2 vs depth=3

Result:
    depth=1 → Path Recall 0.48
    depth=2 → Path Recall 0.76
    depth=3 → Path Recall 0.81

Cost:
    latency +47ms

Decision:
    Use depth=2 as default because depth=3 adds little recall but
    significantly increases noise and latency.
