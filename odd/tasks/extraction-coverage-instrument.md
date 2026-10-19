# Extraction coverage instrument (front d) — the layer measures the invariant it claims

**Owner**: parent. **Branch**: `feat/extraction-coverage-instrument`. **Risk**: none — the layer is
`optional_layers: blocking: false`, so no gate decision moves; the report gains a real number where a
placeholder was; every query is `MATCH … RETURN` on the read-only credential.

Today the layer is a hard-coded stub reporting `PENDING` with `coverage: 0.0, threshold=None`. A zero is a
number that reads like a measurement. This feature replaces it with the two measurements the spec and the
operator define, and keeps the layer non-blocking.

## Specs

**S1** — the operator's definition of the quotient (approved 2026-10-19, verbatim): "Denominador: Entidades
activas (`merged_into IS NULL OR merged_into = ''`). Se descartan explícitamente las entidades fusionadas
para no inflar la base." · "Numerador: `COUNT(DISTINCT e)` de entidades activas que poseen al menos una
relación entrante `(:Chunk)-[:MENTIONS]->(e)`. Es la única definición algebraicamente válida para un cociente
acotado en [0, 1]." · "Alcance: Métrica primaria global (REQ-PROV-03), reportando el desglose por
libro/namespace en los detalles (AC-PROV-01)."

**S2** — verbatim: "Sí, incluir el conteo de dead-letter chunks como métrica diagnóstica secundaria en el
reporte de la capa."

**S3** — verbatim: "Conforme con fijar `status = PASSED` y reportar caídas bajo el umbral (0.95) a través de
`warnings` + `rationale` especificando que se trata de diagnóstico de corpus." The mechanism behind it:
`derive_overall_status` returns `PASSED` **before** it looks at the `WARNING` branch
(`domain/evaluation_models.py:374-379`), so a WARNING *status* on an optional layer is masked; `warnings` and
`rationale` are the durable slots (precedent: the retrieval layer, measured with a threshold and never
failing).

**S4** — the five pins approved: (1) complete coverage ⇒ no warnings and `coverage = 1.0`; (2) coverage below
the threshold keeps `PASSED`, adds a warning, and that warning says **corpus**, not instrument; (3) the
denominator is the **active** population (a fake carrying merged entities must still yield the right
quotient); (4) the adapter passes the approved queries and parses their result; (5) **the stub's test is
rewritten, not adapted** — it pins a placeholder (`PENDING`), which is the same distinction as the test that
demanded the spurious warning.

**S5** — verbatim: "Aprobados los 5 pines y las actualizaciones en spec 06, spec 07, README, AGENTS.md y
backlog." Plus: spec 06's acceptance checkbox (`docs/spec/06-evaluation-and-readiness.md:107` — the
implementation found the `:119` this spec first cited was stale, which is the drift this feature's own
predecessor was about) **stays open**, stating that this instrument covers **connectivity and provenance**
(REQ-PROV-03) and never semantic fidelity; the historical records (spec 07, README, `AGENTS.md`) get an
**appended** note, never a rewrite.

**S6** — architecture, approved: a new `ExtractionCoveragePort`; `Neo4jExtractionCoverageAdapter` using
**`execute_read`** and the **R2 read-only credential**, wired per consumer at the composition root like the
other read adapters; the use case rewritten; the dead-letter count **reuses the existing checkpoint port**
(a second way to ask the same question is the duplication this project keeps paying for); the CLI gains
`extraction` (`src/book_graph_rag/main.py:905`, today the layer exists and cannot be requested by name); the
two wiring sites (`main.py:808`, `:886`).

**S7** — measured baseline, 2026-10-19, commit `bd6c9e8`, live graph, read-only: `ENTITY_UNMENTIONED = 0`;
inventory 4 Books / 3468 Chunks / 21393 Entities / 42890 `:MENTIONS`; per-book mentioned entities
7110 / 6526 / 5906 / 1178; `failed_checkpoints = 2` in 1 source. ⇒ **coverage = 100% today**. Two consequences
the implementation must respect: the instrument is born as a **guardian of an invariant** (REQ-PROV-01
guarantees one `:MENTIONS` per extracted entity, so the ratio is 1.0 until the pipeline breaks), and the
report must **state the denominator explicitly** — the audit inventory includes merged entities while the
per-book query excludes them, and the per-book sum (20720) is below the inventory total for exactly that
reason.

## Tasks

- **T1** [S6] `ports/extraction_coverage_port.py` + `infrastructure/neo4j_extraction_coverage_adapter.py`: the
  approved quotient query and the per-book query, `execute_read`, read-only credential. → commit 1
- **T2** [S1] the use case rewritten: the quotient, `threshold=0.95`, `status=PASSED`, a rationale carrying the
  raw numbers, the **explicit denominator** and the per-book breakdown. → commit 1
- **T3** [S2,S6] `failed_checkpoints` as a second metric (no threshold: there is no target for it, and
  inventing one would be inventing a goal), read through the **existing** checkpoint port. → commit 1
- **T4** [S1,S3] the below-threshold path: warning text naming the **corpus diagnosis**, never the instrument.
  → commit 1
- **T5** [S4] the five pins, with RED cited where the behaviour is new; the stub test **rewritten**. → commit 1
- **T6** [S6] the CLI choice and the two wiring sites. → commit 1
- **T7** [S5] the docs: spec 06 (measured + the checkbox open with the agreed wording) · spec 07, README,
  `AGENTS.md` (appended notes) · the backlog entry with this run's evidence. → commit 2
- **T8** (parent) the parent's own verification: the suite, the gates, and one spot check of the adapter's
  queries against the approved text. No independent verifier is required while the Risk line is `none`; if
  implementation makes it `item 3`, that changes and the verifier runs.

## Log

**L1** (2026-10-19, the operator's approval, verbatim): "Propuesta de implementación aprobada en su totalidad:
1. Arquitectura y componentes (Piezas 1 a 6): Adelante con el puerto `ExtractionCoveragePort`, el adaptador
`Neo4jExtractionCoverageAdapter` (con `execute_read`), la reescritura del caso de uso, el reuso del puerto de
checkpoints y el wiring en CLI/roots. 2. Estado y advertencias: Conforme con fijar `status = PASSED` y
reportar caídas bajo el umbral (0.95) a través de `warnings` + `rationale` especificando que se trata de
diagnóstico de corpus. 3. Pines de prueba y documentación: Aprobados los 5 pines y las actualizaciones en
spec 06, spec 07, README, AGENTS.md y backlog. — Modalidad de trabajo: Autorizado abrir el documento de
feature formal (S# y T#) para fijar el contrato. Podés proceder directamente con la implementación completa de
las tareas sin frenar para mostrarme el documento en blanco, siempre que los commits y el PR mantengan la
granularidad y la trazabilidad acordadas."

**L2** (2026-10-19, the measured evidence from the Pi, verbatim): "Auditoría oficial (`book-graph-rag audit
--target bookgraph-neo4j`): Estado: `passed` (exit code 0, 0 bloqueantes). `ENTITY_UNMENTIONED`: total = 0.
Inventario: 4 Books, 3468 Chunks, 21393 Entities, 42890 :MENTIONS. Consecuencia matemática: Cobertura de
entidades activas = 100.0% (invariante confirmado). — Desglose por libro: knowledge:agentic-architectural-
patterns 7110 entidades / 1515 chunks; knowledge:ai-engineering-huyen 6526 / 987; knowledge:graphrag-agentic
5906 / 657; knowledge:essential-graphrag 1178 / 299. — Dead-letter: failed_checkpoints = 2, sources_affected
= 1."

**L3** (parent's note on L2): the quotient is **robust at this boundary** — `unmentioned = 0` makes the ratio
1.0 whatever the denominator is (`x/x`), so the conclusion does not depend on the missing active count. What
does depend on it is the **reported denominator**, which the instrument must print explicitly: the inventory's
21393 includes merged entities while both halves of the quotient exclude them, and the per-book sum (20720)
sits below the inventory total for that reason. No arithmetic will be left to the reader.

**L4** (worker, implementation): commit 1 = `6062e0b5c7b6ffeb90c26871aac9bd085db761f5` (T1-T6), commit 2 =
the docs commit itself. Deviations found and stated, not silently resolved: (a) S5 above cited the checkbox at
`06:119`; the actual line is `:107`, i.e. the spec had a stale reference — corrected here; (b) the dead-letter
count is bounded by each book's current chunk range because the existing checkpoint port has no unbounded
count and building a parallel path was forbidden; (c) the RED this feature could observe is structural (the
two modules did not exist), not behavioural — compensation: pin 3's fake carries 13 entities including 5
merged (8/8 vs a naive 8/13, asserted below the threshold so the pin provably bites), and an integration test
on a real testcontainer Neo4j proves the merged exclusion (2/3) rather than assuming it.

**L5** (parent, T8): the two queries were compared against S1's algebra word by word and match it — with one
improvement over the sketched form: the quotient resolves both halves in a **single pass** (`EXISTS { MATCH
(:Chunk)-[:MENTIONS]->(e) }` per entity), so the numerator and the denominator cannot come from two different
populations and the composability trap becomes structurally impossible rather than a reviewer's duty. The
rationale prints the numbers, the explicit denominator with its filter, the vacuous 0/0 rule and the per-book
breakdown. **One gap found and fixed**: the dead-letter number did not state that it counts within each book's
current chunk range, so it can legitimately differ from a raw `Checkpoint.failed_at` count (the operator's
direct measurement said 2); the rationale now says so instead of leaving the discrepancy unexplained.

**L4** (implementation log, T1–T7 complete). **Commit 1** = `6062e0b5c7b6ffeb90c26871aac9bd085db761f5`
(T1–T6: `ports/extraction_coverage_port.py`, `infrastructure/neo4j_extraction_coverage_adapter.py` with the two
approved queries through `execute_read` on the per-consumer read-only credential, the rewritten use case with
the explicit-denominator rationale, `failed_checkpoints` through the **existing** `CheckpointPort.fetch_state`
over each book's chunk range, the below-threshold corpus warning, the five pins — the stub's placeholder test
rewritten, not adapted — plus a testcontainers triangulation that seeds merged entities against a real Neo4j
and proves the Cypher enforces the exclusion the unit fakes assume; CLI `--layer extraction` and both wiring
sites). **Commit 2** = this commit (T7: spec 06 appended entry with the checkbox kept open, spec 07 item 8,
README and AGENTS.md appended notes, this backlog entry, and this Log; its own hash is a self-reference no
commit can carry and is quoted in the parent's handoff). **Verification run**: `uv run ruff check .` →
`All checks passed!`; `uv run ruff format .` → `2 files reformatted, 475 files left unchanged` (both new test
files; `--check .` clean afterwards); `uv run mypy .` → `Success: no issues found in 477 source files` (no
`type: ignore` added for the fan-out); full suite `uv run --no-sync pytest tests/ -q --ignore=tests/integration
--deselect tests/property/interruption_fuzz_test.py` → `2225 passed, 3 skipped, 1 deselected`; the touched
integration file `tests/integration/test_evaluation_read_only.py` → `3 passed` against testcontainers; the new
`tests/integration/test_extraction_coverage_adapter.py` → `1 passed` (live Cypher proof: 2/3 over the active
population, not 3/5 with the ghosts); `uv run python scripts/validate_architecture.py` → OK; pre-commit hooks
(ruff check, mypy, architecture, format) all passed on commit 1. **Deviations/decisions worth review**:
(a) the approved per-book query also returns each book's chunk count — that is what lets the dead-letter count
reuse `fetch_state` without a third query, and it admits the stated boundary that a FAILED row outside a
book's *current* chunk range is not counted (recorded in the code and in `odd/backlog.md`);
(b) the quotient query returns no merged-entity count (only the two approved halves), so the rationale states
the denominator and its filter instead of printing the inventory's 21393, which this instrument never queries;
(c) an empty active population reports coverage 1.0 (the invariant holds vacuously; a fabricated 0.0 would
read like a measurement — the exact failure this feature replaces).
