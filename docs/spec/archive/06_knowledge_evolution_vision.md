# Fase 08 — Visión: Knowledge Evolution (Draft)

> Estado: **DRAFT** — no commitear hasta que Fase 06 (Query Layer) y Fase 07
> (MCP Server) estén validadas con casos reales. Este documento captura la
> visión mientras está fresca; las fases SDD futuras descubrirán el cómo.

---

## Estado actual

Actualmente existe:

- **Neo4j** con el libro "Agentic Architectural Patterns" indexado
  (entidades, relaciones, estructura editorial completa).
- **MCP server** (Fase 07, pendiente) que expondrá el grafo a agentes
  remotos (Claude Desktop, OpenCode, etc.).
- **OpenCode** como orquestador con agentes SDD y jueces de calidad
  (judgment-day, review-readability, review-reliability, etc.).
- **Engram** como memoria episódica跨-sesión.

## Problema

El conocimiento indexado es **principalmente estático**.

Los agentes consultan el libro, pero:

- no consolidan experiencia propia;
- no registran lecciones aprendidas de proyectos reales;
- no generan memoria permanente más allá del contenido original;
- no evolucionan: el grafo de hoy es idéntico al grafo de mañana.

El MCP, en su forma actual, es "un libro consultable". El objetivo es
convertirlo en **un sistema que desarrolla experiencia propia**.

## Norte

Transformar el MCP en una **plataforma de conocimiento evolutivo** capaz de:

1. Aprender de la experiencia (proyectos terminados, decisiones, errores).
2. Conservar decisiones arquitectónicas como conocimiento estructurado.
3. Registrar errores recurrentes como antipatrones en el grafo.
4. Generar conocimiento reutilizable que mejore futuros proyectos.
5. Reducir la repetición de errores ya conocidos.

## Restricciones

La solución:

- **No debe degradar el rendimiento actual** del MCP ni del indexer.
- **No debe introducir complejidad innecesaria** — cada tecnología nueva
  debe demostrar valor antes de incorporarse.
- **Debe poder crecer gradualmente** — un cambio a la vez, con evidencia.
- **Debe respetar la arquitectura existente** (hexagonal, puertos y
  adaptadores,(Settings al .env.
- **Debe aprovechar Engram** cuando sea apropiado (memoria episódica
  ya existe; consolidarla, no reemplazarla).
- **Debe permitir revisión humana** — ningún conocimiento entra al
  grafo permanente sin validación o al menos trazabilidad.

## Dos evoluciones distintas

Es crítico distinguir dos tipos de evolución que NO deben confundirse:

### Evolución A — Ampliar (commodity)

Más fuentes de datos: Qdrant, web, SQL, APIs externas, PDFs locales.
Esto es "biblioteca más grande". Importante pero **no es el
diferenciador** — todo el mundo agrega data sources.

### Evolución B — Madurar (diferenciador)

El conocimiento existente se vuelve **más profundo con el uso**. El
sistema recuerda qué se preguntó, qué funcionó, qué falló. Esto es
"bibliotecario que recuerda". Casi nadie lo hace bien.

**La prioridad de esta visión es (B).** (A) se evaluará solo cuando
(B) esté funcionando y el grafo demuestre insuficiencia documentada.

---

## Knowledge Lifecycle

Todo conocimiento en el sistema pasa por un lifecycle — no entra
directamente al grafo permanente, y no es permanente para siempre.

```
Observation
    ↓
Candidate (pending validation)
    ↓
Validated (meets confidence + occurrences threshold)
    ↓
Permanent (promoted to Ne o4j)
    ↓
Deprecated (kept with history, no longer recommended)
    ↓
Re-validated (a deprecated pattern may regain relevance)
```

### Por qué cada estado existe

- **Observation**: algo pasó (un juez encontró un bug, Engram registró
  una decisión, un postmortem identificó un antipatrón). Sin contexto,
  sin repetición — es un dato crudo.
- **Candidate**: el dato se estructura como potencial conocimiento,
  pero no merece vivir en el grafo todavía. Necesita evidencia.
- **Validated**: la observación se repitió足够 y con suficiente
  confianza para merecer promoción. Los **criteria** de promoción
  (no un simple umbral numérico — pueden incluir consensus de jueces,
  evidencia temporal, severidad, impacto, frecuencia) los descubre SDD.
- **Permanent**: vive en Neo4j como nodo/relación con status
  `permanent`. Los agentes pueden recomendarlo.
- **Deprecated**: el patrón fue verdad en su momento pero dejó de
  serlo (modelo actualizado, framework cambiado, contexto diferente).
  **No se borra** — el conocimiento de "por qué fue verdad y por qué
  dejó de serlo" es conocimiento en sí mismo.
- **Re-validated**: el lifecycle es **bidireccional**. Un patrón
  deprecated puede volver a ser relevant (un modelo nuevo hace que
  un patrón viejo se reviva). La flecha no es recta, es un grafo.

### El problema que el lifecycle resuelve

Sin lifecycle: cada observación se convierte en patrón → el grafo
se llena de ruido → el agente recomienda anomalías como verdades
universales → **sobreaprendizaje**.

Con lifecycle: una anomalía aislada queda como `Candidate` con
`occurrences: 1, confidence: 0.35` y nunca llega a `Permanent`.
Solo la repetición con evidencia promueve.

> **Future note (no implementar ahora):** el lifecycle podría
> extenderse con un **Knowledge Sources Ranking** — diferentes fuentes
> tienen distintos trust levels (book: 1.0, ADR: 0.95, judge consensus:
> 0.85, single judge: 0.60, Engram observation: 0.40). Un agente que
> encuentra una contradicción entre una observación y el libro original
> necesita saber quién tiene prioridad. Esto es exploración, no
> requisito.
>
> Esto también sugiere que el sistema separa implícitamente dos capas:
> **conocimiento canónico** (libro, ontología, patrones validados —
> muy estable) y **conocimiento experimental** (judges, Engram,
> postmortems, session summaries — muy dinámico). Muchos sistemas
> mezclan ambas y terminan degradándose. Mantener la separación
> explícita es importante.

---

## Lo que el sistema NO aprende automáticamente

El sistema no asume que:

- **una recomendación de un juez sea siempre correcta** — los jueces
  opinan; la repetición valida.
- **una decisión tomada en un proyecto sea universal** — una decisión
  es dato de un contexto; no es patrón hasta que se repite.
- **un antipatrón observado una vez sea generalizable** — una anomalía
  es ruido hasta que la evidencia demuestre lo contrario.
- **más datos impliquen mejor conocimiento** — más observaciones sin
  validación producen más ruido, no más sabiduría.

La promoción de conocimiento requiere **repetición, evidencia y
trazabilidad**. No hay atajos.

---

## Áreas de exploración priorizadas

> Estas son direcciones, no requisitos. Cada área se evalúa con SDD
> antes de implementarse. La prioridad es la utilidad real.

### 🥇 Oro — alinear primero

#### 1. Candidate Knowledge: de experiencia temporal a conocimiento permanente

Todas las fuentes de experiencia temporal (jueces, Engram, postmortems,
ADRs, reviewers) fluyen al mismo lugar: una capa de **Candidate
Knowledge** que NO vive en el grafo permanente todavía.

```
Jueces (judgment-day + 4R reviewers)
Engram (session summaries)
Postmortems
ADRs
Otros reviewers
        ↓
Candidate Knowledge (pending validation)
        ↓
Promotion check: confidence + occurrences + review
        ↓
Neo4j permanent graph
```

Cada candidate lleva metadata:

- `confidence: float` — qué tan segura es esta observación (0..1).
- `occurrences: int` — cuántas veces se observó independientemente.
- `sources: list[str]` — trazabilidad a los proyectos/sesiones donde
  apareció.

Una observación con `occurrences: 1, confidence: 0.35` no se promueve.
Una con `occurrences: 7, confidence: 0.80` se promueve a `Permanent`
tras validación.

**Esto protege contra sobreaprendizaje:** un bug raro en un proyecto
no se convierte en verdad universal. El threshold exacto lo descubre
SDD con experimentación — no se hardcodea en la visión.

##### 1a. Jueces como fuente de Candidate Knowledge

Hoy `judgment-day` y los reviewers (R1-R4) producen informes tras
revisar código. Esos informes **se pierden**. Si se persisten como
`Candidate Knowledge` (no directamente en el grafo), cada proyecto
terminado alimenta el pool de candidates. El grafo se enriquece solo
con los que sobreviven validación.

**Pregunta SDD:** ¿con qué trigger se persiste un informe de judge como
candidate? (respuesta tentativa: al cerrar un SDD change archive).

##### 1b. Memoria episódica consolidada

Engram YA guarda sesiones. Pero esa memoria **no se consolida** en
conocimiento estructurado. Al cerrar un proyecto (SDD archive), el
`session_summary` de Engram debería analizarse y las lecciones
entrar al pool de `Candidate Knowledge`.

Las que sobreviven promoción se persisten como:

- Nodos `Risk` / `Pattern` en el grafo (si son generales).
- ADRs en el repo del proyecto (si son específicas a una decisión).

**Pregunta SDD:** ¿qué hace que una observación de Engram merezca
entrar al pool de candidates vs quedarse como memoria episódica del
proyecto?

#### 2. RAG agéntico — el agente decide dónde buscar

Hoy el MCP solo consulta Neo4j. El agente debe poder decir "no
encontré nada suficiente en el grafo; ¿busco en otro lado?".

Esto NO es "agregar Qdrant ya". Es: **la decisión de buscar más allá
del grafo es del agente, no del arquitecto**. El MCP expone
herramientas de búsqueda (grafo, web, Engram) y el agente decide
cuándo saltar de una a otra según lo que va encontrando.

**Pregunta SDD a responder:** ¿qué señal le dice al agente que el
grafo no fue suficiente? ¿Cuándo confianza < umbral? ¿Cuándo zero
results?

### 🥈 Plata — evaluar después de Oro

#### 3. Expansión del grafo (ontologías)

Hoy: 9 entity types, 8 relationship types. Si después de usar el MCP
3 meses, el 30% de las queries no matchean ningún tipo, ENTONCES
expandir. No agregar tipos "por si acaso".

**Trigger de exploración:** métrica de "queries sin match de tipo"
> 20% sostenido.

#### 4. ADRs como nodos en el grafo

Un ADR no es un archivo Markdown suelto — es un nodo `Decision`
conectado a `Pattern` (usa) y `Risk` (mitiga). El grafo dice "esta
decisión usa este patrón y mitiga este riesgo".

Requiere Fase 07 (MCP server) funcionando primero. Hoy no tenemos
quién genere los ADRs automáticamente.

#### 5. Detección de antipatrones recurrentes

Si el mismo tipo de error aparece en 3+ proyectos juzgados, es un
antipatrón que merece un nodo `Risk` en el grafo con trazabilidad a
los proyectos donde apareció.

**Trigger:** antipatrón detectado en N proyectos → se promueve a
conocimiento permanente.

### 🔴 Evaluar solo con evidencia — NO implementar por anticipado

#### 6. Vector search (Qdrant / PgVector / SQLite-VSS)

Hoy no sabemos si necesitamos vector search. El grafo tiene miles de
entidades. Si las queries semánticas encuentran lo que necesitan vía
Cypher, NO necesitamos vectores.

**Trigger de exploración:** documentar 10+ queries donde el grafo
Cypher fue insuficiente para matching semántico. Entonces evaluar.

#### 7. "Autoaprendizaje" automático

Demasiado vago sin trigger. No implementar "un cron que corre LLM
sobre Engram cada noche". Definir el trigger primero (archive de
SDD change, cierre de proyecto, etc.).

---

## Principio rector

**La prioridad es la utilidad real.**

No se busca añadir tecnologías por moda. Cada mejora deberá demostrar
valor práctico antes de ser incorporada. "Una mejora no implementada
hoy puede convertirse en la mejor decisión de mañana. Se prioriza la
madurez de las decisiones sobre la velocidad de incorporación de
nuevas tecnologías."

## Libertad de implementación

Los agentes SDD son libres de:

- dividir el trabajo en fases;
- posponer mejoras hasta tener evidencia;
- realizar experimentos controlados;
- descartar ideas que no demuestren valor;
- proponer alternativas;
- ejecutar cambios de forma incremental.

La implementación final deberá surgir de la observación del sistema y
no de una decisión predeterminada.

## Criterio de éxito medible

Esta visión se considera exitosa cuando:

> Un agente que consulta el MCP después de 10 proyectos completados
> produce mejores recomendaciones que uno que consulta solo el libro
> original indexado.

Si después de 10 proyectos el grafo enriquecido no produce
recomendaciones measurably mejores, la visión no cumplió su objetivo
y se replantea — sin ego.

## Lo que este spec NO es

- No es un plan de implementación. No dice "usar Qdrant" ni
  "usar PgVector". Dice "evaluar" y deja que SDD descubra.
- No es un commitment temporal. No hay deadline para ninguna área.
- No es una shopping list de tecnologías. Qdrant, Obsidian,
  SQLite-VSS son NOMBRES a evaluar, no decisiones.
- No reemplaza Fase 06 (Query Layer) ni Fase 07 (MCP Server). Es
  la Fase 08 contemplativa que vive después de ambas.

---

## Hoja de ruta (roadmap híbrido)

El roadmap original asumía que el gap principal era Knowledge Evolution.
El roadmap revisado (híbrido) no asume nada: mide primero, decide después.
La bifurcación es data-driven, basada en evidencia real de uso del MCP.

```
Fase 06 (Query Layer)          ✅ COMPLETADO — grafo consultable
  ↓
Fase 07 (MCP Server)           ← exponer query layer a agentes + logging estructurado
  ↓                                logging diseñado para informar la bifurcación
Fase 07.1 (RAG básico)         ← fusionar search_chunks + entity/relation en una sola respuesta
  │                                SCOPE ESTRICTO: solo fusión de existente, sin nueva infra
  ↓
Fase 07.2 (Observación)        ← analizar logs: ¿qué consultan? ¿qué falla? ¿qué falta?
  │                                identificar patrón: ¿retrieval o aprendizaje?
  ↓
  ┌───────────────┴───────────────┐
  ↓                               ↓
CAMINO A: Knowledge Evolution    CAMINO B: Retrieval Evolution
(si el gap es "no aprenden")      (si el gap es "no encuentran")
├──────────────────────────┤     ├──────────────────────────────────┤
│ 08.1 Candidate Knowledge │     │ 06b: Embeddings (solo si          │
│ 08.2 Promotion Mechanism │     │      10+ queries sin match)        │
│ 08.3 RAG agéntico        │     │ 07.3: RAG híbrido                 │
│                          │     │ 07.4: RAG agéntico               │
└──────────────────────────┘     └──────────────────────────────────┘
  ↓                               ↓
Evaluación Plata + Rojo          Evaluación Plata + Rojo
(solo con evidencia)             (solo con evidencia)
```

### Notas sobre el roadmap

- **07.1 es de bajo costo** porque usa capacidades que YA existen en Fase 06
  (`search_chunks` + `find_entity`/`traverse_relationships`). Solo fusiona
  resultados en una respuesta unificada. NO construye ranking, reranking,
  scoring, ni nueva infraestructura de búsqueda.
- **07.2 es el puente.** Si después de mejorar la recuperación los agentes
  aún no aprenden, la evidencia para Camino A (Knowledge Evolution) es más
  fuerte. Si siguen sin encontrar, la evidencia para Camino B (Retrieval)
  es más fuerte.
- **El logging de Fase 07 debe diseñarse para la decisión de 07.2.** Si el
  log no captura `zero_results`, `query_terms`, `entity_not_found`, no se
  podrá distinguir "no encontraron" de "no aprendieron".

Cada nodo del roadmap es un SDD change completo: explore → propose → spec
→ design → tasks → apply → verify → archive.

---

*Draft creado el 2026-06-21 mientras el indexer cargaba el libro en la
Orange Pi. Revisar y refinar después de validar Fase 06/07.*